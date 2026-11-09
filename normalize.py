"""答案归一化：把 AI 的原始回答翻译成脚本 resolveXxx 一定能命中的格式。

核心目标：命中 answerNormalizedMatch 的 level=2（归一化后完全相等），
从而绕过 0.6 的 Dice 相似度阈值。

脚本匹配要点（已从 ocs.user.js 反推确认）：
  removeRedundant: 去首部 "A." / "A、" / "A " 这类选项前缀
  normalizeString: 去标点、全角转半角、转小写，保留 CJK + 字母数字
  因此返回「页面上的选项原文」最稳，返回字母仅作兜底。
"""
from __future__ import annotations

import json
import re
from typing import Iterable

# 脚本 CORRECT_WORDS / INCORRECT_WORDS（第 4255 行）
CORRECT_WORDS = {"是", "对", "正确", "确定", "√", "对的", "是的", "正确的", "true", "t", "yes", "1"}
INCORRECT_WORDS = {"非", "否", "错", "错误", "×", "x", "false", "f", "no", "0", "不对", "不正确的", "错的"}

# 脚本默认分隔符（第 13581 行）
DEFAULT_SEPARATORS = ["===", "#", "---", "###", "|", ";", "；"]

# 选项前缀：严格复刻脚本 removeRedundant 的两条正则（ocs.user.js）。
#   1) 大写字母 + 一段非字母数字非 CJK 的分隔串 + 正文
#   2) 大写字母 + 直接跟 CJK 正文
# 只用 [A-Z]：脚本不处理小写开头前缀，服务端也不能比脚本更宽容。
# 比脚本宽松的后果是——服务端认为 level=2 精确命中，脚本实际匹配不上，
# 表现为「服务端返回了正确答案，脚本却填不上」。
_JS_PREFIX_SEP = re.compile(r"^[A-Z][^A-Za-z0-9\u2E80-\u9FFF]+([A-Za-z0-9\u2E80-\u9FFF]+)")
_JS_PREFIX_CJK = re.compile(r"^[A-Z]([\u2E80-\u9FFF][A-Za-z0-9\u2E80-\u9FFF]*)")

_PUNCT = "，。！？；：\"'、（）【】《》,.!?;:'\"()[]<>\u3000 \t\r\n-"


def strip_prefix(text: str) -> str:
    """去掉选项的字母前缀，与脚本 removeRedundant 逐字等价。

    刻意**不**支持 "(A)" / "（A）" 这类脚本处理不了的写法：
    服务端多做一步归一化，只会让两边判定分歧。
    """
    if not text:
        return ""
    s = _JS_PREFIX_SEP.sub(r"\1", text)
    s = _JS_PREFIX_CJK.sub(r"\1", s)
    return s.strip()


def normalize(text: str) -> str:
    """近似脚本 normalizeString：去标点/空白、全角转半角、小写。"""
    if not text:
        return ""
    s = strip_prefix(text)
    # 全角字母数字 -> 半角
    s = "".join(
        chr(ord(c) - 0xFEE0) if ("\uFF01" <= c <= "\uFF5E") else c for c in s
    )
    s = s.replace("％", "%").replace("−", "-").replace("–", "-").replace("—", "-")
    s = "".join(c for c in s if c not in _PUNCT)
    return s.strip().lower()


def split_options(options: str | None) -> list[str]:
    """把脚本传来的换行分隔选项串拆成列表。

    注意：脚本侧 buildAnswererEnv 已用 dedupeLines 去过重，
    这里必须原样保留顺序与空行结构，保证索引与页面 DOM 一致。
    """
    if not options:
        return []
    lines = [ln for ln in str(options).split("\n")]
    # 去掉尾部空行，中文行内部空白保留
    while lines and not lines[-1].strip():
        lines.pop()
    return [ln.strip() for ln in lines]


def canonical_option(option: str) -> str:
    """选项的归一化指纹文本（去前缀 + 去标点 + 小写），供缓存键使用。"""
    return normalize(option)


def order_insensitive_options(options: list[str]) -> list[str]:
    """把选项列表转成与顺序无关的指纹列表。

    页面打乱选项顺序是常态，用原始列表做缓存键会让同一道题每次都 miss。
    已知边界：位置相关选项（「以上都是」「A 和 B 均正确」）打乱后语义会变，
    这类题命中缓存可能返回与原顺序绑定的答案。
    """
    return sorted(canonical_option(o) for o in options)


def is_letter_answer(text: str) -> bool:
    """是否是纯字母答案（A / AB / A,B 之类）。"""
    t = text.strip().upper().replace(",", "").replace("，", "").replace(" ", "")
    return bool(t) and all("A" <= c <= "Z" for c in t) and len(t) <= 8


def letters_to_indices(text: str) -> list[int]:
    t = text.strip().upper().replace(",", "").replace("，", "").replace(" ", "")
    out = []
    for c in t:
        if "A" <= c <= "Z":
            idx = ord(c) - 65
            if idx not in out:
                out.append(idx)
    return sorted(out)


def indices_to_options(indices: Iterable[int], options: list[str]) -> list[str]:
    """把索引映射回页面上的选项原文（保留原始前缀与文字）。"""
    out = []
    for i in indices:
        if 0 <= i < len(options):
            out.append(options[i])
    return out


def match_text_to_options(answer: str, options: list[str]) -> list[str]:
    """把 AI 给的文字答案匹配到页面选项原文。"""
    if not answer:
        return []
    na = normalize(answer)
    if not na:
        return []

    # 1) 归一化完全相等
    exact = [o for o in options if normalize(o) == na]
    if exact:
        return [exact[0]]

    # 2) 选项包含答案，或答案包含选项（取最长的那个）
    contained = [o for o in options if na and (na in normalize(o) or normalize(o) in na)]
    if contained:
        contained.sort(key=lambda o: len(normalize(o)), reverse=True)
        return [contained[0]]

    return []


def _sub_words(words: Iterable[str], extra: Iterable[str] = ()) -> tuple[str, ...]:
    """只保留长度 >= 2 的归一化词，按长度降序——长词优先匹配。"""
    pool = {normalize(w) for w in words} | {normalize(e) for e in extra}
    return tuple(sorted((w for w in pool if len(w) >= 2), key=len, reverse=True))


_CORRECT_EXACT = frozenset(normalize(w) for w in CORRECT_WORDS)
_INCORRECT_EXACT = frozenset(normalize(w) for w in INCORRECT_WORDS)

# 单字符词（x / f / 0 / 对 / 错 / 是 ...）只走精确匹配，绝不参与子串：
# 否则 "A. Fox" 会因为含 "x" 被判成「错」。
_CORRECT_SUB = _sub_words(CORRECT_WORDS)
# "不正确" 不在脚本词表里，但它是「正确」的否定式，必须先于「正确」命中。
_INCORRECT_SUB = _sub_words(INCORRECT_WORDS, {"不正确"})


def resolve_judgement(answer: str) -> str | None:
    """判断题：返回 '对' 或 '错'，脚本用词表匹配。

    匹配顺序：精确 -> 多字符子串（错误词优先）。
    与脚本 resolveJudgement 的判定方向一致，只是服务端多做一层容错。
    """
    na = normalize(answer)
    if not na:
        return None
    if na in _CORRECT_EXACT:
        return "对"
    if na in _INCORRECT_EXACT:
        return "错"
    # 错误词先查，避免「不正确」被「正确」截胡
    if any(w in na for w in _INCORRECT_SUB):
        return "错"
    if any(w in na for w in _CORRECT_SUB):
        return "对"
    return None


def build_answer_string(
    qtype: str,
    answer: str,
    options: list[str],
    separators: list[str] | None = None,
) -> str:
    """构造最终返回给脚本的 answer 字符串。

    qtype:
      single      -> 选项原文（1 个）
      multiple    -> 选项原文用分隔符连接
      judgement   -> "对" / "错"
      completion  -> JSON 数组字符串（脚本 splitAnswer 会先试 JSON.parse）
    """
    seps = separators or DEFAULT_SEPARATORS
    sep = seps[0] if seps else "#"
    qtype = (qtype or "").strip().lower()

    if qtype == "judgement":
        j = resolve_judgement(answer)
        return j or ""

    if qtype == "completion":
        parts = split_completion(answer)
        if not parts:
            return ""
        # 脚本 splitAnswer 优先 JSON.parse，数组形式最稳
        return json.dumps(parts, ensure_ascii=False)

    if qtype == "multiple":
        picked = pick_options(answer, options)
        if not picked:
            return ""
        return sep.join(picked)

    # single / unknown
    picked = pick_options(answer, options)
    if not picked:
        return ""
    return picked[0]


def pick_options(answer: str, options: list[str]) -> list[str]:
    """从答案文本中挑出命中页面选项的原文列表。

    支持三种形态：
      1) 纯字母           A / AC / A,C
      2) 字母+分隔符      A#C / A===C / A|C
      3) 选项原文         呼吸作用 / 呼吸作用#蒸腾作用
    """
    if not answer or not options:
        return []

    raw = answer.strip()

    # 1) 纯字母整体
    if is_letter_answer(raw):
        mapped = indices_to_options(letters_to_indices(raw), options)
        if mapped:
            return mapped

    # 2) 按分隔符切分，每段优先当字母处理
    hits: list[str] = []
    chunks = [c.strip() for c in re.split(r"===|###|---|#|\||;|；|,\s*|，", raw) if c.strip()]
    for chunk in chunks:
        if is_letter_answer(chunk):
            for opt in indices_to_options(letters_to_indices(chunk), options):
                if opt not in hits:
                    hits.append(opt)
            continue
        m = match_text_to_options(chunk, options)
        if m and m[0] not in hits:
            hits.append(m[0])
    if hits:
        return hits

    # 3) 整体文字匹配兜底
    return match_text_to_options(raw, options)


def split_completion(answer: str) -> list[str]:
    """填空题：拆分多空答案。"""
    if not answer:
        return []
    a = answer.strip()
    # AI 已经给了 JSON 数组
    if a.startswith("["):
        try:
            v = json.loads(a)
            if isinstance(v, list):
                return [str(x).strip() for x in v if str(x).strip()]
        except (json.JSONDecodeError, ValueError):
            pass
    for sep in ("===", "###", "---", "#", "|", "；", ";"):
        if sep in a:
            parts = [p.strip() for p in a.split(sep) if p.strip()]
            if len(parts) > 1:
                return parts
    return [a]