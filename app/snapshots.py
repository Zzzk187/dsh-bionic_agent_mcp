"""Whole-database snapshots, restore, and post-restore vector consistency.

Why this exists
---------------
`governance_actions` records the *field values* an action changed, which is
enough to reverse a single well-understood action. It is NOT enough to recover
from an operation whose blast radius we got wrong — a batch archive, a bad
dedupe run, a maintenance pass that touched 40 memories at once. For those you
want the state of the whole library *before* the run, and you want to be able
to prove afterwards that restoring the rows did not leave the vector index
pointing at text that no longer exists.

Design decisions
----------------
1. **Logical dump, not a file copy.** Copying `agent.db` while another process
   holds a write lock gives you a torn file; dumping rows inside one *read
   transaction* (`db.read_transaction()`) pins a single WAL snapshot, so every
   table in the payload describes the same instant. `db.connect()` is
   autocommit (each SELECT its own implicit transaction) and must NOT be used
   for a multi-table dump.
2. **Payload on disk, metadata in the DB.** Vectors are large; keeping them out
   of SQLite rows keeps the operational tables small. The DB row carries the
   sha256 and byte size so a corrupted/truncated payload is detected on restore
   rather than silently loading half a library. The payload also records the
   per-table column list and a schema version so a restore can prove
   compatibility *before* deleting anything.
3. **Restore is destructive and all-or-nothing.** It runs in one transaction:
   either every table is back to the snapshot or nothing changed. A partial
   restore is worse than no restore.
4. **Restore does not fix vectors silently.** After the rows are back, the
   embedding table may reference content hashes that no longer match. We report
   that honestly (`vector_check`) instead of re-embedding behind the user's
   back, because re-embedding is slow and needs the model.
5. **Foreign keys are deferred, not disabled.** Deleting/inserting in any table
   order transiently violates FKs; `PRAGMA defer_foreign_keys=ON` postpones
   enforcement to COMMIT while keeping `foreign_keys=ON`. Before committing we
   run `PRAGMA foreign_key_check` and refuse to commit on ANY violation, so a
   restore can never leave a database with broken references.
6. **Pre-flight shape validation.** Table sets, rule version, schema version and
   per-table columns are checked BEFORE the first DELETE. An unknown/missing
   table, unknown rule version, unknown higher schema version, or incompatible
   column set is rejected with no changes to the database.
7. **A reversible restore needs a real safety net.** A protective snapshot of
   the current state is taken first; if that fails the restore is aborted. If
   the restore transaction fails, the protective snapshot's metadata row and
   payload file are cleaned up (and any cleanup problem is reported, not
   swallowed), and the original snapshot's `restore_count` is left untouched.
"""

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app import db

# Internal SQLite bookkeeping tables are never snapshotted or restored.
_SKIP_TABLES = {"sqlite_sequence", "sqlite_stat1", "db_snapshots"}

SNAPSHOT_DIR = Path(os.environ.get("BIONIC_SNAPSHOT_DIR")
                    or (Path(db.DB_PATH).resolve().parent / "snapshots"))

SNAPSHOT_RULE_VERSION = "phase6-snapshot-v1"
# Additive schema signature version. Bumped only when the restore format itself
# changes; a snapshot produced by a *newer* program must not be restored here.
SCHEMA_VERSION = 1
# Only these rule versions are understood. Anything else is rejected rather than
# guessed at (a future format could store tables/columns differently).
KNOWN_RULE_VERSIONS = {SNAPSHOT_RULE_VERSION}

# Rows above this count in one table make a single `INSERT` statement too large
# for SQLite's variable limit; restore inserts in chunks instead.
_INSERT_CHUNK = 200


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _data_tables(conn) -> list[str]:
    """Every real (non-internal) table currently in the database."""
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return sorted(r[0] for r in rows if r[0] not in _SKIP_TABLES)


def _ensure_meta_table(conn) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS db_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        label TEXT NOT NULL DEFAULT '',
        kind TEXT NOT NULL DEFAULT 'manual',
        rule_version TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        payload_path TEXT NOT NULL DEFAULT '',
        payload_sha256 TEXT NOT NULL DEFAULT '',
        payload_bytes INTEGER NOT NULL DEFAULT 0,
        table_counts_json TEXT NOT NULL DEFAULT '{}',
        total_rows INTEGER NOT NULL DEFAULT 0,
        actor TEXT NOT NULL DEFAULT '',
        note TEXT NOT NULL DEFAULT '',
        restored_at TEXT,
        restore_count INTEGER NOT NULL DEFAULT 0
    )""")


def create_snapshot(label: str = "", kind: str = "manual", actor: str = "user",
                    note: str = "") -> dict:
    """Dump every table into a JSON payload and record it. Returns metadata.

    Cheap to call before a risky operation; costs roughly the size of the
    library (vectors dominate). Callers that want a safety net should take one
    right before `run_memory_maintenance(apply)`, not per action.

    The multi-table dump runs inside `db.read_transaction()` -- one WAL
    snapshot -- so all tables describe the same instant. The payload also
    stores each table's column list and a schema version, which lets
    `restore_snapshot` prove compatibility *before* deleting anything.
    """
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    created = _now()
    path = None
    tmp = None
    try:
        # DDL must not run inside the read transaction we roll back below.
        with db.connect() as meta_conn:
            _ensure_meta_table(meta_conn)
        with db.read_transaction() as conn:
            tables = _data_tables(conn)
            payload = {"created_at": created, "rule_version": SNAPSHOT_RULE_VERSION,
                       "schema_version": SCHEMA_VERSION, "tables": {}, "columns": {}}
            counts = {}
            for table in tables:
                cur = conn.execute(f'SELECT * FROM "{table}"')
                cols = [d[0] for d in cur.description]
                rows = [dict(r) for r in cur.fetchall()]
                payload["tables"][table] = rows
                payload["columns"][table] = cols
                counts[table] = len(rows)
        blob = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        digest = hashlib.sha256(blob).hexdigest()
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        path = SNAPSHOT_DIR / f"snap-{stamp}-{digest[:8]}.json"
        # Write then rename: a crash mid-write must not leave a half file that
        # later looks like a valid snapshot.
        tmp = path.with_suffix(".json.part")
        tmp.write_bytes(blob)
        tmp.replace(path)
        total = sum(counts.values())
        with db.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO db_snapshots (label,kind,rule_version,created_at,payload_path,"
                "payload_sha256,payload_bytes,table_counts_json,total_rows,actor,note) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (label or f"snapshot-{stamp}", kind, SNAPSHOT_RULE_VERSION, created,
                 str(path), digest, len(blob),
                 json.dumps(counts, ensure_ascii=False, sort_keys=True), total,
                 actor, note))
            snap_id = cur.lastrowid
        tmp = None  # fully renamed; nothing partial left
        return {"ok": True, "snapshot_id": snap_id, "created_at": created,
                "path": str(path), "sha256": digest, "bytes": len(blob),
                "schema_version": SCHEMA_VERSION,
                "table_counts": counts, "total_rows": total,
                "label": label or f"snapshot-{stamp}"}
    except Exception as exc:
        # Do not leave a payload behind without its metadata row (P2-1/P2-7):
        # an orphan file is invisible to list/restore and only wastes space.
        cleanup_errors = []
        for leftover in (tmp, path):
            if leftover is None:
                continue
            try:
                Path(leftover).unlink(missing_ok=True)
            except OSError as cexc:
                cleanup_errors.append(f"{leftover}: {cexc}")
        out = {"ok": False, "message": f"{type(exc).__name__}: {exc}"}
        if cleanup_errors:
            out["cleanup_errors"] = cleanup_errors
        return out


def list_snapshots(limit: int = 20) -> dict:
    limit = max(1, min(int(limit), 200))
    try:
        with db.connect() as conn:
            _ensure_meta_table(conn)
            rows = [db.rowdict(r) for r in conn.execute(
                "SELECT * FROM db_snapshots ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]
        for r in rows:
            try:
                r["table_counts"] = json.loads(r.pop("table_counts_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                r["table_counts"] = {}
            r["exists_on_disk"] = Path(r.get("payload_path") or "").is_file()
        return {"ok": True, "snapshots": rows, "count": len(rows)}
    except Exception as exc:
        return {"ok": False, "message": f"{type(exc).__name__}: {exc}", "snapshots": []}


def _load_payload(row: dict) -> tuple[dict | None, str]:
    path = Path(row.get("payload_path") or "")
    if not path.is_file():
        return None, f"快照文件不存在：{path}"
    blob = path.read_bytes()
    digest = hashlib.sha256(blob).hexdigest()
    if digest != (row.get("payload_sha256") or ""):
        return None, ("快照文件已损坏（sha256 不符）："
                      f"记录 {str(row.get('payload_sha256'))[:12]}… 实际 {digest[:12]}…")
    try:
        return json.loads(blob.decode("utf-8")), ""
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, f"快照文件无法解析：{exc}"


def _read_live_shape(conn) -> tuple[list[str], dict]:
    """Current data tables and their column lists, from ONE consistent read view."""
    live = _data_tables(conn)
    cols = {}
    for table in live:
        cols[table] = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]
    return live, cols


def _validate_payload(payload, live_tables, live_cols) -> tuple:
    """Pure read-only pre-flight: prove compatibility BEFORE the first DELETE.

    Rejects (all with zero writes to the DB):
      * unknown rule version / unknown or higher schema version (P0-2);
      * tables in the snapshot missing from the live DB -- the old
        `if table not in live: continue` silently dropped these (P0-2);
      * tables in the live DB absent from the snapshot -- must not be silently
        skipped nor left as a half-restored mix (P0-2);
      * snapshot columns the live schema no longer has (would fail or lose data).
    Additive columns added since the snapshot are allowed (forward compatible)
    but reported. Legacy payloads (no `columns`/`schema_version`) infer columns
    from the first row and are still restorable. Returns (report, err); a non-
    empty `err` means refuse.
    """
    if not isinstance(payload, dict):
        return None, "快照 payload 不是对象"
    rule = payload.get("rule_version")
    if rule not in KNOWN_RULE_VERSIONS:
        return None, (f"未知快照规则版本 {rule!r}，拒绝恢复"
                      f"（已知：{sorted(KNOWN_RULE_VERSIONS)}）")
    raw_schema = payload.get("schema_version", 0)   # legacy payloads have none
    try:
        schema = int(raw_schema)
    except (TypeError, ValueError):
        return None, f"无效 schema_version {raw_schema!r}，拒绝恢复"
    if schema < 0 or schema > SCHEMA_VERSION:
        return None, (f"未知/过高 schema_version={schema}"
                      f"（本程序支持 0–{SCHEMA_VERSION}），拒绝恢复；请升级程序后再试")
    tables = payload.get("tables")
    if not isinstance(tables, dict) or not tables:
        return None, "快照里没有表数据"
    columns_meta = payload.get("columns") or {}
    snap_set = set(tables)
    live_set = set(live_tables)
    missing = sorted(snap_set - live_set)
    unknown = sorted(live_set - snap_set)
    if missing:
        return None, (f"快照包含当前库不存在的表 {missing}，拒绝恢复"
                      "（旧实现会静默丢整表）")
    if unknown:
        return None, (f"当前库存在快照未覆盖的表 {unknown}，拒绝恢复，"
                      "以免静默跳过或残留半套数据")
    legacy = not isinstance(columns_meta, dict) or not columns_meta
    columns_added = {}
    for table in sorted(snap_set):
        rows = tables[table]
        if not isinstance(rows, list):
            return None, f"快照表 {table} 的数据不是行列表"
        meta_cols = columns_meta.get(table) if isinstance(columns_meta, dict) else None
        if isinstance(meta_cols, list) and meta_cols:
            snap_cols = list(meta_cols)
        elif rows and isinstance(rows[0], dict):
            snap_cols = list(rows[0].keys())      # legacy: infer from first row
        else:
            snap_cols = None                      # legacy + empty table: unknown
        if snap_cols is None:
            continue
        live_set_cols = set(live_cols.get(table, []))
        extra = [c for c in snap_cols if c not in live_set_cols]
        if extra:
            return None, (f"表 {table} 的快照列 {extra} 在当前库中已不存在，"
                          "拒绝恢复（否则整单 INSERT 失败或丢列）")
        added = [c for c in live_cols.get(table, []) if c not in snap_cols]
        if added:
            columns_added[table] = added
    return {
        "rule_version": rule,
        "schema_version": schema,
        "legacy_columns": legacy,
        "snapshot_tables": len(snap_set),
        "live_tables": len(live_set),
        "columns_added_since_snapshot": columns_added,
    }, ""


def _delete_snapshot_artifact(snap_id) -> list:
    """Remove a snapshot's metadata row AND payload file; report any failure.

    Used to clean up the protective `pre-restore-*` snapshot when the restore
    transaction fails. Errors are returned to the caller and surfaced, never
    swallowed. Order: delete the row first (an orphan file is harmless, an
    orphan row would point at a missing file).
    """
    errors = []
    path = None
    try:
        with db.connect() as conn:
            r = conn.execute(
                "SELECT payload_path FROM db_snapshots WHERE id=?",
                (int(snap_id),)).fetchone()
            if r:
                path = r[0]
    except Exception as exc:
        errors.append(f"读取快照 {snap_id} 元数据失败：{type(exc).__name__}: {exc}")
    try:
        with db.transaction() as conn:
            conn.execute("DELETE FROM db_snapshots WHERE id=?", (int(snap_id),))
    except Exception as exc:
        errors.append(f"删除快照 {snap_id} 元数据失败：{type(exc).__name__}: {exc}")
    if path:
        try:
            Path(path).unlink(missing_ok=True)
        except OSError as exc:
            errors.append(f"删除快照 {snap_id} payload 失败：{exc}")
    return errors


def _apply_payload(conn, payload, snapshot_id, actor, note) -> dict:
    """The destructive part, INSIDE `db.transaction()` (single atomic unit).

    `defer_foreign_keys=ON` postpones FK enforcement to COMMIT so table order
    no longer matters; `foreign_keys` itself stays ON (we never disable it).
    `PRAGMA foreign_key_check` runs before we return -- any violation raises,
    which makes `db.transaction()` roll the whole thing back. Nothing is
    committed from here: the context manager commits only after a clean return.
    """
    tables = payload["tables"]
    conn.execute("PRAGMA defer_foreign_keys = ON")
    try:
        active = int(conn.execute("PRAGMA defer_foreign_keys").fetchone()[0])
    except (TypeError, ValueError):
        active = 0
    if active != 1:
        raise RuntimeError("无法启用 defer_foreign_keys，拒绝在不安全状态下恢复")
    live = _data_tables(conn)
    for table in live:
        conn.execute(f'DELETE FROM "{table}"')
    restored = {}
    for table, rows in tables.items():
        if not rows:
            restored[table] = 0
            continue
        cols = list(rows[0].keys())
        collist = ",".join(f'"{c}"' for c in cols)
        ph = ",".join("?" for _ in cols)
        sql = f'INSERT INTO "{table}" ({collist}) VALUES ({ph})'
        for i in range(0, len(rows), _INSERT_CHUNK):
            chunk = rows[i:i + _INSERT_CHUNK]
            conn.executemany(sql, [tuple(r.get(c) for c in cols) for r in chunk])
        restored[table] = len(rows)
    # AUTOINCREMENT counters live in sqlite_sequence, which is never restored.
    # Leaving them ahead of the data is harmless; behind would re-issue ids.
    for table in restored:
        try:
            r = conn.execute(f'SELECT MAX(rowid) FROM "{table}"').fetchone()
            top = r[0] if r and r[0] is not None else 0
            conn.execute(
                "INSERT INTO sqlite_sequence(name,seq) VALUES (?,?) "
                "ON CONFLICT(name) DO UPDATE SET seq=excluded.seq",
                (table, top))
        except sqlite3.Error:
            pass  # table without AUTOINCREMENT has no sequence entry
    # Record the successful restore in the metadata table. `db_snapshots` is
    # excluded from _data_tables (never DELETEd), so this UPDATE survives and is
    # rolled back together with the restore if anything below fails -- which is
    # exactly the "restore_count must not change on failure" guarantee.
    # NOTE: we deliberately do NOT insert an `events` audit row here. A restore
    # must put every data table back to the snapshot *exactly*; an extra row
    # would make `events` differ from the snapshot and break that contract.
    # Success is already fully recorded by restored_at/restore_count (and the
    # return value). Nothing in the codebase reads event_type='snapshot.restored'.
    conn.execute(
        "UPDATE db_snapshots SET restored_at=?, restore_count=restore_count+1 "
        "WHERE id=?", (_now(), int(snapshot_id)))
    # Explicit, honest FK gate before COMMIT. With defer_foreign_keys the FK
    # violations are only detected here (or at COMMIT); we refuse to commit any.
    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        sample = ", ".join(f"{v[0]}:rowid={v[1]}" for v in violations[:5])
        raise RuntimeError(
            f"PRAGMA foreign_key_check 发现 {len(violations)} 处外键违规"
            f"（{sample}），拒绝提交恢复")
    return restored


def restore_snapshot(snapshot_id: int, actor: str = "user", note: str = "") -> dict:
    """Put every table back to the snapshot's contents, all or nothing.

    Pipeline: read payload -> pre-flight shape validation (zero writes) ->
    protective snapshot (must succeed) -> one atomic restore transaction
    (re-validate, defer FKs, delete+insert, foreign_key_check gate) ->
    post-restore vector report. On any failure the destructive transaction is
    rolled back, the protective snapshot is cleaned up, and the original
    snapshot's `restore_count` is left untouched.
    """
    sid = int(snapshot_id)
    try:
        with db.connect() as conn:
            _ensure_meta_table(conn)
            row = db.rowdict(conn.execute(
                "SELECT * FROM db_snapshots WHERE id=?", (sid,)).fetchone())
        if not row:
            return {"ok": False, "message": f"快照 {snapshot_id} 不存在"}
        payload, err = _load_payload(row)
        if payload is None:
            return {"ok": False, "message": err, "snapshot_id": sid}

        # --- Pre-flight: prove compatibility with ZERO writes, in one read view.
        # Anything rejected here never creates a protective snapshot at all.
        with db.read_transaction() as conn:
            live_tables, live_cols = _read_live_shape(conn)
            # Informational: a DB that is ALREADY FK-dirty cannot be round-
            # tripped under foreign_keys=ON (COMMIT would reject). Surface that
            # up front instead of letting the user discover it at the gate.
            live_fk = len(conn.execute("PRAGMA foreign_key_check").fetchall())
        shape, err = _validate_payload(payload, live_tables, live_cols)
        if err:
            return {"ok": False, "message": err, "snapshot_id": sid,
                    "stage": "preflight", "live_fk_violations": live_fk}
        if shape is not None:
            shape["live_fk_violations"] = live_fk

        # --- Protective snapshot MUST succeed, else abort before any DELETE.
        pre = create_snapshot(label=f"pre-restore-{sid}", kind="auto",
                              actor=actor,
                              note=f"恢复快照 {sid} 之前自动留底")
        if not pre.get("ok"):
            return {"ok": False, "snapshot_id": sid,
                    "stage": "protective_snapshot",
                    "message": ("恢复前保护性快照创建失败，已中止恢复（库未改动）："
                                f"{pre.get('message', '未知原因')}"),
                    "pre_restore_snapshot": None}
        pre_id = pre.get("snapshot_id")

        # --- Atomic destructive restore. `db.transaction()` rolls back on any
        # exception; defer_foreign_keys + foreign_key_check live in _apply_payload.
        restored = {}
        try:
            with db.transaction() as conn:
                live2, cols2 = _read_live_shape(conn)
                _shape2, err2 = _validate_payload(payload, live2, cols2)
                if err2:
                    raise RuntimeError(f"事务内二次校验失败：{err2}")
                restored = _apply_payload(conn, payload, sid, actor, note)
        except Exception as exc:
            # Transaction already rolled back. Clean up the now-pointless
            # protective snapshot; report ANY cleanup problem honestly.
            cleanup_errors = _delete_snapshot_artifact(pre_id) if pre_id else []
            out = {"ok": False, "snapshot_id": sid,
                   "message": f"恢复失败，已回滚事务（库仍是恢复前的状态）：{exc}",
                   "pre_restore_snapshot": None, "shape": shape,
                   "cleanup_errors": cleanup_errors}
            if cleanup_errors:
                out["message"] += "；但保护性快照清理有问题，见 cleanup_errors"
            return out

        vector_check = check_vector_consistency_after_restore()
        return {"ok": True, "snapshot_id": sid,
                "restored_tables": restored, "total_rows": sum(restored.values()),
                "pre_restore_snapshot": pre_id, "shape": shape,
                "vector_check": vector_check,
                "message": "已恢复到该快照" + (
                    "；向量索引需要重建，见 vector_check"
                    if vector_check.get("needs_reindex") else "")}
    except Exception as exc:
        return {"ok": False, "message": f"{type(exc).__name__}: {exc}"}


def check_vector_consistency_after_restore() -> dict:
    """Report whether restored rows and their vectors still agree.

    Deliberately does NOT re-embed: that needs the model, takes time, and would
    turn a fast rollback into a long silent operation.
    """
    try:
        from app.governance_maintenance import get_vector_consistency
        base = get_vector_consistency()
    except Exception as exc:
        return {"ok": False, "needs_reindex": False,
                "message": f"无法检查向量一致性：{type(exc).__name__}: {exc}"}
    missing = int(base.get("active_without_vector") or 0)
    stale = int(base.get("active_stale_vector") or 0)
    needs = bool(missing or stale)
    hint = ""
    if needs:
        hint = ("运行 index_memories(force_rebuild=True) 重建向量索引；"
                "在此之前语义检索可能返回与内容不符的结果")
    out = dict(base)
    out.update({"ok": True, "needs_reindex": needs,
                "missing_vectors": missing, "stale_vectors": stale, "hint": hint})
    return out


def prune_snapshots(keep: int = 10) -> dict:
    """Delete the oldest snapshots (and their payloads) beyond `keep`."""
    keep = max(1, int(keep))
    removed = []
    try:
        with db.connect() as conn:
            _ensure_meta_table(conn)
            rows = conn.execute(
                "SELECT id,payload_path FROM db_snapshots ORDER BY id DESC").fetchall()
        stale = rows[keep:]
        with db.transaction() as conn:
            for snap_id, path in stale:
                conn.execute("DELETE FROM db_snapshots WHERE id=?", (int(snap_id),))
                removed.append(int(snap_id))
        cleanup_errors = []
        for _, path in stale:
            try:
                Path(path).unlink(missing_ok=True)
            except OSError as exc:
                # Do not swallow: an orphan payload file would be invisible to
                # list/restore yet keep consuming disk. Report it to the caller.
                cleanup_errors.append(f"{path}: {exc}")
        out = {"ok": True, "removed": removed, "kept": min(len(rows), keep)}
        if cleanup_errors:
            out["cleanup_errors"] = cleanup_errors
        return out
    except Exception as exc:
        return {"ok": False, "message": f"{type(exc).__name__}: {exc}", "removed": []}
