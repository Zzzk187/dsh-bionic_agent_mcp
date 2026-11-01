"""Task resume: rebuild a task's working context after a restart.

Why this module exists at all
-----------------------------
`resume_task` used to be `UPDATE tasks SET status='active'` plus an audit row.
The docstring promised "resume a paused/handed-off task" but the snapshots it
was supposedly resuming from were never read. A caller following the docs got
a task whose status said "active" and whose context was simply gone -- the
worst failure shape there is, because nothing errors and nothing is missing
from the API's point of view.

So the rules here are:

* A resume packet either carries real context or says out loud that it cannot.
  `context_available: false` with a reason beats a confident empty packet.
* Resuming does not invent progress. It reports the last stage actually
  recorded and the first plan step still pending; it never marks steps done.
* Stuck detection is time-based and deliberately dumb. A task is "stuck" when
  it claims to be active but nothing has touched it for a while. Anything
  cleverer would need to know the task's expected duration, which this schema
  does not have.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from app import db

RESUME_RULE_VERSION = "phase6-resume-v1"

#: A task claiming to be active with no update for this long is worth showing
#: to a human. Not an error -- plenty of tasks legitimately wait overnight.
DEFAULT_STUCK_HOURS = 24.0

#: Resuming a task that has failed this many times stops being automatic.
MAX_AUTO_RETRIES = 3

TERMINAL_STATUSES = {"completed", "cancelled"}
RESUMABLE_STATUSES = {"created", "active", "handoff", "paused", "failed"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(text[:len(fmt) + 6 if "%f" in fmt else 19], fmt).replace(
                tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _hours_since(value) -> float | None:
    ts = _parse_ts(value)
    if ts is None:
        return None
    return max(0.0, (_now() - ts).total_seconds() / 3600.0)


def _plan_of(task: dict) -> list[dict]:
    raw = task.get("plan_json")
    if raw is None:
        return []
    try:
        plan = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        return []
    return plan if isinstance(plan, list) else []


def _next_pending_step(plan: list[dict]) -> dict | None:
    for step in plan:
        if isinstance(step, dict) and str(step.get("status", "pending")) != "done":
            return step
    return None


def build_resume_packet(task_id: int) -> dict:
    """Everything an agent needs to pick a task back up, or an honest refusal."""
    try:
        with db.connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (int(task_id),)).fetchone()
            if not row:
                return {"ok": False, "message": "Task not found", "task_id": int(task_id)}
            task = db.rowdict(row)
            snaps = conn.execute(
                "SELECT * FROM snapshots WHERE task_id=? ORDER BY stage DESC, id DESC",
                (int(task_id),)).fetchall()
            history = conn.execute(
                "SELECT event_type, detail, created_at FROM events WHERE task_id=? "
                "ORDER BY id DESC LIMIT 20", (int(task_id),)).fetchall()
    except Exception as exc:
        return {"ok": False, "message": f"{type(exc).__name__}: {exc}",
                "task_id": int(task_id)}

    status = str(task.get("status") or "")
    plan = _plan_of(task)
    latest = db.rowdict(snaps[0]) if snaps else None
    next_step = _next_pending_step(plan)

    packet = {
        "ok": True,
        "task_id": int(task_id),
        "title": task.get("title"),
        "goal": task.get("goal"),
        "status": status,
        "recorded_stage": int(task.get("current_stage") or 1),
        "age_hours": _hours_since(task.get("updated_at")),
        "plan": plan,
        "next_step": next_step,
        "stuck": is_stuck(task),
        "resumable": status in RESUMABLE_STATUSES,
        "snapshot_count": len(snaps),
        "rule_version": RESUME_RULE_VERSION,
    }

    if status in TERMINAL_STATUSES:
        packet["resumable"] = False
        packet["note"] = f"任务已处于终态 {status}，没有可续跑的进度。"
        return packet

    if latest is None:
        # The honest case. Saying "resumed" here would be a lie.
        packet["context_available"] = False
        packet["state"] = {}
        packet["note"] = ("这个任务没有任何阶段快照，所以只能恢复任务骨架（标题/目标/计划），"
                          "不能恢复执行上下文。需要上下文的话，先在工作过程中调用 "
                          "save_stage_snapshot 或 handoff_task。")
        return packet

    try:
        state = json.loads(latest.get("state_json") or "{}")
    except (json.JSONDecodeError, TypeError):
        state = {}

    packet.update({
        "context_available": True,
        "state": state if isinstance(state, dict) else {"value": state},
        "last_summary": latest.get("summary"),
        "last_stage": int(latest.get("stage") or 0),
        "last_snapshot_at": latest.get("created_at"),
        "snapshot_age_hours": _hours_since(latest.get("created_at")),
        "recent_events": [db.rowdict(e) for e in history],
    })
    if next_step is not None:
        packet["note"] = (f"从第 {packet['recorded_stage']} 阶段的快照恢复；"
                          f"下一步是计划里的第 {next_step.get('step')} 步：{next_step.get('title')}。")
    else:
        packet["note"] = "计划里所有步骤都已标记完成，但任务还没结项——确认后可以 complete_task。"
    return packet


def is_stuck(task: dict, stuck_hours: float = DEFAULT_STUCK_HOURS) -> bool:
    """A task that claims to be running but nothing has touched it."""
    if str(task.get("status") or "") not in ("active", "handoff"):
        return False
    age = _hours_since(task.get("updated_at"))
    return age is not None and age >= float(stuck_hours)


def find_stuck_tasks(stuck_hours: float = DEFAULT_STUCK_HOURS, limit: int = 50) -> dict:
    """Tasks to look at after a restart. Read-only."""
    try:
        stuck_hours = max(0.1, float(stuck_hours))
    except (TypeError, ValueError):
        stuck_hours = DEFAULT_STUCK_HOURS
    limit = max(1, min(int(limit), 200))
    try:
        with db.connect() as conn:
            rows = [db.rowdict(r) for r in conn.execute(
                "SELECT * FROM tasks WHERE status IN ('active','handoff','failed') "
                "ORDER BY updated_at ASC LIMIT ?", (limit,)).fetchall()]
    except Exception as exc:
        return {"ok": False, "message": f"{type(exc).__name__}: {exc}", "tasks": []}

    stuck = []
    for task in rows:
        age = _hours_since(task.get("updated_at"))
        candidate = dict(task)
        candidate["plan"] = _plan_of(task)
        candidate.pop("plan_json", None)
        candidate["age_hours"] = age
        candidate["stuck"] = is_stuck(task, stuck_hours)
        candidate["retry_count"] = int(task.get("retry_count") or 0)
        if candidate["stuck"] or str(task.get("status")) == "failed":
            stuck.append(candidate)
    return {
        "ok": True,
        "rule_version": RESUME_RULE_VERSION,
        "stuck_hours": stuck_hours,
        "scanned": len(rows),
        "count": len(stuck),
        "tasks": stuck,
        "note": ("这里只列出「声称在跑但很久没动」和「已标记失败」的任务，不自动重试——"
                 "重启后该不该继续跑是人的决定，不是时间的函数。"),
    }


def resume(task_id: int) -> dict:
    """Move a resumable task back to active and return its resume packet."""
    packet = build_resume_packet(task_id)
    if not packet.get("ok"):
        return packet
    if not packet.get("resumable"):
        return {**packet, "ok": False,
                "message": packet.get("note") or "任务不可续跑"}

    try:
        with db.connect() as conn:
            retries = 0
            row = conn.execute("SELECT retry_count FROM tasks WHERE id=?",
                               (int(task_id),)).fetchone()
            if row is not None:
                try:
                    retries = int(row[0] or 0)
                except (TypeError, ValueError):
                    retries = 0
            prev = conn.execute("SELECT status FROM tasks WHERE id=?",
                                (int(task_id),)).fetchone()
            prev_status = str(prev[0]) if prev else ""
            retries = retries + 1 if prev_status == "failed" else retries
            conn.execute(
                "UPDATE tasks SET status='active',retry_count=?,"
                "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (retries, int(task_id)))
            conn.execute(
                "INSERT INTO events(task_id,event_type,detail) VALUES(?,?,?)",
                (int(task_id), "task.resumed",
                 f"stage={packet['recorded_stage']}; from={prev_status or 'unknown'}; "
                 f"context={'yes' if packet.get('context_available') else 'no'}; "
                 f"retry={retries}"))
    except Exception as exc:
        return {"ok": False, "message": f"{type(exc).__name__}: {exc}",
                "task_id": int(task_id)}

    return {**packet, "ok": True, "status": "active",
            "retry_count": retries,
            "auto_retry_exhausted": retries >= MAX_AUTO_RETRIES}


def mark_failed(task_id: int, reason: str = "") -> dict:
    """Record that a stage failed, without pretending it is still running."""
    try:
        with db.connect() as conn:
            task = conn.execute("SELECT * FROM tasks WHERE id=?", (int(task_id),)).fetchone()
            if not task:
                return {"ok": False, "message": "Task not found", "task_id": int(task_id)}
            conn.execute(
                "UPDATE tasks SET status='failed',updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (int(task_id),))
            conn.execute(
                "INSERT INTO events(task_id,event_type,detail) VALUES(?,?,?)",
                (int(task_id), "task.failed", (reason or "")[:500]))
            retries = 0
            row = conn.execute("SELECT retry_count FROM tasks WHERE id=?",
                               (int(task_id),)).fetchone()
            if row is not None:
                try:
                    retries = int(row[0] or 0)
                except (TypeError, ValueError):
                    retries = 0
    except Exception as exc:
        return {"ok": False, "message": f"{type(exc).__name__}: {exc}",
                "task_id": int(task_id)}
    return {"ok": True, "task_id": int(task_id), "status": "failed",
            "retry_count": retries,
            "can_auto_retry": retries < MAX_AUTO_RETRIES,
            "note": ("失败状态会保留完整的阶段快照与事件，resume_task 可以带上下文重试；"
                     f"自动重试上限 {MAX_AUTO_RETRIES} 次，超过后需要人工判断是不是路走错了。")}
