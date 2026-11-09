"""SQLite 缓存：题干指纹 -> 答案。重复题不重复烧 token。

键的构成：
  题干原文 + 选项的「顺序无关归一化指纹」+ 题型 + 图片内容摘要

  - 选项用归一化文本排序后做指纹：网课平台普遍打乱选项顺序，
    用原始列表做键会让同一道题每次都 miss。
    缓存里存的是**选项原文**而非字母，所以乱序命中也不会答错：
    脚本 resolveSingle/Multiple 都是拿文本归一化比对，与字母位置无关。
  - 图片参与键：同一题干、不同配图的题不能共享答案，否则错误答案会被
    固化一整个 TTL。

写放大控制：
  命中计数攒批刷盘，行数检查降频——缓存命中是最热路径，不能每次都 commit。
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time
from typing import Any

from config import settings
from normalize import order_insensitive_options

log = logging.getLogger("cache")

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None

# 命中计数缓冲：key -> 未刷盘次数
_hits_buf: dict[str, int] = {}
_hits_total = 0
_HITS_FLUSH_EVERY = 64

# 行数检查降频
_puts_since_count = 0
_COUNT_EVERY = 8


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


def make_key(
    title: str,
    options: list[str],
    qtype: str,
    images: list[str] | tuple[str, ...] = (),
) -> str:
    """缓存键：顺序无关的选项指纹 + 题目 + 题型 + 图片摘要。"""
    img_digests = [
        hashlib.sha256(i.encode("utf-8")).hexdigest()[:16] for i in images if i
    ]
    raw = json.dumps(
        {
            "t": title.strip(),
            "o": order_insensitive_options(options),
            "ty": qtype,
            "im": img_digests,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _flush_hits(conn: sqlite3.Connection) -> None:
    """把攒下的命中计数一次性写盘。调用方必须持有 _lock。"""
    global _hits_total
    if not _hits_buf:
        return
    conn.executemany(
        "UPDATE answers SET hits = hits + ? WHERE key = ?",
        [(n, k) for k, n in _hits_buf.items()],
    )
    conn.commit()
    _hits_buf.clear()
    _hits_total = 0


def flush() -> None:
    """显式刷出待写命中计数（进程退出前调用）。未建连则直接返回。"""
    if _conn is None or not _hits_buf:
        return
    try:
        with _lock:
            _flush_hits(_connect())
    except sqlite3.Error as e:
        log.warning("命中计数刷盘失败: %s", e)


def get(
    title: str,
    options: list[str],
    qtype: str,
    images: list[str] | tuple[str, ...] = (),
) -> dict[str, Any] | None:
    global _hits_total
    if not settings.cache_enabled:
        return None
    key = make_key(title, options, qtype, images)
    cutoff = time.time() - settings.cache_ttl_days * 86400
    try:
        with _lock:
            conn = _connect()
            row = conn.execute(
                "SELECT payload FROM answers WHERE key=? AND created_at>=?", (key, cutoff)
            ).fetchone()
            if not row:
                return None
            _hits_buf[key] = _hits_buf.get(key, 0) + 1
            _hits_total += 1
            if _hits_total >= _HITS_FLUSH_EVERY or len(_hits_buf) >= _HITS_FLUSH_EVERY:
                _flush_hits(conn)
        return json.loads(row[0])
    except (sqlite3.Error, json.JSONDecodeError) as e:
        log.warning("缓存读取失败: %s", e)
        return None


def put(
    title: str,
    options: list[str],
    qtype: str,
    payload: dict[str, Any],
    images: list[str] | tuple[str, ...] = (),
) -> None:
    if not settings.cache_enabled or not payload:
        return
    key = make_key(title, options, qtype, images)
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
            _maybe_evict(conn)
    except sqlite3.Error as e:
        log.warning("缓存写入失败: %s", e)


def _maybe_evict(conn: sqlite3.Connection) -> None:
    """每 _COUNT_EVERY 次写入才做一次行数检查，避免每次 AI 返回都全表 COUNT。"""
    global _puts_since_count
    _puts_since_count += 1
    if _puts_since_count < _COUNT_EVERY:
        return
    _puts_since_count = 0
    _evict(conn)


def _evict(conn: sqlite3.Connection) -> None:
    n = conn.execute("SELECT COUNT(*) FROM answers").fetchone()[0]
    if n <= settings.cache_max_rows:
        return
    # 删到上限的 90%，留出缓冲：行数检查是降频的，不留余量会反复小幅超限
    target = max(1, int(settings.cache_max_rows * 0.9))
    over = n - target
    if over <= 0:
        return
    conn.execute(
        "DELETE FROM answers WHERE key IN ("
        "SELECT key FROM answers ORDER BY hits ASC, created_at ASC LIMIT ?)",
        (over,),
    )
    conn.commit()
    log.info("缓存淘汰 %d 行（上限 %d，剩余 %d）", over, settings.cache_max_rows, target)


def stats() -> dict[str, Any]:
    try:
        with _lock:
            conn = _connect()
            total = conn.execute("SELECT COUNT(*) FROM answers").fetchone()[0]
            hits = conn.execute("SELECT COALESCE(SUM(hits),0) FROM answers").fetchone()[0]
            pending = sum(_hits_buf.values())
        return {
            "rows": total,
            "total_hits": hits + pending,
            "pending_hits": pending,
            "enabled": settings.cache_enabled,
        }
    except sqlite3.Error as e:
        return {"error": str(e), "enabled": settings.cache_enabled}


def list_items(
    page: int = 1,
    size: int = 20,
    query: str = "",
    sort: str = "recent",
) -> tuple[list[dict[str, Any]], int]:
    """分页列出缓存条目，返回 (items, total)。

    只回列表需要的字段：完整 payload 留给 /solve 命中路径，
    管理页不需要把 options 原文整段搬过去。
    """
    if not settings.cache_enabled:
        return [], 0

    size = max(1, min(int(size), 200))
    page = max(1, int(page))
    offset = (page - 1) * size

    where = ""
    params: list[Any] = []
    q = (query or "").strip()
    if q:
        where = " WHERE question LIKE ? OR payload LIKE ?"
        like = f"%{q}%"
        params = [like, like]

    order = "created_at DESC" if sort != "hits" else "hits DESC, created_at DESC"
    cutoff = time.time() - settings.cache_ttl_days * 86400

    try:
        with _lock:
            conn = _connect()
            total = conn.execute(
                f"SELECT COUNT(*) FROM answers{where}", params
            ).fetchone()[0]
            rows = conn.execute(
                "SELECT key, question, options, qtype, payload, created_at, hits "
                f"FROM answers{where} ORDER BY {order} LIMIT ? OFFSET ?",
                [*params, size, offset],
            ).fetchall()
            # 锁内快照：_flush_hits 会 clear()，锁外读会撞见半空状态
            pending = dict(_hits_buf)
    except sqlite3.Error as e:
        log.warning("缓存列举失败: %s", e)
        return [], 0

    items: list[dict[str, Any]] = []
    for key, question, options, qtype, payload, created_at, hits in rows:
        try:
            data = json.loads(payload) if payload else {}
        except (json.JSONDecodeError, TypeError):
            data = {}
        try:
            opts = json.loads(options) if options else []
            options_count = len(opts) if isinstance(opts, list) else 0
        except (json.JSONDecodeError, TypeError):
            options_count = 0

        items.append(
            {
                "key": key,
                "question": question,
                "qtype": qtype,
                "answer": data.get("answer", "") if isinstance(data, dict) else "",
                "confidence": data.get("confidence") if isinstance(data, dict) else None,
                "options_count": options_count,
                "hits": (hits or 0) + pending.get(key, 0),
                "created_at": created_at,
                "expired": bool(created_at and created_at < cutoff),
            }
        )
    return items, total


def delete(key: str) -> bool:
    """删除单条缓存。返回是否真的删掉了（key 不存在返回 False）。"""
    if not key:
        return False
    try:
        with _lock:
            conn = _connect()
            cur = conn.execute("DELETE FROM answers WHERE key=?", (key,))
            conn.commit()
            removed = cur.rowcount > 0
            # 待刷盘的命中计数也要丢掉，否则下次 flush 会 UPDATE 一条不存在的行
            _hits_buf.pop(key, None)
        return removed
    except sqlite3.Error as e:
        log.warning("缓存删除失败: %s", e)
        return False
