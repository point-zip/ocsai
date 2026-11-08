"""用脚本真实的 resolveXxx 语义反向验证答案格式能否命中。

这里把 ocs.user.js 的匹配逻辑用 Python 复刻，确保服务端输出的
answer 字符串在脚本里一定能被选中，而不是靠猜。
"""
from __future__ import annotations

import json
import re

from normalize import (
    build_answer_string,
    normalize,
    pick_options,
    resolve_judgement,
    split_completion,
    split_options,
    strip_prefix,
)

# ---- 复刻脚本 normalizeString / removeRedundant ----
_PUNCT_RE = re.compile(r"[，。！？；：\"'、（）【】《》,.\-!?;:'\"()\[\]<>\s]")


def js_remove_redundant(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"^[A-Z][^A-Za-z0-9\u2E80-\u9FFF]+([A-Za-z0-9\u2E80-\u9FFF]+)", r"\1", s)
    s = re.sub(r"^[A-Z]([\u2E80-\u9FFF][A-Za-z0-9\u2E80-\u9FFF]*)", r"\1", s)
    return s


def js_normalize(s: str) -> str:
    s = s.replace("％", "%")
    s = _PUNCT_RE.sub("", s)
    s = "".join(
        chr(ord(c) - 0xFEE0) if "\uFF01" <= c <= "\uFF5E" else c for c in s
    )
    return s.strip().lower()


def js_answer_normalized_match(answers: list[str], options: list[str]) -> int | None:
    """返回匹配到的选项索引，对应脚本 level 最高者。"""
    na = [js_normalize(js_remove_redundant(a)) for a in answers]
    best_idx, best_level = None, 0
    for i, opt in enumerate(options):
        no = js_normalize(js_remove_redundant(opt))
        if not no:
            continue
        level = 0
        for a in na:
            if a == no:
                level = 2
                break
            if a and no and a in no:
                level = max(level, 1)
        if level > best_level:
            best_level, best_idx = level, i
    return best_idx if best_level > 0 else None


def js_resolve_single(answer: str, options: list[str]) -> str | None:
    all_answers = [a for a in re.split(r"===|###|---|#|\||;|；", answer) if a.strip()]
    idx = js_answer_normalized_match(all_answers, options)
    if idx is not None:
        return options[idx]
    return None


def js_resolve_multiple(answer: str, options: list[str]) -> list[str]:
    parts = [a for a in re.split(r"===|###|---|#|\||;|；", answer) if a.strip()]
    idx = js_answer_normalized_match(parts, options)
    if idx is None:
        return []
    hits = []
    for p in parts:
        i = js_answer_normalized_match([p], options)
        if i is not None and options[i] not in hits:
            hits.append(options[i])
    return hits


_CORRECT = {"是", "对", "正确", "确定", "√", "对的", "是的", "正确的", "true", "t", "yes", "1"}
_INCORRECT = {"非", "否", "错", "错误", "×", "x", "false", "f", "no", "0", "不对", "不正确的", "错的"}


def js_resolve_judgement(answer: str, options: list[str]) -> str | None:
    na = js_normalize(js_remove_redundant(answer))
    want = None
    if na in _CORRECT or any(w == na for w in _CORRECT):
        want = "correct"
    elif na in _INCORRECT or any(w == na for w in _INCORRECT):
        want = "incorrect"
    if want is None:
        return None
    for opt in options:
        no = js_normalize(js_remove_redundant(opt))
        pool = _CORRECT if want == "correct" else _INCORRECT
        if no in pool:
            return opt
    return None


# ---------------- 测试 ----------------
CASES = []


def case(name, qtype, ai_answer, options_raw, expect):
    CASES.append((name, qtype, ai_answer, options_raw, expect))


case(
    "单选-中文选项精确命中",
    "single",
    "B",
    "A. 光合作用\nB. 呼吸作用\nC. 蒸腾作用\nD. 吸收作用",
    "B. 呼吸作用",
)
case(
    "单选-无字母前缀",
    "single",
    "A",
    "光合作用\n呼吸作用\n蒸腾作用",
    "光合作用",
)
case(
    "多选-两个字母",
    "multiple",
    "A#C",
    "A. 甲\nB. 乙\nC. 丙\nD. 丁",
    ["A. 甲", "C. 丙"],
)
case(
    "判断-正确",
    "judgement",
    "对",
    "正确\n错误",
    "正确",
)
case(
    "判断-错误",
    "judgement",
    "错",
    "正确\n错误",
    "错误",
)
case(
    "判断-判断题但选项是√×",
    "judgement",
    "×",
    "√\n×",
    "×",
)


def run() -> int:
    ok, fail = 0, 0
    for name, qtype, ai_answer, options_raw, expect in CASES:
        options = split_options(options_raw)
        ans = build_answer_string(qtype, ai_answer, options)
        if qtype == "single":
            got = js_resolve_single(ans, options)
            passed = got == expect
        elif qtype == "multiple":
            got = js_resolve_multiple(ans, options)
            passed = got == expect
        else:
            got = js_resolve_judgement(ans, options)
            passed = got == expect
        mark = "PASS" if passed else "FAIL"
        if passed:
            ok += 1
        else:
            fail += 1
        print(f"[{mark}] {name}")
        print(f"       answer={ans!r} -> got={got!r} expect={expect!r}")

    # 填空题：JSON 数组必须能被 splitAnswer 解析
    print("\n--- 填空 ---")
    comp_opts = []
    ans = build_answer_string("completion", '["线粒体","叶绿体"]', comp_opts)
    parsed = split_completion(ans)
    okc = parsed == ["线粒体", "叶绿体"]
    print(f"[{'PASS' if okc else 'FAIL'}] 填空-JSON数组 answer={ans!r} -> {parsed}")
    ok, fail = (ok + 1, fail) if okc else (ok, fail + 1)

    print(f"\n结果: {ok} passed, {fail} failed")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(run())