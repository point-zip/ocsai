"""模拟 ocs.user.js 的 defaultAnswerWrapperHandler + resolveSingle 全链路。

验证 ocs-config.json 这份配置能被脚本真正跑通：
  data 占位符替换 / handler 对象 / 响应 handler / 答案命中
任何一环错位，脚本都会判"题库没搜索到答案"。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

MOCK_PORT = 6798
SRV_PORT = 6767
BASE = f"http://127.0.0.1:{SRV_PORT}"

MOCK_STATE: dict[str, Any] = {"calls": 0, "body": {}}


def make_mock_app():
    app = FastAPI()

    @app.post("/v1/responses")
    async def responses(request: Request):
        body = await request.json()
        MOCK_STATE["calls"] = int(MOCK_STATE["calls"]) + 1
        MOCK_STATE["body"] = body
        payload = {"letters": ["B"], "blanks": [], "confidence": 0.95}
        return JSONResponse(
            {
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps(payload, ensure_ascii=False),
                            }
                        ],
                    }
                ]
            }
        )

    @app.get("/v1/stats")
    async def stats():
        return MOCK_STATE

    return app


# ---------- 复刻脚本逻辑 ----------
def js_resolve_placeholder(data: Any, env: dict[str, Any], encode: bool = False) -> Any:
    """复刻 resolvePlaceHolder（第 4511 行）。

    脚本行为：match(/\\${(.*?)}/g) 取到含 ${} 的完整串，
    再取括号内名字做 env 查表，最后整串替换。
    """
    if isinstance(data, str):
        for ph in re.findall(r"\$\{.*?\}", data):
            key = ph[2:-1]
            value = env.get(key)
            if value is None:
                value = "undefined"
            data = data.replace(ph, urllib.parse.quote(str(value)) if encode else str(value))
    elif isinstance(data, dict):
        for k in list(data.keys()):
            data[k] = js_resolve_placeholder(data[k], env, encode)
    return data


def run_js_handler_factory(handler_src: str):
    """用 node 真实执行 Function(handler)()，返回可调用包装。"""

    def _call(arg=None):
        code = (
            "const s=process.env.OCS_H;"
            "const a=process.env.OCS_A;"
            "const fn=Function(s)();"
            "const r=fn(a===undefined?undefined:JSON.parse(a));"
            "process.stdout.write(JSON.stringify(r===undefined?null:r));"
        )
        env = os.environ.copy()
        env["OCS_H"] = handler_src
        if arg is not None:
            env["OCS_A"] = json.dumps(arg, ensure_ascii=False)
        res = subprocess.run(
            ["node", "-e", code], capture_output=True, text=True, env=env, timeout=30
        )
        if res.returncode != 0:
            raise RuntimeError(f"node 执行 handler 失败: {res.stderr[:400]}")
        out = res.stdout.strip()
        return json.loads(out) if out else None

    return _call


def build_request(wrapper
: dict[str, Any], env: dict[str, Any]) -> dict[str, Any]:
    """复刻 defaultAnswerWrapperHandler 的 POST 分支（第 4436 行）。"""
    data = {}
    for key, val in (wrapper.get("data") or {}).items():
        if isinstance(val, dict) and "handler" in val:
            # Function(handler)() 必须返回一个函数，再以 env 调用
            fn = run_js_handler_factory(val["handler"])
            if not callable(fn):
                raise TypeError(f"data 字段 {key} 的解析器必须返回函数")
            data[key] = fn(env)
        else:
            data[key] = js_resolve_placeholder(val, env)
    return data


def call_server(url: str, payload: dict, headers: dict | None = None) -> Any:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def run_script_handler(handler_src: str, response_data: Any) -> list:
    """复刻 Function(handler)() -> responseHandler(responseData)。"""
    fn = run_js_handler_factory(handler_src)
    info = fn(response_data)
    results = []
    if isinstance(info, list) and info:
        if all(isinstance(i, list) for i in info):
            for item in info:
                results.append((item[0], item[1]))
        else:
            results.append((info[0], info[1]))
    return results


def js_remove_redundant(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"^[A-Z][^A-Za-z0-9\u2E80-\u9FFF]+([A-Za-z0-9\u2E80-\u9FFF]+)", r"\1", s)
    s = re.sub(r"^[A-Z]([\u2E80-\u9FFF][A-Za-z0-9\u2E80-\u9FFF]*)", r"\1", s)
    return s


_PUNCT = re.compile(r"[，。！？；：\"'、（）【】《》,.\-!?;:'\"()\[\]<>\s]")


def js_normalize(s: str) -> str:
    s = _PUNCT.sub("", (s or "").replace("％", "%"))
    s = "".join(chr(ord(c) - 0xFEE0) if "\uFF01" <= c <= "\uFF5E" else c for c in s)
    return s.strip().lower()


def js_resolve_single(answer: str, options: list[str]) -> str | None:
    parts = [a for a in re.split(r"===|###|---|#|\||;|；", answer) if a.strip()]
    na = [js_normalize(js_remove_redundant(p)) for p in parts]
    best, best_lv = None, 0
    for opt in options:
        no = js_normalize(js_remove_redundant(opt))
        if not no:
            continue
        lv = 0
        for a in na:
            if a == no:
                lv = 2
                break
            if a and no and a in no:
                lv = max(lv, 1)
        if lv > best_lv:
            best_lv, best = lv, opt
    return best if best_lv > 0 else None


def wait_up(url: str, tries: int = 80) -> bool:
    for _ in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                json.loads(r.read().decode("utf-8"))
            return True
        except Exception:
            time.sleep(0.5)
    return False


def main() -> int:
    py = sys.executable
    env_vars = os.environ.copy()
    env_vars.update(
        {
            "OCS_BASE_URL": f"http://127.0.0.1:{MOCK_PORT}/v1",
            "OCS_API_KEY": "test-key",
            "OCS_MODEL": "mock-model",
            "OCS_CACHE_PATH": "cache_sim.sqlite3",
            "OCS_PORT": str(SRV_PORT),
        }
    )
    for f in ("cache_sim.sqlite3", "cache_sim.sqlite3-wal", "cache_sim.sqlite3-shm"):
        if os.path.exists(f):
            os.remove(f)

    mock_code = (
        "import sys; sys.path.insert(0, r'%s');"
        "from test_script_sim import make_mock_app;"
        "import uvicorn;"
        "uvicorn.run(make_mock_app(), host='127.0.0.1', port=%d, log_level='error')"
        % (os.getcwd(), MOCK_PORT)
    )
    mock = subprocess.Popen([py, "-c", mock_code], env=env_vars)
    srv = subprocess.Popen([py, "server.py"], env=env_vars)

    try:
        if not wait_up(f"http://127.0.0.1:{MOCK_PORT}/v1/stats", 40):
            print("FAIL: mock 未启动")
            return 1
        if not wait_up(f"{BASE}/health", 80):
            print("FAIL: 服务未启动")
            return 1
        print("服务已就绪\n")

        wrapper = json.load(open("ocs-config.json", encoding="utf-8"))[0]

        # --- 模拟脚本抓到的题目 ---
        options_text = "A. 线粒体\nB. 叶绿体\nC. 细胞核"
        env = {
            "type": "single",
            "title": "光合作用的场所是",
            "options": options_text,
        }

        print("--- 1. 复刻 data 占位符替换 ---")
        payload = build_request(wrapper, env)
        print("payload:", json.dumps(payload, ensure_ascii=False))
        assert payload["title"] == "光合作用的场所是", "title 替换失败"
        assert payload["type"] == "single", "type 替换失败"
        assert payload["images"] == [], f"images 应为空数组，实际 {payload['images']!r}"
        print("[PASS] 占位符替换正确，images 保留数组类型\n")

        print("--- 2. 模拟无图片时的 env（无 images 字段）---")
        env_noimg = {"type": "single", "title": "光合作用的场所是", "options": options_text}
        payload2 = build_request(wrapper, env_noimg)
        assert payload2["images"] == [], f"缺 images 时应为 []，实际 {payload2['images']!r}"
        print("[PASS] env.images 缺失时不炸\n")

        print("--- 3. 发送到服务端并跑 handler ---")
        resp = call_server(wrapper["url"], payload)
        print("response:", json.dumps(resp, ensure_ascii=False))
        results = run_script_handler(wrapper["handler"], resp)
        print("handler 解析结果:", results)
        assert results, "handler 返回空，脚本会判无答案"
        print("[PASS] handler 正确返回 [[question, answer]]\n")

        print("--- 4. 跑脚本的 resolveSingle ---")
        answer = results[0][1]
        options = [o.strip() for o in options_text.split("\n")]
        picked = js_resolve_single(answer, options)
        print(f"answer={answer!r} -> picked={picked!r}")
        assert picked == "B. 叶绿体", f"未命中，实际 {picked!r}"
        print("[PASS] 命中的正是正确答案 B. 叶绿体\n")

        print("--- 5. 无答案时应返回空数组 ---")
        empty = run_script_handler(wrapper["handler"], {"ok": False, "reason": "unmatched"})
        assert empty == [], f"应返回 []，实际 {empty!r}"
        print("[PASS] 无答案时 handler 返回 []，脚本会显示'题库没搜索到答案'\n")

        print("全链路模拟通过：配置可直接使用")
        return 0
    finally:
        for p in (srv, mock):
            try:
                p.terminate()
                p.wait(timeout=8)
            except Exception:
                p.kill()


if __name__ == "__main__":
    raise SystemExit(main())