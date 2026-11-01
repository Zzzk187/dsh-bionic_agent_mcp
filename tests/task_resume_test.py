"""Task resume: does it actually restore context, or just flip a status flag?

The bug this guards against is the latter. It is easy to write a resume that
returns {"ok": True, "status": "active"} and feels done -- the previous
implementation was exactly that, and every test that only checked `ok` would
have passed on it. So these assertions look at the *content* of the packet:
did the state come back, did it say so when it could not, does resuming a
finished task refuse.
"""
import os
import sys
import tempfile
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="bionic-resume-test-")
os.environ["BIONIC_DB_PATH"] = str(Path(TMP) / "agent.db")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import db, task_resume  # noqa: E402
from app import mcp_server as mcp  # noqa: E402

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return ok


def fresh():
    db.init_db()
    with db.transaction() as conn:
        conn.execute("DELETE FROM snapshots")
        conn.execute("DELETE FROM tasks")
        conn.execute("DELETE FROM events")


# 所有断言都走 mcp.* 工具面，而不是直接调 task_resume.*。用户调用的是前者；
# 早先直接调模块函数的版本，在 resume_task 被退回空壳时依然全绿（反向验证抓出来的）。


def test_no_snapshot_is_honest():
    print("\n=== 没有快照时必须说实话，不能假装恢复成功 ===")
    fresh()
    task = mcp.create_task(title="没有快照的任务", goal="验证诚实性")
    tid = task["id"]
    packet = mcp.get_resume_packet(tid)
    check("能拿到任务骨架", packet.get("ok") and packet.get("title") == "没有快照的任务",
          str(packet.get("title")))
    check("明确说上下文不可用", packet.get("context_available") is False,
          f"context_available={packet.get('context_available')}")
    check("note 里解释了为什么", "没有" in (packet.get("note") or "")
          and "快照" in (packet.get("note") or ""), (packet.get("note") or "")[:40])
    check("state 是空对象而不是 None", packet.get("state") == {}, str(packet.get("state")))

    resumed = mcp.resume_task(tid)
    check("仍然可以续跑（骨架是有效的）", resumed.get("ok") is True, str(resumed.get("status")))
    check("续跑结果照样标注没有上下文", resumed.get("context_available") is False,
          "没有把空上下文说成有")


def test_state_comes_back():
    print("\n=== 有快照时，state 必须真的回来 ===")
    fresh()
    task = mcp.create_task(title="带快照的任务", goal="验证 state 恢复")
    tid = task["id"]
    state = {"files": ["app/a.py", "app/b.py"], "cursor": 3,
             "中文键": "中文值要原样保留"}
    snap = mcp.save_stage_snapshot(tid, summary="已完成第一轮改动", state_json=__import__("json").dumps(
        state, ensure_ascii=False))
    check("快照写入成功", snap.get("id") is not None, str(snap.get("id")))

    packet = mcp.get_resume_packet(tid)
    check("上下文可用", packet.get("context_available") is True, "context_available=True")
    check("state 与写入的一致", packet.get("state") == state,
          str(packet.get("state"))[:60])
    check("中文键值没有乱码", (packet.get("state") or {}).get("中文键") == "中文值要原样保留",
          str((packet.get("state") or {}).get("中文键")))
    check("带回了阶段摘要", packet.get("last_summary") == "已完成第一轮改动",
          str(packet.get("last_summary")))
    check("带回了快照时间", bool(packet.get("last_snapshot_at")), str(packet.get("last_snapshot_at")))

    resumed = mcp.resume_task(tid)
    check("续跑返回的 state 也在", resumed.get("state") == state, "resume 也带上下文")
    check("续跑后状态是 active", resumed.get("status") == "active", str(resumed.get("status")))


def test_next_step_not_invented():
    print("\n=== 续跑不能自己把计划步骤标成完成 ===")
    fresh()
    task = mcp.create_task(title="三步任务", goal="验证不虚报进度")
    tid = task["id"]
    mcp.save_stage_snapshot(tid, summary="起步", state_json="{}")
    packet = mcp.get_resume_packet(tid)
    plan = packet.get("plan") or []
    check("计划有三步", len(plan) == 3, f"{len(plan)} 步")
    check("没有任何一步被标成 done",
          all(s.get("status") == "pending" for s in plan),
          str([s.get("status") for s in plan]))
    next_step = packet.get("next_step") or {}
    check("下一步是第一歩", next_step.get("step") == 1, str(next_step.get("step")))
    check("note 说清了从哪继续", "第 1 步" in (packet.get("note") or ""),
          (packet.get("note") or "")[:60])


def test_terminal_task_refused():
    print("\n=== 已完成的任务不能续跑 ===")
    fresh()
    task = mcp.create_task(title="已完成的任务", goal="验证拒绝")
    tid = task["id"]
    mcp.save_stage_snapshot(tid, summary="做完了", state_json="{}")
    mcp.complete_task(tid, result="搞定")
    packet = mcp.get_resume_packet(tid)
    check("标记为不可续跑", packet.get("resumable") is False,
          f"resumable={packet.get('resumable')}")
    r = mcp.resume_task(tid)
    check("resume 明确拒绝", r.get("ok") is False, str(r.get("message"))[:40])
    with db.connect() as conn:
        status = conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()[0]
    check("状态没有被改回 active", status == "completed", status)


def test_failed_then_resume():
    print("\n=== 失败可以带上下文重试，但有上限 ===")
    fresh()
    task = mcp.create_task(title="会失败的任务", goal="验证失败重试")
    tid = task["id"]
    mcp.save_stage_snapshot(tid, summary="跑到一半炸了", state_json='{"cursor": 7}')
    f = mcp.fail_task(tid, "第三方接口 502")
    check("标记失败成功", f.get("ok") and f.get("status") == "failed", str(f.get("status")))
    check("失败后仍可重试", f.get("can_auto_retry") is True, str(f.get("can_auto_retry")))

    packet = mcp.get_resume_packet(tid)
    check("失败任务仍带上下文", packet.get("context_available") is True,
          str(packet.get("state")))
    r1 = mcp.resume_task(tid)
    check("第一次重试成功", r1.get("ok") is True, str(r1.get("retry_count")))
    check("retry_count 变成 1", r1.get("retry_count") == 1, str(r1.get("retry_count")))

    for _ in range(task_resume.MAX_AUTO_RETRIES + 1):
        mcp.fail_task(tid, "又炸了")
        r = mcp.resume_task(tid)
    check(f"重试 {task_resume.MAX_AUTO_RETRIES} 次后标记耗尽",
          r.get("auto_retry_exhausted") is True,
          f"retry_count={r.get('retry_count')} exhausted={r.get('auto_retry_exhausted')}")
    check("重试次数被持久化", int(r.get("retry_count") or 0) >= task_resume.MAX_AUTO_RETRIES,
          str(r.get("retry_count")))


def test_stuck_detection():
    print("\n=== 卡住检测：只报告，不自动重试 ===")
    fresh()
    old = mcp.create_task(title="很老的任务", goal="验证卡住检测")
    with db.transaction() as conn:
        conn.execute(
            "UPDATE tasks SET status='active', updated_at=datetime('now','-48 hours') WHERE id=?",
            (old["id"],))
    fresh_task = mcp.create_task(title="新任务", goal="刚建")
    res = mcp.find_stuck_tasks(stuck_hours=24)
    check("扫描成功", res.get("ok") is True, f"scanned={res.get('scanned')}")
    ids = [t["id"] for t in res.get("tasks") or []]
    check("老任务被点出来", old["id"] in ids, str(ids))
    check("新任务没被误报", fresh_task["id"] not in ids, str(ids))
    item = next((t for t in res.get("tasks") or [] if t["id"] == old["id"]), {})
    check("给出了停滞时长", (item.get("age_hours") or 0) >= 47, str(item.get("age_hours")))
    check("说明只报告不自动重试", "不自动重试" in (res.get("note") or ""),
          (res.get("note") or "")[:40])

    with db.connect() as conn:
        status = conn.execute("SELECT status FROM tasks WHERE id=?", (old["id"],)).fetchone()[0]
    check("检测没有改变任务状态", status == "active", status)


def test_bad_input():
    print("\n=== 异常输入不崩 ===")
    check("不存在的任务返回 ok=False", task_resume.build_resume_packet(99999).get("ok") is False)
    check("resume 不存在的任务不崩", task_resume.resume(99999).get("ok") is False)
    check("fail 不存在的任务不崩", task_resume.mark_failed(99999).get("ok") is False)
    bad = mcp.find_stuck_tasks(stuck_hours="abc")
    check("非法 stuck_hours 退回默认", bad.get("stuck_hours") == task_resume.DEFAULT_STUCK_HOURS,
          str(bad.get("stuck_hours")))


def test_tool_surface():
    print("\n=== MCP 工具面 ===")
    for name in ("resume_task", "get_resume_packet", "find_stuck_tasks", "fail_task"):
        check(f"工具 {name} 存在", callable(getattr(mcp, name, None)))


def run():
    test_no_snapshot_is_honest()
    test_state_comes_back()
    test_next_step_not_invented()
    test_terminal_task_refused()
    test_failed_then_resume()
    test_stuck_detection()
    test_bad_input()
    test_tool_surface()
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n=== {len(RESULTS) - len(failed)}/{len(RESULTS)} 通过 ===")
    return 1 if failed else 0


if __name__ == "__main__":
    code = run()
    import shutil
    shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(code)
