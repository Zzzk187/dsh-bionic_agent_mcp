"""S6-FIX-03: `init_db()` 只读探测自检。

这一组测试守的是「已经初始化好的库，启动时不该被写」这件事。它很容易悄悄
退化：把只读探测删掉、或者探测判据放宽，代码照样能跑、功能照样正常，只是
每次启动又去碰一次 `journal_mode=WAL`，把 mtime 刷掉 —— 单看行为看不出来，
只有比对文件指纹才知道。

所以 A/B 两组直接用 sha256 + size + mtime_ns + 附属文件四件套做硬断言：
已初始化的库跑 `init_db()` **前后必须逐字节一致**，而且不许凭空多出
`-wal`/`-shm`。

C 组守的是反面：不完整的库**不能**被误判成完整而跳过初始化。四种残缺形态
（缺表 / 缺列 / 旧阶段 / 只建了一半）都要能被识别、补齐，且原有数据一条不少。

D 组守异常：损坏、目录当库、锁冲突、探测内部抛错、只读文件。核心要求是
「不许静默通过」，错误必须明确，且库本身不能被破坏。

E 组守并发：Core 与 Governance 两个实例会同时启动，两个进程同时初始化不能
撞出重复列或半截 schema。
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
import _env_guard  # noqa: E402,F401  OPT-04a：数据库隔离闸门（必须在任何 app.* 之前）

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMPS: list[str] = []
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def mktmp(prefix: str = "bionic-initdb-") -> Path:
    d = tempfile.mkdtemp(prefix=prefix)
    _TMPS.append(d)
    return Path(d)


def fingerprint(db_path: Path) -> dict:
    """库的指纹：内容 + 大小 + mtime + 三个附属文件。"""
    p = Path(db_path)
    data = p.read_bytes()
    st = os.stat(p)
    side = {}
    for suffix in ("-wal", "-shm", "-journal"):
        q = Path(str(p) + suffix)
        side[suffix] = q.stat().st_size if q.is_file() else None
    return {
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
        "mtime_ns": st.st_mtime_ns,
        "sidecars": side,
    }


def same_fp(a: dict, b: dict) -> bool:
    return (a["sha256"] == b["sha256"]
            and a["size"] == b["size"]
            and a["mtime_ns"] == b["mtime_ns"]
            and a["sidecars"] == b["sidecars"])


def diff_fp(a: dict, b: dict) -> str:
    bits = []
    if a["sha256"] != b["sha256"]:
        bits.append(f"sha {a['sha256'][:12]}!={b['sha256'][:12]}")
    if a["size"] != b["size"]:
        bits.append(f"size {a['size']}!={b['size']}")
    if a["mtime_ns"] != b["mtime_ns"]:
        bits.append(f"mtime {a['mtime_ns']}!={b['mtime_ns']}")
    if a["sidecars"] != b["sidecars"]:
        bits.append(f"sidecars {a['sidecars']}!={b['sidecars']}")
    return ", ".join(bits)


def table_names(db_path: Path) -> set:
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        return {r[0] for r in rows if not r[0].startswith("sqlite_")}
    finally:
        conn.close()


def index_names(db_path: Path) -> set:
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'").fetchall()
        return {r[0] for r in rows}
    finally:
        conn.close()


def trigger_names(db_path: Path) -> set:
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'").fetchall()
        return {r[0] for r in rows}
    finally:
        conn.close()


def seed_memory(db_path: Path, mem_id: int, content: str) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO memories(id, content, category, source, confidence, status) "
            "VALUES (?,?,?,?,?,?)",
            (mem_id, content, "general", "seed", 0.9, "active"))
        conn.commit()
    finally:
        conn.close()


def read_memory(db_path: Path, mem_id: int):
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT content, status FROM memories WHERE id=?", (mem_id,)).fetchone()
        return row
    finally:
        conn.close()


_WRITE_PREFIXES = ("CREATE ", "ALTER ", "INSERT ", "UPDATE ", "DELETE ",
                   "DROP ", "REPLACE ", "VACUUM", "REINDEX", "ANALYZE")

# 只有这几个 pragma 的赋值会改数据库文件本身（journal_mode 改文件格式、
# user_version/application_id 写文件头、page_size/auto_vacuum/encoding 同理）。
# busy_timeout / foreign_keys / synchronous / cache_size 这些是**连接级**设置，
# 只影响当前连接，不写文件——把它们也算成写会让断言变成假警报。
_FILE_WRITE_PRAGMAS = ("journal_mode", "user_version", "application_id",
                       "writable_schema", "page_size", "auto_vacuum", "encoding")


def _is_write_sql(stmt: str) -> bool:
    """判断一条 SQL 是否会改动数据库文件。"""
    s = " ".join(stmt.split())
    if not s:
        return False
    upper = s.upper()
    if upper.startswith("PRAGMA"):
        if "=" not in s:
            # `PRAGMA journal_mode`（不带等号）是读，`PRAGMA table_info(x)` 也是读。
            return False
        name = s[len("PRAGMA"):].split("=")[0].strip().lower()
        return name in _FILE_WRITE_PRAGMAS
    return upper.startswith(_WRITE_PREFIXES)


# ---------------------------------------------------------------------------
# A. 已初始化库：只读通过，指纹一字不变
# ---------------------------------------------------------------------------
def test_initialized_is_readonly() -> None:
    from app import db

    tmp = mktmp("fix03-a-")
    db.DB_PATH = tmp / "agent.db"
    first = db.init_db()
    check("A 首次建库走完整初始化", first["action"] == "full-init (new)", first["action"])
    check("A 首次建库后自校验通过", first["verified"] is True)

    probe = db.probe_database_state()
    check("A 探测判定 schema 完整", probe["schema_complete"] is True, str(probe["error"]))
    check("A 探测零副作用（没建 -wal/-shm）",
          probe["sidecar_created"] is False, str(probe["sidecar_created"]))
    check("A 探测走 immutable 路径", probe["probe_mode"] == "immutable", probe["probe_mode"])

    before = fingerprint(db.DB_PATH)
    second = db.init_db()
    after = fingerprint(db.DB_PATH)

    check("A 二次调用走只读路径", second["action"] == "readonly-skip", second["action"])
    check("A 二次调用自校验通过", second["verified"] is True)
    check("A 二次调用后 sha256 不变", before["sha256"] == after["sha256"])
    check("A 二次调用后 size 不变", before["size"] == after["size"])
    check("A 二次调用后 mtime_ns 不变", before["mtime_ns"] == after["mtime_ns"])
    check("A 二次调用后附属文件不变", before["sidecars"] == after["sidecars"])
    check("A 指纹整体逐字节一致", same_fp(before, after), diff_fp(before, after))

    # 连跑三次，确认不是「第一次碰巧没写」
    for _ in range(3):
        db.init_db()
    third = fingerprint(db.DB_PATH)
    check("A 连跑三次后仍逐字节一致", same_fp(before, third), diff_fp(before, third))


def test_readonly_path_executes_no_write_sql() -> None:
    """只读路径上必须一条写 SQL 都没发出去。"""
    from app import db

    tmp = mktmp("fix03-a2-")
    db.DB_PATH = tmp / "agent.db"
    db.init_db()

    recorded: list[str] = []
    orig_connect = sqlite3.connect

    def spy(*args, **kwargs):
        conn = orig_connect(*args, **kwargs)
        try:
            conn.set_trace_callback(recorded.append)
        except Exception:
            pass
        return conn

    sqlite3.connect = spy
    try:
        db.init_db()
    finally:
        sqlite3.connect = orig_connect

    writes = [s for s in recorded if _is_write_sql(s)]
    check("A 只读路径未执行任何写 SQL",
          not writes, f"共 {len(recorded)} 条语句，写的有 {len(writes)} 条：{writes[:3]}")
    check("A 只读路径确实发出了读语句", len(recorded) > 0, f"{len(recorded)} 条")


# ---------------------------------------------------------------------------
# B. 新库：正常初始化，二次调用转只读
# ---------------------------------------------------------------------------
def test_new_database() -> None:
    from app import db

    tmp = mktmp("fix03-b-")
    db.DB_PATH = tmp / "fresh.db"

    probe = db.probe_database_state()
    check("B 库不存在时探测报 exists=False", probe["database_exists"] is False)
    check("B 库不存在时探测报 needs_migration", probe["needs_migration"] is True)
    check("B 库不存在时探测无错误", probe["error"] == "", probe["error"])
    check("B 探测没有造出空库文件", not db.DB_PATH.exists())

    res = db.init_db()
    check("B 新库初始化成功", res["action"] == "full-init (new)", res["action"])
    check("B 初始化后自校验通过", res["verified"] is True)
    check("B 库文件已创建且非空",
          db.DB_PATH.is_file() and db.DB_PATH.stat().st_size > 0)

    tables = table_names(db.DB_PATH)
    missing = [t for t in db.REQUIRED_TABLES if t not in tables]
    check("B 必要表全部创建", not missing, f"缺 {missing}")

    idx = index_names(db.DB_PATH)
    missing_idx = [i for i in db.REQUIRED_INDEXES if i not in idx]
    check("B 必要索引全部创建", not missing_idx, f"缺 {missing_idx}")

    trg = trigger_names(db.DB_PATH)
    missing_trg = [t for t in db.REQUIRED_TRIGGERS if t not in trg]
    check("B 必要触发器已创建", not missing_trg, f"缺 {missing_trg}")

    after_probe = db.probe_database_state()
    check("B 初始化后探测为 schema_complete", after_probe["schema_complete"] is True)
    check("B 初始化后探测无缺失项",
          not after_probe["missing_tables"] and not after_probe["missing_columns"]
          and not after_probe["missing_indexes"] and not after_probe["missing_triggers"])

    seed_memory(db.DB_PATH, 4, "偏好喝冰美式咖啡")
    seed_memory(db.DB_PATH, 5, "中文文档撰写偏好")

    before = fingerprint(db.DB_PATH)
    again = db.init_db()
    after = fingerprint(db.DB_PATH)
    check("B 第二次调用走只读路径", again["action"] == "readonly-skip", again["action"])
    check("B 第二次调用指纹不变", same_fp(before, after), diff_fp(before, after))

    row4 = read_memory(db.DB_PATH, 4)
    row5 = read_memory(db.DB_PATH, 5)
    check("B 初始化数据未丢（id=4）", row4 is not None and "冰美式" in row4[0], str(row4))
    check("B 初始化数据未丢（id=5）", row5 is not None and "中文" in row5[0], str(row5))


# ---------------------------------------------------------------------------
# C. 不完整库：必须识别，不能被当成完整
# ---------------------------------------------------------------------------
def test_missing_table() -> None:
    from app import db

    tmp = mktmp("fix03-c1-")
    db.DB_PATH = tmp / "agent.db"
    db.init_db()
    seed_memory(db.DB_PATH, 7, "缺表测试的既有数据")

    conn = sqlite3.connect(str(db.DB_PATH))
    try:
        conn.execute("DROP TABLE memory_feedback")
        conn.commit()
    finally:
        conn.close()

    probe = db.probe_database_state()
    check("C1 缺表能被探测到",
          "memory_feedback" in probe["missing_tables"], str(probe["missing_tables"]))
    check("C1 缺表时 schema_complete=False", probe["schema_complete"] is False)
    check("C1 缺表时 needs_migration=True", probe["needs_migration"] is True)

    res = db.init_db()
    check("C1 缺表进完整初始化", res["action"].startswith("full-init"), res["action"])
    check("C1 补齐后自校验通过", res["verified"] is True)
    final = db.probe_database_state()
    check("C1 补齐后 schema_complete", final["schema_complete"] is True, str(final["error"]))
    row = read_memory(db.DB_PATH, 7)
    check("C1 既有数据未丢", row is not None and "缺表测试" in row[0], str(row))


def test_missing_column() -> None:
    from app import db

    tmp = mktmp("fix03-c2-")
    db.DB_PATH = tmp / "agent.db"
    db.init_db()
    seed_memory(db.DB_PATH, 8, "缺列测试的既有数据")

    conn = sqlite3.connect(str(db.DB_PATH))
    try:
        conn.execute("ALTER TABLE memories DROP COLUMN memory_type")
        conn.commit()
    finally:
        conn.close()

    probe = db.probe_database_state()
    check("C2 缺列能被探测到",
          "memories.memory_type" in probe["missing_columns"], str(probe["missing_columns"]))
    check("C2 缺列时 schema_complete=False", probe["schema_complete"] is False)

    res = db.init_db()
    check("C2 缺列进完整初始化", res["action"].startswith("full-init"), res["action"])
    check("C2 补齐后自校验通过", res["verified"] is True)
    final = db.probe_database_state()
    check("C2 补齐后 schema_complete", final["schema_complete"] is True, str(final["error"]))
    row = read_memory(db.DB_PATH, 8)
    check("C2 既有数据未丢", row is not None and "缺列测试" in row[0], str(row))


def test_old_phase_schema() -> None:
    """只跑到 phase4 的老库：缺 phase5/6/7 的表和列。"""
    from app import db

    tmp = mktmp("fix03-c3-")
    db.DB_PATH = tmp / "agent.db"
    conn = sqlite3.connect(str(db.DB_PATH))
    try:
        conn.executescript("""
            CREATE TABLE memories (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              content TEXT NOT NULL,
              category TEXT NOT NULL DEFAULT 'general',
              source TEXT NOT NULL DEFAULT 'user',
              confidence REAL NOT NULL DEFAULT 1.0,
              status TEXT NOT NULL DEFAULT 'active',
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE tasks (
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
            CREATE TABLE snapshots (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              task_id INTEGER NOT NULL,
              stage INTEGER NOT NULL,
              summary TEXT NOT NULL,
              state_json TEXT NOT NULL DEFAULT '{}',
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              FOREIGN KEY(task_id) REFERENCES tasks(id)
            );
            CREATE TABLE events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              task_id INTEGER,
              event_type TEXT NOT NULL,
              detail TEXT NOT NULL,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
        """)
        conn.execute(
            "INSERT INTO memories(id,content,category,source,confidence,status) "
            "VALUES (11,'旧阶段库里的数据','general','seed',0.8,'active')")
        conn.commit()
    finally:
        conn.close()

    probe = db.probe_database_state()
    check("C3 旧阶段库判为不完整", probe["schema_complete"] is False)
    check("C3 旧阶段库能被读到", probe["readable"] is True, probe["error"])
    check("C3 推导出阶段号 4", probe["schema_version"] == 4, str(probe["schema_version"]))
    check("C3 缺 phase6/7 的表",
          "memory_feedback" in probe["missing_tables"]
          and "knowledge_docs" in probe["missing_tables"],
          str(probe["missing_tables"][:4]))
    check("C3 缺 phase5 的列",
          "memories.importance_score" in probe["missing_columns"],
          str(probe["missing_columns"][:4]))

    res = db.init_db()
    check("C3 旧阶段库进完整初始化", res["action"].startswith("full-init"), res["action"])
    check("C3 迁移后自校验通过", res["verified"] is True)
    final = db.probe_database_state()
    check("C3 迁移后 schema_complete", final["schema_complete"] is True, str(final["error"]))
    check("C3 迁移后阶段号为 7", final["schema_version"] == 7, str(final["schema_version"]))
    row = read_memory(db.DB_PATH, 11)
    check("C3 老数据未丢", row is not None and "旧阶段库" in row[0], str(row))
    conn = sqlite3.connect(str(db.DB_PATH))
    try:
        val = conn.execute(
            "SELECT memory_status FROM memories WHERE id=11").fetchone()
    finally:
        conn.close()
    check("C3 迁移回填补上了 memory_status",
          val is not None and val[0] == "active", str(val))


def test_partial_init() -> None:
    """只建了一张表就中断的半截库。"""
    from app import db

    tmp = mktmp("fix03-c4-")
    db.DB_PATH = tmp / "agent.db"
    conn = sqlite3.connect(str(db.DB_PATH))
    try:
        conn.execute(
            "CREATE TABLE memories (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "content TEXT NOT NULL, category TEXT NOT NULL DEFAULT 'general', "
            "source TEXT NOT NULL DEFAULT 'manual', confidence REAL NOT NULL DEFAULT 0.8, "
            "status TEXT NOT NULL DEFAULT 'active', "
            "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
            "updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        conn.execute(
            "INSERT INTO memories(id,content,category,source,confidence,status) "
            "VALUES (12,'半截库里的数据','general','seed',0.8,'active')")
        conn.commit()
    finally:
        conn.close()

    probe = db.probe_database_state()
    check("C4 半截库判为不完整", probe["schema_complete"] is False)
    check("C4 半截库缺 13 张表",
          len(probe["missing_tables"]) == len(db.REQUIRED_TABLES) - 1,
          str(len(probe["missing_tables"])))

    res = db.init_db()
    check("C4 半截库进完整初始化", res["action"].startswith("full-init"), res["action"])
    check("C4 补齐后自校验通过", res["verified"] is True)
    final = db.probe_database_state()
    check("C4 补齐后 schema_complete", final["schema_complete"] is True, str(final["error"]))
    row = read_memory(db.DB_PATH, 12)
    check("C4 半截库原数据未丢", row is not None and "半截库" in row[0], str(row))


# ---------------------------------------------------------------------------
# D. 异常：不许静默通过，也不许破坏库
# ---------------------------------------------------------------------------
def test_corrupt_file() -> None:
    from app import db

    tmp = mktmp("fix03-d1-")
    db.DB_PATH = tmp / "broken.db"
    garbage = b"this is definitely not a sqlite database" * 20
    db.DB_PATH.write_bytes(garbage)
    before = fingerprint(db.DB_PATH)

    probe = db.probe_database_state()
    check("D1 损坏库 readable=False", probe["readable"] is False)
    check("D1 损坏库不报完整", probe["schema_complete"] is False)
    check("D1 损坏库错误信息非空", probe["error"] != "", probe["error"])
    check("D1 错误信息指明不是数据库",
          "not a database" in probe["error"].lower(), probe["error"])
    check("D1 损坏文件仍被判为存在（失败≠不存在）",
          probe["database_exists"] is True)

    raised = None
    try:
        db.init_db()
    except Exception as exc:      # noqa: BLE001 - 就是要捕获一切
        raised = exc
    check("D1 init_db 明确抛错而非静默通过", raised is not None,
          type(raised).__name__ if raised else "没有抛错")
    after = fingerprint(db.DB_PATH)
    check("D1 损坏文件未被清空/替换",
          after["size"] == len(garbage) and after["sha256"] == before["sha256"],
          diff_fp(before, after))


def test_directory_as_database() -> None:
    from app import db

    tmp = mktmp("fix03-d2-")
    fake = tmp / "adir.db"
    fake.mkdir()
    db.DB_PATH = fake

    probe = db.probe_database_state()
    check("D2 目录当库时 readable=False", probe["readable"] is False)
    check("D2 目录当库不算存在", probe["database_exists"] is False)
    check("D2 目录当库不报完整", probe["schema_complete"] is False)
    check("D2 目录未被写入内容", fake.is_dir() and not list(fake.iterdir()))


def test_lock_conflict() -> None:
    """持 EXCLUSIVE 锁时探测要么读到真结构，要么明确报错——不能瞎报完整。"""
    from app import db

    tmp = mktmp("fix03-d3-")
    db.DB_PATH = tmp / "agent.db"
    db.init_db()

    # 关掉 WAL，退回回滚日志模式，这样 EXCLUSIVE 才能真正挡住读。
    conn = sqlite3.connect(str(db.DB_PATH))
    holder = sqlite3.connect(str(db.DB_PATH), timeout=0.2)
    try:
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.commit()
        conn.close()
        holder.execute("PRAGMA busy_timeout=200")
        holder.execute("BEGIN EXCLUSIVE")
        holder.execute("UPDATE memories SET status='active' WHERE id=-1")

        probe = db.probe_database_state()
        if probe["readable"] is False:
            check("D3 锁冲突时探测明确报错", probe["error"] != "",
                  probe["error"])
            check("D3 锁冲突不误判完整", probe["schema_complete"] is False)
        else:
            check("D3 锁冲突时读到的是完整 schema",
                  probe["schema_complete"] is True,
                  str(probe["missing_tables"][:3]))
            check("D3 锁冲突时无错误", probe["error"] == "", probe["error"])
    finally:
        try:
            holder.rollback()
        except Exception:
            pass
        try:
            holder.close()
        except Exception:
            pass


def test_probe_internal_error() -> None:
    """探测内部抛错时，init_db 不能把它当「已完整」直接返回。"""
    from app import db

    tmp = mktmp("fix03-d4-")
    db.DB_PATH = tmp / "agent.db"
    db.init_db()

    orig_connect = sqlite3.connect

    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("simulated probe failure")

    sqlite3.connect = boom
    try:
        probe = db.probe_database_state()
        check("D4 探测异常写入 error", probe["error"] != "", probe["error"])
        check("D4 探测异常 readable=False", probe["readable"] is False)
        check("D4 探测异常不报完整", probe["schema_complete"] is False)

        raised = None
        try:
            res = db.init_db()
            check("D4 init_db 未走只读捷径",
                  res["action"] != "readonly-skip", res["action"])
        except Exception as exc:      # noqa: BLE001
            raised = exc
            check("D4 init_db 明确抛错", True, type(exc).__name__)
        if raised is None:
            check("D4 init_db 明确抛错或明确报错", True)
    finally:
        sqlite3.connect = orig_connect

    after = db.probe_database_state()
    check("D4 恢复后库仍完整", after["schema_complete"] is True, str(after["error"]))


def test_readonly_file_needs_migration() -> None:
    """需要迁移的库如果是只读文件，必须明确失败而不是假装成功。"""
    from app import db

    tmp = mktmp("fix03-d5-")
    db.DB_PATH = tmp / "ro.db"
    conn = sqlite3.connect(str(db.DB_PATH))
    try:
        conn.execute(
            "CREATE TABLE memories (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "content TEXT NOT NULL, category TEXT NOT NULL DEFAULT 'general', "
            "source TEXT NOT NULL DEFAULT 'manual', confidence REAL NOT NULL DEFAULT 0.8, "
            "status TEXT NOT NULL DEFAULT 'active', "
            "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
            "updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        conn.commit()
    finally:
        conn.close()

    raised = None
    try:
        os.chmod(db.DB_PATH, 0o444)
        try:
            db.init_db()
        except Exception as exc:      # noqa: BLE001
            raised = exc
        check("D5 只读库需要迁移时明确抛错", raised is not None,
              type(raised).__name__ if raised else "没有抛错")
        if raised is not None:
            check("D5 错误信息指明只读/权限",
                  any(k in str(raised).lower()
                      for k in ("readonly", "read-only", "unable", "permission")),
                  str(raised)[:120])
        final = db.probe_database_state()
        check("D5 只读库仍未被判为完整", final["schema_complete"] is False)
    finally:
        try:
            os.chmod(db.DB_PATH, 0o666)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# E. 并发初始化
# ---------------------------------------------------------------------------
def test_threads_concurrent_init() -> None:
    from app import db

    tmp = mktmp("fix03-e1-")
    db.DB_PATH = tmp / "agent.db"

    errors: list = []
    actions: list = []

    def worker() -> None:
        try:
            res = db.init_db()
            actions.append(res["action"])
        except Exception as exc:      # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)

    check("E1 双线程初始化无异常", not errors, str(errors[:2]))
    check("E1 双线程都返回了结果", len(actions) == 2, str(actions))
    final = db.probe_database_state()
    check("E1 并发后 schema 完整", final["schema_complete"] is True, str(final["error"]))
    check("E1 并发后无缺失项",
          not final["missing_tables"] and not final["missing_columns"]
          and not final["missing_indexes"] and not final["missing_triggers"])

    # 幂等：再来一次应当走只读
    res = db.init_db()
    check("E1 并发后再调走只读路径", res["action"] == "readonly-skip", res["action"])


def test_processes_concurrent_init() -> None:
    """两个独立进程同时初始化（模拟 Core 与 Governance 同时启动）。"""
    from app import db

    tmp = mktmp("fix03-e2-")
    target = tmp / "agent.db"

    env = dict(os.environ)
    env["BIONIC_DB_PATH"] = str(target)
    env["PYTHONPATH"] = str(ROOT)

    code = (
        "import sys;"
        "from app import db;"
        "r = db.init_db();"
        "print(r['action'])"
    )

    procs = [
        subprocess.Popen([sys.executable, "-c", code],
                         cwd=str(ROOT), env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for _ in range(2)
    ]
    outs = []
    for p in procs:
        out, err = p.communicate(timeout=300)
        outs.append((p.returncode, out.decode("utf-8", "replace").strip(),
                     err.decode("utf-8", "replace").strip()))

    check("E2 两个进程都成功退出", all(rc == 0 for rc, _, _ in outs),
          str([(rc, e[:80]) for rc, _, e in outs]))
    final = db.probe_database_state(target)
    check("E2 并发后 schema 完整", final["schema_complete"] is True, str(final["error"]))

    conn = sqlite3.connect(str(target))
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(memories)")]
    finally:
        conn.close()
    check("E2 无重复列", len(cols) == len(set(cols)), str(cols))
    check("E2 memories 列数与预期一致",
          len(cols) == len(db.REQUIRED_COLUMNS["memories"]),
          f"{len(cols)} vs {len(db.REQUIRED_COLUMNS['memories'])}")

    env2 = dict(env)
    again = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), env=env2,
                           capture_output=True, timeout=300)
    check("E2 第三个进程走只读路径",
          "readonly-skip" in again.stdout.decode("utf-8", "replace"),
          again.stdout.decode("utf-8", "replace").strip()[:80])


def test_init_db_does_not_touch_other_dbs() -> None:
    """init_db 只能动它自己的库：换一个 DB_PATH 不该碰到上一个库。"""
    from app import db

    tmp = mktmp("fix03-e3-")
    db.DB_PATH = tmp / "one.db"
    db.init_db()
    one_before = fingerprint(db.DB_PATH)

    db.DB_PATH = tmp / "two.db"
    db.init_db()

    one_after = fingerprint(db.DB_PATH.parent / "one.db")
    check("E3 初始化另一张库不影响第一张",
          same_fp(one_before, one_after), diff_fp(one_before, one_after))


# ---------------------------------------------------------------------------
def main() -> int:
    prod = Path(ROOT) / "data" / "agent.db"
    prod_before = (prod.stat().st_size, prod.stat().st_mtime_ns) if prod.is_file() else None

    from app import db

    test_initialized_is_readonly()
    test_readonly_path_executes_no_write_sql()
    test_new_database()
    test_missing_table()
    test_missing_column()
    test_old_phase_schema()
    test_partial_init()
    test_corrupt_file()
    test_directory_as_database()
    test_lock_conflict()
    test_probe_internal_error()
    test_readonly_file_needs_migration()
    test_threads_concurrent_init()
    test_processes_concurrent_init()
    test_init_db_does_not_touch_other_dbs()

    prod_after = (prod.stat().st_size, prod.stat().st_mtime_ns) if prod.is_file() else None
    check("生产库未被本次测试触碰", prod_before == prod_after,
          f"{prod_before} -> {prod_after}")

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = len(RESULTS) - passed
    print(f"\n{'=' * 60}")
    print(f"PASS {passed} / FAIL {failed} / 共 {len(RESULTS)} 项")
    if failed:
        print("\n失败项：")
        for name, ok, detail in RESULTS:
            if not ok:
                print(f"  [FAIL] {name}" + (f" — {detail}" if detail else ""))
    return 0 if failed == 0 else 1


def _cleanup() -> None:
    for d in _TMPS:
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    try:
        rc = main()
    finally:
        _cleanup()
    sys.exit(rc)
