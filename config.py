"""配置层：全部来自环境变量 / .env，改配置不用动代码。"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def _load_dotenv() -> None:
    """极简 .env 加载，避免额外依赖。已存在的环境变量优先。"""
    p = BASE_DIR / ".env"
    if not p.is_file():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        os.environ.setdefault(k, v)


_load_dotenv()


def _s(name: str, default: str = "") -> str:
    v = os.getenv(name)
    return default if v is None else v.strip()


def _i(name: str, default: int) -> int:
    try:
        return int(_s(name, "") or default)
    except ValueError:
        return default


def _b(name: str, default: bool) -> bool:
    v = _s(name, "")
    if not v:
        return default
    return v.lower() in ("1", "true", "yes", "on")


def _f(name: str, default: float) -> float:
    try:
        return float(_s(name, "") or default)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    api_key: str
    base_url: str
    model: str
    reasoning_effort: str
    ai_timeout: float
    use_json_schema: bool
    host: str
    port: int
    api_token: str
    cache_enabled: bool
    cache_path: Path
    cache_ttl_days: int
    cache_max_rows: int

    @property
    def responses_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/responses"

    def validate(self) -> list[str]:
        warn: list[str] = []
        if not self.api_key:
            warn.append("OCS_API_KEY 未设置，/solve 将直接返回空答案")
        if not self.model:
            warn.append("OCS_MODEL 未设置")
        if self.ai_timeout >= 58:
            warn.append(
                f"OCS_AI_TIMEOUT={self.ai_timeout}s 接近脚本硬超时 60s，建议 <=50"
            )
        return warn


def load_settings() -> Settings:
    cache_path = Path(_s("OCS_CACHE_PATH", "cache.sqlite3"))
    if not cache_path.is_absolute():
        cache_path = BASE_DIR / cache_path
    return Settings(
        api_key=_s("OCS_API_KEY"),
        base_url=_s("OCS_BASE_URL", "https://api.openai.com/v1"),
        model=_s("OCS_MODEL", "gpt-4.1-mini"),
        reasoning_effort=_s("OCS_REASONING_EFFORT", "low"),
        ai_timeout=_f("OCS_AI_TIMEOUT", 45.0),
        use_json_schema=_b("OCS_USE_JSON_SCHEMA", True),
        host=_s("OCS_HOST", "0.0.0.0"),
        port=_i("OCS_PORT", 6767),
        api_token=_s("OCS_API_TOKEN", ""),
        cache_enabled=_b("OCS_CACHE_ENABLED", True),
        cache_path=cache_path,
        cache_ttl_days=_i("OCS_CACHE_TTL_DAYS", 30),
        cache_max_rows=_i("OCS_CACHE_MAX_ROWS", 50000),
    )


settings = load_settings()