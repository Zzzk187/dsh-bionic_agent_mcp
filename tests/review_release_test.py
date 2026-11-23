"""pending_review 是一个排除标记，因此必须有一条出去的路径。

`pending_review` 不是"某个界面里的状态"，而是召回/语义检索/关键词检索三条路
共同的过滤条件（memory_lifecycle 的 SQL 里写着 `memory_status NOT IN
('pending_review','superseded')`）。这意味着：

    mark_review 之后，如果没有任何代码把它放回来，
    这条记忆就永远从 agent 眼里消失了 —— 而它在库里依然是 status='active'，
    从数据库表面看一切正常。

这条测试守的就是那个出口：
1. 裁决冲突（not_conflict / kept_both / duplicate_confirmed）会解除关联记忆的保留；
2. 裁决为 pending（重新打开）时不解除 —— 那等于自己把刚立的问题赦免了；
3. 单独的 release_review_status 可用，且重复调用幂等；
4. 解除之后再检索，记忆真的回来了（不只是字段变了）。
"""
import os
import sys
import tempfile
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="bionic-review-test-")
os.environ["BIONIC_DB_PATH"] = str(Path(TMP) / "agent.db")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import _env_guard  # noqa: E402,F401  OPT-04a：数据库隔离闸门（必须在任何 app.* 之前）

from app import db  # noqa: E402
from app import mcp_server as mcp  # noqa: E402
from app import governance_maintenance as gm  # noqa: E402

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return ok


def fresh():
    db.init_db()
    with db.transaction() as conn:
        for t in ("memory_embeddings", "memory_links", "memory_feedback",
                  "memory_conflicts", "consolidation_candidates", "governance_actions",
                  "governance_runs", "snapshots", "tasks", "memories", "events"):
            conn.execute(f"DELETE FROM {t}")


def state(mid):
    with db.connect() as conn:
        r = conn.execute("SELECT status, memory_status FROM memories WHERE id=?",
                         (int(mid),)).fetchone()
    return (r["status"], r["memory_status"]) if r else (None, None)


def recalled(mid, query="桌面版端口"):
    res = mcp.semantic_search_memories(query, limit=10)
    rows = res if isinstance(res, list) else (res.get("results") or res.get("memories") or [])
    return int(mid) in [int(m["id"]) for m in rows]


def make_pair():
    a = mcp.save_memory(content="DSH 桌面版这次端口是 59647", category="环境")
    b = mcp.save_memory(content="DSH 桌面版这次端口是 51366", category="环境")
    with db.transaction() as conn:
        conn.execute("UPDATE memories SET tags_json='[1]'")
    return a, b


def hold_through_maintenance(mid_hint=None):
    """跑到 review+apply，让引擎自己把某条记忆标成 pending_review。"""
    gm.run_memory_maintenance(mode="review", limit=50)
    acts = mcp.review_governance_actions(approval_status="pending").get("actions") or []
    if not acts:
        return None, []
    mcp.apply_governance_actions(action_ids=[x["id"] for x in acts])
    return [int(x["memory_id"]) for x in acts], acts


def pending_conflict_id():
    rows = mcp.list_memory_conflicts(resolution="pending")
    items = rows.get("conflicts") or rows.get("data") or []
    return int(items[0]["id"]) if items else None


def test_hold_actually_hides_the_memory():
    print("\n=== 被保留的记忆确实检索不到（这就是问题本身）===")
    fresh()
    make_pair()
    held, acts = hold_through_maintenance()
    check("引擎把某条记忆标成待审核", bool(held), str(held))
    if not held:
        return
    mid = held[0]
    check("字段上确实是 pending_review", state(mid)[1] == "pending_review", str(state(mid)))
    check("但 db 里仍是 active（表面看着正常）", state(mid)[0] == "active", str(state(mid)))
    check("语义检索不再返回它", recalled(mid) is False, "hidden")


def test_adjudication_releases_the_hold():
    print("\n=== 人工裁决后保留被解除，记忆回到检索 ===")
    fresh()
    make_pair()
    held, _ = hold_through_maintenance()
    if not held:
        check("前置：有待审核记忆", False, "skip")
        return
    mid = held[0]
    cid = pending_conflict_id()
    check("存在待裁决冲突", cid is not None, str(cid))
    if cid is None:
        return
    res = mcp.review_memory_conflict(conflict_id=cid, resolution="kept_both",
                                    note="两条都保留")
    check("裁决成功", res.get("ok") is True, str(res.get("resolution")))
    check("返回值里点出解除了谁", res.get("review_released") == [mid],
          str(res.get("review_released")))
    check("记忆回到 active", state(mid) == ("active", "active"), str(state(mid)))
    check("语义检索重新能拿到它", recalled(mid) is True, "visible")


def test_pending_resolution_keeps_the_hold():
    print("\n=== 裁决回 pending（重新打开）时不解除 ===")
    fresh()
    make_pair()
    held, _ = hold_through_maintenance()
    if not held:
        check("前置：有待审核记忆", False, "skip")
        return
    mid = held[0]
    cid = pending_conflict_id()
    if cid is None:
        check("前置：有冲突", False, "skip")
        return
    res = mcp.review_memory_conflict(conflict_id=cid, resolution="pending",
                                    note="还需要再看")
    check("裁决记录为 pending", res.get("resolution") == "pending", str(res.get("resolution")))
    check("没有解除保留", not res.get("review_released"), str(res.get("review_released")))
    check("记忆仍是待审核", state(mid)[1] == "pending_review", str(state(mid)))


def test_release_is_explicit_and_idempotent():
    print("\n=== 单独解除：幂等、且只对真正被保留的记忆生效 ===")
    fresh()
    a = mcp.save_memory(content="普通记忆一条", category="环境")
    res = mcp.release_review_status(memory_id=a["id"], reason="本来就没保留")
    check("非待审核 -> ok 但 changed=False", res.get("ok") is True and res.get("changed") is False,
          str(res.get("message")))
    check("状态没被乱改", state(a["id"]) == ("active", "active"), str(state(a["id"])))

    mcp.set_review_status(memory_id=a["id"], reason="先标上")
    check("标记生效", state(a["id"])[1] == "pending_review", str(state(a["id"])))
    first = mcp.release_review_status(memory_id=a["id"], reason="放行")
    check("解除生效", first.get("changed") is True, str(first.get("message")))
    check("回到 active", state(a["id"]) == ("active", "active"), str(state(a["id"])))
    second = mcp.release_review_status(memory_id=a["id"], reason="再来一次")
    check("重复解除幂等", second.get("ok") is True and second.get("changed") is False,
          str(second.get("message")))

    # 归档的记忆不该被这个工具"复活"
    mcp.archive_memory(memory_id=a["id"])
    third = mcp.release_review_status(memory_id=a["id"], reason="归档的不该动")
    check("归档记忆不被解除", third.get("changed") is False and state(a["id"])[0] == "archived",
          str(state(a["id"])))


def test_release_is_reversible():
    print("\n=== 解除动作本身可回滚（误放行还能收回去）===")
    fresh()
    a = mcp.save_memory(content="可回滚验证用", category="环境")
    mcp.set_review_status(memory_id=a["id"], reason="标上")
    mcp.release_review_status(memory_id=a["id"], reason="放行")
    with db.connect() as conn:
        aid = conn.execute(
            "SELECT MAX(id) FROM governance_actions WHERE action_type='mark_review_revert'"
        ).fetchone()[0]
    check("解除动作有审计记录", aid is not None, str(aid))
    check("回滚前是 active", state(a["id"])[1] == "active", str(state(a["id"])))
    rb = mcp.rollback_governance_action(action_id=int(aid), reason="回滚验证")
    check("回滚成功", rb.get("ok") is True and rb.get("changed") is True,
          str(rb.get("message")))
    check("回滚后重新变回待审核", state(a["id"])[1] == "pending_review", str(state(a["id"])))


def test_duplicate_confirmed_also_releases():
    print("\n=== 裁决为完全重复时也会解除保留（归档的是输家，赢家要能检索）===")
    fresh()
    make_pair()
    held, _ = hold_through_maintenance()
    if not held:
        check("前置：有待审核记忆", False, "skip")
        return
    cid = pending_conflict_id()
    if cid is None:
        check("前置：有冲突", False, "skip")
        return
    res = mcp.review_memory_conflict(conflict_id=cid, resolution="duplicate_confirmed",
                                    note="逐字重复")
    check("裁决执行", res.get("ok") is True, str(res.get("resolution")))
    with db.connect() as conn:
        left = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE status='active' AND "
            "memory_status='pending_review'").fetchone()[0]
    check("没有记忆被留在待审核里", left == 0, f"{left} 条")


def run():
    test_hold_actually_hides_the_memory()
    test_adjudication_releases_the_hold()
    test_pending_resolution_keeps_the_hold()
    test_release_is_explicit_and_idempotent()
    test_release_is_reversible()
    test_duplicate_confirmed_also_releases()
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n=== {len(RESULTS) - len(failed)}/{len(RESULTS)} 通过 ===")
    return 1 if failed else 0


if __name__ == "__main__":
    code = run()
    import shutil
    shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(code)
