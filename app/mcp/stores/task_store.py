"""任务域 SQL（OPT-03 3.2 二期：从 core_tools.py 下沉）。

设计红线与 memory_store 一致：**每个函数都接收调用方传入的 conn，自己不 open、
不 commit、不开事务**。任务写操作天然带「改 tasks + 写 snapshots + 记事件」三件事，
事务边界必须留给工具函数——下沉前它们都在同一个 `with connect() as conn:` 里，
一旦 store 自己开连接，`handoff_task` 就可能出现「快照写了但任务没推进」的半截状态。

SQL 常量逐字搬移，未做任何改写；`ORDER BY stage DESC`、`LIMIT 100`、
`current_stage+1` 这类细节都保持原样。
"""
from __future__ import annotations

# ---- tasks ----

_INSERT_TASK = "INSERT INTO tasks(title,goal,plan_json) VALUES(?,?,?)"
_SELECT_TASK = "SELECT * FROM tasks WHERE id=?"
_SELECT_TASKS_RECENT = "SELECT * FROM tasks ORDER BY updated_at DESC LIMIT 100"
_UPDATE_TASK_HANDOFF = (
    "UPDATE tasks SET status='handoff',current_stage=current_stage+1,"
    "updated_at=CURRENT_TIMESTAMP WHERE id=?"
)
_UPDATE_TASK_COMPLETED = (
    "UPDATE tasks SET status='completed',result=?,updated_at=CURRENT_TIMESTAMP WHERE id=?"
)

# ---- snapshots ----

_INSERT_SNAPSHOT = "INSERT INTO snapshots(task_id,stage,summary,state_json) VALUES(?,?,?,?)"
_SELECT_SNAPSHOT = "SELECT * FROM snapshots WHERE id=?"
_SELECT_SNAPSHOTS_BY_TASK = "SELECT * FROM snapshots WHERE task_id=? ORDER BY stage DESC"

# ---- events ----
# 注意：带 task_id 的这一版与 memory_store 里那条（不带 task_id）不是同一个语义，
# 合并成一个函数就得给 task_id 造默认值，而 NULL 和 0 在下面的查询里是不同的意思。

_INSERT_TASK_EVENT = "INSERT INTO events(task_id,event_type,detail) VALUES(?,?,?)"
_SELECT_EVENTS_BY_TASK = "SELECT * FROM events WHERE task_id=? ORDER BY id DESC LIMIT ?"
_SELECT_EVENTS_ALL = "SELECT * FROM events ORDER BY id DESC LIMIT ?"


def insert_task(conn, title: str, goal: str, plan_json: str) -> int:
    cur = conn.execute(_INSERT_TASK, (title, goal, plan_json))
    return int(cur.lastrowid)


def fetch_task(conn, task_id: int):
    return conn.execute(_SELECT_TASK, (task_id,)).fetchone()


def fetch_recent_tasks(conn):
    return conn.execute(_SELECT_TASKS_RECENT).fetchall()


def insert_task_event(conn, task_id, event_type: str, detail: str) -> None:
    conn.execute(_INSERT_TASK_EVENT, (task_id, event_type, detail))


def insert_snapshot(conn, task_id: int, stage: int, summary: str, state_json: str) -> int:
    cur = conn.execute(_INSERT_SNAPSHOT, (task_id, stage, summary, state_json))
    return int(cur.lastrowid)


def fetch_snapshot(conn, snapshot_id: int):
    return conn.execute(_SELECT_SNAPSHOT, (snapshot_id,)).fetchone()


def fetch_snapshots(conn, task_id: int):
    return conn.execute(_SELECT_SNAPSHOTS_BY_TASK, (task_id,)).fetchall()


def mark_handoff(conn, task_id: int) -> int:
    """推进到下一阶段；返回 rowcount（0 表示任务不存在）。"""
    cur = conn.execute(_UPDATE_TASK_HANDOFF, (task_id,))
    return int(cur.rowcount)


def mark_completed(conn, task_id: int, result: str) -> int:
    """标记完成；返回 rowcount（0 表示任务不存在）。"""
    cur = conn.execute(_UPDATE_TASK_COMPLETED, (result, task_id))
    return int(cur.rowcount)


def fetch_events_by_task(conn, task_id: int, limit: int):
    return conn.execute(_SELECT_EVENTS_BY_TASK, (task_id, limit)).fetchall()


def fetch_events_all(conn, limit: int):
    return conn.execute(_SELECT_EVENTS_ALL, (limit,)).fetchall()
