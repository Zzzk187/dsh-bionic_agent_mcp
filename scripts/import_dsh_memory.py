"""把 dsh-memory/记忆.jsonl 的 34 条真实记忆导入 bionic-agent MCP 的 SQLite 库。

设计要点（都对应一个具体的坑）：
1. **幂等**：源 id（A01/A02/...）写进 tags 作为 `src:A01`，重复跑只更新不新增。
   之所以不靠 content 判重——正文可能被后续编辑，靠正文比对会重复导入。
2. **不丢原文**：dsh-memory 的 body 是 markdown（含表格），标题单独存在 title。
   这里把 `# 标题` + 正文拼成 content，保证星图和检索里读到的是完整信息。
3. **保留元数据**：kind→category、trust→confidence、date→created_at、
   uses→access_count，全部映射过去，否则星图的时间/权重维度就没了依据。
4. **不自动跑向量**：嵌入模型此刻 model_ready=false，导入只写 memories 表；
   向量由 index_memories 之后补（它会按 content_hash 增量算）。
5. 走 SQLite 直连而不是 save_memory MCP 函数——同一进程里没有 MCP client，
   且这里需要控制 created_at，函数签名给不了。
"""
from __future__ import annotations

import io
import json
import sqlite3
import sys
from pathlib import Path

# 2026-11-08 模块化后 jsonl 进了 data/。路径不在这里写死第二次：
# app.knowledge.DSH_MEMORY_ROOT 认 DSH_MEMORY_ROOT 环境变量，与 dsh-memory/config.ps1 同一套约定。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.knowledge import DSH_MEMORY_ROOT  # noqa: E402
from app import db as _app_db  # noqa: E402  [S0/P2-1] 统一 DB 配置

SRC = Path(DSH_MEMORY_ROOT) / "data" / "记忆.jsonl"
# [S0/P2-1] 目标库统一走 app.db.DB_PATH（认 BIONIC_DB_PATH 环境变量），
# 不再写死 D 盘绝对路径；保留模块级名字 DB 供测试 monkeypatch 隔离。
DB = Path(_app_db.DB_PATH)
SOURCE_TAG = "dsh-memory"  # 用来识别"这条是从 dsh-memory 导进来的"


def load_rows() -> list[dict]:
    rows = []
    with io.open(SRC, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def build_content(row: dict) -> str:
    """标题 + 正文。标题进 content 是因为星图节点要显示可读名字。"""
    title = (row.get("title") or "").strip()
    body = (row.get("body") or "").strip()
    if title and body:
        return f"# {title}\n\n{body}"
    return title or body


# [S0/P1-8] 源 status → (memories.status, memories.memory_status) 的显式映射。
# 这两个列语义不同、不能混写：status 是唯一的检索过滤字段（active/archived）；
# memory_status 是治理视图（active/archived/superseded/pending_review）。
# 未知值绝不静默落到 'active'（那正是本次要修的「未知状态被吞掉」）。
SRC_STATUS_MAP = {
    "active": ("active", "active"),
    "archived": ("archived", "archived"),
    "superseded": ("active", "superseded"),
    "pending_review": ("active", "pending_review"),
}


def resolve_source_status(raw) -> dict:
    """把源 status 解析成可落库的两列；纯函数，方便单测。

    known=False 表示无法识别：调用方必须记录报告，且不得用它新建记忆、
    不得把它写进任何状态列。缺字段按 active 初始化但显式计数（兼容老数据，
    不算「未知状态被静默吞掉」）。
    """
    src = str(raw or "").strip().lower()
    if src in SRC_STATUS_MAP:
        st, mst = SRC_STATUS_MAP[src]
        return {"src": src, "status": st, "memory_status": mst,
                "known": True, "note": ""}
    if not src:
        st, mst = SRC_STATUS_MAP["active"]
        return {"src": "", "status": st, "memory_status": mst,
                "known": True, "note": "源缺 status 字段，按 active 初始化（已记录）"}
    return {"src": src, "status": None, "memory_status": None,
            "known": False, "note": f"未知源 status={src!r}，不静默映射为 active"}


def main(dry_run: bool = False) -> int:
    """dry_run=True 时只统计「会新增/会更新多少」，不写库。

    sync_all.py 一直给这个脚本传 --dry-run，但以前没接：传了照样写。
    """
    if not SRC.is_file():
        print(f"[FAIL] 源文件不存在：{SRC}")
        return 1
    if not DB.is_file():
        print(f"[FAIL] 目标库不存在：{DB}")
        return 1

    rows = load_rows()
    print(f"源记忆 {len(rows)} 条：{SRC}")

    # [S0/P2-1] 与 app.db.connect() 同套并发参数，避免多进程写时 "database is locked"。
    conn = sqlite3.connect(str(DB), timeout=_app_db.BUSY_TIMEOUT_SECONDS)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=%d" % int(_app_db.BUSY_TIMEOUT_SECONDS * 1000))
    _app_db._retry_locked(lambda: conn.execute("PRAGMA journal_mode=WAL"))
    conn.execute("PRAGMA foreign_keys=ON")
    # 注意：计数器叫 added/updated_count/skipped，不要复用 row 里的 updated 时间戳，
    # 否则字符串和整数混在同一个变量里，第二次运行会 TypeError（踩过）。
    added = updated_count = skipped = 0
    # [S0/P1-8] 未知源状态逐条记录、汇总上报，绝不静默当成 active。
    unknown_status = []
    status_defaulted = 0

    try:
        for row in rows:
            src_id = str(row.get("id") or "").strip()
            if not src_id:
                skipped += 1
                continue

            content = build_content(row)
            if not content:
                skipped += 1
                continue

            # [S0/P1-8] 先解析源 status：未知值不新增、不改写、不静默映射。
            # 已存在行的 UPDATE 根本不写 status/memory_status（见下），所以这里的
            # 解析只决定「新建行」的状态列；未知则整行跳过并上报。
            stat = resolve_source_status(row.get("status"))
            if not stat["known"]:
                unknown_status.append({"src_id": src_id, "status": stat["src"],
                                       "note": stat["note"]})
                skipped += 1
                continue
            if not stat["src"]:
                status_defaulted += 1

            # tags 里固定塞一个 src:<id>，作为幂等锚点
            #
            # 注意：源数据里 tags 有两种形态 —— 28 条是列表，6 条是
            # "powerShell,编码,BOM" 这样的逗号分隔字符串。对字符串直接
            # list() 会逐字符拆开（变成 ["P","o","w","e","r"...]），
            # 曾在库里留下 tags 长达 17 个单字符的脏数据。所以这里分清类型。
            raw_tags = row.get("tags")
            if isinstance(raw_tags, str):
                tags = [t.strip() for t in raw_tags.replace("，", ",").split(",")]
                tags = [t for t in tags if t]
            elif isinstance(raw_tags, list):
                tags = [str(t).strip() for t in raw_tags if str(t).strip()]
            else:
                tags = []
            anchor = f"src:{src_id}"
            if SOURCE_TAG not in tags:
                tags.append(SOURCE_TAG)
            tags = [t for t in tags if t != anchor] + [anchor]

            category = str(row.get("kind") or "general").strip() or "general"
            # trust: user→0.95（用户亲口），verified→0.8
            trust = str(row.get("trust") or "").strip()
            confidence = 0.95 if trust == "user" else 0.8
            created = str(row.get("date") or "").strip() or None
            # [S0] 与 INSERT 路径保持同样的兜底：源行万一同时缺 updated/date，
            # 旧逻辑会给 UPDATE 传 None，直接撞 updated_at NOT NULL，整次同步全量
            # 回滚。真实源 47 行都有这两个字段，此处只补一致性，不改正常语义。
            updated_at_val = str(row.get("updated") or "").strip() or created or "2026-10-17 00:00:00"
            uses = int(row.get("uses") or 0)
            last_used = str(row.get("last_used") or "").strip() or None
            loc = str(row.get("loc") or "").strip()

            # 幂等查找：靠 tags_json 里的 src:<id>
            existing = conn.execute(
                "SELECT id FROM memories WHERE tags_json LIKE ?", (f'%"{anchor}"%',)
            ).fetchone()

            if existing:
                # [S0/P1-2] UPDATE 不写 access_count / last_accessed_at：这两列是
                # MCP 自己的使用统计（governance_service 自增、检索回写），本地源
                # 的 uses/last_used 长期为 0/空，旧逻辑会把它清零覆盖，丢真实统计。
                # [S0/P1-8] 同样不写 status / memory_status：前者是唯一检索过滤列、
                # 后者是治理状态，单向同步不得反向覆盖或复活 archived。
                conn.execute(
                    """UPDATE memories SET content=?, category=?, source=?, confidence=?,
                           tags_json=?, updated_at=?
                       WHERE id=?""",
                    (content[:10000], category[:100], f"dsh-memory:{loc}"[:200], confidence,
                     json.dumps(tags, ensure_ascii=False), updated_at_val,
                     existing["id"]),
                )
                updated_count += 1
            else:
                # 新建行才按显式映射初始化状态列；访问统计源端为 0/空属正常初始化。
                cur = conn.execute(
                    """INSERT INTO memories(content,category,source,confidence,tags_json,
                           created_at,updated_at,access_count,last_accessed_at,
                           status,memory_status)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (content[:10000], category[:100], f"dsh-memory:{loc}"[:200], confidence,
                     json.dumps(tags, ensure_ascii=False),
                     created or "2026-10-17 00:00:00",
                     updated_at_val or created or "2026-10-17 00:00:00",
                     uses, last_used, stat["status"], stat["memory_status"]),
                )
                conn.execute(
                    "INSERT INTO events(event_type,detail) VALUES(?,?)",
                    ("memory.created",
                     f"memory_id={cur.lastrowid}; category={category}; source=dsh-memory; src_id={src_id}"),
                )
                added += 1

        if dry_run:
            conn.rollback()
        else:
            conn.commit()
        total = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        active = conn.execute("SELECT COUNT(*) FROM memories WHERE status='active'").fetchone()[0]
    finally:
        conn.close()

    print(f"新增 {added} / 更新 {updated_count} / 跳过 {skipped}")
    print(f"库内合计 memories={total}，其中 active={active}")
    if unknown_status:
        print(f"[WARN] 未知源 status 共 {len(unknown_status)} 条（未导入、未改写）：{unknown_status}")
    # 机器可读的一行，供 sync_all.py 之类的编排脚本解析（人读上面两行）。
    import json as _json
    print(_json.dumps({"ok": True, "source_total": len(rows),
                      "dry_run": dry_run,
                      "inserted": added, "updated": updated_count,
                      "skipped": skipped, "total": total,
                      "active": active,
                       "unknown_status": unknown_status,
                       "status_defaulted": status_defaulted}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(dry_run="--dry-run" in sys.argv[1:]))
