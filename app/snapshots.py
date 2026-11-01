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
   holds a write lock gives you a torn file; dumping rows inside a read
   transaction gives a consistent view and works under WAL.
2. **Payload on disk, metadata in the DB.** Vectors are large; keeping them out
   of SQLite rows keeps the operational tables small. The DB row carries the
   sha256 and byte size so a corrupted/truncated payload is detected on restore
   rather than silently loading half a library.
3. **Restore is destructive and all-or-nothing.** It runs in one transaction:
   either every table is back to the snapshot or nothing changed. A partial
   restore is worse than no restore.
4. **Restore does not fix vectors silently.** After the rows are back, the
   embedding table may reference content hashes that no longer match. We report
   that honestly (`vector_check`) instead of re-embedding behind the user's
   back, because re-embedding is slow and needs the model.
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
    """
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    created = _now()
    try:
        with db.connect() as conn:
            _ensure_meta_table(conn)
            tables = _data_tables(conn)
            payload = {"created_at": created, "rule_version": SNAPSHOT_RULE_VERSION,
                       "tables": {}}
            counts = {}
            for table in tables:
                rows = [dict(r) for r in conn.execute(f'SELECT * FROM "{table}"').fetchall()]
                payload["tables"][table] = rows
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
            _ensure_meta_table(conn)
            cur = conn.execute(
                "INSERT INTO db_snapshots (label,kind,rule_version,created_at,payload_path,"
                "payload_sha256,payload_bytes,table_counts_json,total_rows,actor,note) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (label or f"snapshot-{stamp}", kind, SNAPSHOT_RULE_VERSION, created,
                 str(path), digest, len(blob),
                 json.dumps(counts, ensure_ascii=False, sort_keys=True), total,
                 actor, note))
            snap_id = cur.lastrowid
        return {"ok": True, "snapshot_id": snap_id, "created_at": created,
                "path": str(path), "sha256": digest, "bytes": len(blob),
                "table_counts": counts, "total_rows": total,
                "label": label or f"snapshot-{stamp}"}
    except Exception as exc:
        return {"ok": False, "message": f"{type(exc).__name__}: {exc}"}


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


def restore_snapshot(snapshot_id: int, actor: str = "user", note: str = "") -> dict:
    """Put every table back to the snapshot's contents, all or nothing.

    Refuses to run on a corrupt/missing payload. Reports post-restore vector
    drift rather than silently re-embedding.
    """
    try:
        with db.connect() as conn:
            _ensure_meta_table(conn)
            row = db.rowdict(conn.execute(
                "SELECT * FROM db_snapshots WHERE id=?", (int(snapshot_id),)).fetchone())
        if not row:
            return {"ok": False, "message": f"快照 {snapshot_id} 不存在"}
        payload, err = _load_payload(row)
        if payload is None:
            return {"ok": False, "message": err, "snapshot_id": int(snapshot_id)}
        tables = payload.get("tables") or {}
        if not isinstance(tables, dict) or not tables:
            return {"ok": False, "message": "快照里没有表数据", "snapshot_id": int(snapshot_id)}
        # A safety snapshot of the CURRENT state, so the restore itself is
        # reversible. Cheap insurance against restoring the wrong snapshot.
        pre = create_snapshot(label=f"pre-restore-{snapshot_id}", kind="auto",
                               actor=actor, note=f"恢复快照 {snapshot_id} 之前自动留底")
        restored = {}
        try:
            with db.transaction() as conn:
                # Delete children first so FK constraints never fire mid-restore.
                live = _data_tables(conn)
                for table in reversed(live):
                    conn.execute(f'DELETE FROM "{table}"')
                for table, rows in tables.items():
                    if table not in live:
                        continue
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
                # AUTOINCREMENT counters live in sqlite_sequence, which is never
                # restored. Leaving them ahead of the data is harmless, but
                # leaving them behind would re-issue ids that already exist.
                for table in restored:
                    try:
                        row = conn.execute(
                            f'SELECT MAX(rowid) FROM "{table}"').fetchone()
                        top = row[0] if row and row[0] is not None else 0
                        conn.execute(
                            "INSERT INTO sqlite_sequence(name,seq) VALUES (?,?) "
                            "ON CONFLICT(name) DO UPDATE SET seq=excluded.seq",
                            (table, top))
                    except sqlite3.Error:
                        pass  # table without AUTOINCREMENT has no sequence entry
                conn.execute(
                    "UPDATE db_snapshots SET restored_at=?, restore_count=restore_count+1 "
                    "WHERE id=?", (_now(), int(snapshot_id)))
                conn.execute(
                    "INSERT INTO events (task_id,event_type,detail) VALUES (NULL,?,?)",
                    ("snapshot.restored",
                     f"snapshot_id={snapshot_id}; tables={len(restored)}; "
                     f"actor={actor[:50]}; note={note[:120]}"))
        except Exception as exc:
            return {"ok": False, "snapshot_id": int(snapshot_id),
                    "message": f"恢复失败，已回滚事务（库仍是恢复前的状态）：{exc}",
                    "pre_restore_snapshot": pre.get("snapshot_id") if pre.get("ok") else None}
        # Rows are back; the vector index was restored with them, but content
        # hashes may no longer match if the snapshot predates later edits.
        vector_check = check_vector_consistency_after_restore()
        return {"ok": True, "snapshot_id": int(snapshot_id),
                "restored_tables": restored, "total_rows": sum(restored.values()),
                "pre_restore_snapshot": pre.get("snapshot_id") if pre.get("ok") else None,
                "vector_check": vector_check,
                "message": "已恢复到该快照" + (
                    "；向量索引需要重建，见 vector_check" if vector_check.get("needs_reindex") else "")}
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
        for _, path in stale:
            try:
                Path(path).unlink(missing_ok=True)
            except OSError:
                pass
        return {"ok": True, "removed": removed, "kept": min(len(rows), keep)}
    except Exception as exc:
        return {"ok": False, "message": f"{type(exc).__name__}: {exc}", "removed": []}
