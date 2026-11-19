"""Portable Memory Package —— 记忆包导出与校验（S1）。

本模块**只做导出与校验**，不含恢复/导入/合并/完整替换（留给 S2+）：
  * export_portable()           四种模式导出，写临时文件后原子改名为正式 ZIP
  * validate_package_archive()  14 项安全与完整性校验（只读，不解压到正式目录）
  * inspect_package()           只读查看包摘要（附带校验）
  * list_packages()             列出受管目录中的包
  * build_manifest()            构造 manifest.json 内容
  * calculate_sha256()          流式 SHA-256

数据安全红线（实现层面恪守）：
  * 生产库只用只读连接 + Connection.backup() 隔离副本做逻辑快照，绝不写生产库。
  * 不修改 dsh-memory 权威源；只读取。
  * 默认包目录 data/memory_packages/，BIONIC_PACKAGE_DIR 覆盖时必须仍在项目根内。
  * 包内一律相对路径 + '/' 分隔符，拒绝 '..'、盘符、绝对路径、反斜杠、符号链接逃逸。
  * 凭据 / .env / console-token / token / secret / 模型缓存 / var/ 一律排除。

依赖：仅标准库，外加复用 app.db（路径/连接参数）、app.embeddings（模型名）、
app.snapshots（表枚举常量）。不新增第三方依赖。
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sqlite3
import stat
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from . import db, embeddings, snapshots, knowledge as _knowledge

# 复用 knowledge 的唯一口径：读 DSH_MEMORY_ROOT 环境变量，默认 C:\Users\xing\.dsh\dsh-memory
DEFAULT_MEMORY_ROOT = _knowledge.DSH_MEMORY_ROOT

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
FORMAT_NAME = "bionic-memory-package"
FORMAT_VERSION = 1
PROJECT_NAME = "bionic-agent-mcp"
GENERATOR_VERSION = "s1-portable-backup-v1"

BACKUP_TYPES = ("source_only", "source_with_db", "full", "knowledge_tasks")

# 安全上限
MAX_FILE_SIZE = 100 * 1024 * 1024          # 单文件 100MB
MAX_TOTAL_UNCOMPRESSED = 1024 * 1024 * 1024  # 解压总量 1GB
MAX_FILE_COUNT = 20000
MAX_COMPRESSION_RATIO = 200                # 单条目压缩比异常阈值
HASH_CHUNK = 1024 * 1024

# 敏感/禁止入包的名称片段（大小写不敏感）
SENSITIVE_PARTS = (
    ".env", "console-token", "secret", "password", "credential",
    "private_key", "id_rsa", ".pem", "token",
)
SENSITIVE_DIRS = {"var", "__pycache__", "model_cache", "node_modules", ".git", "backups"}
# 明确禁止入包的文件名（即使类别匹配）
SENSITIVE_NAMES = {".env", ".env.example", "console-token", "token", "secret"}

CHECKSUM_RE = re.compile(r"^([0-9a-fA-F]{64})  (.+)$")

# dsh-memory 源目录分类（相对 mem_root 的路径，posix 风格）
_TOP_WAKE = ("约定.md", "性格.md", "记忆.md", "记忆索引.md")


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------
def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _default_package_dir() -> Path:
    return Path(db.DB_PATH).resolve().parent / "memory_packages"


def _resolve_package_dir(package_dir=None, allow_external_dir: bool = False) -> Path:
    """解析包目录；默认强制位于项目根内，防止任意路径写入。"""
    if package_dir is None:
        p = _default_package_dir()
    else:
        p = Path(package_dir)
    p = p.resolve()
    if not allow_external_dir:
        root = _project_root().resolve()
        try:
            p.relative_to(root)
        except ValueError:
            raise ValueError(f"包目录必须位于项目根内：{p}（root={root}）")
    return p


def calculate_sha256(source, *, stream: bool = True) -> str:
    """计算 SHA-256。source 可为：文件路径(Path/str)、bytes、或二进制文件对象。

    大文件流式读取，避免一次性载入内存。
    """
    h = hashlib.sha256()
    if isinstance(source, (str, Path)):
        with open(source, "rb") as fh:
            for chunk in iter(lambda: fh.read(HASH_CHUNK), b""):
                h.update(chunk)
    elif isinstance(source, (bytes, bytearray)):
        h.update(bytes(source))
    elif hasattr(source, "read"):
        while True:
            chunk = source.read(HASH_CHUNK)
            if not chunk:
                break
            h.update(chunk)
    else:
        raise TypeError(f"不支持的 SHA-256 输入类型：{type(source)!r}")
    return h.hexdigest()


def _safe_rel(rel: str) -> bool:
    """包内相对路径安全性检查。"""
    if not rel or rel.startswith("/") or rel.startswith("\\"):
        return False
    if re.match(r"^[A-Za-z]:", rel):       # 盘符绝对路径
        return False
    if "\\" in rel:                         # 反斜杠（强制 '/'）
        return False
    parts = PurePosixPath(rel).parts
    if any(p in ("..", ".", "") for p in parts):
        return False
    return True


def _is_sensitive(rel: str) -> bool:
    low = rel.lower()
    parts = set(PurePosixPath(low).parts)
    if parts & SENSITIVE_DIRS:
        return True
    name = PurePosixPath(low).name
    if name in SENSITIVE_NAMES:
        return True
    return any(tok in low for tok in SENSITIVE_PARTS)


# ---------------------------------------------------------------------------
# 版本元数据（真实读取，取不到则明确 unknown）
# ---------------------------------------------------------------------------
def _read_project_version() -> str:
    try:
        import tomllib
        with open(_project_root() / "pyproject.toml", "rb") as fh:
            return str(tomllib.load(fh).get("project", {}).get("version") or "unknown")
    except Exception:
        return "unknown"


def _read_source_version() -> str:
    import subprocess
    try:
        out = subprocess.run(
            ["git", "-C", str(_project_root()), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    return "unknown"


def _sidecar_snapshot(db_path) -> dict:
    """记录 -wal/-shm/-journal 的存在与大小，用于证明这次读取没留下新东西。"""
    out: dict = {}
    for suffix in ("-wal", "-shm", "-journal"):
        p = Path(str(db_path) + suffix)
        try:
            out[suffix] = p.stat().st_size if p.is_file() else None
        except OSError:
            out[suffix] = None
    return out


def _wal_has_content(db_path) -> bool:
    """库旁边是否有一个**装着数据**的 -wal。

    WAL 模式下最近的提交可能还留在 -wal 里、主文件尚未合并。这种时候不能按
    immutable 读（它只看主文件，会读到旧内容），必须真只读打开。
    干净的库在最后一个连接正常关闭后 SQLite 会删掉 -wal/-shm，所以"没有 -wal"
    是最常见的情形——也正是可以走零副作用那条路的情形。
    """
    wal = Path(str(db_path) + "-wal")
    try:
        return wal.is_file() and wal.stat().st_size > 0
    except OSError:
        return True   # 判断不了就当有，走安全的那条


def _readonly_uri(db_path, *, immutable: bool = False) -> str:
    """构造只读 URI（路径做 URL 编码）。

    Windows 的 `D:/a/b.db` 要变成 `file:/D:/a/b.db?mode=ro`：绝对路径需要前导
    斜杠，反斜杠换成正斜杠，特殊字符要转义（否则路径里的 `?`/`#` 会被当成查询
    串/锚点）。加 `immutable=1` 时 SQLite 连 -wal/-shm 都不碰，代价是只认主文件。
    """
    p = str(Path(db_path).resolve()).replace("\\", "/")
    if not p.startswith("/"):
        p = "/" + p
    uri = "file:" + quote(p, safe="/:") + "?mode=ro"
    if immutable:
        uri += "&immutable=1"
    return uri


def read_database_metadata(db_path=None) -> dict:
    """只读读取一个数据库的 schema / embedding 元数据，**可指定库、零写入**。

    `db_path` 省略时才回落到全局 `db.DB_PATH`（保持既有调用兼容）；测试与批量
    场景一律显式传路径，绝不依赖全局状态。

    读法按 WAL 实况二选一，不拿正确性换零副作用：
      * 旁边没有装着数据的 -wal → `mode=ro&immutable=1`：零副作用，连 -shm/-wal
        都不会被创建；
      * 旁边有装着数据的 -wal → `mode=ro`：能读到尚未合并的内容，代价是 SQLite
        会在库旁留下/触碰 -wal、-shm（`sidecar_created` 会如实报出来）。

    返回字段：
      db_path / database_exists / readable / read_mode / user_version /
      schema_version（读不到为 0，保持 `_read_schema_version` 的历史契约）/
      embedding{model_name,dimensions} / error（空串=没出错）/
      sidecar_created / sidecar_after
    """
    target = Path(db_path) if db_path is not None else Path(db.DB_PATH)
    res: dict = {
        "db_path": str(target),
        "database_exists": False,
        "readable": False,
        "read_mode": "",
        "user_version": None,
        "schema_version": 0,
        "embedding": {"model_name": "unknown", "dimensions": None},
        "error": "",
        "sidecar_created": False,
        "sidecar_after": {},
    }
    # 模型名先按运行期默认填上，库里读到真值再覆盖；维度读不到就留 None，不伪造。
    try:
        res["embedding"]["model_name"] = embeddings.DEFAULT_MODEL
    except Exception:
        pass

    try:
        res["database_exists"] = target.is_file()
    except OSError as exc:
        res["error"] = "无法判断库文件是否存在：%s" % exc
        return res
    if not res["database_exists"]:
        # 不存在不是错误：这是"没有库可读"。绝不为读元数据造一个空库。
        return res

    use_immutable = not _wal_has_content(target)
    res["read_mode"] = "immutable" if use_immutable else "readonly"
    before = _sidecar_snapshot(target)
    conn = None
    try:
        conn = sqlite3.connect(_readonly_uri(target, immutable=use_immutable), uri=True)
        # 连接本身可能不碰文件，第一条语句才算真读；所以 readable 在读完才置位。
        try:
            uv = conn.execute("PRAGMA user_version").fetchone()[0]
            res["readable"] = True
            res["user_version"] = int(uv)
            res["schema_version"] = int(uv)
        except sqlite3.Error as exc:
            res["error"] = "读取 user_version 失败：%s" % exc

        if res["readable"]:
            try:
                row = conn.execute(
                    "SELECT model_name, dimension FROM memory_embeddings LIMIT 1"
                ).fetchone()
                if row and row[0]:
                    res["embedding"]["model_name"] = str(row[0])
                if row and row[1]:
                    res["embedding"]["dimensions"] = int(row[1])
            except sqlite3.Error as exc:
                # 没有这张表是正常情形（例如只导源码的库）；只有真正的读错误才报。
                if "no such table" not in str(exc).lower():
                    msg = "读取 embedding 元数据失败：%s" % exc
                    res["error"] = (res["error"] + "；" + msg) if res["error"] else msg
    except sqlite3.Error as exc:
        res["error"] = "无法只读打开数据库：%s" % exc
    except Exception as exc:            # noqa: BLE001 - 元数据读取绝不该炸掉导出
        res["error"] = "读取元数据时出错：%s" % exc
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    after = _sidecar_snapshot(target)
    res["sidecar_after"] = after
    res["sidecar_created"] = any(
        after.get(k) is not None and before.get(k) is None for k in ("-wal", "-shm")
    )
    return res


def _read_schema_version(db_path=None) -> int:
    """从**指定**数据库真实读取 user_version（无法读取返回 0，不伪造）。"""
    return int(read_database_metadata(db_path)["schema_version"])


def _read_embedding_info(db_path=None) -> dict:
    """真实读取 embedding 模型名与维度；读不到明确标 unknown。"""
    return dict(read_database_metadata(db_path)["embedding"])


# ---------------------------------------------------------------------------
# 源目录枚举：按类别收集 (zip_rel, abs_path, category)
# ---------------------------------------------------------------------------
def _classify_source_rel(rel: str) -> str | None:
    """判断 dsh-memory 内一个相对路径（posix）属于哪个类别；None=排除。"""
    top = rel.split("/", 1)[0]
    if "/" not in rel and rel in _TOP_WAKE:
        return "wake"
    if rel == "config.ps1":
        return "config"
    if top == "knowledge" and rel.endswith(".md"):
        return "knowledge"
    if top == "mine":
        if rel.endswith(".md"):
            return "mine_md"
        if PurePosixPath(rel).suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
            return "mine_media"
        return None
    if top == "卷" and rel.endswith(".md"):
        return "volumes"
    if top == "data" and rel.endswith(".jsonl"):
        return "data_jsonl"
    if top == "docs" and rel.endswith(".md"):
        return "docs"
    if top in ("bin", "lib") and rel.endswith(".ps1"):
        return "scripts"
    return None


# 模式 -> 允许的源类别
_MODE_SOURCE_CATEGORIES = {
    "source_only": {"wake", "config", "knowledge", "mine_md", "mine_media",
                    "volumes", "data_jsonl", "docs", "scripts", "agents"},
    "source_with_db": {"wake", "config", "knowledge", "mine_md", "mine_media",
                       "volumes", "data_jsonl", "docs", "scripts", "agents"},
    "full": {"wake", "config", "knowledge", "mine_md", "mine_media",
             "volumes", "data_jsonl", "docs", "scripts", "agents"},
    "knowledge_tasks": {"knowledge", "mine_md"},
}

# 模式 -> 需要的 DB 快照类型：None / "full" / "tasks"
_MODE_DB = {
    "source_only": None,
    "source_with_db": "full",
    "full": "full",
    "knowledge_tasks": "tasks",
}
_MODE_NEEDS_META = {"full"}


def _iter_source_files(source_root: Path, backup_type: str):
    """产出 (zip_rel, abs_path, category)。zip_rel 以 'source/' 开头。"""
    allowed = _MODE_SOURCE_CATEGORIES[backup_type]
    out = []
    if not source_root.is_dir():
        return out
    for path in sorted(source_root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(source_root).as_posix()
        if _is_sensitive(rel):
            continue
        cat = _classify_source_rel(rel)
        if cat is None or cat not in allowed:
            continue
        out.append((f"source/{rel}", path, cat))
    # AGENTS.md 在父目录，属于 source/AGENTS.md（原始位置 ../AGENTS.md）
    if "agents" in allowed:
        agents = source_root.parent / "AGENTS.md"
        if agents.is_file() and not agents.is_symlink():
            out.append(("source/AGENTS.md", agents, "agents"))
    return out


# ---------------------------------------------------------------------------
# DB 逻辑快照（Connection.backup() 隔离副本，绝不写生产库）
# ---------------------------------------------------------------------------
def _dump_db_logical(db_path: Path, mode: str) -> tuple[dict, dict]:
    """用隔离副本生成逻辑快照。mode: 'full' 全表 / 'tasks' 任务快照相关表。"""
    if not Path(db_path).is_file():
        raise FileNotFoundError(f"数据库不存在：{db_path}")
    fd, tmp = tempfile.mkstemp(prefix="s1-dbcopy-", suffix=".db")
    os.close(fd)
    try:
        src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        dst = sqlite3.connect(tmp)
        try:
            src.backup(dst)   # 隔离副本，生产库零写入
        finally:
            src.close()
        dst.row_factory = sqlite3.Row
        if mode == "tasks":
            wanted = [t for t in ("tasks", "snapshots", "events")
                      if _table_exists(dst, t)]
        else:
            wanted = [r[0] for r in dst.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()]
        tables: dict[str, list] = {}
        for name in wanted:
            cols = [r[1] for r in dst.execute(f'PRAGMA table_info("{name}")').fetchall()]
            rows = [dict(r) for r in dst.execute(f'SELECT * FROM "{name}"').fetchall()]
            tables[name] = {"columns": cols, "rows": rows}
        dst.close()
        payload = {
            "created_at": _now_iso(),
            "source": "sqlite-connection-backup-isolated-copy",
            "user_version": _read_schema_version(db_path),
            "schema_version": snapshots.SCHEMA_VERSION,
            "rule_version": snapshots.SNAPSHOT_RULE_VERSION,
            "mode": mode,
            "tables": tables,
        }
        stats = {
            "tables": len(tables),
            "rows": sum(len(v["rows"]) for v in tables.values()),
            "table_names": sorted(tables),
        }
        return payload, stats
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _table_exists(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


# ---------------------------------------------------------------------------
# manifest 构造
# ---------------------------------------------------------------------------
def build_manifest(
    backup_type: str,
    file_entries: list[dict],
    *,
    db_stats: dict | None = None,
    source_stats: dict | None = None,
    warnings: list[str] | None = None,
    excluded: dict | None = None,
    db_path=None,
) -> dict:
    """file_entries: [{path,size_bytes,sha256,category,required}, ...]（不含 manifest/checksums 本身）。

    db_path 指定元数据要读哪个库；省略时回落到全局 db.DB_PATH（兼容旧调用）。
    """
    allowed_categories = sorted(set(_MODE_SOURCE_CATEGORIES[backup_type])
                                | ({"db_snapshot"} if _MODE_DB[backup_type] else set())
                                | ({"project_meta"} if backup_type in _MODE_NEEDS_META else set()))
    return {
        "format_name": FORMAT_NAME,
        "format_version": FORMAT_VERSION,
        "project_name": PROJECT_NAME,
        "project_version": _read_project_version(),
        "schema_version": _read_schema_version(db_path),
        "created_at": _now_iso(),
        "backup_type": backup_type,
        "source_version": _read_source_version(),
        "embedding": _read_embedding_info(db_path),
        "checksum_algorithm": "sha256",
        "generator_version": GENERATOR_VERSION,
        "file_count": len(file_entries),
        "total_size_bytes": sum(int(e["size_bytes"]) for e in file_entries),
        "file_manifest": file_entries,
        "included_categories": allowed_categories,
        "excluded_categories": excluded or {},
        "source_stats": source_stats or {},
        "db_snapshot_stats": db_stats or {},
        "warnings": warnings or [],
    }


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------
def export_portable(
    backup_type: str = "source_only",
    *,
    source_root=None,
    db_path=None,
    package_dir=None,
    allow_external_dir: bool = False,
    source_version_override: str | None = None,
) -> dict:
    """按 backup_type 导出一个便携包，返回结构化结果。"""
    result = {
        "ok": False, "package_id": "", "archive_name": "", "backup_type": backup_type,
        "file_count": 0, "total_size_bytes": 0, "archive_size_bytes": 0,
        "manifest": {}, "validation": {}, "warnings": [], "errors": [],
    }
    if backup_type not in BACKUP_TYPES:
        result["errors"].append(f"未知导出模式：{backup_type}")
        return result

    root = _project_root()
    # 复用 knowledge 的唯一口径（DSH_MEMORY_ROOT 环境变量 / 默认 C:\Users\xing\.dsh\dsh-memory）
    mem_root = Path(source_root) if source_root else Path(DEFAULT_MEMORY_ROOT)
    dbp = Path(db_path) if db_path else Path(db.DB_PATH)

    try:
        pkg_dir = _resolve_package_dir(package_dir, allow_external_dir)
    except ValueError as exc:
        result["errors"].append(str(exc))
        return result
    pkg_dir.mkdir(parents=True, exist_ok=True)

    entries: list[tuple[str, bytes | dict, str, bool]] = []  # (zip_rel, content, category, required)
    warnings: list[str] = []
    excluded: dict[str, str] = {
        "var/": "运行时产物，不备份",
        "model_cache/": "模型缓存可由配置重建，体量大，不打包",
        "credentials": ".env/console-token/token/secret 等凭据禁止入包",
        "__pycache__/*.pyc": "Python 缓存",
    }
    source_stats = {"exists": mem_root.is_dir()}
    if mem_root.is_dir():
        # 不写本机绝对路径（可移植性 + 隐私）：只记相对层级标记
        source_stats["root_marker"] = "../dsh-memory"
        source_stats["file_count"] = 0  # 由下面填充

    # --- 源文件 ---
    src_files = _iter_source_files(mem_root, backup_type)
    for zip_rel, abs_path, cat in src_files:
        size = abs_path.stat().st_size
        if size > MAX_FILE_SIZE:
            warnings.append(f"跳过超大文件 {zip_rel}（{size} B > {MAX_FILE_SIZE} B）")
            continue
        with open(abs_path, "rb") as fh:
            content = fh.read()
        entries.append((zip_rel, content, cat, True))
    if source_stats.get("exists"):
        source_stats["file_count"] = len(src_files)
        source_stats["categories"] = sorted({c for _, _, c in src_files})

    # --- DB 逻辑快照 ---
    db_stats: dict = {}
    db_mode = _MODE_DB[backup_type]
    if db_mode and dbp.is_file():
        try:
            payload, db_stats = _dump_db_logical(dbp, db_mode)
            raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
            entries.append(("database/logical_snapshot.json", raw, "db_snapshot", True))
        except Exception as exc:
            result["errors"].append(f"DB 逻辑快照生成失败：{exc}")
            return result
    elif db_mode and not dbp.is_file():
        result["errors"].append(f"该模式需要数据库，但数据库不存在：{dbp}")
        return result

    # --- 项目元数据（full）---
    if backup_type in _MODE_NEEDS_META:
        meta = {
            "project_name": PROJECT_NAME,
            "project_version": _read_project_version(),
            "schema_version": _read_schema_version(dbp),
            "embedding": _read_embedding_info(dbp),
            "note": "仅非敏感元数据；不含凭据、密钥、日志",
        }
        entries.append(("metadata/project_meta.json",
                        json.dumps(meta, ensure_ascii=False, sort_keys=True).encode("utf-8"),
                        "project_meta", True))

    if not entries:
        result["errors"].append("没有任何可导出的文件（源目录为空或全部被排除）")
        return result

    total = len(entries)
    if total > MAX_FILE_COUNT:
        result["errors"].append(f"文件数 {total} 超过上限 {MAX_FILE_COUNT}")
        return result

    # --- file_manifest + checksums（manifest 本身也要被校验）---
    file_manifest = []
    checksum_lines = []
    for zip_rel, content, cat, required in sorted(entries, key=lambda x: x[0]):
        if not _safe_rel(zip_rel) or _is_sensitive(zip_rel):
            result["errors"].append(f"拒绝不安全路径入包：{zip_rel}")
            return result
        digest = calculate_sha256(content)
        size = len(content)
        file_manifest.append({
            "path": zip_rel, "size_bytes": size, "sha256": digest,
            "category": cat, "required": bool(required),
        })
        checksum_lines.append(f"{digest}  {zip_rel}")

    manifest = build_manifest(
        backup_type, file_manifest, db_stats=db_stats,
        source_stats=source_stats, warnings=warnings, excluded=excluded,
        db_path=dbp,
    )
    if source_version_override is not None:
        manifest["source_version"] = source_version_override
    manifest_bytes = json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
    checksum_lines.append(f"{calculate_sha256(manifest_bytes)}  manifest.json")
    checksum_bytes = ("\n".join(sorted(checksum_lines)) + "\n").encode("utf-8")

    # --- 原子写临时 ZIP ---
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    short = os.urandom(4).hex()
    archive_name = f"bionic-memory-{backup_type}-{stamp}-{short}.zip"
    result["archive_name"] = archive_name
    result["package_id"] = Path(archive_name).stem

    fd, tmp = tempfile.mkstemp(prefix=".s1-pkg-", suffix=".zip", dir=str(pkg_dir))
    os.close(fd)
    tmp_path = Path(tmp)
    try:
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as z:
            for fm in file_manifest:
                content = dict((e[0], e[1]) for e in entries)[fm["path"]]
                z.writestr(fm["path"], content)
            z.writestr("manifest.json", manifest_bytes)
            z.writestr("checksums.sha256", checksum_bytes)
        # 写完后校验临时包，通过才原子改名
        val = validate_package_archive(tmp_path, expected_type=backup_type)
        result["validation"] = val
        if not val.get("ok"):
            result["errors"].append("导出包自校验未通过：" + "; ".join(val.get("errors", [])[:5]))
            return result
        final = pkg_dir / archive_name
        if final.exists():
            # 极低概率碰撞：换名
            archive_name = f"bionic-memory-{backup_type}-{stamp}-{short}-{os.urandom(2).hex()}.zip"
            final = pkg_dir / archive_name
            result["archive_name"] = archive_name
            result["package_id"] = Path(archive_name).stem
        os.replace(tmp_path, final)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass

    result["ok"] = True
    result["file_count"] = len(file_manifest)
    result["total_size_bytes"] = sum(e["size_bytes"] for e in file_manifest)
    result["archive_size_bytes"] = final.stat().st_size
    result["manifest"] = manifest
    result["warnings"] = warnings
    return result


# ---------------------------------------------------------------------------
# 校验（只读；不解压到正式目录）
# ---------------------------------------------------------------------------
def _parse_checksums(raw: bytes) -> dict[str, str]:
    out = {}
    for line in raw.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        m = CHECKSUM_RE.match(line)
        if m:
            out[m.group(2)] = m.group(1).lower()
    return out


def validate_package_archive(
    package_path, expected_type: str | None = None, *, max_total_bytes: int = MAX_TOTAL_UNCOMPRESSED,
) -> dict:
    """14 项安全与完整性校验。返回结构化结果，任一关键项失败 ok=False。"""
    out = {
        "ok": False, "package_id": Path(package_path).stem, "errors": [], "warnings": [],
        "checks": {}, "file_count": 0, "total_size_bytes": 0,
    }
    errs = out["errors"]
    warns = out["warnings"]
    checks = out["checks"]
    path = Path(package_path)

    # 1. ZIP 可读
    try:
        zf = zipfile.ZipFile(path, "r")
        checks["zip_open"] = True
    except Exception as exc:
        errs.append(f"1.ZIP 无法读取：{exc}")
        checks["zip_open"] = False
        return out

    try:
        infos = zf.infolist()
        names = [i.filename for i in infos]

        # 10. 重复路径 / 大小写冲突
        seen = set()
        seen_ci = {}
        dup = []
        ci_dup = []
        for n in names:
            if n in seen:
                dup.append(n)
            seen.add(n)
            k = n.lower()
            if k in seen_ci and seen_ci[k] != n:
                ci_dup.append((seen_ci[k], n))
            seen_ci[k] = n
        checks["unique_paths"] = not dup
        if dup:
            errs.append(f"10.重复路径：{dup[:5]}")
        checks["case_conflicts"] = not ci_dup
        if ci_dup:
            errs.append(f"10.大小写冲突路径：{ci_dup[:5]}")

        # 9. 路径安全
        bad_paths = [n for n in names if not _safe_rel(n)]
        checks["path_safety"] = not bad_paths
        if bad_paths:
            errs.append(f"9.不安全路径：{bad_paths[:5]}")

        # 13. 敏感文件
        sensitive = [n for n in names if _is_sensitive(n)]
        checks["no_sensitive"] = not sensitive
        if sensitive:
            errs.append(f"13.疑似凭据/敏感文件入包：{sensitive[:5]}")

        # 11. 数量/总量/单文件
        total_size = sum(i.file_size for i in infos)
        biggest = max((i.file_size for i in infos), default=0)
        out["file_count"] = len(names)
        out["total_size_bytes"] = total_size
        checks["size_limits"] = (
            len(names) <= MAX_FILE_COUNT and total_size <= max_total_bytes
            and biggest <= MAX_FILE_SIZE
        )
        if not checks["size_limits"]:
            errs.append(f"11.数量/体积超限：count={len(names)} total={total_size} max={biggest}")

        # 12. 压缩比 / ZIP 炸弹：仅当「压缩比异常」且「单条目解压后体积也异常大」才拒。
        # 高压缩比本身不足以定罪——重复文本这类合法文件压缩比可达数百。
        suspicious = [
            (i.filename, i.file_size / i.compress_size)
            for i in infos
            if i.compress_size > 0
            and i.file_size / i.compress_size > MAX_COMPRESSION_RATIO
            and i.file_size > 10 * 1024 * 1024
        ]
        checks["compression_ratio"] = not suspicious
        if suspicious:
            errs.append(f"12.压缩比异常且体积超限：{[(n, round(r, 1)) for n, r in suspicious[:5]]}")

        # 2. CRC / testzip（损坏 ZIP 可能抛解压异常，必须捕获而非冒泡）
        try:
            bad_crc = zf.testzip()
            checks["crc"] = bad_crc is None
            if bad_crc is not None:
                errs.append(f"2.CRC 校验失败：{bad_crc}")
        except Exception as exc:
            checks["crc"] = False
            errs.append(f"2.CRC 校验异常（ZIP 损坏）：{type(exc).__name__}: {exc}")

        # 3. manifest 存在且合法
        if "manifest.json" not in names:
            errs.append("3.缺少 manifest.json")
            checks["manifest_present"] = False
            return out
        checks["manifest_present"] = True
        try:
            manifest = json.loads(zf.read("manifest.json"))
            checks["manifest_parse"] = isinstance(manifest, dict)
        except Exception as exc:
            errs.append(f"3.manifest 解析失败：{exc}")
            checks["manifest_parse"] = False
            return out

        required_fields = ("format_name", "format_version", "backup_type",
                           "file_manifest", "checksum_algorithm")
        missing_fields = [f for f in required_fields if f not in manifest]
        checks["manifest_fields"] = not missing_fields
        if missing_fields:
            errs.append(f"3.manifest 缺字段：{missing_fields}")

        # 4. format_name / version
        fmt_ok = manifest.get("format_name") == FORMAT_NAME
        ver = manifest.get("format_version")
        try:
            ver_int = int(ver)
        except (TypeError, ValueError):
            ver_int = -1
        if not fmt_ok:
            errs.append(f"4.format_name 不支持：{manifest.get('format_name')}")
        if ver_int < 1 or ver_int > FORMAT_VERSION:
            errs.append(f"4.format_version 不支持（未知高版本拒绝）：{ver}")
        checks["format_supported"] = bool(fmt_ok and 1 <= ver_int <= FORMAT_VERSION)
        if expected_type and manifest.get("backup_type") != expected_type:
            errs.append(f"4.备份类型不符：期望 {expected_type}，实际 {manifest.get('backup_type')}")
            checks["type_matches"] = False
        else:
            checks["type_matches"] = True

        # 5. checksums 存在可解析
        if "checksums.sha256" not in names:
            errs.append("5.缺少 checksums.sha256")
            checks["checksums_present"] = False
            return out
        checks["checksums_present"] = True
        try:
            expected = _parse_checksums(zf.read("checksums.sha256"))
            checks["checksums_parse"] = bool(expected)
        except Exception as exc:
            errs.append(f"5.checksums 解析失败：{exc}")
            checks["checksums_parse"] = False
            return out

        # 6. 逐文件重算 SHA-256（重新读取 ZIP 实际内容）
        fm = manifest.get("file_manifest") or []
        mismatches = []
        for entry in fm:
            p = entry.get("path", "")
            want = str(entry.get("sha256", "")).lower()
            if p not in names:
                mismatches.append((p, "MISSING"))
                continue
            try:
                got = calculate_sha256(zf.read(p))
            except Exception as exc:
                mismatches.append((p, f"READ_ERR:{exc}"))
                continue
            if got != want:
                mismatches.append((p, f"{want[:12]}!={got[:12]}"))
        checks["sha256_match"] = not mismatches
        if mismatches:
            errs.append(f"6.SHA-256 不匹配：{mismatches[:5]}")

        # 6b. manifest.json 本身也要被 checksums 校验
        man_hash = expected.get("manifest.json")
        actual_man = calculate_sha256(zf.read("manifest.json"))
        checks["manifest_checksum"] = man_hash == actual_man
        if man_hash != actual_man:
            errs.append("6.manifest.json 的 SHA-256 与 checksums 不一致")

        # 7. manifest 清单与 ZIP 实际集合一致（排除 manifest/checksums 本身）
        data_names = set(n for n in names if n not in ("manifest.json", "checksums.sha256"))
        fm_names = set(str(e.get("path", "")) for e in fm)
        checks["manifest_zip_set_match"] = data_names == fm_names
        if data_names != fm_names:
            errs.append(f"7.清单与实际文件集合不一致：多={sorted(data_names - fm_names)[:3]} "
                        f"少={sorted(fm_names - data_names)[:3]}")
        # checksums 覆盖集合
        check_names = set(expected) - {"manifest.json"}
        checks["checksums_cover"] = check_names == data_names
        if check_names != data_names:
            errs.append("7.checksums 覆盖集合与数据文件不一致")

        # 8. 必需文件齐全
        required_missing = [str(e.get("path")) for e in fm
                            if e.get("required") and str(e.get("path")) not in names]
        checks["required_present"] = not required_missing
        if required_missing:
            errs.append(f"8.必需文件缺失：{required_missing[:5]}")

        # 14. embedding 信息真实完整
        emb = manifest.get("embedding") or {}
        model_ok = bool(emb.get("model_name")) and emb.get("model_name") != "unknown"
        dim = emb.get("dimensions")
        dim_ok = isinstance(dim, int) and dim > 0
        checks["embedding_info"] = model_ok and dim_ok
        if not checks["embedding_info"]:
            warns.append(f"14.Embedding 元数据不完整（model={emb.get('model_name')}, dim={dim}）")

    finally:
        zf.close()

    out["ok"] = not errs
    return out


def inspect_package(package_path, expected_type: str | None = None) -> dict:
    """只读查看包摘要：manifest 关键字段 + 校验结果。"""
    val = validate_package_archive(package_path, expected_type=expected_type)
    out = {
        "ok": val["ok"], "package_id": val["package_id"], "validation": val,
        "manifest": {}, "file_count": val["file_count"],
        "total_size_bytes": val["total_size_bytes"],
    }
    try:
        with zipfile.ZipFile(package_path, "r") as zf:
            if "manifest.json" in zf.namelist():
                m = json.loads(zf.read("manifest.json"))
                out["manifest"] = {
                    "format_name": m.get("format_name"),
                    "format_version": m.get("format_version"),
                    "backup_type": m.get("backup_type"),
                    "created_at": m.get("created_at"),
                    "project_version": m.get("project_version"),
                    "schema_version": m.get("schema_version"),
                    "embedding": m.get("embedding"),
                    "included_categories": m.get("included_categories"),
                    "db_snapshot_stats": m.get("db_snapshot_stats"),
                    "source_stats": m.get("source_stats"),
                }
    except Exception as exc:
        out["ok"] = False
        out.setdefault("errors", []).append(f"inspect 读取失败：{exc}")
    return out


def list_packages(package_dir=None, allow_external_dir: bool = False) -> dict:
    """列出受管目录中的包（不打开内容，只看文件名/大小/时间）。"""
    try:
        p = _resolve_package_dir(package_dir, allow_external_dir)
    except ValueError as exc:
        return {"ok": False, "errors": [str(exc)], "packages": []}
    if not p.is_dir():
        return {"ok": True, "packages": [], "package_dir": str(p)}
    items = []
    for f in sorted(p.glob("bionic-memory-*.zip")):
        st = f.stat()
        items.append({
            "name": f.name, "package_id": f.stem, "size_bytes": st.st_size,
            "created_at": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
        })
    return {"ok": True, "packages": items, "package_dir": str(p), "count": len(items)}


def delete_package(package_id: str, *, package_dir=None,
                   allow_external_dir: bool = False) -> dict:
    """S3 受管删除一个便携包（只删受管目录内的 bionic-memory-*.zip）。

    package_id 就是 list_packages 返回的 stem，不接受任何自由路径。
    绝不删保护性备份（restore_backups/）、staging 或受管目录外的任何文件。
    """
    out = {"ok": False, "package_id": str(package_id or ""), "deleted_path": "",
           "errors": [], "warnings": []}
    pid = str(package_id or "").strip()
    if (not pid or pid in (".", "..") or "/" in pid or "\\" in pid
            or re.match(r"^[A-Za-z]:", pid) or ".." in pid
            or not pid.startswith("bionic-memory-")):
        out["errors"].append("非法 package_id（必须是受管包 stem，形如 bionic-memory-...）")
        return out
    try:
        p = _resolve_package_dir(package_dir, allow_external_dir)
    except ValueError as exc:
        out["errors"].append(str(exc))
        return out
    target = (p / (pid + ".zip")).resolve()
    try:
        target.relative_to(p.resolve())
    except ValueError:
        out["errors"].append("目标不在受管包目录内，拒绝删除")
        return out
    if not target.is_file():
        out["errors"].append("包不存在")
        return out
    try:
        target.unlink()
    except OSError as exc:
        out["errors"].append(f"删除失败：{exc}")
        return out
    out["ok"] = True
    out["deleted_path"] = str(target)
    return out
