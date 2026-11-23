# -*- coding: utf-8 -*-
"""测试环境的数据库隔离闸门（OPT-04a）。

**必须在任何 `app.*` 导入之前 import 本模块。** 一行就够::

    import _env_guard  # noqa: F401  ← 必须在任何 app.* 之前

## 为什么需要它

`app/db.py` 在**模块导入时**就把 `DB_PATH` 定死了::

    DB_PATH = Path(os.environ.get("BIONIC_DB_PATH")
                   or (Path(__file__).resolve().parent.parent / "data" / "agent.db"))

所以「先 import app.db、再改环境变量」是无效的——进程已经拿着生产库路径了。
历史事故（OPT-02 §11.3）：`lexical_test.py` 直接跑时没设环境变量，它调用真工具
`semantic_search_memories(auto_index=True)`，于是 `index_memories()` 把缺失的向量
写进了 `data/agent.db`（`memory_embeddings` 51 → 52）。

## 做了什么

1. 若 `BIONIC_DB_PATH` 已被显式设置**且指向的是别的库**（调用方自己隔离），一律不插手。
   **但它指着生产库时不算「已隔离」**——那种情况下本模块会覆盖它并沿用沙箱，
   因为「环境变量非空、看着像配好了、实际就是 data/agent.db」是最危险的伪隔离。
2. 否则建 `<项目根>/var/test_db/<名字>-<pid>-<随机>/`，用 SQLite 的在线备份 API
   把生产库复制进去，然后把 `BIONIC_DB_PATH` 指过去。
3. 顺带把 `BIONIC_SNAPSHOT_DIR` 也指进这个沙箱，避免快照落在 `data/snapshots/`。
4. `atexit` 删掉沙箱；启动时顺手清理僵尸沙箱（进程已死或超过 2 小时）。

## 三个刻意的设计选择

**（一）为什么复制生产库，而不是用空库**

`lexical_test.py` 的主基准是「拿每条记忆自己的标题当查询、看能不能把自己找回来」，
它需要真实的记忆语料；空库会让 48 道题全部无锚点，测试直接失去意义。复制一份
生产库既保住语义，又保证写入落在副本上。**复制 ≠ 使用生产库**：写进副本的每一个
字节都在 `var/test_db/` 里，退出即删。

**（二）为什么沙箱必须放在项目根内**

`app/portable_backup.py` 的 `_resolve_package_dir()` 默认要求包目录位于项目根内
（防任意路径写入）。包目录默认取 `Path(db.DB_PATH).parent / "memory_packages"`，
所以把库放到系统 temp 目录会让**所有备份/恢复相关测试报
「包目录必须位于项目根内」**——实测 `admin_backup_restore_test.py` 由 104 PASS/0 FAIL
变成 100 PASS/4 FAIL、`portable_restore_mcp_test.py` 直接抛 `AttributeError`。
放 `var/test_db/` 下就绕开了这个连锁反应，且 `var/` 本来就在未跟踪区。

**（三）为什么复制走 `immutable=1` 只读连接**

普通 `mode=ro` 打开一个 WAL 模式的库会在旁边建 `-shm`/`-wal`，那就等于动了生产库
的目录（S6-FIX-03 已实测）。`immutable=1` 保证零副作用。代价是读不到未合并的
`-wal`——所以这里先看一眼 `-wal` 有没有实际内容：有内容的库走 `mode=ro`（会短暂
产生 sidecar，但那是**已经存在的** WAL 状态，不是我们创造的），没有就走 immutable。

## 不归它管的事

- 各测试文件自己设的 `BIONIC_DB_PATH` / `db.DB_PATH`：优先级更高，本模块不覆盖。
- `tests/run_tests.py`：它不 import `app.*`，但会给每个子进程**清掉**
  `BIONIC_DB_PATH`，保证子进程各自重新走一遍本模块，而不是共享父进程那一个沙箱
  （共享会让第 2 个测试看到第 1 个测试留下的数据，那是另一种污染）。
"""
from __future__ import annotations

import atexit
import os
import shutil
import sqlite3
import sys
import time
import uuid
from pathlib import Path

__all__ = [
    "ENV_DB_PATH", "ENV_SNAPSHOT_DIR", "SANDBOX_ROOT", "PRODUCTION_DB",
    "is_isolated", "sandbox_dir", "production_sha256", "main",
]

ENV_DB_PATH = "BIONIC_DB_PATH"
ENV_SNAPSHOT_DIR = "BIONIC_SNAPSHOT_DIR"

TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parent
SANDBOX_ROOT = ROOT / "var" / "test_db"
PRODUCTION_DB = ROOT / "data" / "agent.db"

# 超过这个秒数的沙箱认为是崩死留下的，启动时清掉
STALE_SECONDS = 2 * 60 * 60

_created: Path | None = None


def _wal_has_content(db_path: Path) -> bool:
    """旁边有没有非空 `-wal`（决定能不能用 immutable 打开）。"""
    wal = db_path.with_name(db_path.name + "-wal")
    try:
        return wal.is_file() and wal.stat().st_size > 0
    except OSError:
        return False


def _readonly_uri(db_path: Path, *, immutable: bool = True) -> str:
    """构造只读 URI（path 段已编码，避免 `#`/`%`/`?` 被当成 URI 语法）。"""
    from urllib.parse import quote

    p = db_path.resolve().as_posix()
    suffix = "?mode=ro&immutable=1" if immutable else "?mode=ro"
    return "file:" + quote(p, safe="/:") + suffix


def production_sha256() -> str:
    """生产库当前 SHA-256（不存在时返回空串）。只读文件，不连接数据库。"""
    import hashlib

    if not PRODUCTION_DB.is_file():
        return ""
    h = hashlib.sha256()
    with open(PRODUCTION_DB, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest().upper()


def _pid_alive(pid: int) -> bool:
    """进程是否还活着。

    **不能用 `os.kill(pid, 0)`**。Windows 上它没有「只探测」语义：`CTRL_C_EVENT`
    的数值恰好是 0，所以 `sig=0` 走的是「发控制台事件」那一支
    （`GenerateConsoleCtrlEvent(CTRL_C_EVENT, pid)`），`pid` 那里要的是**进程组 ID**，
    不是进程 ID，于是「命令是否失败」和「目标是否存活」根本不对应。

    实测（Python 3.13.13 / Windows 11），对一个**已 kill 并 wait 完毕**的 pid：

    - `os.kill(该 pid, 0)` → 正常返回 = 被判成「活着」
    - `os.kill(从未存在过的 pid, 0)` → `OSError [WinError 87] 参数错误`

    即：pid 只要还在系统进程表里（含刚退出尚未回收的），它就返回成功。
    **结果是「死进程被判活」，而不是反过来。** 这个错误方向在别处会造成
    比误删更难受的问题——`app/portable_restore.py:1465` 的僵尸锁检测就是这么写的，
    于是恢复进程崩溃留下的 `.active.lock` 永远回收不掉，下次恢复被永久拒绝
    （已实测，见报告「遗留风险 R1」）。本模块不能沿用这种写法。

    顺带更正一条我曾写错的判断：早先这里写着「成功时真的投递了 Ctrl+C，
    所以会打断同组进程」。后来用独立进程组的对照实验去验（目标同组非组长／
    独立组组长／已死／从未存在四种，全部由第三方进程调用 `os.kill`），
    目标进程**没有一个被打断**，因此那条因果未获证实，已从注释里撤掉。

    正确做法是逐平台分开：Windows 用
    `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)` + `GetExitCodeProcess`
    判 `STILL_ACTIVE`（只查询、不发任何信号）；POSIX 上 `os.kill(pid, 0)`
    才是真的探测，可以放心用。
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _sandbox_pid(name: str) -> int:
    """从沙箱目录名里取 pid。

    目录名格式固定在下面 `main()` 里：`f"{label}-{os.getpid()}-{uuid8}"`，
    所以 pid 永远是**倒数第二段**。早先的实现是「从左往右找第一个全数字段」，
    那在 label 本身全为数字时会取错；按位置取没有这个问题。
    """
    parts = name.split("-")
    if len(parts) < 2:
        return 0
    try:
        return int(parts[-2])
    except (TypeError, ValueError):
        return 0


def _prune_stale() -> None:
    """清掉崩死留下的沙箱（进程已死、或超过 STALE_SECONDS）。"""
    if not SANDBOX_ROOT.is_dir():
        return
    now = time.time()
    for child in SANDBOX_ROOT.iterdir():
        try:
            if not child.is_dir():
                continue
            age = now - child.stat().st_mtime
            pid = _sandbox_pid(child.name)
            alive = _pid_alive(pid) if pid > 0 else False
            if not alive or age > STALE_SECONDS:
                shutil.rmtree(child, ignore_errors=True)
        except OSError:
            continue


def _seed_from_production(dst: Path) -> str:
    """把生产库在线备份到 dst。返回 "copied" / "no-production-db"。"""
    if not PRODUCTION_DB.is_file():
        return "no-production-db"
    immutable = not _wal_has_content(PRODUCTION_DB)
    src = sqlite3.connect(_readonly_uri(PRODUCTION_DB, immutable=immutable), uri=True,
                          timeout=10)
    try:
        dst_conn = sqlite3.connect(str(dst), timeout=10)
        try:
            with dst_conn:
                src.backup(dst_conn)
            # 副本改成 DELETE 日志模式：退出时删得干净，也不会留 -wal/-shm
            dst_conn.execute("PRAGMA journal_mode=DELETE")
        finally:
            dst_conn.close()
    finally:
        src.close()
    return "copied"


def sandbox_dir() -> Path | None:
    """本进程的沙箱目录；未启用隔离时返回 None。"""
    return _created


def is_isolated() -> bool:
    """当前进程的 `db.DB_PATH` 是否还指着生产库。

    只做**路径判断**，不 import `app.db`（导入它正是我们要防的事）。
    """
    raw = (os.environ.get(ENV_DB_PATH) or "").strip()
    if not raw:
        return False
    try:
        return Path(raw).resolve() != PRODUCTION_DB.resolve()
    except OSError:
        return True


def _points_at_production(raw: str) -> bool:
    """这个环境变量值是不是就指着生产库。

    三种都算：绝对路径、相对路径（相对当前工作目录）、大小写/分隔符不同的写法。
    用 `Path.resolve()` 归一，避免 `data\\agent.db` 与 `data/agent.db`、`DATA/AGENT.DB`
    这类写法被漏判——Windows 上它们指向同一个文件。
    """
    if not raw:
        return False
    try:
        return Path(raw).resolve() == PRODUCTION_DB.resolve()
    except (OSError, RuntimeError):
        return False


def _cleanup(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def main() -> str:
    """启用隔离。返回一行人话说明（供测试打印）。

    幂等：重复调用直接返回上次状态。
    """
    global _created
    if _created is not None:
        return f"测试沙箱已就绪：{_created}"

    pre = (os.environ.get(ENV_DB_PATH) or "").strip()
    if pre and not _points_at_production(pre):
        # 调用方指向的是**别的**库：尊重它（各测试文件里的显式设置、CI 的明确意图）
        return f"沿用调用方指定的隔离库：{pre}"
    if pre:
        # 调用方把库指成了生产库。这是最危险的一种「以为隔离了」：
        # 环境变量非空、看着像配好了，实际 DB_PATH 就是 data/agent.db。
        # 不接手的话，测试会直接读写生产库——正是本模块要防的事。
        override_note = f"（覆盖了调用方设成生产库的 {ENV_DB_PATH}={pre}）"
        os.environ.pop(ENV_DB_PATH, None)
    else:
        override_note = ""

    _prune_stale()
    SANDBOX_ROOT.mkdir(parents=True, exist_ok=True)

    label = Path(sys.argv[0]).stem or "session"
    label = "".join(ch if (ch.isalnum() or ch in "-_") else "-" for ch in label)[:40]
    box = SANDBOX_ROOT / f"{label}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    box.mkdir(parents=True, exist_ok=True)

    db_file = box / "agent.db"
    seeded = _seed_from_production(db_file)

    os.environ[ENV_DB_PATH] = str(db_file)
    os.environ.setdefault(ENV_SNAPSHOT_DIR, str(box / "snapshots"))

    _created = box
    atexit.register(_cleanup, box)
    return f"测试沙箱：{box}（{seeded}）{override_note}"


# import 即生效——这正是「必须在 app.* 之前 import」的兑现方式
_status = main()

if os.environ.get("BIONIC_TEST_VERBOSE"):
    print(f"[env_guard] {_status}", file=sys.stderr)
