"""记忆表的数据访问层（OPT-03 3.2 一期：6 个记忆工具、19 处 SQL）。

**设计红线：store 函数一律接收调用方传入的 `conn`，自己不 open、不 commit、
不开事务。** 事务边界仍由工具函数决定——工具在 `with connect() as conn:` 里
按原顺序调 store，任何一步抛异常整块回滚，与下沉前逐字节等价。

为什么不做成「每个 store 函数自带 connect」：那会让 6 个工具里原本 1 个事务
变成多处独立事务。`save_memory` 现在一次写入 memories + events(created) +
events(ttl_refused)，一旦中间失败就留下「记忆存在但没有审计」的半截状态，
而 `INSERT INTO events` 失败恰好是最难复现的那类。

SQL 文本逐字从 `app/mcp/core_tools.py` 搬来，没有改写、没有加索引、没有
改动 WHERE 条件——`archive_memory` 与 `supersede_memory` 都写
`status='archived'`，但前者带 `AND status='active'` 守卫、后者是无条件覆盖，
所以必须是两个函数（合成一个就会让 supersede 在并发下多改一行）。
"""
from __future__ import annotations

import sqlite3

# --- 逐字保留的 SQL 常量 -------------------------------------------------
_INSERT_MEMORY = """INSERT INTO memories(content,category,source,confidence,tags_json,expires_at,memory_type)
               VALUES(?,?,?,?,?,?,?)"""

_INSERT_MEMORY_SUPERSEDING = """INSERT INTO memories(content,category,source,confidence,status,tags_json,supersedes_id)
               VALUES(?,?,?,?, 'active', ?, ?)"""

_ARCHIVE_GUARDED = (
    "UPDATE memories SET status='archived',updated_at=CURRENT_TIMESTAMP "
    "WHERE id=? AND status='active'"
)

_ARCHIVE_UNGUARDED = (
    "UPDATE memories SET status='archived',updated_at=CURRENT_TIMESTAMP WHERE id=?"
)

_RESTORE_GUARDED = (
    "UPDATE memories SET status='active',updated_at=CURRENT_TIMESTAMP "
    "WHERE id=? AND status='archived'"
)

_INSERT_EVENT = "INSERT INTO events(event_type,detail) VALUES(?,?)"
_EXISTS_MEMORY = "SELECT id FROM memories WHERE id=?"
_SET_EXPIRES_AT = "UPDATE memories SET expires_at=?,updated_at=CURRENT_TIMESTAMP WHERE id=?"
_SET_MEMORY_TYPE = "UPDATE memories SET memory_type=? WHERE id=?"
_SELECT_UNTYPED_ACTIVE = (
    "SELECT id, content, category, source FROM memories "
    "WHERE memory_type IS NULL AND status='active' ORDER BY id LIMIT ?"
)
_SELECT_TYPE_FIELDS = "SELECT content, category, source FROM memories WHERE id=?"

_INSERT_LINK = (
    "INSERT INTO memory_links(source_memory_id,target_memory_id,relation,note) "
    "VALUES(?,?,?,?)"
)

_SELECT_MEMORY = "SELECT * FROM memories WHERE id=?"
_SELECT_ACTIVE_MEMORY = "SELECT * FROM memories WHERE id=? AND status='active'"


# --- 读 -------------------------------------------------------------------
def fetch_memory(conn: sqlite3.Connection, memory_id: int) -> sqlite3.Row | None:
    """按 id 取记忆，含已归档。返回 sqlite3.Row 或 None。"""
    return conn.execute(_SELECT_MEMORY, (memory_id,)).fetchone()


def fetch_active_memory(conn: sqlite3.Connection, memory_id: int) -> sqlite3.Row | None:
    """按 id 取**活跃**记忆（status='active'）；归档的返回 None。"""
    return conn.execute(_SELECT_ACTIVE_MEMORY, (memory_id,)).fetchone()


# --- 写 -------------------------------------------------------------------
def insert_memory(
    conn: sqlite3.Connection,
    *,
    content: str,
    category: str,
    source: str,
    confidence: float,
    tags_json: str,
    expires_at: str | None,
    memory_type: str | None,
) -> int:
    """插入一条新记忆（status 走表默认值 'active'），返回新 id。"""
    cur = conn.execute(
        _INSERT_MEMORY,
        (content, category, source, confidence, tags_json, expires_at, memory_type),
    )
    return int(cur.lastrowid)


def insert_memory_superseding(
    conn: sqlite3.Connection,
    *,
    content: str,
    category: str,
    source: str,
    confidence: float,
    tags_json: str,
    supersedes_id: int,
) -> int:
    """插入一条「取代版」记忆：status 显式写 'active'，并记 supersedes_id。

    与 `insert_memory` 分开而不是加可选参数，是为了让两条 INSERT 的列清单
    都保持逐字不变——`supersede_memory` 原本就显式写 status，`save_memory`
    原本就靠默认值，混成一个函数必然要改其中一条的 SQL。
    """
    cur = conn.execute(
        _INSERT_MEMORY_SUPERSEDING,
        (content, category, source, confidence, tags_json, supersedes_id),
    )
    return int(cur.lastrowid)


def update_memory_fields(
    conn: sqlite3.Connection,
    memory_id: int,
    assignments: list[str],
    values: list,
) -> int:
    """按 `assignments`（如 `["content=?", "updated_at=CURRENT_TIMESTAMP"]`）更新。

    返回 rowcount；**0 表示没有这条活跃记录**（调用方据此回「Active memory
    not found」）。WHERE 里的 `status='active'` 守卫不能省——它让「更新归档
    记忆」变成明确的失败，而不是悄悄改掉一条不该出现的历史记录。
    """
    sql = f"UPDATE memories SET {', '.join(assignments)} WHERE id=? AND status='active'"
    cur = conn.execute(sql, list(values) + [memory_id])
    return int(cur.rowcount)


def archive_memory_row(conn: sqlite3.Connection, memory_id: int) -> int:
    """带守卫的归档：只归档 `status='active'` 的行。返回 rowcount。"""
    return int(conn.execute(_ARCHIVE_GUARDED, (memory_id,)).rowcount)


def force_archive_memory_row(conn: sqlite3.Connection, memory_id: int) -> int:
    """无守卫的归档，仅供 `supersede_memory` 用（旧记录此时必然已读出来是活跃的）。"""
    return int(conn.execute(_ARCHIVE_UNGUARDED, (memory_id,)).rowcount)


def restore_memory_row(conn: sqlite3.Connection, memory_id: int) -> int:
    """把归档记忆改回活跃：只认 `status='archived'` 的行。返回 rowcount。"""
    return int(conn.execute(_RESTORE_GUARDED, (memory_id,)).rowcount)


def insert_event(conn: sqlite3.Connection, event_type: str, detail: str) -> None:
    """写一条审计事件（无 task_id 的那种）。"""
    conn.execute(_INSERT_EVENT, (event_type, detail))

def insert_memory_link(
    conn: sqlite3.Connection,
    source_memory_id: int,
    target_memory_id: int,
    relation: str,
    note: str,
) -> int:
    """在 memory_links 建一条关系，返回 link id。"""
    cur = conn.execute(
        _INSERT_LINK, (source_memory_id, target_memory_id, relation, note)
    )
    return int(cur.lastrowid)


def memory_exists(conn: sqlite3.Connection, memory_id: int) -> bool:
    """只判存在性，不取整行（link_memories / get_memory_links 用）。"""
    return conn.execute(_EXISTS_MEMORY, (memory_id,)).fetchone() is not None


def set_expires_at(conn: sqlite3.Connection, memory_id: int, value: str | None) -> None:
    """改 expires_at 并顺带刷 updated_at（与下沉前同一条 SQL）。"""
    conn.execute(_SET_EXPIRES_AT, (value, memory_id))


def update_memory_type(conn: sqlite3.Connection, memory_id: int, memory_type: str) -> None:
    conn.execute(_SET_MEMORY_TYPE, (memory_type, memory_id))


def untyped_active_rows(conn: sqlite3.Connection, limit: int):
    """还没定类型的活跃记忆（backfill_memory_types 的候选源）。"""
    return conn.execute(_SELECT_UNTYPED_ACTIVE, (limit,)).fetchall()


def select_all_for_protection(conn: sqlite3.Connection, *, category: str,
                              include_archived: bool):
    """保护分档统计的行集：动态拼 category / status 条件。

    与下沉前逐字同源（`WHERE 1=1` 起手 + 两个可选 AND），因为这里的
    `include_archived` 语义是「只在默认时才加 status 过滤」——写成固定 SQL
    会让 include_archived=True 时多出一条不该有的条件。
    """
    sql = "SELECT * FROM memories WHERE 1=1"
    args: list = []
    if category:
        sql += " AND category=?"
        args.append(category)
    if not include_archived:
        sql += " AND status='active'"
    return conn.execute(sql, args).fetchall()


def fetch_type_fields(conn: sqlite3.Connection, memory_id: int):
    """classify_memory 需要的三列。"""
    return conn.execute(_SELECT_TYPE_FIELDS, (memory_id,)).fetchone()
