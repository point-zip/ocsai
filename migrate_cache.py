#!/usr/bin/env python3
"""把旧版缓存库迁移到新版 key 算法。

背景
----
v1 的缓存键 = sha256(题目 + **原始选项列表** + 题型)。
现在多了两项：选项做**顺序无关归一化**、**图片内容摘要**参与哈希。
结果是旧 key 在新代码下永远算不出来——旧行不是"数据"，是死行：
永远命中不了，却一直占着 cache_max_rows 配额，而且因为 hits 可能很高，
会长期赖在 _evict 的 "ORDER BY hits ASC" 队列尾部淘汰不掉。

本脚本读旧库里仍然有效的字段（question / options / qtype / payload /
created_at / hits），用**当前代码的 cache.make_key** 重算键再写入。

key 算法只从 cache.make_key 取，本脚本不复制一份实现——单一真源。

用法
----
  # 先干跑，只看统计
  python migrate_cache.py --src old.sqlite3 --dry-run

  # 正式迁移：生成新库，旧库原封不动
  python migrate_cache.py --src old.sqlite3
  # -> old 同目录下 cache.migrated.sqlite3

  # 指定目标
  python migrate_cache.py --src old.sqlite3 --dst new.sqlite3

  # 原地迁移（自动备份为 <src>.bak-<时间戳>）
  python migrate_cache.py --src cache.sqlite3 --in-place

  # 迁移后抽样自检：真正走一遍 cache.get
  python migrate_cache.py --src old.sqlite3 --verify

注意
----
旧 key 不含图片信息，所以迁移后所有行都被当作**无图**记录。
如果旧库里混有图片题的缓存，它们会以无图身份存在，理论上可能与
「同题干的纯文字题」撞键。投机的做法是用 --skip-image-suspects
按题干关键词跳过这些行；彻底的做法是只迁移能确认来源的题。
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import sqlite3
import sys
import time
from pathlib import Path

import cache as cache_mod

SCHEMA = """
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

INDEX = "CREATE INDEX IF NOT EXISTS idx_created ON answers(created_at)"

# 题干里出现这些词，多半是配图题。旧库丢了图片信息，迁移会把它降级成无图记录。
IMAGE_HINTS = (
    "如图", "下图", "上图", "图中", "图片", "图所示", "观察图", "见图", "所示图", "图示",
)

UPSERT = """
INSERT INTO answers(key, question, options, qtype, payload, created_at, hits)
VALUES(?,?,?,?,?,?,?)
ON CONFLICT(key) DO UPDATE SET
    question   = CASE WHEN excluded.created_at > answers.created_at
                      THEN excluded.question ELSE answers.question END,
    options    = CASE WHEN excluded.created_at > answers.created_at
                      THEN excluded.options ELSE answers.options END,
    payload    = CASE WHEN excluded.created_at > answers.created_at
                      THEN excluded.payload ELSE answers.payload END,
    qtype      = CASE WHEN excluded.created_at > answers.created_at
                      THEN excluded.qtype ELSE answers.qtype END,
    created_at = MAX(answers.created_at, excluded.created_at),
    hits       = MAX(answers.hits, excluded.hits)
"""


def ensure_target_schema(conn: sqlite3.Connection) -> None:
    conn.execute(SCHEMA)
    conn.execute(INDEX)
    conn.commit()


def read_source(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """按列名读，兼容缺列的老库（早期版本可能没有 hits）。"""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(answers)")}
    if not cols:
        raise ValueError("源库里没有 answers 表，确认路径是否正确")
    hits_expr = "COALESCE(hits, 0)" if "hits" in cols else "0"
    sql = (
        "SELECT key, question, options, qtype, payload, created_at, "
        f"{hits_expr} AS hits FROM answers"
    )
    conn.row_factory = sqlite3.Row
    return list(conn.execute(sql))


def looks_like_image_question(question: str) -> bool:
    return any(h in question for h in IMAGE_HINTS)


def backup(path: Path) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dst = path.with_name(f"{path.name}.bak-{stamp}")
    dst.write_bytes(path.read_bytes())
    # 顺带把 WAL 一起复制，否则备份可能缺最新写入
    for suffix in ("-wal", "-shm"):
        side = path.with_name(path.name + suffix)
        if side.exists():
            dst.with_name(dst.name + suffix).write_bytes(side.read_bytes())
    return dst


def open_source(src: Path, in_place: bool) -> sqlite3.Connection:
    """打开源库。

    非原地迁移一律**只读**打开：读写模式会让 SQLite 在关闭时做
    autocheckpoint，把 -wal 合并进主库——等于顺手改写了用户的旧库。
    只读模式读不到未合并 WAL 时（缺 -shm 且目录不可写）才回退，并明确告知。
    """
    if in_place:
        conn = sqlite3.connect(str(src))
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn
    try:
        conn = sqlite3.connect(f"{src.as_uri()}?mode=ro", uri=True)
        # 只探测连接自身是否可用（含能否读到 WAL），不要查业务表——
        # 否则「缺表」会被误判成「打不开」而错误地回退到读写模式
        conn.execute("PRAGMA schema_version").fetchone()
        return conn
    except sqlite3.Error as e:
        print(f"[WARN] 只读打开失败（{e}），回退读写模式")
        print("       读写模式会在关闭时合并 -wal，源库文件体积可能变化（数据不变）")
        conn = sqlite3.connect(str(src))
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn


def migrate(args: argparse.Namespace) -> int:
    src = Path(args.src).expanduser().resolve()
    if not src.is_file():
        print(f"[FAIL] 源库不存在: {src}")
        return 1

    if args.in_place:
        dst = src
    else:
        dst = Path(args.dst).expanduser().resolve() if args.dst else src.with_name(
            "cache.migrated.sqlite3"
        )
        if dst.exists() and not args.dry_run:
            print(f"[FAIL] 目标已存在: {dst}\n       换个 --dst，或先删掉它")
            return 1

    wal = src.with_name(src.name + "-wal")
    if wal.exists() and wal.stat().st_size > 0:
        print(
            f"[INFO] 检测到未合并的 WAL ({wal.stat().st_size} 字节)，"
            "只读模式下会直接读取其中的数据，不会改写源库"
        )

    src_conn = open_source(src, args.in_place)
    try:
        rows = read_source(src_conn)
    except ValueError as e:
        print(f"[FAIL] {e}")
        return 1
    finally:
        src_conn.close()

    if args.limit:
        rows = rows[: args.limit]

    total = len(rows)
    print(f"[INFO] 源库: {src}")
    print(f"[INFO] 目标: {'(原地迁移)' if args.in_place else dst}")
    print(f"[INFO] 读入 {total} 行")

    if args.dry_run:
        print("[INFO] --dry-run：只统计，不写任何文件")

    # ---- 计算新键 ----
    stats = {
        "ok": 0,
        "same_key": 0,
        "bad_row": 0,
        "image_skipped": 0,
        "collision": 0,
    }
    computed: list[tuple] = []
    seen: set[str] = set()

    for row in rows:
        question = (row["question"] or "").strip()
        qtype = (row["qtype"] or "").strip()
        if not question:
            stats["bad_row"] += 1
            continue

        if args.skip_image_suspects and looks_like_image_question(question):
            stats["image_skipped"] += 1
            if args.verbose:
                print(f"  [SKIP] 疑似图片题: {question[:40]}")
            continue

        try:
            # options 列存的是 json 数组的字符串形式
            opts = json.loads(row["options"]) if row["options"] else []
        except (ValueError, TypeError):
            opts = []
        if not isinstance(opts, list):
            opts = []

        try:
            new_key = cache_mod.make_key(question, [str(o) for o in opts], qtype)
        except Exception as e:  # noqa: BLE001
            stats["bad_row"] += 1
            print(f"  [WARN] 算键失败，跳过: {question[:40]!r} -> {e}")
            continue

        if new_key == row["key"]:
            stats["same_key"] += 1

        if new_key in seen:
            stats["collision"] += 1
            if args.verbose:
                print(f"  [COLLIDE] {question[:40]}")
        seen.add(new_key)

        computed.append(
            (
                new_key,
                question,
                row["options"] or "",
                qtype,
                row["payload"],
                float(row["created_at"] or 0),
                int(row["hits"] or 0),
            )
        )
        stats["ok"] += 1

    print(
        f"[INFO] 可迁移 {stats['ok']} 行"
        f"（键未变 {stats['same_key']}，键冲突合并 {stats['collision']}，"
        f"无法解析 {stats['bad_row']}，按图片题跳过 {stats['image_skipped']}）"
    )

    if args.dry_run:
        print("\n[DRY-RUN] 未写入任何内容。去掉 --dry-run 正式执行。")
        return 0

    if not computed:
        print("[WARN] 没有可迁移的行")
        return 0

    # ---- 写入 ----
    if args.in_place:
        bak = backup(src)
        print(f"[INFO] 已备份: {bak}")
        target = sqlite3.connect(str(src))
        try:
            ensure_target_schema(target)
            target.execute("DELETE FROM answers")
            target.commit()
            target.executemany(UPSERT, computed)
            target.commit()
        finally:
            target.close()
        print(f"[OK] 原地迁移完成，共写入 {len(computed)} 行")
    else:
        target = sqlite3.connect(str(dst))
        try:
            target.execute("PRAGMA journal_mode=WAL")
            ensure_target_schema(target)
            target.executemany(UPSERT, computed)
            target.commit()
            written = target.execute("SELECT COUNT(*) FROM answers").fetchone()[0]
        finally:
            target.close()
        print(f"[OK] 写入 {dst}，当前 {written} 行")
        print("     确认无误后，把 OCS_CACHE_PATH 指向它，或直接替换旧库文件")

    if args.verify:
        return verify(dst, computed)
    return 0


def verify(dst: Path, computed: list[tuple]) -> int:
    """抽样真跑 cache.get：把 cache 的 settings 临时指向迁移后的库。"""
    print("\n[VERIFY] 抽样走一遍 cache.get（真实命中路径）")

    original = cache_mod.settings
    # frozen dataclass 不能原地改，用 dataclasses.replace 造一份
    cache_mod.settings = dataclasses.replace(
        original, cache_path=dst, cache_enabled=True
    )
    cache_mod._conn = None  # 丢掉旧连接
    cache_mod.flush()

    sample = computed[:5]
    ok = fail = 0
    try:
        for key, question, options_json, qtype, _payload, _created, _hits in sample:
            opts = json.loads(options_json) if options_json else []
            got = cache_mod.get(question, [str(o) for o in opts], qtype)
            if got is not None:
                ok += 1
                print(f"  [PASS] {question[:36]} -> {got.get('answer')!r}")
            else:
                fail += 1
                print(f"  [FAIL] {question[:36]} 未命中 (key={key[:12]}…)")
    finally:
        cache_mod.flush()
        cache_mod._conn = None
        cache_mod.settings = original

    print(f"\n[VERIFY] {ok} passed, {fail} failed")
    return 0 if fail == 0 else 1


def main() -> int:
    p = argparse.ArgumentParser(
        description="把旧版缓存库迁移到新版 key 算法",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--src", required=True, help="旧缓存库路径")
    p.add_argument("--dst", help="目标路径（默认 <src 同目录>/cache.migrated.sqlite3）")
    p.add_argument(
        "--in-place", action="store_true", help="原地迁移源库，自动备份 .bak-<时间戳>"
    )
    p.add_argument("--dry-run", action="store_true", help="只统计，不写文件")
    p.add_argument(
        "--skip-image-suspects",
        action="store_true",
        help="跳过题干疑似配图的旧行（旧库无法还原图片信息）",
    )
    p.add_argument("--limit", type=int, help="只处理前 N 行，用于试跑")
    p.add_argument("--verify", action="store_true", help="迁移后抽样走一遍 cache.get")
    p.add_argument("--verbose", action="store_true", help="打印逐行明细")
    args = p.parse_args()

    if args.in_place and args.dst:
        print("[FAIL] --in-place 与 --dst 不能同时使用")
        return 1

    return migrate(args)


if __name__ == "__main__":
    sys.exit(main())
