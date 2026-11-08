"""OpenAI Responses API 客户端。

协议要点：
  POST {base}/responses
  body: {model, reasoning:{effort}, input:[{role, content:[...]}], text:{format:{...}}}
  响应: output[].content[].text  (type == "output_text")
"""
from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from config import settings

log = logging.getLogger("ai")

SYSTEM_PROMPT = """你是网课题库的答题引擎。用户会给你一道题，你必须选出正确答案。

严格遵守以下规则：
1. 只依据题目和选项本身作答，不要解释，不要输出思考过程。
2. 如果题目信息不足以判断，confidence 设为 0。
3. 答案必须完全来自给定的选项，不要改写、不要意译。

题型处理：
- single（单选）：letters 里只放 1 个正确选项的字母
- multiple（多选）：letters 里放全部正确选项的字母
- judgement（判断）：letters 放代表「正确/错误」那个选项的字母
- completion（填空）：blanks 按顺序放每个空的答案；不需要 letters

letters 使用大写字母，选项按 A、B、C、D... 顺序编号。
"""

ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "letters": {
            "type": "array",
            "items": {"type": "string"},
            "description": "正确选项的字母，如 ['A'] 或 ['A','C']；填空题为空数组",
        },
        "blanks": {
            "type": "array",
            "items": {"type": "string"},
            "description": "填空题每个空的答案，按顺序；非填空题为空数组",
        },
        "confidence": {
            "type": "number",
            "description": "0 到 1 的置信度，0 表示完全无法判断",
        },
    },
    "required": ["letters", "blanks", "confidence"],
    "additionalProperties": False,
}


def build_user_content(
    title: str, options: list[str], qtype: str, images: list[str]
) -> list[dict[str, Any]]:
    """组装 Responses API 的 content 数组。"""
    parts: list[str] = []
    type_label = {
        "single": "单选题",
        "multiple": "多选题",
        "judgement": "判断题",
        "completion": "填空题",
    }.get(qtype, "未知题型")

    parts.append(f"题型：{type_label}")
    parts.append(f"题目：{title}")

    if options:
        parts.append("选项：")
        for i, opt in enumerate(options):
            letter = chr(65 + i) if i < 26 else f"#{i + 1}"
            parts.append(f"{letter}. {opt}")

    if qtype == "completion":
        parts.append("这是填空题，请把每个空的答案按顺序放进 blanks 数组。")
    elif qtype == "judgement":
        parts.append("这是判断题，请选出代表正确或错误的那个选项的字母。")
    elif qtype == "multiple":
        parts.append("这是多选题，请选出所有正确选项。")
    else:
        parts.append("这是单选题，请只选一个最正确的选项。")

    content: list[dict[str, Any]] = [{"type": "input_text", "text": "\n".join(parts)}]

    for img in images:
        if img and img.startswith("data:"):
            content.append({"type": "input_image", "image_url": img})
        elif img:
            content.append({"type": "input_image", "image_url": img})

    return content


def extract_output_text(data: dict[str, Any]) -> str:
    """从 Responses API 响应里取文本。兼容 output_text 快捷字段与 output 数组。"""
    direct = data.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()

    texts: list[str] = []
    for item in data.get("output") or []:
        if not isinstance(item, dict):
            continue
        for c in item.get("content") or []:
            if isinstance(c, dict) and c.get("type") in ("output_text", "text"):
                t = c.get("text")
                if isinstance(t, str) and t.strip():
                    texts.append(t.strip())
    return "\n".join(texts).strip()


def parse_answer_payload(raw: str) -> dict[str, Any]:
    """解析模型输出的 JSON，容忍 ```json 围栏与前后杂字符。"""
    if not raw:
        return {}
    s = raw.strip()
    if s.startswith("```"):
        s = s.split("```")[1] if "```" in s[3:] else s[3:]
        if s.lstrip().lower().startswith("json"):
            s = s.lstrip()[4:]
        s = s.strip().rstrip("`").strip()
    try:
        v = json.loads(s)
        if isinstance(v, dict):
            return v
    except (json.JSONDecodeError, ValueError):
        pass

    # 退化：抓第一个大括号块
    start, end = s.find("{"), s.rfind("}")
    if 0 <= start < end:
        try:
            v = json.loads(s[start : end + 1])
            if isinstance(v, dict):
                return v
        except (json.JSONDecodeError, ValueError):
            pass
    return {}


async def ask_ai(
    title: str,
    options: list[str],
    qtype: str,
    images: list[str],
) -> dict[str, Any]:
    """调用 Responses API。返回 {letters, blanks, confidence}。"""
    if not settings.api_key:
        log.warning("OCS_API_KEY 未设置，跳过 AI 调用")
        return {}

    body: dict[str, Any] = {
        "model": settings.model,
        "input": [
            {"role": "system", "content": [{"type": "input_text", "text": SYSTEM_PROMPT}]},
            {"role": "user", "content": build_user_content(title, options, qtype, images)},
        ],
        "reasoning": {"effort": settings.reasoning_effort},
    }

    if settings.use_json_schema:
        body["text"] = {
            "format": {
                "type": "json_schema",
                "name": "answer",
                "strict": True,
                "schema": ANSWER_SCHEMA,
            }
        }

    headers = {
        "Authorization": f"Bearer {settings.api_key}",
        "Content-Type": "application/json",
    }

    timeout = httpx.Timeout(settings.ai_timeout, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(settings.responses_url, json=body, headers=headers)

        if resp.status_code >= 400:
            detail = resp.text[:500]
            log.error("AI 请求失败 %s: %s", resp.status_code, detail)
            # 不支持 json_schema 的网关：降级重试一次
            if settings.use_json_schema and resp.status_code in (400, 422):
                log.warning("尝试移除 json_schema 重试")
                body.pop("text", None)
                resp = await client.post(settings.responses_url, json=body, headers=headers)
                if resp.status_code >= 400:
                    log.error("AI 重试仍失败 %s: %s", resp.status_code, resp.text[:500])
                    return {}
            else:
                return {}

        data = resp.json()

    raw = extract_output_text(data)
    if not raw:
        log.warning("AI 返回空文本")
        return {}

    payload = parse_answer_payload(raw)
    if not payload:
        log.warning("AI 输出无法解析为 JSON: %s", raw[:200])
        return {}
    return payload