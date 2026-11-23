"""Multi-process concurrency regression tests for the SQLite layer.

Why this file exists: `init_db()` used to migrate columns with an inline
"read PRAGMA table_info, then ALTER" pair. Two processes starting at the same
moment on the same new file both observed the column missing, both ran the
ALTER, and the loser died with:

    sqlite3.OperationalError: duplicate column name: supersedes_id

That is a startup crash, not a theoretical race -- it reproduced with 6
processes where 3 failed. These tests pin the fix so it cannot silently come
back. They are written to fail loudly rather than skip: a concurrency test that
silently skips is worse than none.

Note on the lock test: it must not be self-deadlocking. An earlier version had
the *holding* process open a write transaction and then start a second
connection that would wait for its own lock -- SQLite cannot grant that, it
just times out. The holder and the waiter therefore live in separate processes.

Run from the project root:

    python tests/concurrency_test.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
import _env_guard  # noqa: E402,F401  OPT-04a：数据库隔离闸门（必须在任何 app.* 之前）

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Each worker initialises the database from an empty file. If the migration is
# not race-safe the losers crash here.
_INIT_WORKER = """
import sys
sys.path.insert(0, r"{root}")
from app import mcp_server
mcp_server._ensure_db()
print("ok")
"""

# `save_memory` returns the memory dict itself (not {"ok": True}); a failure is
# a dict carrying "ok": False. Counting on "ok" alone silently reported 0 rows.
_WRITE_WORKER = """
import sys, json
sys.path.insert(0, r"{root}")
from app import mcp_server
tag = "{tag}"
ok = 0
for i in range({count}):
    r = mcp_server.save_memory(
        content="并发写入测试 {tag} 第 " + str(i) + " 条",
        category="test", source="concurrency_test",
        tags=[tag, "concurrency"],
    )
    if r and r.get("ok") is False:
        continue
    if r and r.get("id") is not None:
        ok += 1
print(json.dumps({{"ok": ok}}))
"""

# Holder keeps a write transaction open for `hold` seconds, then rolls back.
# It writes `ready_file` only AFTER the write lock is actually held, so the test
# never has to guess how long importing + init_db() takes.
_HOLDER = """
import sqlite3, sys, time, pathlib
sys.path.insert(0, r"{root}")
from app import db
db.init_db()
conn = sqlite3.connect(db.DB_PATH, timeout=30)
conn.execute("BEGIN IMMEDIATE")
conn.execute("INSERT INTO memories(content, category) VALUES('holder','test')")
pathlib.Path(r"{ready_file}").write_text("locked", encoding="utf-8")
time.sleep({hold})
conn.rollback()
conn.close()
print("holder done")
"""

# Waiter starts a few hundred ms later and must WAIT for the lock instead of
# failing immediately with "database is locked".
_WAITER = """
import sqlite3, sys, time, json
sys.path.insert(0, r"{root}")
from app import db
t0 = time.time()
with db.transaction() as conn:
    conn.execute("INSERT INTO memories(content, category) VALUES('waiter','test')")
print(json.dumps({{"waited": time.time() - t0}}))
"""

_RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, passed: bool, detail: str = "") -> None:
    _RESULTS.append((name, passed, detail))
    mark = "PASS" if passed else "FAIL"
    print(f"[{mark}] {name}" + (f" — {detail}" if detail else ""))


def run_worker(code: str, env: dict, timeout: int = 180) -> tuple[int, str, str]:
    proc = subprocess.run(
        [sys.executable, "-c", code],
        env=env, capture_output=True, timeout=timeout,
    )
    return (
        proc.returncode,
        proc.stdout.decode("utf-8", "replace"),
        proc.stderr.decode("utf-8", "replace"),
    )


def _env(db_path: Path, **extra) -> dict:
    base = dict(os.environ, BIONIC_DB_PATH=str(db_path), PYTHONUTF8="1")
    base.update({k: str(v) for k, v in extra.items()})
    return base


def test_concurrent_init() -> None:
    """N processes initialising the same empty database must all succeed."""
    with tempfile.TemporaryDirectory(prefix="bionic-conc-init-") as td:
        db_path = Path(td) / "agent.db"
        env = _env(db_path)
        code = _INIT_WORKER.format(root=PROJECT_ROOT)
        procs = [
            subprocess.Popen([sys.executable, "-c", code], env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            for _ in range(6)
        ]
        results = [p.communicate(timeout=180) + (p.returncode,) for p in procs]
        failed = [
            r for r in results
            if r[2] != 0 or "ok" not in r[0].decode("utf-8", "replace")
        ]
        detail = ""
        if failed:
            msg = failed[0][1].decode("utf-8", "replace").strip().splitlines()
            detail = msg[-1] if msg else f"exit={failed[0][2]}"
        check("6 个进程同时初始化同一个空库，全部成功",
              not failed,
              f"{len(results) - len(failed)}/{len(results)} 成功" if not failed else detail)

        code = (
            "import sys; sys.path.insert(0, r'%s')\n"
            "from app import db\n"
            "with db.connect() as c:\n"
            "    t = {r[0] for r in c.execute(\"SELECT name FROM sqlite_master WHERE type='table'\")}\n"
            "    cols = {r[1] for r in c.execute('PRAGMA table_info(memories)')}\n"
            "    print('tables', len(t))\n"
            "    print('links', 'memory_links' in t)\n"
            "    print('supersedes_id', 'supersedes_id' in cols)\n"
            "    print('feed', 'memory_feedback' in t)\n"
            "    print('integrity', c.execute('PRAGMA integrity_check').fetchone()[0])\n"
        ) % PROJECT_ROOT
        rc, out, err = run_worker(code, env)
        check("竞争之后数据库结构完整",
              rc == 0 and "integrity ok" in out,
              out.strip().replace("\n", " ") or err.strip()[-200:])


def test_concurrent_writes() -> None:
    """Four processes writing at once must not lose or corrupt rows."""
    with tempfile.TemporaryDirectory(prefix="bionic-conc-write-") as td:
        db_path = Path(td) / "agent.db"
        env = _env(db_path)
        rc, _, err = run_worker(_INIT_WORKER.format(root=PROJECT_ROOT), env)
        if rc != 0:
            check("并发写入前置建库", False, err.strip()[-200:])
            return

        workers, per_worker = 4, 40
        procs = []
        for i in range(workers):
            c = _WRITE_WORKER.format(root=PROJECT_ROOT, tag=f"w{i}", count=per_worker)
            procs.append(subprocess.Popen(
                [sys.executable, "-c", c], env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            ))
        written = 0
        errs = []
        for p in procs:
            o, e = p.communicate(timeout=300)
            if p.returncode != 0:
                errs.append(e.decode("utf-8", "replace").strip()[-200:])
                continue
            for line in o.decode("utf-8", "replace").splitlines():
                line = line.strip()
                if line.startswith("{"):
                    written += json.loads(line).get("ok", 0)

        expected = workers * per_worker
        check("4 进程并发写入无报错", not errs, errs[0] if errs else "errs=0")
        check("写入条数完整（无丢失）", written == expected, f"{written}/{expected} 条")

        code = (
            "import sys; sys.path.insert(0, r'%s')\n"
            "from app import db\n"
            "with db.connect() as c:\n"
            "    n = c.execute(\"SELECT COUNT(*) FROM memories WHERE source='concurrency_test'\").fetchone()[0]\n"
            "    print('rows', n)\n"
            "    print('integrity', c.execute('PRAGMA integrity_check').fetchone()[0])\n"
            "    print('fk', len(c.execute('PRAGMA foreign_key_check').fetchall()))\n"
        ) % PROJECT_ROOT
        rc, out, err = run_worker(code, env)
        check("写入后完整性检查通过",
              rc == 0 and "integrity ok" in out and "fk 0" in out and f"rows {expected}" in out,
              out.strip().replace("\n", " ") or err.strip()[-200:])


def test_busy_timeout_actually_waits() -> None:
    """A writer blocked by another process's write lock must WAIT, not fail fast.

    Holder and waiter are separate processes: SQLite can never grant a lock to
    a connection that is itself holding it, so a single-process version would
    just deadlock and prove nothing.
    """
    with tempfile.TemporaryDirectory(prefix="bionic-conc-lock-") as td:
        db_path = Path(td) / "agent.db"
        env = _env(db_path)
        rc, _, err = run_worker(_INIT_WORKER.format(root=PROJECT_ROOT), env)
        if rc != 0:
            check("持锁等待测试前置建库", False, err.strip()[-200:])
            return

        hold_seconds = 3
        ready_file = Path(td) / "lock-ready"
        holder = subprocess.Popen(
            [sys.executable, "-c", _HOLDER.format(
                root=PROJECT_ROOT, hold=hold_seconds, ready_file=ready_file)],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        # Wait for the holder to actually own the lock. A fixed sleep is a
        # timing assumption: under load the holder can still be importing while
        # the waiter races ahead and finishes instantly, failing the test on
        # correct code. 30s cap so a broken holder cannot hang the suite.
        deadline = time.time() + 30
        while time.time() < deadline and not ready_file.exists():
            if holder.poll() is not None:
                break
            time.sleep(0.05)
        if not ready_file.exists():
            holder.communicate(timeout=30)
            check("持锁等待测试前置：holder 真拿到写锁", False,
                  "holder 未能在 30s 内取得写锁（它已经退出或卡住）")
            return
        t0 = time.time()
        rc_w, out_w, err_w = run_worker(
            _WAITER.format(root=PROJECT_ROOT), env, timeout=120)
        elapsed = time.time() - t0
        holder.communicate(timeout=60)

        waited = None
        if rc_w == 0:
            for line in out_w.splitlines():
                line = line.strip()
                if line.startswith("{"):
                    waited = json.loads(line).get("waited")
        check("被另一个进程的写锁挡住时是等待而不是立刻失败",
              rc_w == 0 and waited is not None and waited > 0.5,
              f"等了 {waited:.1f}s（进程总耗时 {elapsed:.1f}s）" if waited is not None
              else err_w.strip()[-200:])


def test_busy_timeout_configurable() -> None:
    """BIONIC_DB_BUSY_TIMEOUT must reach the actual PRAGMA."""
    with tempfile.TemporaryDirectory(prefix="bionic-conc-bto-") as td:
        db_path = Path(td) / "agent.db"
        env = _env(db_path, BIONIC_DB_BUSY_TIMEOUT=7)
        code = (
            "import sys; sys.path.insert(0, r'%s')\n"
            "from app import db\n"
            "db.init_db()\n"
            "with db.connect() as c:\n"
            "    print('bto', c.execute('PRAGMA busy_timeout').fetchone()[0])\n"
            "    print('journal', c.execute('PRAGMA journal_mode').fetchone()[0])\n"
        ) % PROJECT_ROOT
        rc, out, err = run_worker(code, env)
        check("busy_timeout 可配置且生效", rc == 0 and "bto 7000" in out,
              out.strip().replace("\n", " ") or err.strip()[-200:])
        check("journal_mode 为 wal", "journal wal" in out,
              out.strip().splitlines()[-1] if out else "")


def run() -> int:
    print("=== 并发/多进程回归 ===")
    test_concurrent_init()
    test_concurrent_writes()
    test_busy_timeout_actually_waits()
    test_busy_timeout_configurable()
    failed = [r for r in _RESULTS if not r[1]]
    print(f"\n=== {len(_RESULTS) - len(failed)}/{len(_RESULTS)} 通过 ===")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(run())
