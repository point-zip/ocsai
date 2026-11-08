"""SQLite 缓存：题干指纹 -> 答案。重复题不重复烧 token。"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time
from typing import Any

from config import settings

log = logging.getLogger("cache")

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def _connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(str(settings.cache_path), check_same_thread=False)
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute(
            """
            CREATE TABLE IF NOT EXISTS answers (
                key        TEXT PRIMARY KEY,
                question   TEXT NOT NULL,
                options    TEXT NOT NULL,
                qtype      TEXT NOT NULL,
                payload    TEXT NOT NULL,
                created_at REAL NOT NULL,
                hits       INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        _conn.execute("CREATE INDEX IF NOT EXISTS idx_created ON answers(created_at)")
        _conn.commit()
    return _conn


def make_key(title: str, options: list[str], qtype: str) -> str:
    raw = json.dumps(
        {"t": title.strip(), "o": options, "ty": qtype}, ensure_ascii=False, sort_keys=True
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def get(title: str, options: list[str], qtype: str) -> dict[str, Any] | None:
    if not settings.cache_enabled:
        return None
    key = make_key(title, options, qtype)
    cutoff = time.time() - settings.cache_ttl_days * 86400
    try:
        with _lock:
            conn = _connect()
            row = conn.execute(
                "SELECT payload FROM answers WHERE key=? AND created_at>=?", (key, cutoff)
            ).fetchone()
            if not row:
                return None
            conn.execute("UPDATE answers SET hits=hits+1 WHERE key=?", (key,))
            conn.commit()
        return json.loads(row[0])
    except sqlite3.Error as e:
        log.warning("缓存读取失败: %s", e)
        return None


def put(title: str, options: list[str], qtype: str, payload: dict[str, Any]) -> None:
    if not settings.cache_enabled or not payload:
        return
    key = make_key(title, options, qtype)
    try:
        with _lock:
            conn = _connect()
            conn.execute(
                """
                INSERT INTO answers(key, question, options, qtype, payload, created_at, hits)
                VALUES(?,?,?,?,?,?,0)
                ON CONFLICT(key) DO UPDATE SET
                    payload=excluded.payload,
                    created_at=excluded.created_at
                """,
                (
                    key,
                    title[:2000],
                    json.dumps(options, ensure_ascii=False)[:4000],
                    qtype,
                    json.dumps(payload, ensure_ascii=False),
                    time.time(),
                ),
            )
            conn.commit()
            _evict(conn)
    except sqlite3.Error as e:
        log.warning("缓存写入失败: %s", e)


def _evict(conn: sqlite3.Connection) -> None:
    n = conn.execute("SELECT COUNT(*) FROM answers").fetchone()[0]
    if n <= settings.cache_max_rows:
        return
    over = n - settings.cache_max_rows
    conn.execute(
        "DELETE FROM answers WHERE key IN ("
        "SELECT key FROM answers ORDER BY hits ASC, created_at ASC LIMIT ?)",
        (over,),
    )
    conn.commit()


def stats() -> dict[str, Any]:
    try:
        with _lock:
            conn = _connect()
            total = conn.execute("SELECT COUNT(*) FROM answers").fetchone()[0]
            hits = conn.execute("SELECT COALESCE(SUM(hits),0) FROM answers").fetchone()[0]
        return {"rows": total, "total_hits": hits, "enabled": settings.cache_enabled}
    except sqlite3.Error as e:
        return {"error": str(e), "enabled": settings.cache_enabled}