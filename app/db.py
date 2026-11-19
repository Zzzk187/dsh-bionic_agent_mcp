import os
import sqlite3
import time
from pathlib import Path
from contextlib import closing, contextmanager
from urllib.parse import quote

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

# ---------------------------------------------------------------------------
# S6-FIX-03: 初始化前的只读探测
# ---------------------------------------------------------------------------
# 为什么要有这一段：`init_db()` 以前每次调用都无条件连库 + `PRAGMA
# journal_mode=WAL` + 跑全套幂等 DDL。对**已经初始化好**的库，这些写一个都不
# 需要——但 `journal_mode=WAL` 是文件格式变更，会碰文件头、刷新 mtime，于是
# 「只是启动一次」也会让生产库的指纹变掉，只读审计也就没法自证清白。
#
# 所以先做一次**纯只读**探测：能确定"结构完整"就直接返回，一个字节都不写。
# 探测失败或拿不准时绝不假设完整——一律退回原来的完整初始化，那套 DDL 幂等，
# 多跑一次无害。宁可多写一次，不可少写一次。

SCHEMA_PHASE = 7
"""当前 schema 阶段号。

db.py 没有 user_version、也没有迁移版本表，版本是靠"哪些表/列存在"隐式表达的
（S6 审计已记录：user_version=0、无 %migrat%/%schema% 表）。这里把这种隐式表达
显式化，好让探测给出可读结论，但**判定仍然只看表/列/索引/触发器在不在**，
不看这个数字。
"""

REQUIRED_TABLES = (
    "memories",
    "tasks",
    "snapshots",
    "events",
    "memory_links",
    "memory_embeddings",
    "governance_runs",
    "governance_actions",
    "memory_conflicts",
    "memory_feedback",
    "consolidation_candidates",
    "knowledge_docs",
    "knowledge_chunks",
    "knowledge_embeddings",
)

# 每张表必须有的列。只用 PRAGMA table_info 读结构，不碰任何行、不看内容。
REQUIRED_COLUMNS = {
    "memories": (
        "id", "content", "category", "source", "confidence", "status",
        "created_at", "updated_at",
        "tags_json", "expires_at", "supersedes_id", "conflict_group",
        "importance_score", "confidence_score", "freshness_score", "access_count",
        "last_accessed_at", "last_evaluated_at", "memory_status", "protected",
        "memory_type",
    ),
    "tasks": (
        "id", "title", "goal", "status", "current_stage", "plan_json", "result",
        "created_at", "updated_at", "retry_count",
    ),
    "snapshots": ("id", "task_id", "stage", "summary", "state_json", "created_at"),
    "events": ("id", "task_id", "event_type", "detail", "created_at"),
    "memory_links": (
        "id", "source_memory_id", "target_memory_id", "relation", "note", "created_at",
    ),
    "memory_embeddings": (
        "memory_id", "model_name", "content_hash", "dimension", "vector_json", "updated_at",
    ),
    "governance_runs": (
        "id", "mode", "status", "rule_version", "started_at", "finished_at",
        "candidates_scanned", "actions_generated", "actions_applied", "actions_failed",
        "summary", "stats_json",
    ),
    "governance_actions": (
        "id", "run_id", "action_type", "memory_id", "target_memory_id",
        "old_value_json", "new_value_json", "reason", "rule_version",
        "approval_status", "execution_status", "created_at", "executed_at",
        "error_message", "rollback_info_json", "rolled_back_at",
    ),
    "memory_conflicts": (
        "id", "run_id", "memory_id", "candidate_memory_id", "conflict_type",
        "similarity", "confidence", "reason", "recommendation", "resolution",
        "resolved_by", "resolved_at", "created_at", "rule_version",
    ),
    "memory_feedback": (
        "memory_id", "retrieved_count", "used_count", "helpful_count",
        "not_relevant_count", "corrected_count", "last_retrieved_at",
        "last_used_at", "last_feedback_at", "history_json",
    ),
    "consolidation_candidates": (
        "id", "kind", "status", "title", "detail", "source_memory_ids",
        "target_memory_id", "confidence", "rule_version", "created_at",
        "resolved_at", "resolved_by", "notes_json",
    ),
    "knowledge_docs": (
        "id", "path", "rel_path", "root", "domain", "title", "content",
        "content_hash", "byte_size", "mtime", "chunk_count", "imported_at", "updated_at",
    ),
    "knowledge_chunks": (
        "id", "doc_id", "ord", "heading", "content", "content_hash",
        "char_count", "updated_at",
    ),
    "knowledge_embeddings": (
        "chunk_id", "model_name", "content_hash", "dimension", "vector_json", "updated_at",
    ),
}

# 索引不影响数据正确性，但它是 init_db 承诺的一部分：缺了就说明这张库没跑到当前
# 初始化，应该让它补上（CREATE INDEX IF NOT EXISTS 幂等）。
REQUIRED_INDEXES = (
    "idx_memories_status_updated",
    "idx_snapshots_task_stage",
    "idx_events_task_created",
    "idx_memory_embeddings_model",
    "idx_gov_actions_run",
    "idx_gov_actions_status",
    "idx_gov_actions_memory",
    "idx_mem_conflicts_memory",
    "idx_mem_conflicts_resolution",
    "idx_memories_gov_status",
    "idx_feedback_memory",
    "idx_consol_kind_status",
    "idx_consol_status_created",
    "idx_knowledge_chunks_doc_ord",
    "idx_knowledge_docs_root",
    "idx_knowledge_chunks_doc",
    "idx_knowledge_embeddings_model",
)

REQUIRED_TRIGGERS = ("trg_memories_confidence_score",)


def _probe_uri(path: Path) -> str:
    """构造探测用的只读 URI。

    Windows 的 `D:/a/b.db` 要变成 `file:/D:/a/b.db?mode=ro`：绝对路径需要前导
    斜杠，反斜杠换成正斜杠，特殊字符要转义（否则路径里的 `?`/`#` 会被当成查询
    串/锚点）。`mode=ro` 保证 SQLite 既不创建文件也不写库。
    """
    p = str(Path(path).resolve()).replace("\\", "/")
    if not p.startswith("/"):
        p = "/" + p
    return "file:" + quote(p, safe="/:") + "?mode=ro"


def _wal_has_content(db_path: Path) -> bool:
    """库旁边是否有一个**装着数据**的 -wal。

    WAL 模式下最近的提交可能还留在 -wal 里、主文件尚未合并。这种时候不能用
    immutable 读（它只看主文件，会读到旧结构），必须真只读打开。
    干净的库在最后一个连接正常关闭后 SQLite 会删掉 -wal/-shm，所以"没有 -wal"
    是最常见的情形——也正是我们希望走上零副作用那条路的情形。
    """
    wal = Path(str(db_path) + "-wal")
    try:
        return wal.is_file() and wal.stat().st_size > 0
    except OSError:
        return True   # 判断不了就当有，走安全的那条


def _sidecar_snapshot(db_path: Path) -> dict:
    """记录 -wal/-shm/-journal 的存在与大小，用来证明探测没留下东西。"""
    out: dict = {}
    for suffix in ("-wal", "-shm", "-journal"):
        p = Path(str(db_path) + suffix)
        try:
            out[suffix] = p.stat().st_size if p.is_file() else None
        except OSError:
            out[suffix] = None
    return out


def _derive_schema_phase(tables, missing_columns) -> int:
    """按"哪些表/列已存在"推导阶段号。只用于报告，不参与完整性判定。"""
    if "memories" not in tables:
        return 0
    missing = {c.split(".", 1)[1] for c in missing_columns if c.startswith("memories.")}
    if "importance_score" in missing:
        return 4
    if "memory_type" in missing:
        return 5
    if "knowledge_docs" not in tables:
        return 6
    return SCHEMA_PHASE


def probe_database_state(db_path=None) -> dict:
    """只读探测数据库初始化状态。**不改库，也不创建库文件。**

    返回字段：
      database_exists   库文件在不在（不在不算错误）
      readable          能否只读打开并读到 sqlite_master
      schema_complete   是否已满足当前阶段的完整 schema
      schema_version    按表/列存在性推导的阶段号（仅报告用）
      db_user_version   PRAGMA user_version 原值（本库不用它，仅供诊断）
      journal_mode      读到的 journal_mode（immutable 下反映主文件头，仅供诊断）
      missing_tables / missing_columns / missing_indexes / missing_triggers
      needs_migration   是否该进完整初始化
      probe_mode        实际读法："immutable" | "readonly" | ""
      error             出错说明；空串表示没出错
      sidecar_created   探测途中是否新建了 -wal/-shm（正常应为 False）
      path              被测库路径

    两条硬约定（任务 §四）：
      * 任何异常都写进 `error` 且 `readable=False`，**绝不静默当完整**；
      * 探测失败 **不等于** 库不存在——`database_exists` 只看文件在不在。
    """
    target = Path(db_path) if db_path is not None else DB_PATH
    res: dict = {
        "database_exists": False,
        "readable": False,
        "schema_complete": False,
        "schema_version": 0,
        "db_user_version": None,
        "journal_mode": "",
        "missing_tables": list(REQUIRED_TABLES),
        "missing_columns": [],
        "missing_indexes": list(REQUIRED_INDEXES),
        "missing_triggers": list(REQUIRED_TRIGGERS),
        "needs_migration": True,
        "probe_mode": "",
        "error": "",
        "sidecar_created": False,
        "path": str(target),
    }

    try:
        res["database_exists"] = target.is_file()
    except OSError as exc:
        res["error"] = "无法判断库文件是否存在：%s" % exc
        return res

    if not res["database_exists"]:
        # 不存在不是错误：这是一张待建的新库，交给 init_db 走建库路径。
        return res

    # 选读法：-wal 里没内容时用 immutable（零副作用，连 -shm 都不建）；-wal 里
    # 有内容时必须真只读打开，否则读不到尚未合并的 schema。
    use_immutable = not _wal_has_content(target)
    uri = _probe_uri(target)
    if use_immutable:
        uri += "&immutable=1"
    res["probe_mode"] = "immutable" if use_immutable else "readonly"

    before = _sidecar_snapshot(target)
    conn = None
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=BUSY_TIMEOUT_SECONDS)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=%d" % int(BUSY_TIMEOUT_SECONDS * 1000))
        except sqlite3.Error:
            pass

        # 这一步才真正触发读；损坏的库会在这里抛 "file is not a database"。
        master = conn.execute(
            "SELECT type, name FROM sqlite_master "
            "WHERE type IN ('table', 'index', 'trigger')"
        ).fetchall()
        tables = {r["name"] for r in master if r["type"] == "table"}
        indexes = {r["name"] for r in master if r["type"] == "index"}
        triggers = {r["name"] for r in master if r["type"] == "trigger"}
        res["readable"] = True

        try:
            row = conn.execute("PRAGMA journal_mode").fetchone()
            res["journal_mode"] = (row[0] if row else "") or ""
        except sqlite3.Error:
            res["journal_mode"] = ""
        try:
            row = conn.execute("PRAGMA user_version").fetchone()
            res["db_user_version"] = row[0] if row else None
        except sqlite3.Error:
            res["db_user_version"] = None

        # sqlite_ 开头的系统表不算 schema 的一部分。
        tables = {t for t in tables if not t.startswith("sqlite_")}
        res["missing_tables"] = [t for t in REQUIRED_TABLES if t not in tables]
        res["missing_indexes"] = [i for i in REQUIRED_INDEXES if i not in indexes]
        res["missing_triggers"] = [t for t in REQUIRED_TRIGGERS if t not in triggers]

        # 只对**存在**的表查列；缺表就不必再逐条列它的列（缺表本身已经足够）。
        missing_cols: list = []
        for table in REQUIRED_TABLES:
            if table in res["missing_tables"]:
                continue
            try:
                have = {r["name"] for r in conn.execute("PRAGMA table_info(%s)" % table)}
            except sqlite3.Error as exc:
                res["readable"] = False
                res["schema_complete"] = False
                res["needs_migration"] = True
                res["error"] = "读取表结构失败（%s）：%s" % (table, exc)
                return res
            for col in REQUIRED_COLUMNS.get(table, ()):
                if col not in have:
                    missing_cols.append("%s.%s" % (table, col))
        res["missing_columns"] = missing_cols

        res["schema_version"] = _derive_schema_phase(tables, missing_cols)
        res["schema_complete"] = (
            not res["missing_tables"]
            and not res["missing_columns"]
            and not res["missing_indexes"]
            and not res["missing_triggers"]
        )
        res["needs_migration"] = not res["schema_complete"]
        return res
    except sqlite3.Error as exc:
        res["readable"] = False
        res["schema_complete"] = False
        res["needs_migration"] = True
        res["error"] = "%s: %s" % (type(exc).__name__, exc)
        return res
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        res["sidecar_created"] = _sidecar_snapshot(target) != before


def init_db() -> dict:
    """确保数据库可用。**已初始化的库走只读路径，一个字节都不写。**

    四条分支（任务 §五）：
      1. 探测失败（损坏 / 打不开 / 读不到结构）
         → 退回完整初始化。那套 DDL 幂等；库真坏了会在写路径上明确报错，
           不会被当成"已完整"悄悄放过。
      2. 库不存在 → 完整初始化（建库、建表、建索引、迁移）。
      3. 库存在且 schema 完整 → 只读探测后直接返回：不开可写连接、不设
         journal_mode、不动 mtime。
      4. 库存在但 schema 不完整 → 完整初始化补齐；补完**重新探测**，仍不完整
         就抛错，绝不假报成功。

    返回本次做了什么的结构化说明。历史调用方都忽略返回值，故兼容。
    """
    report: dict = {"path": str(DB_PATH), "probe": None, "action": "", "verified": False}

    probe = probe_database_state()
    report["probe"] = probe

    if probe["readable"] and probe["schema_complete"]:
        report["action"] = "readonly-skip"
        report["verified"] = True
        return report

    if not probe["database_exists"]:
        report["action"] = "full-init (new)"
    elif probe["error"]:
        # 不算完整，但也不能当不存在：走完整初始化，让真实错误在那一步带着
        # 原始异常冒出来，而不是在这里被吞掉。
        report["action"] = "full-init (probe-error: %s)" % probe["error"]
    else:
        report["action"] = "full-init (incomplete)"

    # 并发保护（任务 §六.8）：从探测到这里之间，另一个实例可能已经把初始化做完
    # 了（Core 与 Governance 同时启动就是这种情形）。写之前再确认一次，能省掉
    # 大半的重复 DDL。这一次确认失败也照旧往下走，不做判定依据。
    if probe["database_exists"]:
        again = probe_database_state()
        if again["readable"] and again["schema_complete"]:
            report["probe"] = again
            report["action"] = "readonly-skip (settled by peer)"
            report["verified"] = True
            return report

    _init_db_write()

    after = probe_database_state()
    report["probe_after"] = after
    if not (after["readable"] and after["schema_complete"]):
        raise RuntimeError(
            "init_db() 写入后 schema 仍不完整："
            "missing_tables=%r missing_columns=%r error=%r"
            % (after["missing_tables"], after["missing_columns"], after["error"])
        )
    report["verified"] = True
    return report


def _init_db_write():
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


@contextmanager
def read_transaction():
    """A single, consistent read view across several SELECTs (WAL snapshot).

    `connect()` is autocommit: every SELECT is its own implicit transaction, so
    a multi-table dump can read table A at T1 and table B at T2 while a writer
    commits in between -- the payload would mix two different states. A plain
    `BEGIN` here pins one WAL snapshot: the first SELECT establishes it and all
    later SELECTs in this transaction see exactly that same view. This is what
    `snapshots.create_snapshot` needs so payload + table_counts describe one
    moment in time.

    Additive only: `connect()` and `transaction()` keep their original behaviour,
    so Phases 1-8 are untouched. This is read-only -- nothing is committed. The
    PRAGMAs that cannot change inside a transaction are set before BEGIN.
    """
    conn = sqlite3.connect(DB_PATH, timeout=BUSY_TIMEOUT_SECONDS)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=%d" % int(BUSY_TIMEOUT_SECONDS * 1000))
    _retry_locked(lambda: conn.execute("PRAGMA journal_mode=WAL"))
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("BEGIN")  # deferred read txn; first SELECT pins the WAL snapshot
    try:
        yield conn
        conn.rollback()   # read-only: cleanly release the snapshot, no writes
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
