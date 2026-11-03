import os
import sqlite3
import time
from pathlib import Path
from contextlib import closing, contextmanager

# BIONIC_DB_PATH lets a second instance (or a test) run against its own database
# instead of the user's live one. Without it, anything that spins up
# `python -m app.admin_ui` on another port still writes to data/agent.db.
DB_PATH = Path(
    os.environ.get("BIONIC_DB_PATH")
    or (Path(__file__).resolve().parent.parent / "data" / "agent.db")
)

# Two MCP servers and the Web panel may write the same file concurrently.
# SQLite allows one writer at a time; this is how long a losing writer waits
# for the lock before giving up. 30s is generous for the small transactions
# here and still far from "hang forever".
BUSY_TIMEOUT_SECONDS = float(os.environ.get("BIONIC_DB_BUSY_TIMEOUT", "30"))


def _retry_locked(fn, attempts: int = 40, delay: float = 0.05):
    """Retry `fn` while SQLite says the database is locked.

    `busy_timeout` covers statements that need the write lock, but it does NOT
    cover switching the journal mode: `PRAGMA journal_mode=WAL` is a file-format
    change that SQLite tries with a zero-length wait, so it fails instantly with
    "database is locked" when another process happens to be holding the file at
    that exact moment. Three of our processes (two MCP servers + the Web panel)
    start at the same time, so this is a real startup race, not a test artifact:
    with 6 processes initialising one empty file it reproduces in roughly one
    attempt in three.

    Retrying is safe because every caller here is idempotent DDL.
    """
    last = None
    for i in range(attempts):
        try:
            return fn()
        except sqlite3.OperationalError as exc:
            if "database is locked" not in str(exc).lower():
                raise
            last = exc
            time.sleep(delay * (1 + i * 0.25))   # mild backoff, keep total small
    raise last

def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    # NOTE: `with sqlite3.connect(...)` only commits on exit, it never closes the
    # handle. On Windows that leaves the file locked, so database files cannot be
    # deleted or backed up afterwards. Use `closing()` + an explicit commit.
    with closing(sqlite3.connect(DB_PATH, timeout=BUSY_TIMEOUT_SECONDS)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=%d" % int(BUSY_TIMEOUT_SECONDS * 1000))
        # journal_mode is a file-format change and ignores busy_timeout, so it
        # has to be retried explicitly; the schema below is idempotent DDL.
        _retry_locked(lambda: conn.execute("PRAGMA journal_mode=WAL"))
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'general',
            source TEXT NOT NULL DEFAULT 'user',
            confidence REAL NOT NULL DEFAULT 1.0,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            goal TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'created',
            current_stage INTEGER NOT NULL DEFAULT 1,
            plan_json TEXT NOT NULL DEFAULT '[]',
            result TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            stage INTEGER NOT NULL,
            summary TEXT NOT NULL,
            state_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(task_id) REFERENCES tasks(id)
        );
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER,
            event_type TEXT NOT NULL,
            detail TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """)
        # Additive migrations: preserve existing data and old databases.
        #
        # These go through _add_column_if_missing rather than inline
        # `if "x" not in columns: ALTER ...` because two MCP instances can
        # start at the same moment against the same new file: both read the
        # column list before either commits, then both try to add the column
        # and the loser dies with "duplicate column name". That is a real
        # startup crash, not a theoretical race -- reproduced with 6 processes
        # where 3 failed.
        _add_column_if_missing(conn, "memories", "tags_json", "TEXT NOT NULL DEFAULT '[]'")
        _add_column_if_missing(conn, "memories", "expires_at", "TEXT")
        _add_column_if_missing(conn, "memories", "supersedes_id", "INTEGER")
        _add_column_if_missing(conn, "memories", "conflict_group", "TEXT")
        # Retry bookkeeping for task resume. Without it, a task that fails and
        # is retried is indistinguishable from one that never failed, and
        # nothing can stop an automatic retry loop from spinning forever.
        _add_column_if_missing(conn, "tasks", "retry_count", "INTEGER NOT NULL DEFAULT 0")
        conn.execute("CREATE TABLE IF NOT EXISTS memory_links ("
                     "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                     "source_memory_id INTEGER NOT NULL,"
                     "target_memory_id INTEGER NOT NULL,"
                     "relation TEXT NOT NULL,"
                     "note TEXT NOT NULL DEFAULT '',"
                     "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,"
                     "FOREIGN KEY(source_memory_id) REFERENCES memories(id),"
                     "FOREIGN KEY(target_memory_id) REFERENCES memories(id))")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_status_updated ON memories(status, updated_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_snapshots_task_stage ON snapshots(task_id, stage DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_task_created ON events(task_id, created_at DESC)")
        # Phase 4: persisted local embedding vectors; existing memory rows are untouched.
        conn.execute("""CREATE TABLE IF NOT EXISTS memory_embeddings (
            memory_id INTEGER PRIMARY KEY,
            model_name TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            dimension INTEGER NOT NULL,
            vector_json TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(memory_id) REFERENCES memories(id) ON DELETE CASCADE
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_embeddings_model ON memory_embeddings(model_name)")
        # ---- Phase 5: memory governance. Purely additive; old rows keep working. ----
        _migrate_phase5(conn)
        # ---- Phase 6: lifecycle types, feedback & consolidation candidates ----
        _migrate_phase6(conn)
        # ---- Phase 7: read-only knowledge base (dsh-memory knowledge/ + mine/) ----
        _migrate_phase7(conn)
        conn.commit()


# Each entry: (column, SQL type+constraints, backfill SQL or None)
PHASE5_MEMORY_COLUMNS = [
    ("importance_score", "REAL NOT NULL DEFAULT 50", None),
    ("confidence_score", "REAL NOT NULL DEFAULT 70",
     "UPDATE memories SET confidence_score = ROUND(MIN(100.0, MAX(0.0, COALESCE(confidence, 0.7) * 100.0)), 2) "
     "WHERE confidence_score IS NULL OR confidence_score = 70"),
    ("freshness_score", "REAL NOT NULL DEFAULT 70", None),
    ("access_count", "INTEGER NOT NULL DEFAULT 0", None),
    ("last_accessed_at", "TEXT", None),
    ("last_evaluated_at", "TEXT", None),
    ("memory_status", "TEXT NOT NULL DEFAULT 'active'", None),
    ("protected", "INTEGER NOT NULL DEFAULT 0", None),
]


def _add_column_if_missing(conn, table: str, column: str, ddl: str) -> bool:
    """Add a column if absent; return True only if this call actually added it.

    The check and the ALTER are two statements, so two processes starting at the
    same moment can both observe the column missing and both try to add it. The
    loser gets "duplicate column name" -- which is not an error here, it means
    the other instance already did the work. Treating that as success keeps
    startup idempotent across processes.
    """
    existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column in existing:
        return False
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
    except sqlite3.OperationalError as exc:
        if "duplicate column name" in str(exc).lower():
            return False          # someone else won the race
        raise
    return True


def _migrate_phase5(conn) -> dict:
    """Idempotent Phase-5 migration. Safe to run on every startup and repeatedly.

    `memories.status` stays the single authoritative retrieval filter
    (active / archived). `memories.memory_status` is a governance VIEW field
    (active / archived / superseded / pending_review) and is backfilled from
    status + supersedes links so old data is never lost or mislabelled.
    """
    applied: list[str] = []
    for column, ddl, backfill in PHASE5_MEMORY_COLUMNS:
        if _add_column_if_missing(conn, "memories", column, ddl):
            applied.append(f"memories.{column}")
        if backfill:
            try:
                conn.execute(backfill)
            except Exception:
                # Backfill must never block startup; scoring recomputes on demand.
                pass

    # Governance view status backfill for pre-Phase-5 rows.
    try:
        conn.execute("""
            UPDATE memories SET memory_status = CASE
                WHEN status = 'archived' THEN
                    CASE WHEN EXISTS (SELECT 1 FROM memory_links l
                                      WHERE l.target_memory_id = memories.id
                                        AND l.relation = 'supersedes')
                         THEN 'superseded' ELSE 'archived' END
                ELSE 'active' END
            WHERE memory_status IS NULL OR memory_status = '' OR memory_status = 'active'
        """)
    except Exception:
        pass

    conn.execute("""CREATE TABLE IF NOT EXISTS governance_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        mode TEXT NOT NULL DEFAULT 'dry_run',
        status TEXT NOT NULL DEFAULT 'running',
        rule_version TEXT NOT NULL DEFAULT '',
        started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        finished_at TEXT,
        candidates_scanned INTEGER NOT NULL DEFAULT 0,
        actions_generated INTEGER NOT NULL DEFAULT 0,
        actions_applied INTEGER NOT NULL DEFAULT 0,
        actions_failed INTEGER NOT NULL DEFAULT 0,
        summary TEXT NOT NULL DEFAULT '',
        stats_json TEXT NOT NULL DEFAULT '{}'
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS governance_actions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id INTEGER,
        action_type TEXT NOT NULL,
        memory_id INTEGER NOT NULL,
        target_memory_id INTEGER,
        old_value_json TEXT NOT NULL DEFAULT '{}',
        new_value_json TEXT NOT NULL DEFAULT '{}',
        reason TEXT NOT NULL DEFAULT '',
        rule_version TEXT NOT NULL DEFAULT '',
        approval_status TEXT NOT NULL DEFAULT 'pending',
        execution_status TEXT NOT NULL DEFAULT 'pending',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        executed_at TEXT,
        error_message TEXT,
        rollback_info_json TEXT NOT NULL DEFAULT '{}',
        rolled_back_at TEXT,
        FOREIGN KEY(run_id) REFERENCES governance_runs(id),
        FOREIGN KEY(memory_id) REFERENCES memories(id),
        FOREIGN KEY(target_memory_id) REFERENCES memories(id)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS memory_conflicts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id INTEGER,
        memory_id INTEGER NOT NULL,
        candidate_memory_id INTEGER NOT NULL,
        conflict_type TEXT NOT NULL,
        similarity REAL,
        confidence REAL NOT NULL DEFAULT 0.0,
        reason TEXT NOT NULL DEFAULT '',
        recommendation TEXT NOT NULL DEFAULT '',
        resolution TEXT NOT NULL DEFAULT 'pending',
        resolved_by TEXT NOT NULL DEFAULT '',
        resolved_at TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        rule_version TEXT NOT NULL DEFAULT '',
        FOREIGN KEY(run_id) REFERENCES governance_runs(id),
        FOREIGN KEY(memory_id) REFERENCES memories(id),
        FOREIGN KEY(candidate_memory_id) REFERENCES memories(id),
        UNIQUE(memory_id, candidate_memory_id, conflict_type)
    )""")
    # Rows inserted after the migration cannot inherit `confidence` through a
    # column DEFAULT, so keep the two in sync with a trigger that only fires on
    # the untouched default value (never overwrites a scored value).
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS trg_memories_confidence_score
        AFTER INSERT ON memories
        BEGIN
            UPDATE memories
               SET confidence_score = ROUND(MIN(100.0, MAX(0.0,
                       COALESCE(NEW.confidence, 0.7) * 100.0)), 2)
             WHERE id = NEW.id AND confidence_score = 70;
        END
    """)

    conn.execute("CREATE INDEX IF NOT EXISTS idx_gov_actions_run ON governance_actions(run_id, id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_gov_actions_status ON governance_actions(approval_status, execution_status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_gov_actions_memory ON governance_actions(memory_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mem_conflicts_memory ON memory_conflicts(memory_id, created_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mem_conflicts_resolution ON memory_conflicts(resolution)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_gov_status ON memories(memory_status)")
    return {"applied_columns": applied}


# ---- Phase 6: lifecycle types, unified feedback & consolidation candidates ----
# Purely additive. Old rows keep memory_type = NULL (readable/searchable).
PHASE6_MEMORY_COLUMNS = [
    # memory_type is nullable; untyped legacy data stays searchable and is
    # surfaced as "unknown" by the recall layer rather than forced into a bucket.
    ("memory_type", "TEXT", None),
]


def _migrate_phase6(conn) -> dict:
    """Idempotent Phase-6 migration. Safe to run on every startup and repeatedly.

    Adds:
      * memories.memory_type   (nullable; episodic/semantic/preference/procedural/goal/reflection)
      * memory_feedback        (retrieved/used/helpful/not_relevant/corrected counters, one row per memory)
      * consolidation_candidates (auto-generated merge/extract candidates, NOT applied to facts)

    Never drops columns, never rewrites existing rows, never removes old data.
    """
    applied: list[str] = []
    for column, ddl, backfill in PHASE6_MEMORY_COLUMNS:
        if _add_column_if_missing(conn, "memories", column, ddl):
            applied.append(f"memories.{column}")
        if backfill:
            try:
                conn.execute(backfill)
            except Exception:
                pass

    # One feedback row per memory; created lazily but the table must exist.
    conn.execute("""CREATE TABLE IF NOT EXISTS memory_feedback (
        memory_id INTEGER PRIMARY KEY,
        retrieved_count INTEGER NOT NULL DEFAULT 0,
        used_count INTEGER NOT NULL DEFAULT 0,
        helpful_count INTEGER NOT NULL DEFAULT 0,
        not_relevant_count INTEGER NOT NULL DEFAULT 0,
        corrected_count INTEGER NOT NULL DEFAULT 0,
        last_retrieved_at TEXT,
        last_used_at TEXT,
        last_feedback_at TEXT,
        history_json TEXT NOT NULL DEFAULT '[]',
        FOREIGN KEY(memory_id) REFERENCES memories(id) ON DELETE CASCADE
    )""")

    conn.execute("""CREATE TABLE IF NOT EXISTS consolidation_candidates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL,                 -- 'merge_duplicate' | 'extract_semantic' | 'extract_procedural' | 'stale_fact' | 'missing_source' | 'conflict_review' | 'archive_review'
        status TEXT NOT NULL DEFAULT 'pending',  -- pending | approved | applied | rejected | skipped
        title TEXT NOT NULL DEFAULT '',
        detail TEXT NOT NULL DEFAULT '',
        source_memory_ids TEXT NOT NULL DEFAULT '[]',  -- JSON array of contributing memory ids
        target_memory_id INTEGER,          -- populated only after approval+application
        confidence REAL NOT NULL DEFAULT 0.0,
        rule_version TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        resolved_at TEXT,
        resolved_by TEXT NOT NULL DEFAULT '',
        notes_json TEXT NOT NULL DEFAULT '{}',
        FOREIGN KEY(target_memory_id) REFERENCES memories(id)
    )""")

    conn.execute("CREATE INDEX IF NOT EXISTS idx_feedback_memory ON memory_feedback(memory_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_consol_kind_status ON consolidation_candidates(kind, status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_consol_status_created ON consolidation_candidates(status, created_at DESC)")
    return {"applied_columns": applied}


def _migrate_phase7(conn) -> dict:
    """Idempotent Phase-7 migration: the read-only knowledge base.

    Why this exists: `knowledge/` (82 md, 400K chars) and `mine/` (24 md) in
    dsh-memory were never reachable from MCP -- there was no table to hold them,
    so semantic search over "what have I learned" only ever covered the 36
    memory rows. This migration adds the two tables that make them searchable.

    Why two tables and not one: a document is the unit a human cites
    ("knowledge/语音/2026-10-20-本地TTS与声音克隆.md"), but a whole document is
    a bad embedding unit (median 10K chars, max 20K). Chunks are what get
    embedded and scored; the doc table holds title/path/mtime for display and
    for deciding whether a re-import is needed at all.

    Never touches `memories`: these are documents, not facts. They have no
    lifecycle, no governance, no conflict detection -- and they must not, or a
    82-doc import would flood the conflict/review queue.

    Safe to run on every startup and repeatedly; nothing is dropped or rewritten.
    """
    conn.execute("""CREATE TABLE IF NOT EXISTS knowledge_docs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        path TEXT NOT NULL UNIQUE,          -- absolute source path, the idempotency anchor
        rel_path TEXT NOT NULL DEFAULT '',  -- path relative to its root, for display
        root TEXT NOT NULL DEFAULT '',      -- 'knowledge' | 'mine'
        domain TEXT NOT NULL DEFAULT '',    -- knowledge/ subdir, e.g. '语音' ('' for mine)
        title TEXT NOT NULL DEFAULT '',
        content TEXT NOT NULL,              -- full markdown, kept so a chunk can be re-read in context
        content_hash TEXT NOT NULL,         -- sha256 of content; unchanged hash = skip re-import
        byte_size INTEGER NOT NULL DEFAULT 0,
        mtime INTEGER NOT NULL DEFAULT 0,
        chunk_count INTEGER NOT NULL DEFAULT 0,
        imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""")

    # Ord is the section's position in the document. Without it a chunk list is
    # just a bag of text and "show me the section before this one" is impossible.
    conn.execute("""CREATE TABLE IF NOT EXISTS knowledge_chunks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        doc_id INTEGER NOT NULL,
        ord INTEGER NOT NULL DEFAULT 0,
        heading TEXT NOT NULL DEFAULT '',
        content TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        char_count INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(doc_id) REFERENCES knowledge_docs(id) ON DELETE CASCADE
    )""")

    # Vectors live here rather than on knowledge_chunks so a document edit can
    # drop its vectors with a plain DELETE ... WHERE chunk_id IN (...) and the
    # chunk text stays intact for display in the meantime.
    conn.execute("""CREATE TABLE IF NOT EXISTS knowledge_embeddings (
        chunk_id INTEGER PRIMARY KEY,
        model_name TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        dimension INTEGER NOT NULL,
        vector_json TEXT NOT NULL,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(chunk_id) REFERENCES knowledge_chunks(id) ON DELETE CASCADE
    )""")

    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_knowledge_chunks_doc_ord ON knowledge_chunks(doc_id, ord)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_knowledge_docs_root ON knowledge_docs(root, domain)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_doc ON knowledge_chunks(doc_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_knowledge_embeddings_model ON knowledge_embeddings(model_name)")
    return {"phase": 7}


def init_db_phase5() -> dict:
    """Run the Phase-5 migration on the real database. Idempotent."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(DB_PATH, timeout=BUSY_TIMEOUT_SECONDS)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=%d" % int(BUSY_TIMEOUT_SECONDS * 1000))
        _retry_locked(lambda: conn.execute("PRAGMA journal_mode=WAL"))
        result = _migrate_phase5(conn)
        conn.commit()
        return result

@contextmanager
def connect():
    """Open a connection with settings that survive multi-process writes.

    Three instances may write the same file at once (two MCP servers + the Web
    panel). SQLite serialises writers with a file lock; without a busy handler
    a losing writer fails immediately with "database is locked" instead of
    waiting its turn. `timeout=` installs that handler, and the PRAGMAs make
    the wait explicit and bounded rather than relying on the 5s Python default.

    WAL is set once at creation time and persists in the file header, but it is
    re-asserted here because it is cheap and makes the mode explicit on every
    connection (a non-WAL file would otherwise block readers during writes).
    """
    conn = sqlite3.connect(DB_PATH, timeout=BUSY_TIMEOUT_SECONDS)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=%d" % int(BUSY_TIMEOUT_SECONDS * 1000))
    _retry_locked(lambda: conn.execute("PRAGMA journal_mode=WAL"))
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


@contextmanager
def transaction():
    """Explicit atomic transaction for multi-table governance operations.

    Unlike `connect()`, any exception rolls the whole unit back instead of
    committing whatever happened to succeed. `connect()` keeps its original
    behaviour so Phases 1-4 are unaffected.
    """
    conn = sqlite3.connect(DB_PATH, timeout=BUSY_TIMEOUT_SECONDS)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=%d" % int(BUSY_TIMEOUT_SECONDS * 1000))
    _retry_locked(lambda: conn.execute("PRAGMA journal_mode=WAL"))
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # IMMEDIATE takes the write lock up front. With the default deferred BEGIN a
    # read-then-write transaction can be promoted mid-flight and fail with
    # SQLITE_BUSY_SNAPSHOT, which busy_timeout does NOT retry.
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.commit()
    except BaseException:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()

def rowdict(row):
    return dict(row) if row else None
