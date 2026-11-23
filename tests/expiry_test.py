"""Expiry: does `expires_at` do anything, and does it refuse to do too much?

Two failure modes matter here and they pull in opposite directions:

* The old one -- expiry that is written but never acted on, so an "expired"
  memory keeps ranking at full strength forever. That is a silent lie.
* The tempting overcorrection -- expiry deleting or auto-archiving memories.
  A wrong timestamp then silently destroys data.

So the assertions check both: expired memories must actually be demoted, and
protected / never-expire categories must be ignored rather than retired.
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="bionic-expiry-test-")
os.environ["BIONIC_DB_PATH"] = str(Path(TMP) / "agent.db")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import _env_guard  # noqa: E402,F401  OPT-04a：数据库隔离闸门（必须在任何 app.* 之前）

from app import db, expiry  # noqa: E402
from app import mcp_server as mcp  # noqa: E402

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return ok


def fresh():
    """清库。

    顺序必须子表在前：governance_actions / memory_conflicts 都 FK 到
    memories 和 governance_runs，先删父表会直接 IntegrityError 而不是
    给出能读懂的失败信息。
    """
    db.init_db()
    with db.transaction() as conn:
        for table in (
            "memory_embeddings",
            "memory_links",
            "memory_feedback",
            "memory_conflicts",
            "consolidation_candidates",
            "governance_actions",
            "governance_runs",
            "snapshots",
            "tasks",
            "memories",
            "events",
        ):
            conn.execute(f"DELETE FROM {table}")


def set_expiry_raw(mid, days_ago):
    """Write an expires_at in the past directly (save_memory cannot)."""
    ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    with db.transaction() as conn:
        conn.execute("UPDATE memories SET expires_at=? WHERE id=?", (ts, mid))
    return ts


def test_ttl_refused_for_durable_categories():
    print("\n=== 长期有效的类别不接受 TTL，且要说清原因 ===")
    fresh()
    r = mcp.save_memory(content="用户偏好中文交付", category="preference", ttl_days=7)
    check("记忆仍然保存成功", r.get("id") is not None, str(r.get("id")))
    check("明确报告 TTL 未生效", r.get("ttl_applied") is False, str(r.get("ttl_applied")))
    check("给出了拒绝原因", "长期有效" in (r.get("ttl_warning") or ""),
          (r.get("ttl_warning") or "")[:50])
    check("没有写入 expires_at", not r.get("expires_at"), str(r.get("expires_at")))

    r2 = mcp.save_memory(content="项目用 SQLite", category="project", ttl_days=7)
    check("普通项目类也拒绝", r2.get("ttl_applied") is False, str(r2.get("ttl_warning"))[:40])


def test_ttl_accepted_for_ephemeral():
    print("\n=== 临时类记忆的 TTL 真的写进去了 ===")
    fresh()
    r = mcp.save_memory(content="今晚临时调试用端口 8899", category="temp", ttl_days=2)
    check("TTL 生效", r.get("ttl_applied") is True, str(r.get("ttl_applied")))
    check("返回了 expires_at", bool(r.get("expires_at")), str(r.get("expires_at")))
    with db.connect() as conn:
        row = conn.execute("SELECT expires_at FROM memories WHERE id=?", (r["id"],)).fetchone()
    check("库里确实存了 expires_at", bool(row[0]), str(row[0]))
    check("时间在未来", expiry._parse_ts(row[0]) > datetime.now(timezone.utc),
          str(row[0]))


def test_expired_memory_demoted_in_recall():
    print("\n=== 过期记忆在召回里被降权（而不是被过滤掉）===")
    fresh()
    # Both memories carry *identical* text, so every scoring component except
    # expiry is the same for both. That isolates the one thing under test:
    # if the penalty is not applied the two scores are exactly equal and the
    # ranking assertion below fails. (An earlier version used two similar but
    # different texts, and the assertion passed for the wrong reason -- the
    # non-expired one simply matched better.)
    body = "临时调试端口 8899，记忆检索要考虑过期因素"
    fresh_mem = mcp.save_memory(content=body, category="temp")
    old_mem = mcp.save_memory(content=body, category="temp")
    set_expiry_raw(old_mem["id"], 10)  # 10 天前就过期了

    res = mcp.recall_memories(query="临时调试端口 过期因素 记忆检索", top_k=10, explain=True)
    items = res.get("results") or []
    check("召回成功", res.get("ok") is not False, f"{len(items)} 条")
    old_item = next((i for i in items if i.get("id") == old_mem["id"]), None)
    check("过期的记忆仍然出现在结果里（降权不是删除）", old_item is not None,
          "没有把它过滤掉")
    if old_item:
        check("被标记为已过期", old_item.get("_expired") is True,
              str(old_item.get("_expired")))
        check("带上了惩罚值", (old_item.get("_expiry_penalty") or 0) > 0,
              str(old_item.get("_expiry_penalty")))
    fresh_item = next((i for i in items if i.get("id") == fresh_mem["id"]), None)
    if old_item and fresh_item:
        check("惩罚之后，本来更匹配的过期记忆不再排第一",
              fresh_item["_score"] > old_item["_score"],
              f"fresh={fresh_item['_score']:.4f} vs expired={old_item['_score']:.4f}")


def test_protected_ignores_expiry():
    print("\n=== 受保护/关键档的记忆忽略自己的有效期 ===")
    fresh()
    pref = mcp.save_memory(content="用户要求全部用中文", category="preference")
    mcp.protect_memory(pref["id"])
    set_expiry_raw(pref["id"], 30)
    scan = mcp.scan_expired_memories()
    ids = [e["memory_id"] for e in scan.get("expired") or []]
    check("受保护的没有被列为可处理过期", pref["id"] not in ids, str(ids))
    skipped = {s["memory_id"]: s["reason"] for s in scan.get("skipped") or []}
    check("进了 skipped 并说明原因", pref["id"] in skipped, str(skipped.get(pref["id"], ""))[:50])
    check("原因里提到受保护", "受保护" in skipped.get(pref["id"], ""),
          skipped.get(pref["id"], "")[:40])


def test_never_expire_categories_refuse():
    print("\n=== identity/profile/fact 类根本不允许设有效期 ===")
    fresh()
    mem = mcp.save_memory(content="我是谁", category="identity")
    r = mcp.set_memory_expiry(mem["id"], days=7)
    check("设置被拒绝", r.get("ok") is False, str(r.get("message"))[:50])
    check("原因说清了", "长期有效" in (r.get("message") or ""), (r.get("message") or "")[:40])
    with db.connect() as conn:
        val = conn.execute("SELECT expires_at FROM memories WHERE id=?", (mem["id"],)).fetchone()[0]
    check("库里的 expires_at 仍是空", not val, str(val))


def test_dry_run_changes_nothing():
    print("\n=== 过期策略默认 dry_run，不改数据 ===")
    fresh()
    mem = mcp.save_memory(content="临时配置 A", category="temp")
    set_expiry_raw(mem["id"], 5)
    with db.connect() as conn:
        before = conn.execute("SELECT memory_status FROM memories WHERE id=?", (mem["id"],)).fetchone()[0]
    res = mcp.run_expiry_policy(dry_run=True)
    check("报告 dry_run", res.get("dry_run") is True, str(res.get("dry_run")))
    check("列出了待处理数量", res.get("would_demote") == 1, str(res.get("would_demote")))
    with db.connect() as conn:
        after = conn.execute("SELECT memory_status FROM memories WHERE id=?", (mem["id"],)).fetchone()[0]
    check("dry_run 没有改状态", before == after, f"{before} -> {after}")


def test_apply_marks_not_deletes():
    print("\n=== 真正执行时：标记 + 可选送审，但不删除 ===")
    fresh()
    mem = mcp.save_memory(content="临时缓存 key", category="cache")
    set_expiry_raw(mem["id"], 3)
    res = mcp.run_expiry_policy(dry_run=False)
    check("执行成功", res.get("applied") == 1, str(res.get("applied")))
    with db.connect() as conn:
        row = conn.execute("SELECT status, memory_status, content FROM memories WHERE id=?",
                           (mem["id"],)).fetchone()
    check("记忆没有被删除", row is not None, "还在")
    check("status 没有变成 archived", row[0] == "active", str(row[0]))
    check("原文没有改动", row[2] == "临时缓存 key", str(row[2]))
    with db.connect() as conn:
        ev = [r[0] for r in conn.execute(
            "SELECT event_type FROM events WHERE detail LIKE ?", (f"%memory_id={mem['id']}%",))]
    check("写了过期审计事件", "memory.expired" in ev, str(ev))

    res2 = mcp.run_expiry_policy(dry_run=False, archive=True)
    check("archive=True 时能执行", res2.get("applied") == 1, str(res2.get("applied")))
    with db.connect() as conn:
        st = conn.execute("SELECT memory_status FROM memories WHERE id=?", (mem["id"],)).fetchone()[0]
    check("送进待审核而不是直接归档", st == "pending_review", str(st))


def test_grace_period():
    print("\n=== 刚过期不立刻处理（防时钟/时区错误一锅端）===")
    fresh()
    mem = mcp.save_memory(content="刚刚过期一分钟", category="temp")
    ts = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    with db.transaction() as conn:
        conn.execute("UPDATE memories SET expires_at=? WHERE id=?", (ts, mem["id"]))
    scan = mcp.scan_expired_memories()
    ids = [e["memory_id"] for e in scan.get("expired") or []]
    check("1 分钟内过期不算过期", mem["id"] not in ids, str(ids))
    check("算进了 still_valid", scan.get("still_valid") >= 1, str(scan.get("still_valid")))
    check("宽限期是 24 小时", scan.get("grace_hours") == 24.0, str(scan.get("grace_hours")))


def test_bad_inputs():
    print("\n=== 异常输入 ===")
    fresh()
    mem = mcp.save_memory(content="测试", category="temp")
    check("ttl_days 负数不崩", mcp.save_memory(content="x", category="temp",
                                            ttl_days=-5).get("id") is not None)
    r = mcp.set_memory_expiry(mem["id"], expires_at="不是日期")
    check("非法日期被拒绝", r.get("ok") is False, str(r.get("message"))[:40])
    r2 = mcp.set_memory_expiry(99999, days=1)
    check("不存在的记忆返回 ok=False", r2.get("ok") is False, str(r2.get("message"))[:30])
    r3 = mcp.set_memory_expiry(mem["id"], days=0)
    check("days=0 表示清除", r3.get("ok") is True and r3.get("expires_at") is None,
          str(r3.get("expires_at")))


def run():
    test_ttl_refused_for_durable_categories()
    test_ttl_accepted_for_ephemeral()
    test_expired_memory_demoted_in_recall()
    test_protected_ignores_expiry()
    test_never_expire_categories_refuse()
    test_dry_run_changes_nothing()
    test_apply_marks_not_deletes()
    test_grace_period()
    test_bad_inputs()
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n=== {len(RESULTS) - len(failed)}/{len(RESULTS)} 通过 ===")
    return 1 if failed else 0


if __name__ == "__main__":
    code = run()
    import shutil
    shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(code)
