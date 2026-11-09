"""OCS 题库服务：把脚本的搜题请求转给 AI，返回脚本能精确匹配的答案。

端点：
  POST /solve   脚本调用
  GET  /health  健康检查
  GET  /stats   缓存统计
"""
from __future__ import annotations

import asyncio
import atexit
import json
import logging
import time
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

import cache
from ai_client import ask_ai
from config import settings
from normalize import (
    DEFAULT_SEPARATORS,
    build_answer_string,
    letters_to_indices,
    resolve_judgement,
    split_options,
)

# 多选题答案分隔符：与 normalize.build_answer_string 共用同一个来源，
# 避免 server 用 "#"、normalize 用 "===" 这种两处常量各说各话。
MULTI_SEP = DEFAULT_SEPARATORS[0]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("server")

app = FastAPI(title="OCS AI Tiku", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

START_TS = time.time()
STATS = {"requests": 0, "cache_hit": 0, "ai_calls": 0, "errors": 0, "empty": 0}

# 命中计数攒批写盘，退出时兜底刷一次，别把最后几十次命中丢了
atexit.register(cache.flush)


def _auth_ok(request: Request) -> bool:
    if not settings.api_token:
        return True
    token = (
        request.headers.get("x-api-token")
        or request.headers.get("x-ocs-token")
        or ""
    )
    if token == settings.api_token:
        return True
    authz = request.headers.get("authorization", "")
    if authz.lower().startswith("bearer "):
        return authz[7:].strip() == settings.api_token
    return False


async def _read_body(request: Request) -> dict[str, Any]:
    """兼容 JSON body 与表单提交。"""
    ct = (request.headers.get("content-type") or "").lower()
    if "application/json" in ct:
        try:
            data = await request.json()
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, ValueError):
            return {}
    form = await request.form()
    return {k: str(v) for k, v in form.items()}


def _parse_images(raw: Any) -> list[str]:
    """脚本用 ${images} 传数组，JSON.stringify 后是数组；也可能被转成字符串。"""
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw if x]
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return []
        if s.startswith("["):
            try:
                v = json.loads(s)
                if isinstance(v, list):
                    return [str(x) for x in v if x]
            except (json.JSONDecodeError, ValueError):
                pass
        if s.startswith("data:"):
            return [s]
    return []


def _validate_images(images: list[str]) -> str | None:
    """校验图片数量与体积，返回错误原因或 None。

    超限时**整体拒绝**而不是丢掉几张：图片是解题必需信息，
    丢图等于让 AI 瞎猜，而且裁剪后的图片列表还会污染缓存键
    （有图请求退化成无图请求的键）。
    """
    if len(images) > max(0, settings.max_images):
        return "too_many_images"
    max_bytes = max(1, settings.max_image_kb) * 1024
    for img in images:
        if not img:
            continue
        # data URI 的 base64 段长度 × 3/4 ≈ 原始字节数
        approx = len(img) * 3 // 4 if img.startswith("data:") else len(img.encode("utf-8"))
        if approx > max_bytes:
            return "image_too_large"
    return None


async def _solve(title: str, options_raw: str, qtype: str, images: list[str]) -> dict[str, Any]:
    options = split_options(options_raw)
    qtype = (qtype or "").strip().lower()
    if qtype not in ("single", "multiple", "judgement", "completion"):
        qtype = "unknown"

    # 缓存是同步 SQLite，丢线程池：不能在事件循环里 fsync 阻塞整个服务。
    cached = await asyncio.to_thread(cache.get, title, options, qtype, images)
    if cached is not None:
        STATS["cache_hit"] += 1
        cached = dict(cached)
        cached["cached"] = True
        return cached

    try:
        STATS["ai_calls"] += 1
        payload = await ask_ai(title, options, qtype, images)
    except httpx.TimeoutException:
        STATS["errors"] += 1
        log.warning("AI 超时 %ss，题目: %s", settings.ai_timeout, title[:40])
        return {"ok": False, "reason": "timeout"}
    except httpx.HTTPError as e:
        STATS["errors"] += 1
        log.error("AI 网络错误: %s", e)
        return {"ok": False, "reason": "network"}
    except Exception as e:  # noqa: BLE001
        STATS["errors"] += 1
        log.exception("AI 调用异常: %s", e)
        return {"ok": False, "reason": "exception"}

    if not payload:
        STATS["empty"] += 1
        return {"ok": False, "reason": "no_payload"}

    confidence = payload.get("confidence")
    # bool 是 int 的子类：模型返回 true 时不能被当成 1.0
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        confidence = 0.0
    confidence = float(confidence)
    if confidence < settings.min_confidence:
        STATS["empty"] += 1
        log.info(
            "置信度 %.2f 低于阈值 %.2f，拒绝作答: %s",
            confidence,
            settings.min_confidence,
            title[:40],
        )
        return {"ok": False, "reason": "low_confidence"}

    letters = payload.get("letters") or []
    blanks = payload.get("blanks") or []
    answer_text = ""

    if qtype == "completion":
        if isinstance(blanks, list) and blanks:
            answer_text = json.dumps(
                [str(b).strip() for b in blanks if str(b).strip()], ensure_ascii=False
            )
    else:
        letter_str = ""
        if isinstance(letters, list):
            letter_str = "".join(str(x).upper().strip() for x in letters if str(x).strip())
        elif isinstance(letters, str):
            letter_str = letters.upper().strip()

        if letter_str:
            # 复用 normalize 里的实现：去重 + 排序，别在 server 里再手写一遍
            idxs = letters_to_indices(letter_str)
            out_of_range = [i for i in idxs if i >= len(options)]
            if out_of_range:
                # AI 幻觉出页面不存在的字母。宁可判失败也不要给半截答案：
                # 多选题丢一项必然判错，单选题错位会填错选项。
                STATS["empty"] += 1
                log.warning(
                    "AI 返回越界字母 letters=%s idxs=%s 选项数=%d，题目: %s",
                    letter_str,
                    idxs,
                    len(options),
                    title[:40],
                )
                return {"ok": False, "reason": "index_out_of_range"}
            picked = [options[i] for i in idxs]
            if picked:
                if qtype == "judgement":
                    answer_text = resolve_judgement(picked[0]) or ""
                elif qtype == "multiple":
                    answer_text = MULTI_SEP.join(picked)
                else:
                    answer_text = picked[0]

        if not answer_text and isinstance(blanks, list) and blanks:
            answer_text = build_answer_string(qtype, str(blanks[0]), options)

    if not answer_text:
        STATS["empty"] += 1
        return {"ok": False, "reason": "unmatched"}

    result = {
        "ok": True,
        "question": title,
        "answer": answer_text,
        "type": qtype,
        "confidence": confidence,
        "cached": False,
    }
    await asyncio.to_thread(cache.put, title, options, qtype, result, images)
    return result


@app.api_route("/solve", methods=["GET", "HEAD"])
async def solve_probe() -> dict[str, Any]:
    """连接探测端点，专供脚本的 probeAnswerer 使用。

    脚本先发 HEAD，遇到 405/501 再退回 GET，只有 2xx/3xx 才显示「连接成功」。
    探测请求不带鉴权头，所以这里不做 token 校验。
    Starlette 会为 GET 路由自动注册 HEAD。
    """
    return {
        "ok": True,
        "service": "ocs-ai-tiku",
        "endpoint": "POST /solve",
        "probe": True,
    }


@app.post("/solve")
async def solve(request: Request) -> JSONResponse:
    STATS["requests"] += 1

    if not _auth_ok(request):
        return JSONResponse({"ok": False, "reason": "unauthorized"}, status_code=401)

    body = await _read_body(request)
    title = str(body.get("title") or "").strip()
    options_raw = str(body.get("options") or "")
    qtype = str(body.get("type") or "unknown")
    images = _parse_images(body.get("images"))

    if not title:
        return JSONResponse({"ok": False, "reason": "empty_title"}, status_code=200)

    bad_images = _validate_images(images)
    if bad_images:
        STATS["errors"] += 1
        log.warning(
            "拒绝请求 %s: imgs=%d (上限 %d 张 / %d KB)",
            bad_images,
            len(images),
            settings.max_images,
            settings.max_image_kb,
        )
        return JSONResponse({"ok": False, "reason": bad_images}, status_code=200)

    log.info(
        "solve type=%s opts=%d imgs=%d title=%s",
        qtype,
        len(split_options(options_raw)),
        len(images),
        title[:60].replace("\n", " "),
    )

    result = await _solve(title, options_raw, qtype, images)
    return JSONResponse(result, status_code=200)


@app.get("/health")
def health() -> dict[str, Any]:
    # 同步端点：cache.stats() 是阻塞 SQLite，交给 FastAPI 的线程池
    return {
        "ok": True,
        "uptime": round(time.time() - START_TS, 1),
        "model": settings.model,
        "effort": settings.reasoning_effort,
        "cache": cache.stats(),
        "stats": STATS,
        "warnings": settings.validate(),
    }


@app.get("/stats")
def stats_endpoint() -> dict[str, Any]:
    return {"stats": STATS, "cache": cache.stats()}


@app.get("/")
async def root() -> dict[str, str]:
    return {"service": "OCS AI Tiku", "endpoint": "POST /solve"}


if __name__ == "__main__":
    import uvicorn

    for w in settings.validate():
        log.warning(w)

    uvicorn.run(
        "server:app",
        host=settings.host,
        port=settings.port,
        log_level="info",
    )