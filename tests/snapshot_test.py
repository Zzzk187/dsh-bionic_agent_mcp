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


# ---------------------------------------------------------------------------
# S0 / Batch 1 — snapshot safety regressions (P0-1, P0-2, P2-7, P2-5).
# Each of these uses its OWN isolated temp DB via `_fresh_env()`, so the legacy
# tests above (which share one library and rely on insertion order) stay intact.
# ---------------------------------------------------------------------------

def _fresh_env():
    """Point db + snapshots at a brand-new isolated temp DB and initialise it."""
    import shutil  # noqa: F401  (also used in finally below)
    tmp = tempfile.mkdtemp(prefix="bionic-snap-fk-")
    db.DB_PATH = Path(tmp) / "agent.db"
    snapshots.SNAPSHOT_DIR = Path(tmp) / "snaps"
    os.environ["BIONIC_DB_PATH"] = str(db.DB_PATH)
    os.environ["BIONIC_SNAPSHOT_DIR"] = str(snapshots.SNAPSHOT_DIR)
    db.init_db()
    return tmp


def _seed_fk_shape(conn):
    """A fully FK-consistent multi-table fixture with explicit ids.

    Exercises every FK chain the old `reversed(sorted(live))` delete order broke:
    tasks->snapshots, governance_runs->governance_actions,
    memories->{links,feedback,conflicts,actions,consolidation,embeddings},
    knowledge_docs->chunks->embeddings. Parents are inserted before children so
    this is insertable even with foreign_keys=ON (no deferral needed here).
    """
    conn.execute("INSERT INTO memories (id,content,category,status) VALUES (1,'m1','general','active')")
    conn.execute("INSERT INTO memories (id,content,category,status) VALUES (2,'m2','general','active')")
    conn.execute("INSERT INTO memories (id,content,category,status) VALUES (3,'m3','general','archived')")
    conn.execute("INSERT INTO tasks (id,title,goal) VALUES (1,'t1','g1')")
    conn.execute("INSERT INTO tasks (id,title,goal) VALUES (2,'t2','g2')")
    conn.execute("INSERT INTO snapshots (id,task_id,stage,summary,state_json) VALUES (1,1,1,'s1','{}')")
    conn.execute("INSERT INTO snapshots (id,task_id,stage,summary,state_json) VALUES (2,1,2,'s2','{\"x\":1}')")
    conn.execute("INSERT INTO events (id,task_id,event_type,detail) VALUES (1,1,'created','e1')")
    conn.execute("INSERT INTO memory_links (id,source_memory_id,target_memory_id,relation) VALUES (1,1,2,'related')")
    conn.execute("INSERT INTO memory_embeddings (memory_id,model_name,content_hash,dimension,vector_json) "
                 "VALUES (1,'m','h',3,'[0,0,0]')")
    conn.execute("INSERT INTO governance_runs (id,mode,status) VALUES (1,'dry_run','done')")
    conn.execute("INSERT INTO governance_actions (id,run_id,action_type,memory_id,target_memory_id) "
                 "VALUES (1,1,'protect',1,NULL)")
    conn.execute("INSERT INTO memory_conflicts (id,run_id,memory_id,candidate_memory_id,conflict_type) "
                 "VALUES (1,1,1,2,'duplicate')")
    conn.execute("INSERT INTO memory_feedback (memory_id,retrieved_count) VALUES (1,5)")
    conn.execute("INSERT INTO consolidation_candidates (id,kind,target_memory_id,title) "
                 "VALUES (1,'merge_duplicate',1,'c1')")
    conn.execute("INSERT INTO knowledge_docs (id,path,rel_path,root,content,content_hash) "
                 "VALUES (1,'C:/old/knowledge/a.md','knowledge/a.md','knowledge','doc A','ha')")
    conn.execute("INSERT INTO knowledge_chunks (id,doc_id,ord,content,content_hash) "
                 "VALUES (1,1,0,'chunk A','hc')")
    conn.execute("INSERT INTO knowledge_embeddings (chunk_id,model_name,content_hash,dimension,vector_json) "
                 "VALUES (1,'m','he',3,'[0,0,0]')")


def _table_counts(conn):
    return {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            for t in snapshots._data_tables(conn)}


def _tamper_payload(snap, mutate):
    """Rewrite a snapshot payload, keeping sha256 + byte count consistent."""
    import hashlib
    path = Path(snap["path"])
    payload = json.loads(path.read_text(encoding="utf-8"))
    mutate(payload)
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    path.write_bytes(blob)
    digest = hashlib.sha256(blob).hexdigest()
    with db.transaction() as conn:
        conn.execute("UPDATE db_snapshots SET payload_sha256=?, payload_bytes=? WHERE id=?",
                     (digest, len(blob), snap["snapshot_id"]))
    return payload


def test_fk_full_roundtrip():
    print("\n=== [S0/P0-1] 完整 FK 形状下恢复成功且 integrity/FK/行数全对 ===")
    tmp = _fresh_env()
    try:
        with db.transaction() as conn:
            _seed_fk_shape(conn)
        with db.connect() as conn:
            before = _table_counts(conn)
        snap = snapshots.create_snapshot(label="fk-full")
        check("FK-full 快照创建成功", snap.get("ok"), f"rows={snap.get('total_rows')}")
        # 模拟破坏：改写内容 + 加一条 task（不改变表集合）
        with db.transaction() as conn:
            conn.execute("UPDATE memories SET content='DAMAGED'")
            conn.execute("INSERT INTO tasks (title,goal) VALUES ('破坏','x')")
        res = snapshots.restore_snapshot(snap["snapshot_id"])
        check("P0-1: 真实 FK 形状下恢复成功（旧代码必因 FOREIGN KEY 失败）", res.get("ok"),
              str(res.get("message"))[:100])
        with db.connect() as conn:
            after = _table_counts(conn)
            integ = conn.execute("PRAGMA integrity_check").fetchone()[0]
            fk = conn.execute("PRAGMA foreign_key_check").fetchall()
        check("恢复后每表行数与快照一致", after == before,
              f"{sum(before.values())} → {sum(after.values())}")
        check("PRAGMA integrity_check = ok", integ == "ok", integ)
        check("PRAGMA foreign_key_check = 0 行", len(fk) == 0, f"{len(fk)} 违规")
        check("restored_tables 行数合计与快照一致",
              sum((res.get("restored_tables") or {}).values()) == sum(before.values()),
              f"restored={sum((res.get('restored_tables') or {}).values())}")
    finally:
        import shutil; shutil.rmtree(tmp, ignore_errors=True)


def test_schema_table_mismatch_refused():
    print("\n=== [S0/P0-2] 表集合差异必须 preflight 拒绝且不改库 ===")
    tmp = _fresh_env()
    try:
        with db.transaction() as conn:
            _seed_fk_shape(conn)
        with db.connect() as conn:
            before = _table_counts(conn)
        snap = snapshots.create_snapshot(label="shape")
        # 快照里凭空多一张当前库没有的表
        def add_fake(p):
            p["tables"]["s0_fake_table"] = [{"id": 1, "x": 1}]
            p["columns"]["s0_fake_table"] = ["id", "x"]
        _tamper_payload(snap, add_fake)
        res = snapshots.restore_snapshot(snap["snapshot_id"])
        check("P0-2: 未知表被拒绝", (not res.get("ok")) and res.get("stage") == "preflight",
              str(res.get("message"))[:80])
        check("P0-2: 拒绝信息明确提到表", "表" in str(res.get("message")), "")
        with db.connect() as conn:
            after = _table_counts(conn)
        check("拒绝后库零改动", after == before, "")
        # 再测「当前库有、快照没有」：删掉快照里一个表
        snap2 = snapshots.create_snapshot(label="shape2")
        def drop_table(p):
            p["tables"].pop("events", None)
            p["columns"].pop("events", None)
        _tamper_payload(snap2, drop_table)
        res2 = snapshots.restore_snapshot(snap2["snapshot_id"])
        check("P0-2: 快照缺表也被拒绝（不再静默跳过）",
              (not res2.get("ok")) and res2.get("stage") == "preflight",
              str(res2.get("message"))[:80])
    finally:
        import shutil; shutil.rmtree(tmp, ignore_errors=True)


def test_unknown_rule_and_schema_refused():
    print("\n=== [S0/P0-2] 未知规则版本 / 过高 schema 版本必须拒绝 ===")
    tmp = _fresh_env()
    try:
        with db.transaction() as conn:
            _seed_fk_shape(conn)
        snap1 = snapshots.create_snapshot(label="rule")
        _tamper_payload(snap1, lambda p: p.update({"rule_version": "future-format-v9"}))
        r1 = snapshots.restore_snapshot(snap1["snapshot_id"])
        check("P0-2: 未知 rule_version 被拒",
              (not r1.get("ok")) and "规则版本" in str(r1.get("message")),
              str(r1.get("message"))[:70])
        snap2 = snapshots.create_snapshot(label="schema")
        _tamper_payload(snap2, lambda p: p.update({"schema_version": 999}))
        r2 = snapshots.restore_snapshot(snap2["snapshot_id"])
        check("P0-2: 过高 schema_version 被拒",
              (not r2.get("ok")) and "schema_version" in str(r2.get("message")),
              str(r2.get("message"))[:70])
    finally:
        import shutil; shutil.rmtree(tmp, ignore_errors=True)


def test_protective_snapshot_failure_aborts():
    print("\n=== [S0/P2-7] 保护快照失败必须立即中止、零 DELETE ===")
    tmp = _fresh_env()
    try:
        with db.transaction() as conn:
            _seed_fk_shape(conn)
        snap = snapshots.create_snapshot(label="target")
        with db.connect() as conn:
            before = _table_counts(conn)
            meta_before = conn.execute("SELECT COUNT(*) FROM db_snapshots").fetchone()[0]
        orig = snapshots.create_snapshot
        # 让 restore 内部那次「保护性快照」返回失败
        snapshots.create_snapshot = lambda *a, **k: {"ok": False, "message": "模拟磁盘满"}
        try:
            res = snapshots.restore_snapshot(snap["snapshot_id"])
        finally:
            snapshots.create_snapshot = orig
        check("P2-7: 保护快照失败返回中止",
              (not res.get("ok")) and res.get("stage") == "protective_snapshot",
              str(res.get("message"))[:80])
        check("P2-7: 明确带上失败原因", "模拟磁盘满" in str(res.get("message")), "")
        with db.connect() as conn:
            after = _table_counts(conn)
            meta_after = conn.execute("SELECT COUNT(*) FROM db_snapshots").fetchone()[0]
        check("保护失败后库零改动", after == before, "")
        check("保护失败后没有残留 pre-restore 元数据行", meta_after == meta_before,
              f"{meta_before} → {meta_after}")
    finally:
        import shutil; shutil.rmtree(tmp, ignore_errors=True)


def test_pre_restore_orphan_cleanup():
    print("\n=== [S0/P2-7] 恢复失败时清理 pre-restore 孤儿 + restore_count 不变 ===")
    tmp = _fresh_env()
    try:
        with db.transaction() as conn:
            _seed_fk_shape(conn)
        snap = snapshots.create_snapshot(label="will-fail-in-txn")
        with db.connect() as conn:
            meta_before = conn.execute("SELECT COUNT(*) FROM db_snapshots").fetchone()[0]
            rc_before = conn.execute(
                "SELECT restore_count FROM db_snapshots WHERE id=?",
                (snap["snapshot_id"],)).fetchone()[0]
        files_before = len(list(Path(snapshots.SNAPSHOT_DIR).glob("snap-*.json")))
        # 破坏 payload：把一条 memory 的 content 设成 None（NOT NULL 违反），
        # 但保持表集合/列/rule/schema 全合法 → preflight 会通过，事务内 INSERT 才失败。
        def break_row(p):
            p["tables"]["memories"][0]["content"] = None
        _tamper_payload(snap, break_row)
        res = snapshots.restore_snapshot(snap["snapshot_id"])
        check("恢复因 NOT NULL 违反而失败",
              (not res.get("ok")) and "回滚事务" in str(res.get("message")),
              str(res.get("message"))[:90])
        check("清理没有报错（如实报告，不吞异常）",
              not res.get("cleanup_errors"), str(res.get("cleanup_errors"))[:60])
        with db.connect() as conn:
            meta_after = conn.execute("SELECT COUNT(*) FROM db_snapshots").fetchone()[0]
            rc_after = conn.execute(
                "SELECT restore_count FROM db_snapshots WHERE id=?",
                (snap["snapshot_id"],)).fetchone()[0]
            integ = conn.execute("PRAGMA integrity_check").fetchone()[0]
        files_after = len(list(Path(snapshots.SNAPSHOT_DIR).glob("snap-*.json")))
        check("pre-restore 元数据行被清理（回到恢复前计数）", meta_after == meta_before,
              f"{meta_before} → {meta_after}")
        check("pre-restore payload 文件被清理（不残留孤儿）", files_after == files_before,
              f"{files_before} → {files_after}")
        check("原快照 restore_count 未被错误修改", rc_after == rc_before,
              f"{rc_before} → {rc_after}")
        check("失败后库仍完整", integ == "ok", integ)
    finally:
        import shutil; shutil.rmtree(tmp, ignore_errors=True)


def test_consistent_read_view():
    print("\n=== [S0/P2-5] create_snapshot 用一致读视图（非 autocommit） ===")
    tmp = _fresh_env()
    try:
        with db.transaction() as conn:
            conn.execute("INSERT INTO memories (content) VALUES ('x')")
        with db.read_transaction() as conn:
            check("read_transaction 内确实在事务里（区别于 connect 的 autocommit）",
                  bool(conn.in_transaction), "")
            n1 = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            n2 = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        check("一致读视图能读到稳定数据", n1 == 1 and n2 == 0, f"memories={n1} tasks={n2}")
        snap = snapshots.create_snapshot(label="consistent")
        payload = json.loads(Path(snap["path"]).read_text(encoding="utf-8"))
        check("payload 带 schema_version", payload.get("schema_version") == snapshots.SCHEMA_VERSION,
              str(payload.get("schema_version")))
        check("payload 带 per-table columns（空表也有列清单）",
              isinstance(payload.get("columns"), dict) and "memories" in payload["columns"], "")
    finally:
        import shutil; shutil.rmtree(tmp, ignore_errors=True)


def run():
    db.init_db()
    test_roundtrip()
    test_corrupt_payload_refused()
    test_sequence_not_reused()
    test_failed_restore_rolls_back()
    test_vector_check_reported()
    test_prune()
    test_tool_surface()
    # --- S0 Batch 1 additions (independent isolated DBs) ---
    test_fk_full_roundtrip()
    test_schema_table_mismatch_refused()
    test_unknown_rule_and_schema_refused()
    test_protective_snapshot_failure_aborts()
    test_pre_restore_orphan_cleanup()
    test_consistent_read_view()
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n=== {len(RESULTS) - len(failed)}/{len(RESULTS)} 通过 ===")
    if failed:
        for name, _ok, detail in failed:
            print(f"    [FAIL] {name} — {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    code = run()
    import shutil
    shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(code)
