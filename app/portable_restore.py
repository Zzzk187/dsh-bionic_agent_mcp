"""S2 — Portable Memory Restore Engine (安全恢复引擎).

只做恢复编排：三道门（包校验 → 目标检查 → 预览确认）、merge / full_replace
两种模式、保护性备份、分阶段补偿式回滚、跨设备路径重锚、恢复后验证。

架构红线（写在这里，改代码前先读）：
  * dsh-memory 是唯一权威源；本模块绝不反向覆盖它，除非用户显式选择恢复源目录。
  * 生产库只用 mode=ro / Connection.backup() 读；真实恢复留给用户明确授权后的独立操作。
  * 不新增 MCP 工具、不改 registry、不自动重建向量、不自动下载模型。
  * S1 的 DB 快照是【逻辑快照 JSON】(tables[name]={columns,rows})，不是 .db 文件。
  * 文件系统与 SQLite 不共享事务 → 用分阶段补偿式回滚，不声称天然原子。

对应任务：S2 (m01177)。Batch B 实现：目标检查 / 预览 / 令牌 / 状态机 / 持久化日志。
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import sqlite3
import tempfile
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath

from . import db as _db
from . import knowledge as _knowledge
from . import portable_backup as _pb
from . import snapshots as _snap

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LOG_DIRNAME = "restore_logs"
DEFAULT_PACKAGE_DIRNAME = "memory_packages"
DEFAULT_BACKUP_DIRNAME = "restore_backups"

# 状态机
S_CREATED = "CREATED"
S_PACKAGE_VALIDATED = "PACKAGE_VALIDATED"
S_TARGET_INSPECTED = "TARGET_INSPECTED"
S_PREVIEW_READY = "PREVIEW_READY"
S_CONFIRMED = "CONFIRMED"
S_BACKUP_CREATED = "PRE_RESTORE_BACKUP_CREATED"
S_STAGING_READY = "STAGING_READY"
S_SOURCE_RESTORED = "SOURCE_RESTORED"
S_DB_RESTORED = "DATABASE_RESTORED"
S_PATHS_REANCHORED = "PATHS_REANCHORED"
S_VERIFIED = "POST_RESTORE_VERIFIED"
S_COMPLETED = "COMPLETED"
S_ROLLBACK_REQUIRED = "ROLLBACK_REQUIRED"
S_ROLLBACK_STARTED = "ROLLBACK_STARTED"
S_ROLLBACK_VERIFIED = "ROLLBACK_VERIFIED"
S_ROLLED_BACK = "ROLLED_BACK"
S_ROLLBACK_FAILED = "ROLLBACK_FAILED"
S_FAILED = "FAILED"

# 执行中（不可并发）的状态
_ACTIVE_STATES = {
    S_CONFIRMED, S_BACKUP_CREATED, S_STAGING_READY, S_SOURCE_RESTORED,
    S_DB_RESTORED, S_PATHS_REANCHORED, S_VERIFIED,
    S_ROLLBACK_REQUIRED, S_ROLLBACK_STARTED, S_ROLLBACK_VERIFIED,
}

RESTORE_MODES = ("merge", "full_replace")
CONFLICT_POLICIES = ("keep_target", "use_package", "keep_both")
DEFAULT_TOKEN_TTL = 300  # 秒


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _new_restore_id() -> str:
    # 时间 + 安全随机，避免可预测 & 碰撞
    return "rst-" + datetime.now().strftime("%Y%m%dT%H%M%S") + "-" + secrets.token_hex(4)


# ---------------------------------------------------------------------------
# 受管目录（防任意路径写）
# ---------------------------------------------------------------------------
def _resolve_under_project(path_value, default_name: str, *,
                           allow_external: bool = False) -> Path:
    p = Path(path_value) if path_value else (PROJECT_ROOT / "data" / default_name)
    p = p.resolve()
    if not allow_external:
        root = PROJECT_ROOT.resolve()
        try:
            p.relative_to(root)
        except ValueError:
            raise ValueError(f"路径必须位于项目根内：{p}（root={root}）")
    return p


def _log_dir(log_dir=None, *, allow_external: bool = False) -> Path:
    return _resolve_under_project(log_dir, DEFAULT_LOG_DIRNAME,
                                  allow_external=allow_external)


def _package_dir(package_dir=None, *, allow_external: bool = False) -> Path:
    return _resolve_under_project(package_dir, DEFAULT_PACKAGE_DIRNAME,
                                  allow_external=allow_external)


def _backup_dir(backup_dir=None, *, allow_external: bool = False) -> Path:
    return _resolve_under_project(backup_dir, DEFAULT_BACKUP_DIRNAME,
                                  allow_external=allow_external)


# ---------------------------------------------------------------------------
# 持久化恢复日志（每个 restore_id 一个 JSON，状态变更即时落盘）
# ---------------------------------------------------------------------------
def _log_path(restore_id: str, *, log_dir=None, allow_external: bool = False) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(restore_id))
    return _log_dir(log_dir, allow_external=allow_external) / f"{safe}.json"


def _write_log(record: dict, *, log_dir=None, allow_external: bool = False) -> Path:
    d = _log_dir(log_dir, allow_external=allow_external)
    d.mkdir(parents=True, exist_ok=True)
    path = _log_path(record["restore_id"], log_dir=log_dir, allow_external=allow_external)
    tmp = path.with_suffix(".json.part")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(record, fh, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, path)
    return path


def _read_log(restore_id: str, *, log_dir=None, allow_external: bool = False) -> dict | None:
    path = _log_path(restore_id, log_dir=log_dir, allow_external=allow_external)
    if not path.is_file():
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _transition(restore_id: str, state: str, *, detail=None, log_dir=None,
                allow_external: bool = False) -> dict:
    rec = _read_log(restore_id, log_dir=log_dir, allow_external=allow_external)
    if rec is None:
        raise KeyError(f"恢复日志不存在：{restore_id}")
    rec.setdefault("stages", []).append({
        "state": state, "at": _now(), "detail": detail or {},
    })
    rec["state"] = state
    rec["updated_at"] = _now()
    _write_log(rec, log_dir=log_dir, allow_external=allow_external)
    return rec


def get_restore_status(restore_id: str, *, log_dir=None,
                       allow_external: bool = False) -> dict:
    rec = _read_log(restore_id, log_dir=log_dir, allow_external=allow_external)
    if rec is None:
        return {"ok": False, "message": f"恢复记录不存在：{restore_id}"}
    # 不返回令牌密钥/绑定原文等敏感字段
    safe = {k: v for k, v in rec.items() if k not in ("_token_key", "_token_bindings")}
    return {"ok": True, "status": safe}


def list_restore_history(*, log_dir=None, allow_external: bool = False,
                         limit: int = 50) -> dict:
    d = _log_dir(log_dir, allow_external=allow_external)
    items = []
    if d.is_dir():
        for p in sorted(d.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True)[:limit]:
            try:
                with open(p, "r", encoding="utf-8") as fh:
                    rec = json.load(fh)
                items.append({
                    "restore_id": rec.get("restore_id"),
                    "state": rec.get("state"),
                    "restore_mode": rec.get("restore_mode"),
                    "package_id": rec.get("package_id"),
                    "created_at": rec.get("created_at"),
                    "updated_at": rec.get("updated_at"),
                })
            except (OSError, ValueError):
                continue
    return {"ok": True, "count": len(items), "items": items}


# ---------------------------------------------------------------------------
# 指纹（令牌绑定 + 恢复前重算）
# ---------------------------------------------------------------------------
def _source_tree_fingerprint(source_root: Path) -> dict:
    """权威源目录指纹：文件数 + 逐文件 (相对路径, sha256) 的聚合 hash。"""
    root = Path(source_root)
    out = {"root": str(root.resolve()), "count": 0, "total_bytes": 0, "hash": ""}
    if not root.is_dir():
        return out
    h = hashlib.sha256()
    entries = []
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.is_symlink():
            continue
        rel = p.relative_to(root).as_posix()
        try:
            digest = _pb.calculate_sha256(p)
        except OSError:
            continue
        entries.append(f"{rel}|{digest}")
        out["total_bytes"] += p.stat().st_size
    out["count"] = len(entries)
    h.update("\n".join(entries).encode("utf-8"))
    out["hash"] = h.hexdigest()
    return out


def _db_fingerprint(db_path: Path) -> dict:
    """目标库指纹：user_version + 关键表行数 + integrity。只读，mode=ro。"""
    p = Path(db_path)
    out = {"path": str(p.resolve()), "exists": p.is_file(), "user_version": None,
           "integrity": None, "table_counts": {}, "hash": ""}
    if not p.is_file():
        return out
    try:
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=5)
        try:
            out["user_version"] = conn.execute("PRAGMA user_version").fetchone()[0]
            row = conn.execute("PRAGMA integrity_check").fetchone()
            out["integrity"] = row[0] if row else None
            for t in _snap._data_tables(conn):
                try:
                    out["table_counts"][t] = conn.execute(
                        f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                except sqlite3.Error:
                    out["table_counts"][t] = -1
        finally:
            conn.close()
    except sqlite3.Error:
        return out
    h = hashlib.sha256()
    payload = json.dumps({"uv": out["user_version"], "tables": out["table_counts"]},
                         sort_keys=True, ensure_ascii=False)
    h.update(payload.encode("utf-8"))
    out["hash"] = h.hexdigest()
    return out


def _fingerprint_digest(fp: dict) -> str:
    """把多源指纹合成一个稳定字符串，供令牌绑定。"""
    if "table_counts" in fp:
        core = {"kind": "db", "hash": fp.get("hash"), "uv": fp.get("user_version"),
                "integrity": fp.get("integrity")}
    else:
        core = {"kind": "src", "hash": fp.get("hash"), "count": fp.get("count"),
                "total_bytes": fp.get("total_bytes")}
    return hashlib.sha256(json.dumps(core, sort_keys=True).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 一次性确认令牌（HMAC + 安全随机 nonce，绑定包/库/源/模式/策略/过期）
# ---------------------------------------------------------------------------
def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _issue_token(restore_id: str, bindings: dict, *, log_dir=None,
                 allow_external: bool = False, ttl_sec: int = DEFAULT_TOKEN_TTL) -> tuple[str, str]:
    key = secrets.token_bytes(32)
    nonce = secrets.token_bytes(16)
    payload = dict(bindings)
    expire_at = (datetime.now(timezone.utc) + timedelta(seconds=int(ttl_sec))).isoformat()
    payload["restore_id"] = restore_id
    payload["expire_at"] = expire_at
    body = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    mac = hmac.new(key, nonce + body, hashlib.sha256).hexdigest()
    token = f"{_b64(nonce)}.{mac}"
    # 密钥与绑定原文只写进受管日志，不回显给调用方
    rec = _read_log(restore_id, log_dir=log_dir, allow_external=allow_external)
    if rec is None:
        raise KeyError(f"恢复日志不存在：{restore_id}")
    rec["_token_key"] = _b64(key)
    rec["_token_bindings"] = payload
    rec["_token_consumed"] = False
    rec["token_expire_at"] = expire_at
    _write_log(rec, log_dir=log_dir, allow_external=allow_external)
    return token, expire_at


def _verify_token(restore_id: str, token: str, *, log_dir=None,
                  allow_external: bool = False,
                  current_bindings: dict | None = None) -> tuple[bool, str]:
    """校验一次性令牌。

    除了 HMAC + 过期，还可在 ``current_bindings`` 传入执行前【重算】的包 hash /
    目标 DB 指纹 / 源目录指纹 / 模式 / 策略——逐字段与签发时绑定比对，任一变化
    即拒绝执行（任务第七/十三节：包被替换、目标变化、模式改变都要重新预览）。
    """
    rec = _read_log(restore_id, log_dir=log_dir, allow_external=allow_external)
    if rec is None:
        return False, "恢复记录不存在"
    if rec.get("_token_consumed"):
        return False, "确认令牌已使用（一次性）"
    try:
        key = base64.urlsafe_b64decode(rec["_token_key"] + "==")
    except Exception:
        return False, "令牌密钥损坏"
    nonce_s, _, mac = str(token).partition(".")
    if not nonce_s or not mac:
        return False, "令牌格式错误"
    try:
        nonce = base64.urlsafe_b64decode(nonce_s + "==")
    except Exception:
        return False, "令牌 nonce 错误"
    payload = rec.get("_token_bindings") or {}
    body = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    expect = hmac.new(key, nonce + body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expect, mac):
        return False, "令牌校验失败（绑定数据已变化）"
    try:
        expire = datetime.fromisoformat(payload["expire_at"])
    except Exception:
        return False, "令牌过期时间无效"
    if datetime.now(timezone.utc) > expire:
        return False, "令牌已过期，请重新预览"
    # 重算指纹逐字段比对（执行前的安全门）
    if current_bindings is not None:
        for k, want in payload.items():
            if k in ("restore_id", "expire_at"):
                continue
            if k not in current_bindings:
                return False, f"执行前缺少指纹字段 {k}，拒绝恢复"
            got = current_bindings[k]
            if isinstance(got, bool) or isinstance(want, bool):
                same = bool(got) == bool(want)
            else:
                same = str(got) == str(want)
            if not same:
                return False, f"执行前发现绑定项 {k} 已变化，请重新预览"
    return True, ""


def compute_current_bindings(package_path, restore_mode: str, conflict_policy: str, *,
                             delete_extras: bool = False, db_path=None,
                             source_root=None) -> dict:
    """执行前重算令牌绑定的全部指纹（与 preview 签发时口径一致）。"""
    pkg = Path(package_path)
    db_fp = _db_fingerprint(_default_db_path(db_path))
    src_fp = _source_tree_fingerprint(_default_source_root(source_root))
    return {
        "package_sha256": _pb.calculate_sha256(pkg),
        "db_fp": _fingerprint_digest(db_fp),
        "src_fp": _fingerprint_digest(src_fp),
        "restore_mode": restore_mode,
        "conflict_policy": conflict_policy,
        "delete_extras": bool(delete_extras),
    }


def _consume_token(restore_id: str, *, log_dir=None, allow_external: bool = False) -> None:
    rec = _read_log(restore_id, log_dir=log_dir, allow_external=allow_external)
    if rec is not None:
        rec["_token_consumed"] = True
        _write_log(rec, log_dir=log_dir, allow_external=allow_external)


# ---------------------------------------------------------------------------
# 目标环境检查
# ---------------------------------------------------------------------------
def _default_db_path(db_path=None) -> Path:
    return Path(db_path) if db_path else Path(_db.DB_PATH)


def _default_source_root(source_root=None) -> Path:
    return Path(source_root) if source_root else Path(_knowledge.DSH_MEMORY_ROOT)


def inspect_restore_target(*, db_path=None, source_root=None) -> dict:
    """只读识别目标环境 12 项；数据库锁/活动写入需用户侧确认，本函数给提示。"""
    p = _default_db_path(db_path)
    s = _default_source_root(source_root)
    out: dict = {
        "ok": True, "source_root": str(s.resolve()), "source_exists": s.is_dir(),
        "db_path": str(p.resolve()), "db_exists": p.is_file(),
        "db_fingerprint": _db_fingerprint(p),
        "source_fingerprint": _source_tree_fingerprint(s),
        "embedding": {}, "doc_count": 0, "memory_count": 0, "task_count": 0,
        "vector_count": 0, "fk_violations": None, "mirror": {},
        "warnings": [], "blockers": [],
    }
    if p.is_file():
        try:
            conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=5)
            conn.row_factory = sqlite3.Row
            try:
                tables = set(_snap._data_tables(conn))
                if "memory_embeddings" in tables:
                    r = conn.execute(
                        "SELECT model_name, COUNT(*) n FROM memory_embeddings "
                        "GROUP BY model_name ORDER BY n DESC LIMIT 1").fetchone()
                    if r:
                        dim = conn.execute(
                            "SELECT dimension FROM memory_embeddings LIMIT 1").fetchone()
                        out["embedding"] = {"model_name": r["model_name"],
                                            "dimensions": dim[0] if dim else None}
                        out["vector_count"] = r["n"]
                for t, key in (("memories", "memory_count"), ("tasks", "task_count")):
                    if t in tables:
                        out[key] = conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                if "knowledge_docs" in tables:
                    out["doc_count"] = conn.execute(
                        "SELECT COUNT(*) FROM knowledge_docs").fetchone()[0]
                out["fk_violations"] = len(conn.execute("PRAGMA foreign_key_check").fetchall())
            finally:
                conn.close()
        except sqlite3.Error as exc:
            out["blockers"].append(f"数据库只读连接失败：{exc}")
    else:
        out["blockers"].append("目标数据库不存在")
    if out.get("fk_violations"):
        out["warnings"].append(f"目标库存在 {out['fk_violations']} 处外键违规，真实恢复前需人工裁决")
    if not out["source_exists"]:
        out["warnings"].append("目标权威源目录不存在（合并恢复将新增，完整替换需确认）")
    out["ok"] = not out["blockers"]
    return out


# ---------------------------------------------------------------------------
# source/ 包内相对路径 → 目标绝对路径（AGENTS.md 归父目录）
# ---------------------------------------------------------------------------
def _zip_rel_to_target(zip_rel: str, source_root: Path) -> Path | None:
    if not zip_rel.startswith("source/"):
        return None
    rel = zip_rel[len("source/"):]
    if not _pb._safe_rel(rel):
        return None
    if rel == "AGENTS.md":
        return Path(source_root).parent / "AGENTS.md"
    return Path(source_root).joinpath(*PurePosixPath(rel).parts)


def _read_logical_snapshot(zf: zipfile.ZipFile) -> dict | None:
    name = "database/logical_snapshot.json"
    if name not in zf.namelist():
        return None
    try:
        return json.loads(zf.read(name))
    except (ValueError, KeyError):
        return None


def _compat_report(manifest: dict, snap: dict | None, target: dict) -> dict:
    """manifest / schema / embedding 兼容策略。返回 (兼容摘要 + blockers + warnings)。"""
    warnings: list[str] = []
    blockers: list[str] = []
    info: dict = {
        "format_version": manifest.get("format_version"),
        "schema_version": manifest.get("schema_version"),
        "user_version": (snap or {}).get("user_version"),
        "snap_schema_version": (snap or {}).get("schema_version"),
        "embedding": manifest.get("embedding") or {},
    }
    # schema：包高于当前程序 → 阻断
    try:
        sv = int(manifest.get("schema_version") or 0)
    except (TypeError, ValueError):
        sv = -1
    if sv < 0:
        blockers.append("包 schema_version 缺失或无效，禁止猜测")
    elif sv > _snap.SCHEMA_VERSION:
        blockers.append(f"包 schema_version={sv} 高于当前程序支持上限 {_snap.SCHEMA_VERSION}")
    # user_version：未知高版本阻断
    uv = (snap or {}).get("user_version")
    cur_uv = target.get("db_fingerprint", {}).get("user_version")
    if isinstance(uv, int) and isinstance(cur_uv, int) and uv > cur_uv:
        warnings.append(f"包 user_version={uv} 高于目标 {cur_uv}，需确认迁移路径")
    # embedding：不一致只警告，不自动换模型/重建向量
    emb = manifest.get("embedding") or {}
    tgt = target.get("embedding") or {}
    if emb.get("model_name") and tgt.get("model_name") and \
            emb["model_name"] != tgt["model_name"]:
        warnings.append(f"Embedding 模型不一致（包={emb['model_name']} / 目标={tgt['model_name']}），"
                        "恢复后不自动重建向量，需用户另行授权")
    if emb.get("dimensions") and tgt.get("dimensions") and \
            emb["dimensions"] != tgt["dimensions"]:
        warnings.append(f"向量维度不一致（包={emb['dimensions']} / 目标={tgt['dimensions']}）")
    return {"info": info, "warnings": warnings, "blockers": blockers}


# ---------------------------------------------------------------------------
# 预览（Batch B 核心，零写入）
# ---------------------------------------------------------------------------
def preview_restore(
    package_path, *, restore_mode: str = "merge",
    conflict_policy: str = "keep_target", delete_extras: bool = False,
    db_path=None, source_root=None, package_dir=None, log_dir=None,
    allow_external: bool = False, ttl_sec: int = DEFAULT_TOKEN_TTL,
) -> dict:
    """三道门之三：预览。不修改任何目标文件/数据库；生成一次性确认令牌。"""
    if restore_mode not in RESTORE_MODES:
        return {"ok": False, "message": f"未知恢复模式：{restore_mode}"}
    if conflict_policy not in CONFLICT_POLICIES:
        return {"ok": False, "message": f"未知冲突策略：{conflict_policy}"}

    pkg = Path(package_path)
    if not pkg.is_file():
        return {"ok": False, "message": f"包不存在：{pkg}"}

    # 门 1：复用 S1 校验器，绝不另写一套
    val = _pb.validate_package_archive(pkg)
    blockers: list[str] = []
    warnings: list[str] = list(val.get("warnings") or [])
    if not val.get("ok"):
        blockers.extend(val.get("errors") or [])

    restore_id = _new_restore_id()
    out: dict = {
        "ok": False, "restore_id": restore_id,
        "package_id": pkg.stem, "package_path": str(pkg.resolve()),
        "package_sha256": "", "restore_mode": restore_mode,
        "conflict_policy": conflict_policy, "target": {}, "summary": {},
        "warnings": warnings, "blockers": blockers, "compatibility": {},
        "required_space_bytes": 0, "backup_required": True,
        "confirmation_token": "", "token_expire_at": "",
        "validation": {"ok": val.get("ok"), "errors": val.get("errors"),
                       "warnings": val.get("warnings")},
    }

    # 即使校验失败也落一条 PREVIEW_READY 失败日志？不：失败不建可恢复记录，
    # 但要让用户看到 blocker。直接返回。
    if blockers:
        return out

    # 包 hash + manifest + 逻辑快照
    try:
        out["package_sha256"] = _pb.calculate_sha256(pkg)
        with zipfile.ZipFile(pkg, "r") as zf:
            manifest = json.loads(zf.read("manifest.json"))
            snap = _read_logical_snapshot(zf)
            names = zf.namelist()
    except Exception as exc:
        out["blockers"].append(f"读取包内容失败：{exc}")
        return out

    backup_type = manifest.get("backup_type")
    out["backup_type"] = backup_type

    # 门 2：目标环境
    target = inspect_restore_target(db_path=db_path, source_root=source_root)
    out["target"] = target
    out["blockers"].extend(target.get("blockers") or [])
    out["warnings"].extend(target.get("warnings") or [])

    # 模式 × 包类型 能力矩阵
    if restore_mode == "full_replace":
        if backup_type == "source_only":
            out["blockers"].append("source_only 包不含完整数据库状态，禁止全系统完整替换")
        elif backup_type == "knowledge_tasks":
            out["blockers"].append("knowledge_tasks 只支持受限知识/任务范围恢复，不支持全系统完整替换")
    if backup_type not in _pb.BACKUP_TYPES:
        out["blockers"].append(f"未知备份类型：{backup_type}")

    # DB 快照结构检查
    mode_needed = _pb._MODE_DB.get(backup_type)
    if mode_needed and snap is None:
        out["blockers"].append("该包声明含 DB 快照，但逻辑快照缺失或无法解析")
    if snap is not None:
        if not isinstance(snap.get("tables"), dict) or not snap["tables"]:
            out["blockers"].append("逻辑快照没有表数据")

    # 兼容策略
    compat = _compat_report(manifest, snap, target)
    out["compatibility"] = compat["info"]
    out["blockers"].extend(compat["blockers"])
    out["warnings"].extend(compat["warnings"])

    # 文件差异统计（真实比对，非估算）
    src_root = _default_source_root(source_root)
    summary = {
        "files_to_add": 0, "files_to_update": 0, "files_to_skip": 0,
        "files_to_remove": 0, "memories_to_add": 0, "memories_to_update": 0,
        "memories_to_preserve": 0, "documents_to_add": 0,
        "documents_to_update": 0, "tasks_to_restore": 0, "conflicts": 0,
    }
    conflict_detail: list[dict] = []
    plan_files: list[dict] = []
    for n in names:
        if not n.startswith("source/") or n.endswith("/"):
            continue
        tgt_path = _zip_rel_to_target(n, src_root)
        if tgt_path is None:
            out["warnings"].append(f"跳过不安全/无法映射的包内路径：{n}")
            continue
        want_hash = None
        # 从 manifest 找该路径 hash
        for e in manifest.get("file_manifest") or []:
            if e.get("path") == n:
                want_hash = str(e.get("sha256", "")).lower()
                break
        if tgt_path.is_file():
            have = _pb.calculate_sha256(tgt_path)
            if have == want_hash:
                summary["files_to_skip"] += 1
                action = "skip"
            else:
                summary["files_to_update"] += 1
                summary["conflicts"] += 1
                action = conflict_policy  # keep_target / use_package / keep_both
                conflict_detail.append({"path": n, "target": str(tgt_path),
                                        "policy": conflict_policy})
        else:
            summary["files_to_add"] += 1
            action = "add"
        plan_files.append({"zip_path": n, "target": str(tgt_path), "action": action,
                           "sha256": want_hash})
    out["plan_files"] = plan_files
    out["conflicts_files"] = conflict_detail

    # full_replace 的删除项：仅显式授权才列，且必须逐项列出
    if restore_mode == "full_replace":
        if delete_extras:
            pkg_src_rels = set()
            for n in names:
                if n.startswith("source/") and not n.endswith("/"):
                    rel = n[len("source/"):]
                    if rel == "AGENTS.md":
                        continue
                    pkg_src_rels.add(rel)
            if src_root.is_dir():
                for p in sorted(src_root.rglob("*")):
                    if not p.is_file() or p.is_symlink():
                        continue
                    rel = p.relative_to(src_root).as_posix()
                    if rel not in pkg_src_rels:
                        summary["files_to_remove"] += 1
                        out.setdefault("deletions", []).append(str(p))
        else:
            out["warnings"].append("完整替换默认不删除包外文件；如需删除须显式传入 delete_extras=True，"
                                   "届时逐项列在 deletions 中")

    # 记忆/文档/任务差异：从逻辑快照读（真实行数，非估算）
    if snap:
        tables = snap["tables"]
        pkg_mem = {str(r.get("id")): r for r in tables.get("memories", {}).get("rows", [])}
        pkg_docs = {str(r.get("id")): r for r in tables.get("knowledge_docs", {}).get("rows", [])}
        summary["tasks_to_restore"] = len(tables.get("tasks", {}).get("rows", []))
        # 目标库真实行（只读）
        p = _default_db_path(db_path)
        if p.is_file():
            try:
                conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=5)
                conn.row_factory = sqlite3.Row
                try:
                    live_tables = set(_snap._data_tables(conn))
                    live_mem = {}
                    if "memories" in live_tables:
                        live_mem = {str(r["id"]): r for r in conn.execute("SELECT id, content FROM memories")}
                    live_docs = {}
                    if "knowledge_docs" in live_tables:
                        live_docs = {str(r["id"]): r for r in conn.execute(
                            "SELECT id, content_hash FROM knowledge_docs")}
                    for k, row in pkg_mem.items():
                        if k not in live_mem:
                            summary["memories_to_add"] += 1
                        elif str(live_mem[k]["content"]) != str(row.get("content")):
                            summary["memories_to_update"] += 1
                            summary["conflicts"] += 1
                    summary["memories_to_preserve"] = len(set(live_mem) - set(pkg_mem))
                    for k, row in pkg_docs.items():
                        if k not in live_docs:
                            summary["documents_to_add"] += 1
                        elif str(live_docs[k]["content_hash"]) != str(row.get("content_hash")):
                            summary["documents_to_update"] += 1
                finally:
                    conn.close()
            except sqlite3.Error as exc:
                out["warnings"].append(f"读取目标记忆统计失败：{exc}")
    out["summary"] = summary

    # 空间需求：解压总量 × 2（staging + 保护副本）向上取整
    out["required_space_bytes"] = int(val.get("total_size_bytes") or 0) * 2

    if out["blockers"]:
        out["message"] = "预览发现阻断项，禁止恢复"
        return out

    # 落 CREATED → PREVIEW_READY 日志，签发一次性令牌
    _write_log({
        "restore_id": restore_id, "state": S_CREATED,
        "created_at": _now(), "updated_at": _now(),
        "package_id": pkg.stem, "package_path": str(pkg.resolve()),
        "package_sha256": out["package_sha256"], "restore_mode": restore_mode,
        "conflict_policy": conflict_policy, "delete_extras": bool(delete_extras),
        "backup_type": backup_type, "target": target,
        "summary": summary, "warnings": out["warnings"],
        "compatibility": compat["info"], "stages": [],
        "plan_files": plan_files, "deletions": out.get("deletions", []),
        "conflicts_files": conflict_detail,
    }, log_dir=log_dir, allow_external=allow_external)
    bindings = {
        "package_sha256": out["package_sha256"],
        "db_fp": _fingerprint_digest(target["db_fingerprint"]),
        "src_fp": _fingerprint_digest(target["source_fingerprint"]),
        "restore_mode": restore_mode,
        "conflict_policy": conflict_policy,
        "delete_extras": bool(delete_extras),
    }
    token, expire = _issue_token(restore_id, bindings, log_dir=log_dir,
                                 allow_external=allow_external, ttl_sec=ttl_sec)
    _transition(restore_id, S_PACKAGE_VALIDATED,
                detail={"validation_ok": True}, log_dir=log_dir, allow_external=allow_external)
    _transition(restore_id, S_TARGET_INSPECTED,
                detail={"db_exists": target["db_exists"],
                        "source_exists": target["source_exists"]},
                log_dir=log_dir, allow_external=allow_external)
    _transition(restore_id, S_PREVIEW_READY,
                detail={"summary": summary}, log_dir=log_dir, allow_external=allow_external)

    out["ok"] = True
    out["confirmation_token"] = token
    out["token_expire_at"] = expire
    out["message"] = "预览就绪；令牌一次性有效，执行前会重算包 hash 与目标指纹"
    return out


# ---------------------------------------------------------------------------
# 门 4：确认（一次性令牌 + 重算指纹 + 流转 CONFIRMED）
# ---------------------------------------------------------------------------
def confirm_restore(restore_id: str, confirmation_token: str, *,
                    package_path, restore_mode: str, conflict_policy: str = "keep_target",
                    delete_extras: bool = False, db_path=None, source_root=None,
                    log_dir=None, allow_external: bool = False) -> dict:
    """显式确认。重算全部绑定指纹 → 校验令牌 → 一次性消费 → 流转 CONFIRMED。"""
    rec = _read_log(restore_id, log_dir=log_dir, allow_external=allow_external)
    if rec is None:
        return {"ok": False, "message": f"恢复记录不存在：{restore_id}"}
    if rec.get("state") not in (S_PREVIEW_READY, S_CONFIRMED):
        return {"ok": False, "message": f"状态 {rec.get('state')} 不允许确认（需 PREVIEW_READY）"}
    if rec.get("state") == S_CONFIRMED:
        return {"ok": False, "message": "该恢复已确认，禁止重复确认/重复执行"}

    current = compute_current_bindings(package_path, restore_mode, conflict_policy,
                                       delete_extras=delete_extras,
                                       db_path=db_path, source_root=source_root)
    ok, why = _verify_token(restore_id, confirmation_token, log_dir=log_dir,
                            allow_external=allow_external, current_bindings=current)
    if not ok:
        return {"ok": False, "message": why}
    # 与预览签发时的模式/策略也必须一致（已在 current 里比对）
    _consume_token(restore_id, log_dir=log_dir, allow_external=allow_external)
    _transition(restore_id, S_CONFIRMED,
                detail={"confirmed_bindings": current},
                log_dir=log_dir, allow_external=allow_external)
    return {"ok": True, "restore_id": restore_id, "state": S_CONFIRMED,
            "confirmed_bindings": current,
            "message": "已确认；令牌已消费，可进入执行阶段"}


# ---------------------------------------------------------------------------
# 保护性备份（源目录副本 + 数据库 Connection.backup()）
# ---------------------------------------------------------------------------
def _create_protective_backup(restore_id: str, *, source_root, db_path,
                              backup_dir=None, log_dir=None,
                              allow_external: bool = False) -> dict:
    """生成双备份：源目录 copytree + DB Connection.backup()。任一失败整体失败。"""
    bdir = _backup_dir(backup_dir, allow_external=allow_external)
    this = bdir / restore_id
    out: dict = {"ok": False, "backup_dir": str(this), "db_backup": None,
                 "source_backup": None, "errors": []}
    try:
        this.mkdir(parents=True, exist_ok=False)
    except Exception as exc:
        out["errors"].append(f"创建备份目录失败：{exc}")
        return out

    src = Path(source_root)
    dbp = Path(db_path)
    # 1) 源目录保护性副本
    try:
        if src.is_dir():
            dst = this / "source"
            shutil.copytree(src, dst, symlinks=False)
            fp = _source_tree_fingerprint(dst)
            out["source_backup"] = {"path": str(dst), "count": fp["count"],
                                    "hash": fp["hash"]}
    except Exception as exc:
        out["errors"].append(f"源目录备份失败：{exc}")
    # 2) 数据库保护性副本（Connection.backup()，不直接拷运行中的 db）
    try:
        if dbp.is_file():
            dst = this / "agent.db"
            src_conn = sqlite3.connect(f"file:{dbp}?mode=ro", uri=True, timeout=10)
            dst_conn = sqlite3.connect(str(dst))
            try:
                src_conn.backup(dst_conn)
                dst_conn.commit()
            finally:
                src_conn.close()
                dst_conn.close()
            digest = _pb.calculate_sha256(dst)
            fp2 = _db_fingerprint(dst)
            out["db_backup"] = {"path": str(dst), "sha256": digest,
                                "integrity": fp2.get("integrity"),
                                "user_version": fp2.get("user_version")}
    except Exception as exc:
        out["errors"].append(f"数据库保护性备份失败：{exc}")

    out["ok"] = not out["errors"]
    _transition(restore_id, S_BACKUP_CREATED,
                detail={"backup_dir": str(this), "ok": out["ok"],
                        "errors": out["errors"]},
                log_dir=log_dir, allow_external=allow_external)
    return out


# ---------------------------------------------------------------------------
# staging：安全解压 + 清单/hash 复核（目标同文件系统）
# ---------------------------------------------------------------------------
def _stage_package(restore_id: str, package_path, *, source_root,
                   staging_dir=None, log_dir=None, allow_external: bool = False) -> dict:
    """在目标父目录同文件系统建 staging，逐文件安全解压并复核 hash。"""
    pkg = Path(package_path)
    parent = Path(source_root).parent if Path(source_root).parent.is_dir() else Path(tempfile.gettempdir())
    out: dict = {"ok": False, "staging_dir": None, "staged_files": [],
                 "errors": [], "snapshot": None}
    try:
        stage = Path(tempfile.mkdtemp(prefix="s2-stage-", dir=str(parent)))
    except Exception as exc:
        out["errors"].append(f"创建 staging 失败：{exc}")
        return out
    out["staging_dir"] = str(stage)
    try:
        with zipfile.ZipFile(pkg, "r") as zf:
            # 二次防御：即便 S1 已校验，这里再挡一次路径穿越/绝对/敏感
            for info in zf.infolist():
                name = info.filename
                if name.endswith("/"):
                    continue
                if not _pb._safe_rel(name):
                    raise ValueError(f"staging 拒绝不安全路径：{name}")
                if _pb._is_sensitive(name):
                    raise ValueError(f"staging 拒绝敏感文件：{name}")
                target = stage.joinpath(*PurePosixPath(name).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                out["staged_files"].append(name)
            # 复核：staging 里的每个数据文件 hash 必须与 manifest 一致
            manifest = json.loads(zf.read("manifest.json"))
            want = {str(e.get("path")): str(e.get("sha256", "")).lower()
                    for e in manifest.get("file_manifest") or []}
            for rel, digest in want.items():
                f = stage.joinpath(*PurePosixPath(rel).parts)
                if not f.is_file():
                    raise ValueError(f"staging 缺少文件：{rel}")
                if _pb.calculate_sha256(f) != digest:
                    raise ValueError(f"staging 文件 hash 不符：{rel}")
            # 读取逻辑快照（staging 内），供后续 DB 阶段使用
            snap_path = stage / "database" / "logical_snapshot.json"
            if snap_path.is_file():
                with open(snap_path, "r", encoding="utf-8") as fh:
                    out["snapshot"] = json.load(fh)
    except Exception as exc:
        # staging 失败：清理临时目录，目标未动
        shutil.rmtree(stage, ignore_errors=True)
        out["staging_dir"] = None
        out["errors"].append(f"staging 解压/校验失败：{exc}")
        _transition(restore_id, S_FAILED,
                    detail={"stage": "staging", "error": str(exc)[:300]},
                    log_dir=log_dir, allow_external=allow_external)
        return out

    out["ok"] = True
    _transition(restore_id, S_STAGING_READY,
                detail={"staging_dir": str(stage), "files": len(out["staged_files"])},
                log_dir=log_dir, allow_external=allow_external)
    return out


# ---------------------------------------------------------------------------
# 逻辑快照 → snapshots 契约适配（不写生产库，只在 staging/隔离副本操作）
# ---------------------------------------------------------------------------
def _adapt_snapshot_payload(snap: dict) -> dict:
    """把 S1 逻辑快照 tables[name]={columns,rows} 转成 snapshots._apply_payload
    期望的 payload 形状 {rule_version, schema_version, tables:{name:[rows]}, columns:...}。

    仅做形状适配，不在此写库；实际写库由 DB 阶段在隔离副本中完成。
    """
    tables_in = snap.get("tables") or {}
    tables_out = {}
    columns = {}
    for name, spec in tables_in.items():
        if isinstance(spec, dict):
            columns[name] = list(spec.get("columns") or [])
            tables_out[name] = list(spec.get("rows") or [])
    return {
        "created_at": snap.get("created_at"),
        "rule_version": snap.get("rule_version"),
        "schema_version": snap.get("schema_version", 0),
        "tables": tables_out,
        "columns": columns,
    }


def _validate_snapshot_against_db(snap: dict, db_path=None) -> dict:
    """在【隔离副本】上校验逻辑快照：表集合/列/外键/完整性，零写生产库。"""
    p = _default_db_path(db_path)
    out: dict = {"ok": False, "errors": [], "warnings": [], "checks": {}}
    if not p.is_file():
        out["errors"].append("目标数据库不存在，无法校验快照兼容性")
        return out
    fd, tmp = tempfile.mkstemp(prefix="s2-snapcheck-", suffix=".db")
    os.close(fd)
    try:
        src = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=10)
        dst = sqlite3.connect(tmp)
        try:
            src.backup(dst)   # 隔离副本
        finally:
            src.close()
        dst.row_factory = sqlite3.Row
        out["checks"]["integrity"] = dst.execute("PRAGMA integrity_check").fetchone()[0]
        live_tables = [r[0] for r in dst.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()]
        live_cols = {t: [r[1] for r in dst.execute(f'PRAGMA table_info("{t}")').fetchall()]
                     for t in live_tables}
        payload = _adapt_snapshot_payload(snap)
        shape, err = _snap._validate_payload(payload, live_tables, live_cols)
        out["checks"]["preflight"] = shape
        if err:
            out["errors"].append(err)
        # 外键检查（快照行自身）
        fk_bad = dst.execute("PRAGMA foreign_key_check").fetchall()
        out["checks"]["live_fk"] = len(fk_bad)
        if fk_bad:
            out["warnings"].append(f"目标隔离副本存在 {len(fk_bad)} 处外键违规")
        dst.close()
    except Exception as exc:
        out["errors"].append(f"快照校验异常：{exc}")
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    out["ok"] = not out["errors"]
    return out


# ---------------------------------------------------------------------------
# 文件阶段：按 plan 应用 source/，记录 undo，支持 keep_target / use_package / keep_both
# ---------------------------------------------------------------------------
def _stable_keepboth_path(target: Path) -> Path:
    """生成稳定、无碰撞的 keep_both 文件名：name.keepboth-<sha8>.<ext>。"""
    stem, ext = target.stem, target.suffix
    digest = hashlib.sha256(str(target).encode("utf-8")).hexdigest()[:8]
    cand = target.with_name(f"{stem}.keepboth-{digest}{ext}")
    n = 1
    while cand.exists():
        cand = target.with_name(f"{stem}.keepboth-{digest}-{n}{ext}")
        n += 1
    return cand


def _apply_source_files(restore_id: str, stage_dir, source_root, plan_files: list[dict], *,
                        conflict_policy: str, log_dir=None,
                        allow_external: bool = False) -> dict:
    """把 staging/source/ 按计划应用到目标权威源；逐步记录 undo。"""
    out: dict = {"ok": False, "applied": [], "undo": [], "errors": [],
                 "skipped": 0, "conflicts_kept": 0}
    stage = Path(stage_dir)
    src_root = Path(source_root)

    for entry in plan_files:
        zrel = entry.get("zip_path", "")
        tgt = Path(entry.get("target", ""))
        action = entry.get("action", "add")
        staged = stage.joinpath(*PurePosixPath(zrel).parts)
        if not staged.is_file():
            out["errors"].append(f"staging 缺少待应用文件：{zrel}")
            break
        if action == "skip":
            out["skipped"] += 1
            continue
        final = tgt
        if action in ("keep_target", "use_package", "keep_both") and tgt.exists():
            if conflict_policy == "keep_target":
                out["skipped"] += 1
                out["conflicts_kept"] += 1
                continue
            elif conflict_policy == "keep_both":
                final = _stable_keepboth_path(tgt)
            # use_package: 覆盖 tgt（final 保持 tgt）
        try:
            final.parent.mkdir(parents=True, exist_ok=True)
            had_before = final.exists()
            old_bytes = final.read_bytes() if had_before else None
            # 先写临时文件再原子替换，避免写一半
            tmp = final.with_name(final.name + ".s2-tmp")
            shutil.copyfile(staged, tmp)
            os.replace(tmp, final)
            out["applied"].append(str(final))
            out["undo"].append({"path": str(final), "existed": had_before,
                                "old_bytes_b64": base64.b64encode(old_bytes).decode("ascii")
                                if old_bytes is not None else None})
        except Exception as exc:
            out["errors"].append(f"应用文件失败 {zrel} → {final}：{exc}")
            break

    out["ok"] = not out["errors"]
    _transition(restore_id, S_SOURCE_RESTORED,
                detail={"applied": len(out["applied"]), "skipped": out["skipped"],
                        "ok": out["ok"], "errors": out["errors"]},
                log_dir=log_dir, allow_external=allow_external)
    return out


def _undo_source_files(restore_id: str, undo: list[dict], *, log_dir=None,
                       allow_external: bool = False) -> dict:
    """文件系统补偿：按 undo 逆序恢复每个文件（存在前值则写回，否则删除）。"""
    out: dict = {"ok": True, "errors": [], "restored": 0, "removed": 0}
    for item in reversed(undo or []):
        p = Path(item["path"])
        try:
            if item.get("existed") and item.get("old_bytes_b64") is not None:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(base64.b64decode(item["old_bytes_b64"]))
                out["restored"] += 1
            else:
                if p.exists():
                    p.unlink()
                    out["removed"] += 1
        except Exception as exc:
            out["errors"].append(f"回滚文件失败 {p}：{exc}")
    out["ok"] = not out["errors"]
    return out


def _undo_deletions(deleted: list[dict], *, log_dir=None, allow_external: bool = False) -> dict:
    """回滚 full_replace 的删除项（恢复被删除文件原内容）。"""
    out: dict = {"ok": True, "errors": [], "restored": 0}
    for item in reversed(deleted or []):
        p = Path(item["path"])
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(base64.b64decode(item["bytes_b64"]))
            out["restored"] += 1
        except Exception as exc:
            out["errors"].append(f"回滚删除项失败 {p}：{exc}")
    out["ok"] = not out["errors"]
    return out


# ---------------------------------------------------------------------------
# 显式路径的 DB 连接 / 事务（不使用全局 db.DB_PATH，隔离测试安全）
# ---------------------------------------------------------------------------
@contextmanager
def _connect_at(db_path):
    conn = sqlite3.connect(str(db_path), timeout=_db.BUSY_TIMEOUT_SECONDS)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=%d" % int(_db.BUSY_TIMEOUT_SECONDS * 1000))
    try:
        _db._retry_locked(lambda: conn.execute("PRAGMA journal_mode=WAL"))
    except Exception:
        pass
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


@contextmanager
def _transaction_at(db_path):
    conn = sqlite3.connect(str(db_path), timeout=_db.BUSY_TIMEOUT_SECONDS)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=%d" % int(_db.BUSY_TIMEOUT_SECONDS * 1000))
    try:
        _db._retry_locked(lambda: conn.execute("PRAGMA journal_mode=WAL"))
    except Exception:
        pass
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
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


# 快照里属于"本地元数据"、不参与跨设备恢复的表：
# db_snapshots 的 payload_path 指向 data/snapshots/*.json，S1 未打包这些 JSON，
# 恢复它的元数据行只会造成指向空文件的孤儿，故 merge/full 都跳过并如实报告。
_NON_RESTORABLE_TABLES = {"db_snapshots", "sqlite_sequence", "sqlite_stat1"}


def _restorable_tables(snap: dict) -> dict:
    tables = (snap.get("tables") or {})
    return {k: v for k, v in tables.items() if k not in _NON_RESTORABLE_TABLES}


def _pk_columns(conn, table: str) -> list[str]:
    info = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    pk = [r[1] for r in info if r[5]]
    return pk


def _table_columns(conn, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")').fetchall()]


# ---------------------------------------------------------------------------
# DB 阶段：merge —— 只补缺失 PK，不覆盖既有治理状态，不删除 DB-only 行
# ---------------------------------------------------------------------------
def _apply_db_merge(snap: dict, db_path, *, log_dir=None, restore_id="",
                    allow_external: bool = False) -> dict:
    """合并：对快照每张表，按真实主键判断；缺失则插入，已存在则跳过并记录冲突。

    绝不覆盖目标既有行（保留治理状态/access_count/DB-only），不删除任何目标行。
    """
    out: dict = {"ok": False, "inserted": {}, "skipped": {}, "conflicts": [],
                 "errors": [], "table_names": []}
    payload = _adapt_snapshot_payload(snap)
    # [S2 fix] 必须用适配后的 tables:{name:[行dict]}，不能用 _restorable_tables(snap)
    # 的原始 {name:{columns,rows}} —— 后者遍历出的 "rows" 是 dict 不是行列表。
    tables = {k: v for k, v in payload["tables"].items()
              if k not in _NON_RESTORABLE_TABLES}
    try:
        with _transaction_at(db_path) as conn:
            live_tables = set(_snap._data_tables(conn))
            for name, rows in tables.items():
                if name not in live_tables:
                    out["errors"].append(f"快照表 {name} 在目标库不存在，跳过（merge 不建表）")
                    continue
                out["table_names"].append(name)
                live_cols = set(_table_columns(conn, name))
                pk = _pk_columns(conn, name)
                cols = [c for c in (payload["columns"].get(name) or []) if c in live_cols]
                if not cols and rows:
                    cols = [c for c in rows[0].keys() if c in live_cols]
                if not cols:
                    out["errors"].append(f"表 {name} 无可用列，跳过")
                    continue
                ins = 0
                skip = 0
                if pk and all(p in live_cols for p in pk):
                    # 以主键集合判断存在（真实主键，非文本猜测）
                    existing = {tuple(r[i] for i in pk)
                                for r in conn.execute(
                                    f'SELECT {",".join(chr(34)+p+chr(34) for p in pk)} FROM "{name}"'
                                ).fetchall()}
                    for row in rows:
                        key = tuple(row.get(p) for p in pk)
                        if key in existing:
                            skip += 1
                            out["conflicts"].append({"table": name, "pk": list(key),
                                                     "reason": "目标已存在同主键，保留目标"})
                            continue
                        use = [c for c in cols if c in row]
                        ph = ",".join("?" for _ in use)
                        conn.execute(
                            f'INSERT INTO "{name}" ({",".join(chr(34)+c+chr(34) for c in use)}) VALUES ({ph})',
                            [row[c] for c in use],
                        )
                        existing.add(key)
                        ins += 1
                else:
                    # 无主键表：用整行 JSON 指纹去重，避免重复导入
                    existing_fp = set()
                    for r in conn.execute(f'SELECT * FROM "{name}"').fetchall():
                        existing_fp.add(hashlib.sha256(
                            json.dumps(dict(r), sort_keys=True, default=str).encode("utf-8")
                        ).hexdigest())
                    for row in rows:
                        fp = hashlib.sha256(
                            json.dumps({c: row.get(c) for c in cols}, sort_keys=True, default=str).encode("utf-8")
                        ).hexdigest()
                        if fp in existing_fp:
                            skip += 1
                            continue
                        use = [c for c in cols if c in row]
                        ph = ",".join("?" for _ in use)
                        conn.execute(
                            f'INSERT INTO "{name}" ({",".join(chr(34)+c+chr(34) for c in use)}) VALUES ({ph})',
                            [row[c] for c in use],
                        )
                        existing_fp.add(fp)
                        ins += 1
                out["inserted"][name] = ins
                out["skipped"][name] = skip
            # 提交前 FK 检查（merge 也检查，避免插入制造孤儿）
            bad = conn.execute("PRAGMA foreign_key_check").fetchall()
            if bad:
                raise RuntimeError(f"merge 后外键检查发现 {len(bad)} 处违规，拒绝提交")
    except Exception as exc:
        out["errors"].append(f"merge 事务失败（已回滚）：{exc}")
    out["ok"] = not out["errors"]
    if restore_id:
        _transition(restore_id, S_DB_RESTORED,
                    detail={"mode": "merge", "inserted": out["inserted"],
                            "ok": out["ok"], "errors": out["errors"]},
                    log_dir=log_dir, allow_external=allow_external)
    return out


# ---------------------------------------------------------------------------
# DB 阶段：full_replace —— 只替换快照覆盖的表，defer FK + commit 前 FK 门
# ---------------------------------------------------------------------------
def _apply_db_full_replace(snap: dict, db_path, *, log_dir=None, restore_id="",
                           allow_external: bool = False) -> dict:
    """完整替换：对快照覆盖的每张表先清空再全量插入；快照未覆盖的表保留不动。

    复用 snapshots 的契约：defer_foreign_keys + 提交前 foreign_key_check；FK 始终 ON。
    """
    out: dict = {"ok": False, "replaced": {}, "errors": [], "table_names": []}
    payload = _adapt_snapshot_payload(snap)
    # [S2 fix] 同 merge：用适配后的行列表，过滤不可恢复的本地元数据表。
    tables = {k: v for k, v in payload["tables"].items()
              if k not in _NON_RESTORABLE_TABLES}
    try:
        with _transaction_at(db_path) as conn:
            conn.execute("PRAGMA defer_foreign_keys=ON")
            if conn.execute("PRAGMA defer_foreign_keys").fetchone()[0] != 1:
                raise RuntimeError("无法启用 defer_foreign_keys，拒绝完整替换")
            live_tables = set(_snap._data_tables(conn))
            # [S2 fix] 必须先清空所有表、再全量插入（两遍扫描）。
            # 逐表 delete+insert 交错时，删父表(knowledge_docs) 会 ON DELETE CASCADE
            # 连带删掉刚插好的子表行(knowledge_chunks/knowledge_embeddings)，
            # 造成 1006 处孤儿 FK 违规。全清后再插即可避免。
            plan: list[tuple[str, list[dict], list[str], set[str]]] = []
            for name, rows in tables.items():
                if name not in live_tables:
                    out["errors"].append(f"快照表 {name} 在目标库不存在，拒绝完整替换")
                    continue
                out["table_names"].append(name)
                live_cols = set(_table_columns(conn, name))
                cols = [c for c in (payload["columns"].get(name) or []) if c in live_cols]
                plan.append((name, rows, cols, live_cols))
            if not out["errors"]:
                # 第一遍：清空所有目标表（defer FK，级联删除也在此完成）
                for name, _rows, _cols, _lc in plan:
                    conn.execute(f'DELETE FROM "{name}"')
                # 第二遍：全量插入
                for name, rows, cols, live_cols in plan:
                    ins = 0
                    for row in rows:
                        use = [c for c in cols if c in row]
                        if not use and row:
                            use = [c for c in row.keys() if c in live_cols]
                        if not use:
                            continue
                        ph = ",".join("?" for _ in use)
                        conn.execute(
                            f'INSERT INTO "{name}" ({",".join(chr(34)+c+chr(34) for c in use)}) VALUES ({ph})',
                            [row[c] for c in use],
                        )
                        ins += 1
                    out["replaced"][name] = ins
            bad = conn.execute("PRAGMA foreign_key_check").fetchall()
            if bad:
                raise RuntimeError(f"完整替换后外键检查发现 {len(bad)} 处违规，拒绝提交")
    except Exception as exc:
        out["errors"].append(f"完整替换事务失败（已回滚）：{exc}")
    out["ok"] = not out["errors"]
    if restore_id:
        _transition(restore_id, S_DB_RESTORED,
                    detail={"mode": "full_replace", "replaced": out["replaced"],
                            "ok": out["ok"], "errors": out["errors"]},
                    log_dir=log_dir, allow_external=allow_external)
    return out


# ---------------------------------------------------------------------------
# 路径重锚：复用 knowledge.reanchor_paths（它写 db.DB_PATH，执行期临时切换）
# ---------------------------------------------------------------------------
@contextmanager
def _db_path_scope(path_value):
    """临时把共享模块 db.DB_PATH 指向目标库，供 reanchor_paths / 工具复用。"""
    prev = _db.DB_PATH
    try:
        _db.DB_PATH = Path(path_value)
        yield
    finally:
        _db.DB_PATH = prev


def _reanchor_at(new_root, db_path, *, dry_run: bool = True) -> dict:
    with _db_path_scope(db_path):
        return _knowledge.reanchor_paths(str(new_root), dry_run=dry_run)


# ---------------------------------------------------------------------------
# 恢复后验证
# ---------------------------------------------------------------------------
def verify_restored_state(restore_id: str, *, db_path, source_root,
                          stage_snapshot: dict | None = None,
                          log_dir=None, allow_external: bool = False) -> dict:
    """verify_restored_state：DB / 权威源 / 镜像 / 向量四类检查；只报告，不修。"""
    out: dict = {
        "ok": False, "restore_id": restore_id, "database_check": {},
        "foreign_key_check": {}, "source_check": {}, "mirror_check": {},
        "vector_check": {}, "warnings": [], "errors": [],
    }
    p = Path(db_path)
    src = Path(source_root)
    # --- DB ---
    if not p.is_file():
        out["errors"].append("目标数据库不存在")
        return out
    try:
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            integ = conn.execute("PRAGMA integrity_check").fetchone()[0]
            fk_bad = conn.execute("PRAGMA foreign_key_check").fetchall()
            tables = set(_snap._data_tables(conn))
            counts = {}
            for t in ("memories", "memory_links", "memory_feedback", "memory_conflicts",
                      "governance_actions", "tasks", "snapshots", "knowledge_docs",
                      "knowledge_chunks", "knowledge_embeddings", "memory_embeddings"):
                counts[t] = (conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                             if t in tables else None)
            out["database_check"] = {"integrity": integ, "user_version":
                conn.execute("PRAGMA user_version").fetchone()[0],
                "table_count": len(tables), "counts": counts}
            out["foreign_key_check"] = {"violations": len(fk_bad),
                                        "samples": [list(r) for r in fk_bad[:5]]}
            if integ != "ok":
                out["errors"].append(f"integrity_check={integ}")
            if fk_bad:
                out["errors"].append(f"foreign_key_check 有 {len(fk_bad)} 处违规")
            # 镜像（等价 check_mirror 的 SQL 检查，不调用全局路径函数）
            out["mirror_check"] = {
                "docs": counts.get("knowledge_docs"),
                "chunks": counts.get("knowledge_chunks"),
                "vectors": counts.get("knowledge_embeddings"),
                "chunks_match_vectors": counts.get("knowledge_chunks") == counts.get("knowledge_embeddings"),
            }
            if counts.get("knowledge_chunks") != counts.get("knowledge_embeddings"):
                out["warnings"].append("知识块与向量数量不一致（不自动重建，需另行授权）")
            # 向量
            vec = {"model_name": None, "dimensions": None, "count": 0, "compat": "unknown"}
            if "memory_embeddings" in tables:
                r = conn.execute("SELECT model_name, dimension, COUNT(*) n FROM memory_embeddings "
                                 "GROUP BY model_name, dimension ORDER BY n DESC LIMIT 1").fetchone()
                if r:
                    vec = {"model_name": r["model_name"], "dimensions": r["dimension"],
                           "count": r["n"], "compat": "consistent"}
            out["vector_check"] = vec
            if stage_snapshot:
                man_emb = stage_snapshot if "model_name" in stage_snapshot else None
        finally:
            conn.close()
    except sqlite3.Error as exc:
        out["errors"].append(f"数据库校验失败：{exc}")

    # --- 权威源 ---
    try:
        if not src.is_dir():
            out["errors"].append("恢复后权威源目录不存在")
        else:
            fp = _source_tree_fingerprint(src)
            bad = [p for p in src.rglob("*") if p.is_symlink()]
            out["source_check"] = {"root": str(src.resolve()), "file_count": fp["count"],
                                   "total_bytes": fp["total_bytes"],
                                   "symlinks": len(bad), "hash": fp["hash"]}
            if bad:
                out["warnings"].append(f"权威源存在 {len(bad)} 个符号链接，需人工确认无逃逸")
    except Exception as exc:
        out["errors"].append(f"权威源校验失败：{exc}")

    out["ok"] = not out["errors"]
    if restore_id:
        # verify 是只读验证 API，可在无恢复日志的独立上下文调用；
        # 日志存在时记录状态流转，不存在时不阻断验证结果本身。
        try:
            _transition(restore_id, S_VERIFIED,
                        detail={"ok": out["ok"], "errors": out["errors"],
                                "warnings": out["warnings"]},
                        log_dir=log_dir, allow_external=allow_external)
        except KeyError:
            pass
    return out


# ---------------------------------------------------------------------------
# 回滚（补偿式：文件 + DB 双恢复，失败保留所有证据）
# ---------------------------------------------------------------------------
def rollback_restore(restore_id: str, *, file_undo: list[dict], backup_dir,
                     db_path, log_dir=None, allow_external: bool = False) -> dict:
    """按 undo 逆序回滚文件；用保护性 DB 副本经 Connection.backup() 覆盖 db_path。"""
    out: dict = {"ok": False, "restore_id": restore_id, "file_rollback": {},
                 "db_rollback": {}, "errors": []}
    _transition(restore_id, S_ROLLBACK_STARTED, log_dir=log_dir,
                allow_external=allow_external)
    fr = _undo_source_files(restore_id, file_undo, log_dir=log_dir,
                            allow_external=allow_external)
    out["file_rollback"] = fr
    out["errors"].extend(fr.get("errors") or [])
    # DB 反向恢复
    backup_db = Path(backup_dir) / restore_id / "agent.db"
    try:
        if backup_db.is_file():
            dst = sqlite3.connect(str(db_path))
            src = sqlite3.connect(f"file:{backup_db}?mode=ro", uri=True)
            try:
                src.backup(dst)   # 用备份覆盖目标库（单事务原子写）
                dst.commit()
            finally:
                src.close(); dst.close()
            out["db_rollback"] = {"restored_from": str(backup_db), "ok": True}
        else:
            out["db_rollback"] = {"ok": True, "note": "无 DB 备份（该包/阶段未改库）"}
    except Exception as exc:
        out["errors"].append(f"数据库回滚失败：{exc}")
    out["ok"] = not out["errors"]
    _transition(restore_id, S_ROLLBACK_VERIFIED,
                detail={"ok": out["ok"], "errors": out["errors"]},
                log_dir=log_dir, allow_external=allow_external)
    if out["ok"]:
        _transition(restore_id, S_ROLLED_BACK, log_dir=log_dir, allow_external=allow_external)
    else:
        _transition(restore_id, S_ROLLBACK_FAILED,
                    detail={"manual_steps": "保留备份/staging/日志，勿自动重试；人工介入"},
                    log_dir=log_dir, allow_external=allow_external)
    return out


# ---------------------------------------------------------------------------
# 并发锁：同一时刻只允许一个恢复执行（基于日志目录内的锁文件）
# ---------------------------------------------------------------------------
def _lock_path(log_dir=None, allow_external: bool = False) -> Path:
    return _log_dir(log_dir, allow_external=allow_external) / ".active.lock"


def _acquire_lock(log_dir=None, allow_external: bool = False) -> tuple[bool, str]:
    p = _lock_path(log_dir, allow_external=allow_external)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, json.dumps({"pid": os.getpid(), "at": _now()}).encode("utf-8"))
        os.close(fd)
        return True, ""
    except FileExistsError:
        try:
            data = json.loads(p.read_text("utf-8"))
            pid = int(data.get("pid", -1))
        except Exception:
            pid = -1
        alive = False
        if pid > 0:
            try:
                os.kill(pid, 0)
                alive = True
            except OSError:
                alive = False
        if alive:
            return False, f"已有恢复任务在执行（pid={pid}）"
        try:
            p.unlink()
        except OSError:
            pass
        return _acquire_lock(log_dir, allow_external)


def _release_lock(log_dir=None, allow_external: bool = False) -> None:
    try:
        _lock_path(log_dir, allow_external=allow_external).unlink(missing_ok=True)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 主编排：execute_restore
# ---------------------------------------------------------------------------
def execute_restore(restore_id: str, confirmation_token: str, *,
                    package_path, restore_mode: str,
                    conflict_policy: str = "keep_target", delete_extras: bool = False,
                    db_path=None, source_root=None, backup_dir=None,
                    log_dir=None, allow_external: bool = False,
                    do_reanchor: bool = True, fail_inject: str | None = None,
                    plan_files: list[dict] | None = None) -> dict:
    """执行恢复：确认指纹比对 → 保护备份 → staging → 文件+DB → 重锚 → 验证。

    fail_inject 仅供测试故障注入（stage/file/db/reanchor/verify），生产不得传。
    """
    out: dict = {"ok": False, "restore_id": restore_id, "state": None,
                 "stage_results": {}, "errors": [], "warnings": []}
    rec = _read_log(restore_id, log_dir=log_dir, allow_external=allow_external)
    if rec is None:
        return {**out, "errors": ["恢复记录不存在"]}
    if rec.get("state") != S_CONFIRMED:
        return {**out, "errors": [f"状态 {rec.get('state')} 不可执行（需 CONFIRMED）"]}

    got, why = _acquire_lock(log_dir, allow_external)
    if not got:
        return {**out, "errors": [why]}
    try:
        # 执行前重算指纹，与 confirm 时绑定比对（防包/目标在确认后被换）
        current = compute_current_bindings(package_path, restore_mode, conflict_policy,
                                           delete_extras=delete_extras,
                                           db_path=db_path, source_root=source_root)
        confirm_detail = next(
            (s.get("detail", {}) for s in reversed(rec.get("stages") or [])
             if s.get("state") == S_CONFIRMED), {})
        cb = confirm_detail.get("confirmed_bindings") or {}
        if cb and cb != current:
            return {**out, "errors": ["执行前指纹与确认时不一致，拒绝执行；请重新预览确认"]}

        src = _default_source_root(source_root)
        dbp = _default_db_path(db_path)
        file_undo: list[dict] = []
        deleted_undo: list[dict] = []
        stage_dir = None
        applied_files = False

        try:
            # 1) 保护性备份（失败立即中止，零写入）
            bk = _create_protective_backup(restore_id, source_root=src, db_path=dbp,
                                           backup_dir=backup_dir, log_dir=log_dir,
                                           allow_external=allow_external)
            out["stage_results"]["backup"] = {"ok": bk["ok"], "errors": bk["errors"]}
            if not bk["ok"]:
                raise RuntimeError("保护性备份创建失败，中止：" + ";".join(bk["errors"]))

            # 2) staging
            if fail_inject == "stage":
                raise RuntimeError("故障注入：staging 阶段失败")
            st = _stage_package(restore_id, package_path, source_root=src,
                                log_dir=log_dir, allow_external=allow_external)
            out["stage_results"]["staging"] = {"ok": st["ok"], "errors": st["errors"],
                                               "files": len(st["staged_files"])}
            if not st["ok"]:
                raise RuntimeError("staging 失败：" + ";".join(st["errors"][:2]))
            stage_dir = st["staging_dir"]
            snap = st["snapshot"]

            # 2b) 快照兼容校验（隔离副本，零写生产）
            ck = (_validate_snapshot_against_db(snap, db_path=dbp) if snap
                  else {"ok": True, "errors": [], "warnings": ["包无 DB 快照，跳过 DB 校验"]})
            out["stage_results"]["snapshot_check"] = ck
            if not ck["ok"]:
                raise RuntimeError("快照兼容校验失败：" + ";".join(ck["errors"][:2]))
            out["warnings"].extend(ck.get("warnings") or [])

            # 3) 文件应用
            if fail_inject == "file":
                raise RuntimeError("故障注入：文件应用阶段失败")
            plan = plan_files if plan_files is not None else (rec.get("plan_files") or [])
            fr = _apply_source_files(restore_id, stage_dir, src, plan,
                                     conflict_policy=conflict_policy,
                                     log_dir=log_dir, allow_external=allow_external)
            out["stage_results"]["files"] = {"ok": fr["ok"], "applied": len(fr["applied"]),
                                             "errors": fr["errors"]}
            file_undo = fr["undo"]
            applied_files = True
            if not fr["ok"]:
                raise RuntimeError("文件应用失败：" + ";".join(fr["errors"][:2]))

            # 3b) full_replace 删除项（记录 undo 以便回滚）
            if restore_mode == "full_replace" and delete_extras:
                for dpath in (rec.get("deletions") or []):
                    p = Path(dpath)
                    if p.is_file():
                        deleted_undo.append({"path": str(p),
                                             "bytes_b64": base64.b64encode(p.read_bytes()).decode("ascii")})
                        p.unlink()

            # 4) DB 应用
            if snap:
                if fail_inject == "db":
                    raise RuntimeError("故障注入：DB 应用阶段失败")
                dr = (_apply_db_merge(snap, dbp, log_dir=log_dir, restore_id=restore_id,
                                      allow_external=allow_external)
                      if restore_mode == "merge" else
                      _apply_db_full_replace(snap, dbp, log_dir=log_dir,
                                             restore_id=restore_id, allow_external=allow_external))
                out["stage_results"]["database"] = dr
                if not dr["ok"]:
                    raise RuntimeError("数据库应用失败：" + ";".join(dr["errors"][:2]))

            # 5) 路径重锚
            if do_reanchor:
                if fail_inject == "reanchor":
                    raise RuntimeError("故障注入：重锚阶段失败")
                ra = _reanchor_at(src, dbp, dry_run=False)
                out["stage_results"]["reanchor"] = {
                    "ok": ra["ok"], "reanchored": len(ra.get("reanchored", [])),
                    "conflicts": len(ra.get("conflicts", [])),
                    "missing": len(ra.get("missing", [])), "errors": ra.get("errors"),
                }
                if ra.get("conflicts") or ra.get("missing"):
                    out["warnings"].append(
                        f"重锚有 {len(ra.get('conflicts', []))} 冲突 / {len(ra.get('missing', []))} 缺失")
                if not ra["ok"]:
                    raise RuntimeError("路径重锚失败：" +
                                       json.dumps(ra.get("errors"), ensure_ascii=False)[:200])
                _transition(restore_id, S_PATHS_REANCHORED, log_dir=log_dir,
                            allow_external=allow_external)

            # 6) 恢复后验证
            # [S2 fix] 故障注入改为直接抛异常：原先试图插一条 FK 孤儿行，但
            # governance_actions 有多个 NOT NULL 列，INSERT 自身失败被吞，验证照常通过。
            # 直接在验证阶段抛异常，可靠触发「验证失败→ROLLBACK_REQUIRED→回滚」编排。
            if fail_inject == "verify":
                raise RuntimeError("故障注入：恢复后验证阶段强制失败")
            vr = verify_restored_state(restore_id, db_path=dbp, source_root=src,
                                       log_dir=log_dir, allow_external=allow_external)
            out["stage_results"]["verify"] = {"ok": vr["ok"], "errors": vr["errors"],
                                              "warnings": vr["warnings"]}
            if not vr["ok"]:
                raise RuntimeError("恢复后验证失败：" + ";".join(vr["errors"][:2]))

            _transition(restore_id, S_COMPLETED, log_dir=log_dir, allow_external=allow_external)
            out["ok"] = True
            out["state"] = S_COMPLETED
            out["verify"] = vr
            out["message"] = "恢复完成并通过验证"
            if stage_dir:
                shutil.rmtree(stage_dir, ignore_errors=True)
            return out

        except Exception as exc:
            out["errors"].append(str(exc))
            _transition(restore_id, S_ROLLBACK_REQUIRED,
                        detail={"reason": str(exc)[:300]}, log_dir=log_dir,
                        allow_external=allow_external)
            rb = rollback_restore(restore_id, file_undo=file_undo,
                                  backup_dir=_backup_dir(backup_dir, allow_external=allow_external),
                                  db_path=dbp, log_dir=log_dir, allow_external=allow_external)
            if deleted_undo:
                du = _undo_deletions(deleted_undo)
                rb["errors"].extend(du.get("errors") or [])
                rb["ok"] = rb["ok"] and du["ok"]
            out["stage_results"]["rollback"] = rb
            out["state"] = S_ROLLED_BACK if rb["ok"] else "ROLLBACK_FAILED"
            out["message"] = ("恢复失败，已回滚到恢复前状态" if rb["ok"]
                              else "恢复失败且回滚不完整；已保留备份/staging/日志，需人工介入")
            return out
    finally:
        _release_lock(log_dir, allow_external)
