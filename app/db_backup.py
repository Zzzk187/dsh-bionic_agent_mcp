"""SQLite 数据库物理备份（S6-FIX-02，对应审计 P1-3）。

这个模块解决什么问题
--------------------
`data/snapshots/` 里的 `snap-*.json` 是**逻辑** dump：把各表行导出成 JSON。
`bin/备份.ps1` 压的是本地 `dsh-memory` 目录。两者各有用途，但都不包含
`agent.db` 自身的二进制镜像——而库里有一批**只能从库本身重建**的数据：
DB-only memories（例如 id=4 冰美式偏好、id=5 中文文档偏好）、治理记分、
冲突裁决记录、关系图。逻辑 dump 能否还原它们，取决于 dump 时的表结构与
字段是否恰好完整；磁盘坏道、误删、SQLite 页损坏这类事故，缺一个真镜像
就只能靠运气。`data/backups/` 里那 4 个手工 `.bak` 停在 2026-10-30，是临时
处置，不是机制。

所以这里补**物理**备份：用 SQLite 官方 `Connection.backup()` 拍真镜像。

为什么不能用复制文件
--------------------
库是 WAL 模式，`agent.db` 可能落后于 `-wal` 里已提交的事务。分别 copy
`agent.db` 和 `-wal` 属于「先读 A 再读 B」，中间只要有写入就是不一致镜像；
只 copy `agent.db` 则会丢掉最近的已提交事务。Backup API 在 SQLite 内部按页
复制，并在源库被其他进程写入时重启备份，产出的是某一时刻的统一视图。
这正是"数据库正常运行期间也能备份"的手段。

安全约束（本模块恪守）
----------------------
* 源库只用 `mode=ro` 只读 URI 打开，**绝不写生产库**；不复制 `-wal`/`-shm`。
* 先写同目录下的 `.partial-<uuid>` 临时文件，验证通过才 `os.replace` 成正式
  文件；验证失败立即删除临时文件，绝不留下半成品让人误当备份。
* 清理只删**本工具**产物：严格文件名正则 + 目录归属 resolve 校验 + 跳过
  符号链接。新备份验证失败时根本不调用清理。
* 清理自身出错不影响「备份成功」这个结论——两件事分开报告。

时间口径：文件名用**本地时间** `YYYYMMDD_HHMMSS`。中国无夏令时，字符串
字典序与时间序一致，所以排序稳定；跨时区搬迁后旧文件名仍是可读的历史
标记，不做改写。

依赖：仅标准库，外加复用 `app.db` 的库路径与忙等待配置。
"""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from . import db

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
# 默认保留最近 7 份**有效**物理备份（每日一次 ≈ 一周回滚窗口）
DEFAULT_KEEP = 7

# 备份子目录（相对库文件所在目录）。复用 data/backups/，它已在 .gitignore 里。
BACKUP_SUBDIR = "backups"

# 文件名：agent_phys_20261119_050000.db，并发撞同一秒时补 _001/_002
FILE_PREFIX = "agent_phys_"
FILE_RE = re.compile(r"^agent_phys_(\d{8})_(\d{6})(?:_(\d{2,3}))?\.db$")

# 临时文件后缀。刻意不符合 FILE_RE，因此永远不会被当成备份列出或清理。
PARTIAL_SUFFIX = ".partial-"

HASH_CHUNK = 1024 * 1024


class BackupError(RuntimeError):
    """备份/清理过程中可预期的失败。消息面向人，不含 traceback。"""


# ---------------------------------------------------------------------------
# 路径与命名
# ---------------------------------------------------------------------------
def backup_dir() -> Path:
    """备份目录：默认与库文件同级的 backups/。

    跟着 `db.DB_PATH` 走，所以 `BIONIC_DB_PATH` 换了库（测试、第二实例）时
    备份也跟着换目录，不会把测试产物写进生产 data/backups/。
    """
    return Path(db.DB_PATH).resolve().parent / BACKUP_SUBDIR


def is_backup_name(name: str) -> bool:
    """严格判定：这个名字是不是本工具生成的备份。

    这是清理阶段的**唯一**信任来源。故意锚定整串（`^...$`），
    所以 `agent.db.before-cleanup-20261030-220741` 这类历史手工文件、
    `.partial-*` 临时文件、以及任何别的 `.db` 都不会命中。
    """
    return bool(FILE_RE.match(name))


def _stamp(path: Path) -> str:
    """排序键：完整文件名即字典序时间序（无夏令时前提下）。"""
    m = FILE_RE.match(path.name)
    return m.group(0) if m else ""


def _choose_target(base: Path, stamp: str) -> Path:
    """挑一个尚未占用的正式文件名。

    同一秒内并发备份（两个进程同时到点）会撞名；`os.replace` 会覆盖，
    等于悄悄丢一份备份。所以先探测，撞了就用 _001/_002 递增后缀。
    """
    cand = base / f"{FILE_PREFIX}{stamp}.db"
    if not cand.exists():
        return cand
    for i in range(1, 1000):
        cand = base / f"{FILE_PREFIX}{stamp}_{i:03d}.db"
        if not cand.exists():
            return cand
    raise BackupError(f"同一秒内的备份文件过多（>1000）：{base}")


def list_backups(directory: Path | None = None) -> list[Path]:
    """列出本工具生成的备份，按新→旧。

    符号链接一律排除：它可能指向备份目录之外，既不该被当成有效备份，
    更不该进入清理候选。
    """
    base = Path(directory).resolve() if directory else backup_dir()
    if not base.is_dir():
        return []
    out: list[Path] = []
    for p in base.iterdir():
        if p.is_symlink() or not p.is_file():
            continue
        if not is_backup_name(p.name):
            continue
        out.append(p)
    out.sort(key=_stamp, reverse=True)
    return out


def _readonly_uri(path: Path) -> str:
    """构造 sqlite 只读 URI。

    Windows 下 `D:/a/b.db` 要变成 `file:/D:/a/b.db?mode=ro`：
    绝对路径在 URI 里必须有前导斜杠，反斜杠要换成斜杠，特殊字符要转义，
    否则路径里的 `?` 或 `#` 会被当成查询串/锚点。
    """
    p = str(Path(path).resolve()).replace("\\", "/")
    if not p.startswith("/"):
        p = "/" + p
    return "file:" + quote(p, safe="/:") + "?mode=ro"


def _immutable_uri(path: Path) -> str:
    """只读 + immutable。

    仅用于校验**本模块刚产出的备份文件**：它没有 `-wal`、也不会被别的进程
    写，所以 `immutable=1` 是安全的。WAL 模式的库在 `mode=ro` 下若缺 `-shm`
    可能打不开，这是兜底路径。
    """
    p = str(Path(path).resolve()).replace("\\", "/")
    if not p.startswith("/"):
        p = "/" + p
    return "file:" + quote(p, safe="/:") + "?mode=ro&immutable=1"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(HASH_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------
def verify_backup(path: Path) -> dict:
    """校验一份备份是不是「可用」的。

    四项硬条件（任务 §二.6）：文件存在、大小 > 0、integrity_check = ok、
    foreign_key_check 无违规。任一不满足即 ok=False，调用方不得把它当成
    有效备份，更不得在它失败时清理历史备份。
    """
    p = Path(path)
    res: dict = {"ok": False, "path": str(p), "size": 0, "integrity": "",
                 "foreign_key_violations": None, "message": "", "mode": ""}

    if not p.is_file():
        res["message"] = "备份文件不存在"
        return res
    try:
        size = p.stat().st_size
    except OSError as exc:
        res["message"] = f"读不到文件大小：{exc}"
        return res
    res["size"] = size
    if size <= 0:
        res["message"] = "备份文件大小为 0"
        return res

    conn = None
    last_err = ""
    for mode, uri in (("ro", _readonly_uri(p)), ("immutable", _immutable_uri(p))):
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=db.BUSY_TIMEOUT_SECONDS)
            conn.execute("PRAGMA busy_timeout=%d" % int(db.BUSY_TIMEOUT_SECONDS * 1000))
            conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
            res["mode"] = mode
            break
        except sqlite3.Error as exc:
            last_err = str(exc)
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
                conn = None
    if conn is None:
        res["message"] = f"备份文件无法作为 SQLite 库打开：{last_err}"
        return res

    try:
        rows = [r[0] for r in conn.execute("PRAGMA integrity_check").fetchall()]
        integrity = "ok" if rows == ["ok"] else " | ".join(str(r) for r in rows)
        res["integrity"] = integrity
        if integrity != "ok":
            res["message"] = f"integrity_check 未通过：{integrity[:200]}"
            return res

        fk = conn.execute("PRAGMA foreign_key_check").fetchall()
        res["foreign_key_violations"] = len(fk)
        if fk:
            sample = [tuple(r) for r in fk[:3]]
            res["message"] = f"foreign_key_check 发现 {len(fk)} 条违规，例如 {sample}"
            return res
    except sqlite3.Error as exc:
        res["message"] = f"校验查询失败：{exc}"
        return res
    finally:
        try:
            conn.close()
        except Exception:
            pass

    res["ok"] = True
    res["message"] = "校验通过"
    return res


# ---------------------------------------------------------------------------
# 备份
# ---------------------------------------------------------------------------
def create_physical_backup(
    keep: int = DEFAULT_KEEP,
    dry_run: bool = False,
    db_path: Path | str | None = None,
    target_dir: Path | str | None = None,
) -> dict:
    """拍一张物理备份并校验，然后清理旧备份。

    参数都可覆盖，测试因此不必碰生产库。返回 dict，`ok` 是唯一权威结论：

        {"ok": True,  "path", "bytes", "sha256", "verified": {...},
         "pruned": [...], "kept": n, "message"}
        {"ok": False, "message", ...}                     # 失败必带原因

    执行顺序是刻意排的：**先备份、再校验、最后清理**。校验没过就直接
    返回，`prune_backups` 根本不会被调用——这样「新备份坏了」永远不会
    演变成「旧的也没了」。
    """
    try:
        keep = int(keep)
    except (TypeError, ValueError):
        return {"ok": False, "message": f"keep 必须是正整数，收到 {keep!r}"}
    if keep < 1:
        return {"ok": False, "message": f"keep 必须 >= 1，收到 {keep}"}

    src = Path(db_path).resolve() if db_path else Path(db.DB_PATH).resolve()
    base = Path(target_dir).resolve() if target_dir else src.parent / BACKUP_SUBDIR

    res: dict = {
        "ok": False,
        "source": str(src),
        "backup_dir": str(base),
        "keep": keep,
        "message": "",
    }

    # 源库必须是已存在的普通文件。这一条**不能省**：sqlite3.connect() 默认
    # 会创建空文件，省掉它就会「备份成功」出一个空库。
    if not src.is_file():
        res["message"] = f"源数据库不存在或不是普通文件：{src}"
        return res

    if dry_run:
        existing = list_backups(base)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        plan = {
            "ok": True,
            "dry_run": True,
            "source": str(src),
            "backup_dir": str(base),
            "would_write": str(_choose_target(base, stamp)) if base.is_dir() else str(base / f"{FILE_PREFIX}{stamp}.db"),
            "existing": len(existing),
            "existing_names": [p.name for p in existing],
            "keep": keep,
            "would_remove": [p.name for p in existing[keep:]],
            "message": f"[dry-run] 现有 {len(existing)} 份，备份后保留最近 {keep} 份",
        }
        return plan

    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        res["message"] = f"备份目录无法创建：{exc}"
        return res
    if not base.is_dir():
        res["message"] = f"备份目录不是目录：{base}"
        return res

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    final = _choose_target(base, stamp)
    # 临时名带 uuid：两个进程同一秒开始也不会互相踩。刻意不符合 FILE_RE，
    # 所以即便有残留也不会被 list_backups/prune 当成备份。
    partial = base / f"{FILE_PREFIX}{stamp}.db{PARTIAL_SUFFIX}{uuid.uuid4().hex[:8]}"

    # --- 复制（SQLite Backup API）---
    src_conn = None
    dst_conn = None
    copy_error: BaseException | None = None
    try:
        src_conn = sqlite3.connect(
            _readonly_uri(src), uri=True, timeout=db.BUSY_TIMEOUT_SECONDS
        )
        src_conn.execute("PRAGMA busy_timeout=%d" % int(db.BUSY_TIMEOUT_SECONDS * 1000))
        dst_conn = sqlite3.connect(str(partial), timeout=db.BUSY_TIMEOUT_SECONDS)
        # pages=-1 表示一次复制完。库只有几 MB，分块只会多开销；源库若在复制
        # 期间被其他进程写入，SQLite 会重启备份，拿到的是某个一致时刻的镜像。
        src_conn.backup(dst_conn)
        dst_conn.commit()
        # 备份库继承了源库的 WAL 模式。WAL 库连 -shm 都不带就打不开（只读模式
        # 会自己去建一个），对「一个自包含文件就能拿走」的备份语义是负担。
        # 复制已经完成，这里切回单文件模式是安全的常规操作；失败不影响结论。
        try:
            dst_conn.execute("PRAGMA journal_mode=DELETE").fetchone()
        except sqlite3.Error:
            pass
    except BaseException as exc:                   # noqa: BLE001 - 先记下，关连接后再收尾
        copy_error = exc
    finally:
        # 必须先关连接再删文件：Windows 上删一个仍被 sqlite 句柄打开的路径会
        # 报 PermissionError，被 _safe_unlink 吞掉后就留下一份永不清理的
        # .partial 残骸（这正是本测试抓到的问题）。
        for c in (dst_conn, src_conn):
            if c is not None:
                try:
                    c.close()
                except Exception:
                    pass

    if copy_error is not None:
        _safe_unlink(partial)
        if not isinstance(copy_error, (sqlite3.Error, OSError)):
            raise copy_error
        res["message"] = f"备份失败：{copy_error}"
        return res

    # --- 校验（不通过就删掉临时文件，历史备份一动不动）---
    ver = verify_backup(partial)
    if not ver.get("ok"):
        _safe_unlink(partial)
        res["message"] = f"备份校验失败，已丢弃本次备份：{ver.get('message')}"
        res["verify"] = ver
        return res

    # --- 原子改名 ---
    if final.exists():
        try:
            final = _choose_target(base, stamp)
        except BackupError as exc:
            _safe_unlink(partial)
            res["message"] = str(exc)
            return res
    try:
        os.replace(partial, final)
    except OSError as exc:
        _safe_unlink(partial)
        res["message"] = f"备份文件改名失败：{exc}"
        return res
    _remove_sidecars(partial)

    # 改名后再确认一次落盘结果（不是重复校验，是确认 replace 真生效了）
    try:
        final_size = final.stat().st_size
    except OSError as exc:
        res["message"] = f"备份文件改名后不可读：{exc}"
        return res
    if final_size != ver.get("size"):
        res["message"] = (
            f"备份文件改名后大小不一致（{final_size} != {ver.get('size')}）"
        )
        return res

    res.update({
        "ok": True,
        "path": str(final),
        "bytes": final_size,
        "sha256": sha256_file(final),
        "verified": ver,
    })

    # --- 清理（独立失败域：清理炸了不等于备份没成）---
    try:
        pr = prune_backups(keep=keep, protect=[final], directory=base)
    except Exception as exc:                      # noqa: BLE001 - 清理不该反噬结论
        res["pruned"] = []
        res["kept"] = len(list_backups(base))
        res["prune_error"] = str(exc)
        res["message"] = f"备份成功；清理旧备份时出错（已忽略）：{exc}"
        return res

    res["pruned"] = pr.get("removed", [])
    res["skipped"] = pr.get("skipped", [])
    res["kept"] = pr.get("kept", 0)
    removed = res["pruned"]
    res["message"] = (
        f"备份成功（{final_size} 字节）；清理 {len(removed)} 份旧备份，保留 {res['kept']} 份"
        if removed
        else f"备份成功（{final_size} 字节）；无需清理，保留 {res['kept']} 份"
    )
    return res


def _safe_unlink(path: Path) -> None:
    """尽力删除主文件及其 SQLite 伴生文件，失败不抛。

    伴生文件（`-shm` / `-wal` / `-journal`）必须一起清：只要有人以只读身份
    打开过一个 WAL 库，SQLite 就会在旁边建 `-shm`；而 `os.replace` 只搬主
    文件。这些残骸的名字不匹配 FILE_RE，永远进不了清理候选，会在备份目录里
    无声堆积——这是端到端测试抓到的问题。
    """
    base = Path(path)
    try:
        base.unlink(missing_ok=True)
    except OSError:
        pass
    _remove_sidecars(base)


def _remove_sidecars(path: Path) -> None:
    """只删伴生文件，不动主文件（改名成功后主文件已被搬走）。"""
    base = Path(path)
    for suffix in ("-shm", "-wal", "-journal"):
        try:
            Path(f"{base}{suffix}").unlink(missing_ok=True)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 保留策略
# ---------------------------------------------------------------------------
def prune_backups(
    keep: int = DEFAULT_KEEP,
    dry_run: bool = False,
    protect: list[Path | str] | None = None,
    directory: Path | str | None = None,
) -> dict:
    """只保留最近 `keep` 份本工具备份。

    边界安全靠四道检查，缺一不可：
      1. 文件名必须整串匹配 FILE_RE（`agent.db.before-*` 之类天然排除）；
      2. 符号链接直接跳过（它可能指向目录外）；
      3. `resolve()` 后父目录必须**恰好**是备份目录本身，防止 `..`、junction
         或任何解析后逃逸的路径；
      4. `protect` 里的路径永远不删——刚生成的那份哪怕时间戳异常也保住。

    `dry_run=True` 只报告要删什么，不真删。
    """
    try:
        keep = int(keep)
    except (TypeError, ValueError):
        raise BackupError(f"keep 必须是整数，收到 {keep!r}")
    if keep < 1:
        raise BackupError(f"keep 必须 >= 1，收到 {keep}")

    base = Path(directory).resolve() if directory else backup_dir()
    if not base.is_dir():
        return {"ok": True, "removed": [], "skipped": [], "kept": 0,
                "kept_names": [], "message": "备份目录不存在，无需清理"}
    base_real = base.resolve()

    protect_set: set[Path] = set()
    for p in (protect or ()):
        try:
            protect_set.add(Path(p).resolve())
        except OSError:
            pass

    files = list_backups(base)
    protected = [p for p in files if p.resolve() in protect_set]
    others = [p for p in files if p.resolve() not in protect_set]
    ordered = protected + others          # others 已按新→旧

    kept = ordered[:keep]
    drop = ordered[keep:]

    removed: list[str] = []
    skipped: list[dict] = []
    for p in drop:
        # 1) 符号链接：不删，也不跟随
        if p.is_symlink():
            skipped.append({"name": p.name, "reason": "符号链接"})
            continue
        # 2) 严格名字（list_backups 已过滤，这里再确认一次，防止上游改动破坏假设）
        if not is_backup_name(p.name):
            skipped.append({"name": p.name, "reason": "文件名不匹配本工具规则"})
            continue
        # 3) 归属校验：解析后父目录必须就是备份目录
        try:
            rp = p.resolve(strict=True)
        except OSError as exc:
            skipped.append({"name": p.name, "reason": f"无法解析：{exc}"})
            continue
        if rp.parent != base_real:
            skipped.append({"name": p.name, "reason": "解析后不在备份目录内"})
            continue
        # 4) 受保护路径
        if rp in protect_set:
            skipped.append({"name": p.name, "reason": "本次刚生成的备份"})
            continue
        if not dry_run:
            try:
                rp.unlink()
            except OSError as exc:
                skipped.append({"name": p.name, "reason": f"删除失败：{exc}"})
                continue
        removed.append(p.name)

    return {
        "ok": True,
        "removed": removed,
        "skipped": skipped,
        "kept": len(kept),
        "kept_names": [p.name for p in kept],
        "dry_run": dry_run,
        "message": (
            f"{'[dry-run] ' if dry_run else ''}清理 {len(removed)} 份，保留 {len(kept)} 份"
        ),
    }
