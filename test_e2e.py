"""端到端测试：Mock 一个 OpenAI Responses 服务，验证完整链路。

覆盖：请求解析 -> AI 调用 -> 答案归一化 -> 缓存命中 -> 图片透传
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

MOCK_PORT = 6799
SRV_PORT = 6767
BASE = f"http://127.0.0.1:{SRV_PORT}"

# mock 子进程内统计
MOCK_STATE: dict[str, Any] = {"calls": 0, "image_seen": False, "efforts": [], "last": {}}


def make_mock_app():
    app = FastAPI()

    @app.post("/v1/responses")
    async def responses(request: Request):
        body = await request.json()
        MOCK_STATE["calls"] = int(MOCK_STATE["calls"]) + 1
        MOCK_STATE["last"] = body

        effort = (body.get("reasoning") or {}).get("effort")
        MOCK_STATE["efforts"].append(effort)

        # 收集用户文本 + 检测图片
        text = ""
        for item in body.get("input") or []:
            if item.get("role") == "user":
                for c in item.get("content") or []:
                    t = c.get("type")
                    if t == "input_text":
                        text += c.get("text") or ""
                    elif t == "input_image":
                        MOCK_STATE["image_seen"] = True

        # 用 prompt 里的「题型：」判断，避免关键词误判
        qtype = ""
        m = re.search(r"题型：(\S+)", text)
        if m:
            qtype = m.group(1)

        if "完全未知" in text:
            payload = {"letters": [], "blanks": [], "confidence": 0.0}
        elif qtype == "填空题":
            payload = {"letters": [], "blanks": ["线粒体", "叶绿体"], "confidence": 0.9}
        elif qtype == "判断题":
            payload = {"letters": ["A"], "blanks": [], "confidence": 0.9}
        elif qtype == "多选题":
            payload = {"letters": ["A", "C"], "blanks": [], "confidence": 0.9}
        else:
            payload = {"letters": ["B"], "blanks": [], "confidence": 0.95}

        return JSONResponse(
            {
                "id": "resp_mock",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps(payload, ensure_ascii=False),
                            }
                        ],
                    }
                ],
            }
        )

    @app.get("/v1/stats")
    async def stats():
        return MOCK_STATE

    return app


def post_json(url: str, data: dict) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(data, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"_http_error": e.code, "_body": e.read().decode("utf-8", "ignore")}


def get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def wait_up(url: str, tries: int = 80) -> bool:
    for _ in range(tries):
        try:
            get_json(url)
            return True
        except Exception:
            time.sleep(0.5)
    return False


def main() -> int:
    py = sys.executable
    env = os.environ.copy()
    env["OCS_BASE_URL"] = f"http://127.0.0.1:{MOCK_PORT}/v1"
    env["OCS_API_KEY"] = "test-key"
    env["OCS_MODEL"] = "mock-model"
    env["OCS_REASONING_EFFORT"] = "low"
    env["OCS_CACHE_PATH"] = "cache_test.sqlite3"
    env["OCS_CACHE_ENABLED"] = "true"
    env["OCS_PORT"] = str(SRV_PORT)

    for f in ("cache_test.sqlite3", "cache_test.sqlite3-wal", "cache_test.sqlite3-shm"):
        if os.path.exists(f):
            os.remove(f)

    mock_code = (
        "import sys; sys.path.insert(0, r'%s');"
        "from test_e2e import make_mock_app;"
        "import uvicorn;"
        "uvicorn.run(make_mock_app(), host='127.0.0.1', port=%d, log_level='error')"
        % (os.getcwd(), MOCK_PORT)
    )
    mock = subprocess.Popen([py, "-c", mock_code], env=env)
    srv = subprocess.Popen([py, "server.py"], env=env)

    try:
        if not wait_up(f"http://127.0.0.1:{MOCK_PORT}/v1/stats", 40):
            print("FAIL: mock 未启动")
            return 1
        if not wait_up(f"{BASE}/health", 80):
            print("FAIL: 服务未启动")
            return 1
        print("服务已就绪\n")

        ok = fail = 0

        def check(name, cond, detail=""):
            nonlocal ok, fail
            if cond:
                ok += 1
                print(f"[PASS] {name}")
            else:
                fail += 1
                print(f"[FAIL] {name}  {detail}")

        # 1 单选
        r = post_json(
            f"{BASE}/solve",
            {
                "title": "光合作用的场所是",
                "options": "A. 线粒体\nB. 叶绿体\nC. 细胞核",
                "type": "single",
            },
        )
        check("单选返回 ok", r.get("ok") is True, str(r))
        check("单选命中选项原文", r.get("answer") == "B. 叶绿体", str(r.get("answer")))

        # 2 缓存
        r2 = post_json(
            f"{BASE}/solve",
            {
                "title": "光合作用的场所是",
                "options": "A. 线粒体\nB. 叶绿体\nC. 细胞核",
                "type": "single",
            },
        )
        check("第二次走缓存", r2.get("cached") is True, str(r2))
        st = get_json(f"http://127.0.0.1:{MOCK_PORT}/v1/stats")
        check("缓存生效未重复调用AI", st["calls"] == 1, f"calls={st['calls']}")

        # 3 多选
        r3 = post_json(
            f"{BASE}/solve",
            {"title": "多选题测试", "options": "A. 甲\nB. 乙\nC. 丙\nD. 丁", "type": "multiple"},
        )
        check("多选返回 ok", r3.get("ok") is True, str(r3))
        a3 = r3.get("answer", "")
        check("多选包含两个选项", "甲" in a3 and "丙" in a3, repr(a3))

        # 4 填空
        r4 = post_json(
            f"{BASE}/solve",
            {
                "title": "填空题：细胞的能量工厂是____，光合作用场所是____",
                "options": "",
                "type": "completion",
            },
        )
        check("填空返回 ok", r4.get("ok") is True, str(r4))
        a4 = r4.get("answer", "")
        check(
            "填空为JSON数组且含两空",
            a4.startswith("[") and "线粒体" in a4 and "叶绿体" in a4,
            repr(a4),
        )

        # 5 判断
        r5 = post_json(
            f"{BASE}/solve",
            {"title": "判断题：地球是圆的", "options": "A. 正确\nB. 错误", "type": "judgement"},
        )
        check("判断返回 ok", r5.get("ok") is True, str(r5))
        check("判断答案为对/错", r5.get("answer") in ("对", "错"), str(r5.get("answer")))

        # 6 低置信度
        r6 = post_json(
            f"{BASE}/solve",
            {"title": "完全未知的题目", "options": "A. 甲\nB. 乙", "type": "single"},
        )
        check("低置信度不返回答案", r6.get("ok") is False, str(r6))

        # 7 图片
        r7 = post_json(
            f"{BASE}/solve",
            {
                "title": "看图作答，选择正确的一项",
                "options": "A. 甲\nB. 乙",
                "type": "single",
                "images": ["data:image/png;base64,iVBORw0KGgo="],
            },
        )
        check("含图片请求成功", r7.get("ok") is True, str(r7))
        st = get_json(f"http://127.0.0.1:{MOCK_PORT}/v1/stats")
        check("图片以 input_image 传入", st["image_seen"] is True)
        check("reasoning.effort 全为 low", all(e == "low" for e in st["efforts"]))

        # 8 json_schema 已发送
        last = st.get("last") or {}
        check("请求体含 reasoning.effort", last.get("reasoning", {}).get("effort") == "low")
        check("请求体含 text.format", "text" in last and "format" in last.get("text", {}))

        print(f"\n结果: {ok} passed, {fail} failed")
        return 0 if fail == 0 else 1
    finally:
        for p in (srv, mock):
            try:
                p.terminate()
                p.wait(timeout=8)
            except Exception:
                p.kill()


if __name__ == "__main__":
    raise SystemExit(main())