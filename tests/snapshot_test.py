"""Whole-database snapshot / restore tests.

These deliberately operate on a REAL sqlite file (a temp one) rather than
mocking: snapshots are about file-level guarantees (torn writes, sha256,
transaction atomicity, sqlite_sequence) that are exactly what mocks hide.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="bionic-snap-test-")
os.environ["BIONIC_DB_PATH"] = str(Path(TMP) / "agent.db")
os.environ["BIONIC_SNAPSHOT_DIR"] = str(Path(TMP) / "snaps")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import db, snapshots  # noqa: E402
from app import mcp_server as mcp  # noqa: E402

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return ok


def test_roundtrip():
    print("\n=== 快照与恢复 ===")
    mcp.save_memory(content="快照测试第一条：用户偏好中文文档", category="preference")
    mcp.save_memory(content="快照测试第二条：项目用 FastAPI 做面板", category="项目")
    mcp.save_memory(content="快照测试第三条：教训是别用绝对定位", category="教训")

    with db.connect() as conn:
        before_rows = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]

    snap = snapshots.create_snapshot(label="before-damage", note="测试用快照")
    check("快照创建成功", snap.get("ok"), f"id={snap.get('snapshot_id')} rows={snap.get('total_rows')}")
    check("快照记下了每张表的行数", isinstance(snap.get("table_counts"), dict)
          and snap["table_counts"].get("memories") == before_rows,
          f"memories={snap.get('table_counts', {}).get('memories')}")
    payload_file = Path(snap.get("path") or "")
    check("快照文件真的落盘了", payload_file.is_file(),
          f"{payload_file.name} {payload_file.stat().st_size if payload_file.is_file() else 0} 字节")
    check("快照里带 sha256 校验", len(snap.get("sha256") or "") == 64, (snap.get("sha256") or "")[:16] + "…")

    # Destroy the library the way a bad batch job would: mass archive + delete.
    with db.transaction() as conn:
        conn.execute("UPDATE memories SET status='archived', memory_status='archived'")
        conn.execute("INSERT INTO tasks (title,goal) VALUES ('破坏性任务','验证恢复')")
    with db.connect() as conn:
        damaged_active = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE status='active'").fetchone()[0]
        damaged_tasks = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    check("破坏后活跃记忆为 0（模拟误操作）", damaged_active == 0, f"active={damaged_active}")

    res = snapshots.restore_snapshot(snap["snapshot_id"], note="测试恢复")
    check("恢复成功", res.get("ok"), str(res.get("message"))[:70])
    with db.connect() as conn:
        after_active = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE status='active'").fetchone()[0]
        after_tasks = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        after_total = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    check("活跃记忆恢复了", after_active == before_rows, f"active={after_active} 期望={before_rows}")
    check("恢复后的总行数与快照一致", after_total == before_rows, f"total={after_total}")
    check("破坏性任务也被回滚掉了", after_tasks == 0, f"tasks={after_tasks}（快照时是 0）")
    check("恢复前自动留了一张 pre-restore 快照", res.get("pre_restore_snapshot") is not None,
          f"pre_restore_snapshot={res.get('pre_restore_snapshot')}")
    check("恢复了哪些表写在返回值里", "memories" in (res.get("restored_tables") or {}),
          json.dumps(res.get("restored_tables") or {}, ensure_ascii=False)[:90])


def test_corrupt_payload_refused():
    print("\n=== 损坏的快照必须被拒绝 ===")
    snap = snapshots.create_snapshot(label="to-be-corrupted")
    path = Path(snap["path"])
    original = path.read_bytes()
    # Flip the payload so the sha256 no longer matches.
    path.write_bytes(original.replace(b"preference", b"PREFERENCE", 1))
    res = snapshots.restore_snapshot(snap["snapshot_id"])
    check("sha256 不符时拒绝恢复", not res.get("ok"), str(res.get("message"))[:60])
    check("拒绝原因提到损坏", "损坏" in str(res.get("message")), str(res.get("message"))[:60])

    path.unlink()
    res2 = snapshots.restore_snapshot(snap["snapshot_id"])
    check("文件不存在时拒绝恢复", not res2.get("ok"), str(res2.get("message"))[:60])
    path.write_bytes(original)  # leave the tree tidy for later checks


def test_sequence_not_reused():
    print("\n=== 恢复后新插入的 id 不与旧 id 冲突 ===")
    snap = snapshots.create_snapshot(label="seq-check")
    with db.connect() as conn:
        max_before = conn.execute("SELECT MAX(id) FROM memories").fetchone()[0]
    with db.transaction() as conn:
        conn.execute("DELETE FROM memories WHERE id = ?", (max_before,))
    snapshots.restore_snapshot(snap["snapshot_id"])
    created = mcp.save_memory(content="恢复后新建的记忆，id 不能重复", category="general")
    new_id = created.get("id")
    check("新插入拿到了更大的 id", isinstance(new_id, int) and new_id > max_before,
          f"new_id={new_id} max_before={max_before}")
    with db.connect() as conn:
        dup = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE id=?", (new_id,)).fetchone()[0]
    check("库里没有 id 冲突", dup == 1, f"id={new_id} 出现 {dup} 次")


def test_failed_restore_rolls_back():
    """The whole point of a rollback is that a failed one leaves you where you
    were. A half-restored library is worse than no restore at all."""
    print("\n=== 恢复中途失败必须整库回滚 ===")
    snap = snapshots.create_snapshot(label="will-be-broken")
    path = Path(snap["path"])
    payload = json.loads(path.read_text(encoding="utf-8"))
    # A NOT NULL column with None: SQLite rejects the insert mid-transaction.
    payload["tables"]["memories"][0]["content"] = None
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    path.write_bytes(blob)
    import hashlib
    new_digest = hashlib.sha256(blob).hexdigest()
    with db.transaction() as conn:
        conn.execute("UPDATE db_snapshots SET payload_sha256=?, payload_bytes=? WHERE id=?",
                     (new_digest, len(blob), snap["snapshot_id"]))

    with db.connect() as conn:
        live_before = conn.execute(
            "SELECT id, content, status FROM memories ORDER BY id").fetchall()
        count_before = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]

    res = snapshots.restore_snapshot(snap["snapshot_id"])
    check("内容违反约束的恢复被拒绝", not res.get("ok"), str(res.get("message"))[:70])
    check("失败信息说明事务已回滚", "回滚事务" in str(res.get("message")),
          str(res.get("message"))[:70])
    with db.connect() as conn:
        live_after = conn.execute(
            "SELECT id, content, status FROM memories ORDER BY id").fetchall()
        count_after = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    check("失败后记忆数量没变", count_after == count_before,
          f"{count_before} → {count_after}")
    check("失败后记忆内容逐行不变",
          [tuple(r) for r in live_before] == [tuple(r) for r in live_after],
          f"{len(live_before)} 行逐行比对")


def test_vector_check_reported():
    print("\n=== 恢复后向量一致性被如实报告 ===")
    res = snapshots.check_vector_consistency_after_restore()
    check("一致性检查返回成功", res.get("ok"), f"needs_reindex={res.get('needs_reindex')}")
    check("明确给出要不要重建", "needs_reindex" in res, str(res.get("missing_vectors")) + "/"
          + str(res.get("stale_vectors")))
    check("需要重建时给出可操作提示", (not res.get("needs_reindex")) or bool(res.get("hint")),
          str(res.get("hint"))[:60])


def test_prune():
    print("\n=== 快照清理 ===")
    for i in range(3):
        snapshots.create_snapshot(label=f"prune-{i}")
    listed = snapshots.list_snapshots(limit=100)
    total_before = listed.get("count")
    pruned = snapshots.prune_snapshots(keep=3)
    kept = snapshots.list_snapshots(limit=100).get("count")
    check("清理后只剩 keep 个", kept == 3, f"{total_before} → {kept}")
    check("清理确实删了东西", len(pruned.get("removed") or []) > 0,
          f"removed={len(pruned.get('removed') or [])}")


def test_tool_surface():
    print("\n=== MCP 工具面 ===")
    for name in ("create_db_snapshot", "list_db_snapshots", "restore_db_snapshot",
                 "check_vector_consistency", "prune_db_snapshots"):
        check(f"工具 {name} 存在且可调用", callable(getattr(mcp, name, None)))
    r = mcp.list_db_snapshots(limit=5)
    check("list_db_snapshots 可用", r.get("ok"), f"count={r.get('count')}")


def run():
    db.init_db()
    test_roundtrip()
    test_corrupt_payload_refused()
    test_sequence_not_reused()
    test_failed_restore_rolls_back()
    test_vector_check_reported()
    test_prune()
    test_tool_surface()
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n=== {len(RESULTS) - len(failed)}/{len(RESULTS)} 通过 ===")
    return 1 if failed else 0


if __name__ == "__main__":
    code = run()
    import shutil
    shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(code)
