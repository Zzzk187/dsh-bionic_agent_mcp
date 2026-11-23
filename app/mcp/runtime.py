"""MCP 运行时骨架：两个 FastMCP 实例 + 85 个工具共用的底层 helper。

OPT-03 3.1 从 app/mcp_server.py 拆出。**这里不放任何 @*.tool() 函数**：
工具分别住在 core_tools.py（36 个）与 gov_tools.py（49 个），两边都从这里
import 同一个 mcp / mcp_gov 实例与同一份 helper —— 一个实现，两个卖家。

拆之前 help 之间没有跨域调用（AST 实测），所以这里只做下沉不做重排；
函数体逐字搬移，包括注释与空行。
"""
from __future__ import annotations

import sys
from pathlib import Path
import json
import re
from datetime import datetime, timezone

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from mcp.server.fastmcp import FastMCP
from app.db import init_db, connect, rowdict
from app.embeddings import (DEFAULT_MODEL, EmbeddingUnavailable, content_hash,
                            embed_passages, embed_query, cosine_similarity,
                            ensure_warmup, model_state)
from app.governance_rules import load_rules, RULE_VERSION
from app import governance_service as gov
from app import governance_maintenance as maint
from app import memory_lifecycle as life
from app import feedback_loop
from app import task_resume
from app import expiry
from app import snapshots
from app import protection
from app import distillation
from app import activation_spreading as spreading
from app import knowledge
from app import portable_backup as _pbackup
from app import portable_restore as _prestore
# store 层只被 _event 用一次，但 import 放在这里而不是函数内 —— 函数内 import
# 会让「所有 SQL 都已下沉」这件事从 grep 看不出来（下一个人还得点进去看）。
from app.mcp.stores import task_store as _taskstore

# Two FastMCP instances share the same process/module state but are launched as
# separate MCP servers so the host can mount only the lightweight core instance
# for daily use and attach the heavier governance instance on demand. This keeps
# the per-session fixed tool-list token cost down (core = 36 tools).
# Both import the identical app.* backend, so there is no duplicated logic.
mcp = FastMCP("bionic-agent")                 # core: 36 tools (daily + export/validate/memory packages)
mcp_gov = FastMCP("bionic-agent-governance")  # governance / lifecycle / Phase 8 tools

# Preload the embedding model in the background at server startup so the first
# semantic search / recall does not block on a network model download (which
# previously tripped the MCP client timeout). The worker downloads once and the
# instance is cached; see app/embeddings.py.
ensure_warmup(DEFAULT_MODEL)


GOVERNANCE_COLUMNS = {
    "importance_score", "confidence_score", "freshness_score", "access_count",
    "last_accessed_at", "last_evaluated_at", "memory_status", "protected",
}


def _ensure_db():
    init_db()


def _keyword_only_search(query: str, limit: int, category: str) -> list[dict]:
    """Keyword-only fallback used while the embedding model is still loading."""
    from app import memory_lifecycle as _life
    # reuse the keyword path from the fused recall (no semantic component).
    rows = _life._keyword_fallback(query, limit=limit, category=category)
    return [_life._clean(dict(r)) for r in rows]


def _touch_retrieved(items) -> None:
    """Record a real retrieval for the items actually returned to the caller.

    Only access_count / last_accessed_at move (the raw signal behind the
    frequency component). No confidence change, no feedback row, no rescore —
    so "being surfaced must not promote a memory" still holds.
    """
    if not items:
        return
    try:
        ids = [int(it["id"]) for it in items if it.get("id") is not None]
        gov.touch_access(ids)
    except Exception:  # noqa: BLE001 — bookkeeping must not break retrieval
        pass


def _event(event_type: str, detail: str, task_id=None):
    with connect() as conn:
        _taskstore.insert_task_event(conn, task_id, event_type, detail[:2000])


def _memory_out(row, include_governance: bool = False):
    result = rowdict(row)
    if result and "tags_json" in result:
        try:
            result["tags"] = json.loads(result.pop("tags_json") or "[]")
        except (TypeError, json.JSONDecodeError):
            result["tags"] = []
            result.pop("tags_json", None)
    if result and not include_governance:
        for key in GOVERNANCE_COLUMNS:
            result.pop(key, None)
    return result


def _pkg_path(package_id: str):
    """package_id → 受管包路径；非法 id 一律拒绝，绝不解析自由路径。"""
    pid = str(package_id or "").strip()
    if (not pid or pid in (".", "..") or "/" in pid or "\\" in pid
            or re.match(r"^[A-Za-z]:", pid) or ".." in pid
            or not pid.startswith("bionic-memory-")):
        return None, "非法 package_id（必须是受管包 stem，形如 bionic-memory-...）"
    try:
        pdir = _pbackup._resolve_package_dir().resolve()
    except ValueError as exc:
        return None, str(exc)
    target = (pdir / (pid + ".zip")).resolve()
    try:
        target.relative_to(pdir)
    except ValueError:
        return None, "包路径越出受管目录"
    return str(target), None


def _tool_result(operation: str, inner: dict, *, restore_id=None) -> dict:
    """MCP 边界轻量适配：底层返回原样放 result，外层附统一外壳。"""
    ok = bool(inner.get("ok"))
    errors = list(inner.get("errors") or [])
    if not ok and not errors and inner.get("message"):
        errors = [str(inner["message"])]
    out = {
        "ok": ok, "operation": operation, "result": inner,
        "warnings": list(inner.get("warnings") or []), "errors": errors,
    }
    if restore_id is not None:
        out["restore_id"] = restore_id
    status = inner.get("status") or inner.get("state")
    if status:
        out["status"] = status
    return out


def _tool_error(operation: str, message) -> dict:
    return {"ok": False, "operation": operation, "result": {},
            "warnings": [], "errors": [str(message)]}


def _run(server, transport):
    _ensure_db()
    server.run(transport=transport)


def _run_cli():
    """Console-script entry (registered in pyproject.toml as `bionic-agent`)."""
    _run(mcp, "stdio")
