"""管理面板的只读查询（OPT-03 3.3：从 admin_ui.py 下沉）。

为什么单独一个模块而不是放进 `app/mcp/stores/`：那里放的是 **MCP 工具**用的写/读
store，服务对象是 85 个工具；这些查询只服务 Web 面板的统计展示，跟 MCP 契约无关。
混在一起会让「MCP 的 store 层」概念失焦，也会让面板的展示需求悄悄渗进工具层。

**这里只读**：全部是 SELECT，没有一个写语句。面板路由自己仍用 `with connect() as conn:`
掌握连接与事务边界（与 memory_store 同一条红线：store 不 open 连接）。
"""
from __future__ import annotations

import sqlite3

# ---- /api/stats ----

_COUNT_MEMORIES = "SELECT COUNT(*) FROM memories"
_COUNT_ACTIVE = "SELECT COUNT(*) FROM memories WHERE status='active'"
_COUNT_ARCHIVED = "SELECT COUNT(*) FROM memories WHERE status='archived'"
_COUNT_TASKS = "SELECT COUNT(*) FROM tasks"
_COUNT_LINKS = "SELECT COUNT(*) FROM memory_links"
_COUNT_EVENTS = "SELECT COUNT(*) FROM events"

# ---- 单条记忆 ----

_SELECT_MEMORY = "SELECT * FROM memories WHERE id=?"


def stats(conn: sqlite3.Connection) -> dict:
    """六个计数。一次连接里查完，避免界面上几个数字来自不同瞬间的库。"""
    return {
        "memories_total": conn.execute(_COUNT_MEMORIES).fetchone()[0],
        "memories_active": conn.execute(_COUNT_ACTIVE).fetchone()[0],
        "memories_archived": conn.execute(_COUNT_ARCHIVED).fetchone()[0],
        "tasks": conn.execute(_COUNT_TASKS).fetchone()[0],
        "links": conn.execute(_COUNT_LINKS).fetchone()[0],
        "events": conn.execute(_COUNT_EVENTS).fetchone()[0],
    }


def memory_row(conn: sqlite3.Connection, memory_id: int):
    return conn.execute(_SELECT_MEMORY, (memory_id,)).fetchone()


def governance_columns(conn: sqlite3.Connection, ids: list[int]):
    """按 id 批量补治理字段（access_count / 保护 / 三分数）。

    `_memory_out` 默认剔除这些列——那是 MCP 的输出契约；面板要按访问热度分组，
    拿不到时前端会静默把每条记忆都当成「从没翻过」。所以单独补一次。
    """
    if not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    sql = (
        "SELECT id, access_count, last_accessed_at, memory_status, protected, "
        "importance_score, confidence_score, freshness_score "
        f"FROM memories WHERE id IN ({placeholders})"
    )
    return conn.execute(sql, ids).fetchall()


def category_of(conn: sqlite3.Connection, ids: list[int]) -> dict:
    """id → category（评分接口返回里缺分类时补上）。"""
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT id, category FROM memories WHERE id IN ({placeholders})", ids
    )
    return {row[0]: (row[1] or "") for row in rows}


def timeline_rows(conn: sqlite3.Connection, *, axis: str, include_archived: bool):
    """时间线分档用的 (时间戳, 分类) 行集。

    `axis` 与 `include_archived` 都直接拼进 SQL，是因为它们是**列名与状态集合**，
    不是值：SQLite 的参数绑定只能绑值。两个参数都来自 FastAPI 的 Query 声明
    （`pattern="^(created|updated)$"` 与布尔），已经把注入面收在枚举里。
    """
    col = "created_at" if axis == "created" else "updated_at"
    statuses = "('active','archived')" if include_archived else "('active')"
    sql = (
        f"SELECT {col} AS ts, category FROM memories "
        f"WHERE status IN {statuses} AND memory_status != 'pending_review'"
    )
    return conn.execute(sql).fetchall()


def breakdown_rows(conn: sqlite3.Connection, *, include_archived: bool):
    """分类/热度/时间三维统计的行集（三个维度共用一批行）。"""
    statuses = "('active','archived')" if include_archived else "('active')"
    sql = (
        "SELECT id, category, access_count, last_accessed_at, created_at, "
        f"updated_at, importance_score FROM memories "
        f"WHERE status IN {statuses} AND memory_status != 'pending_review'"
    )
    return conn.execute(sql).fetchall()


def protection_rows(conn: sqlite3.Connection, *, category: str, include_archived: bool):
    """保护分档统计的行集（`WHERE 1=1` 起手 + 两个可选 AND，与工具侧同源）。"""
    sql = "SELECT * FROM memories WHERE 1=1"
    args: list = []
    if category:
        sql += " AND category=?"
        args.append(category)
    if not include_archived:
        sql += " AND status='active'"
    return conn.execute(sql, args).fetchall()
