"""Bionic Agent 可视化管理面板（FastAPI + 内嵌静态前端）。

这是一个独立的管理服务，直连 `data/agent.db`，复用 `app.mcp_server` /
`app.governance_service` / `app.memory_lifecycle` 里现成的纯函数，因此与 MCP
工具写的是同一个数据库、行为完全一致（不会形成第二套数据）。

启动:  python -m app.admin_ui            (默认 http://127.0.0.1:8899)
       bionic-agent-admin               (pyproject 注册的控制台入口)
       python -m app.admin_ui --port 9000          (只换端口，仍只监听本机)

安全:  面板没有任何身份认证，默认只绑定 127.0.0.1。
       绑定非本机地址（0.0.0.0 / 192.168.x.x / 机器名）会**拒绝启动**，
       必须显式加 --allow-remote 表示你知道后果：
       python -m app.admin_ui --host 0.0.0.0 --allow-remote

前端是一个零外部依赖的自包含 HTML（app/admin_ui.html），离线可用；
语义检索复用 bionic 已缓存的本地 FastEmbed 模型，不联网。
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import sys
import uuid
import zipfile
from datetime import datetime
from contextlib import asynccontextmanager
from pathlib import Path

# 让脚本式启动（`python app/admin_ui.py`）也能 import app.*。
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fastapi import FastAPI, HTTPException, Query, Request  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse  # noqa: E402

# 复用既有后端（这些函数不依赖 MCP 协议，直接操作 SQLite）。
import app.mcp_server as core  # noqa: E402
from app import governance_service as gov  # noqa: E402
from app import memory_lifecycle as life  # noqa: E402
from app import embeddings as emb  # noqa: E402
from app import snapshots as snap  # noqa: E402
from app import protection as protect  # noqa: E402
from app import distillation as distill
from app import task_resume as resume_mod
from app import recall_profile as recall_prof
from app import expiry as expiry_mod  # noqa: E402
from app import feedback_loop as feedback  # noqa: E402
# S4：复用 S1 便携包 / S2 恢复引擎，面板不重写任何业务逻辑。
from app import portable_backup as pbackup  # noqa: E402
from app import portable_restore as prestore  # noqa: E402
from app.db import rowdict as db_rowdict  # noqa: E402

HTML_PATH = Path(__file__).resolve().with_name("admin_ui.html")

# 本次进程的实例标识：每次启动都不同。
#
# 存在的理由（踩过一次）：验痕文件是按"行"追加的，跨进程累积。上一批 DSH 里
# 留下的 iframe 报到会被下一次自检当成本次的证据 —— 明明还没点开面板，自检
# 却报「浏览器半跑起来了」。带上 boot 之后，自检只认「和当前服务同一实例」的
# 报到，重启后的旧记录自动失效。
BOOT_ID = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"


@asynccontextmanager
async def _lifespan(_app):
    """建库/迁移放在启动时做，而不是等某个接口先撞上 no such table。

    MCP 那边每个工具入口都调 `_ensure_db()`，面板这边原先没有对应动作 —— 于是
    全新机器上（data/agent.db 还不存在）`/api/stats` 直接 500，而首页又能正常
    打开，看症状像前端问题。`init_db()` 本身幂等，重复调用无副作用。
    """
    try:
        core._ensure_db()
    except Exception as exc:  # noqa: BLE001 —— 建库失败要让面板起来后能显示错误，而不是进程消失
        print(f"[bionic-agent-admin] 初始化数据库失败：{exc}", file=sys.stderr)
    yield


app = FastAPI(title="Bionic Agent 管理面板", version="0.1.0", lifespan=_lifespan)

# 允许本机页面跨端口访问。
#
# 面板有两种用法：直接在浏览器打开 8899，或者被 DeepSeek Harness 的 client 插件
# 用 iframe 嵌进去（宿主端口随机，如 59647）。后者是同机不同端口，属于跨源——
# 浏览器会拦掉不带 CORS 头的 fetch/XHR。这个服务只监听 127.0.0.1、只给本机用，
# 所以放行本机来源没有额外风险；限死 localhost/127.0.0.1 而不放开 *，是为了
# 万一将来改了监听地址也不至于变成任意网站都能读。
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^https?://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$",
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _parse_ts(value):
    """把库里各种写法的时间戳解析成 datetime；认不出就返回 None。

    见过的格式：`2026-10-29 15:58:58`、`2026-10-29T15:58:58`、
    带微秒/时区的 ISO。认不出时调用方会把这条计入 unparsed 并如实报告，
    而不是假装它属于某个档。
    """
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip().replace("Z", "+00:00")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f",
                "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(text)
        return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed
    except ValueError:
        return None


def _attach_governance(rows: list[dict]) -> list[dict]:
    """把列表结果里缺的治理字段补回来（access_count / 保护 / 三分数）。

    `_memory_out` 默认剔除这些列，那是给 MCP 工具用的输出契约。面板需要
    access_count 才能按访问热度分组 —— 拿不到时前端会静默把所有记忆当成
    "从没翻过"，分组看起来"全在一档"，而界面不会报任何错。

    不直接改 `_memory_out` 的默认行为：那是 MCP 的稳定输出，面板不该为了
    自己的展示需要去改它。
    """
    if not rows:
        return rows
    ids = [r["id"] for r in rows if "id" in r]
    if not ids:
        return rows
    from app.db import connect
    placeholders = ",".join("?" * len(ids))
    with connect() as conn:
        found = conn.execute(
            f"SELECT id, access_count, last_accessed_at, memory_status, protected, "
            f"importance_score, confidence_score, freshness_score "
            f"FROM memories WHERE id IN ({placeholders})",
            ids,
        ).fetchall()
    extra = {r["id"]: dict(r) for r in found}
    for r in rows:
        e = extra.get(r.get("id"))
        if not e:
            continue
        r["access_count"] = e.get("access_count") or 0
        r["last_accessed_at"] = e.get("last_accessed_at")
        r["memory_status"] = e.get("memory_status")
        r["protected"] = bool(e.get("protected"))
        r["importance"] = e.get("importance_score")
        r["confidence_score"] = e.get("confidence_score")
        r["freshness"] = e.get("freshness_score")
    return rows


def _ok(data):
    return JSONResponse(content={"ok": True, "data": data})


def _err(message: str, status: int = 400):
    return JSONResponse(content={"ok": False, "message": message}, status_code=status)


def _run(fn, *args, **kwargs):
    """调用后端函数，把 {"ok":False} 转成 404/400。"""
    try:
        result = fn(*args, **kwargs)
    except Exception as exc:  # 后端内部异常也暴露给前端
        return _err(f"{type(exc).__name__}: {exc}", status=500)
    if isinstance(result, dict) and result.get("ok") is False:
        return _err(result.get("message", "操作失败"), status=404)
    return _ok(result)


# ---------------------------------------------------------------------------
# S4 备份与恢复中心：受管边界 / 脱敏 / 活动包检查（面板不重写业务逻辑）
# ---------------------------------------------------------------------------
_S4_PID_RE = re.compile(r"^bionic-memory-[A-Za-z0-9_.-]+$")

# 恢复进行中（未到终态）的状态集合；命中这些状态的包禁止删除。
_S4_ACTIVE_STATES = {
    "CONFIRMED", "PRE_RESTORE_BACKUP_CREATED", "STAGING_READY",
    "SOURCE_RESTORED", "DATABASE_RESTORED", "PATHS_REANCHORED",
    "POST_RESTORE_VERIFIED", "ROLLBACK_REQUIRED", "ROLLBACK_STARTED",
    "ROLLBACK_VERIFIED",
}


def _backup_pkg_path(package_id):
    """把前端提交的 package_id 解析成受管目录内的 zip 路径。

    前端只能提交 list 返回的 stem，绝不接受路径。这里复用 S1 的
    _resolve_package_dir（强制位于项目根内），拼名后再 relative_to 校验，
    杜绝盘符 / .. / 分隔符注入。
    """
    pid = str(package_id or "").strip()
    if (not _S4_PID_RE.match(pid) or "/" in pid or "\\" in pid or ".." in pid):
        return None, "非法 package_id（必须是 bionic-memory- 开头的受管包 ID）"
    try:
        d = pbackup._resolve_package_dir()
    except ValueError as exc:
        return None, str(exc)
    target = (d / (pid + ".zip")).resolve()
    try:
        target.relative_to(d.resolve())
    except ValueError:
        return None, "目标不在受管包目录内"
    return target, ""


def _active_restore_package_ids():
    """当前处于活动状态（已确认、未到终态）的恢复记录引用的包 ID。

    用于禁止删除"正在恢复的包"。只读 S2 已有日志格式，不另建状态机。
    """
    d = prestore._log_dir()
    ids = set()
    if not d.is_dir():
        return ids
    for p in d.glob("*.json"):
        try:
            with open(p, "r", encoding="utf-8") as fh:
                rec = json.load(fh)
        except (OSError, ValueError):
            continue
        if rec.get("state") in _S4_ACTIVE_STATES and rec.get("package_id"):
            ids.add(rec["package_id"])
    return ids


def _redact_path(value):
    """去掉本机绝对路径，只保留末段，避免向页面暴露用户目录。"""
    if not value:
        return value
    s = str(value).replace("\\", "/")
    if re.match(r"^[A-Za-z]:", s) or s.startswith("/"):
        return ".../" + s.rstrip("/").split("/")[-1]
    return s


def _safe_target_info(target):
    """脱敏 S2 inspect_restore_target 输出，只保留页面需要的安全字段。"""
    if not isinstance(target, dict):
        return target
    keys = ("ok", "source_exists", "db_exists", "embedding", "doc_count",
            "memory_count", "task_count", "vector_count", "fk_violations",
            "mirror", "warnings", "blockers")
    out = {k: target.get(k) for k in keys}
    for key in ("db_fingerprint", "source_fingerprint"):
        fp = target.get(key)
        if isinstance(fp, dict):
            out[key] = {k: v for k, v in fp.items() if k not in ("path", "root")}
    mirror = out.get("mirror")
    if isinstance(mirror, dict):
        out["mirror"] = {k: _redact_path(v) if isinstance(v, str) else v
                         for k, v in mirror.items()}
    return out


def _package_meta(package_id, path):
    """轻量读 manifest 摘要供列表展示；不跑完整校验（校验由单独接口完成）。"""
    meta = {
        "package_id": package_id, "backup_type": "", "format_version": None,
        "created_at": "", "supports_full_restore": False,
        "check_status": "unchecked", "warnings": [],
    }
    try:
        with zipfile.ZipFile(path, "r") as zf:
            if "manifest.json" in zf.namelist():
                m = json.loads(zf.read("manifest.json"))
                bt = m.get("backup_type") or ""
                meta["backup_type"] = bt
                meta["format_version"] = m.get("format_version")
                meta["created_at"] = m.get("created_at") or ""
                meta["supports_full_restore"] = bt in ("full", "source_with_db")
                w = m.get("warnings") or []
                meta["warnings"] = w[:10] if isinstance(w, list) else []
    except (OSError, ValueError, zipfile.BadZipFile):
        meta["check_status"] = "error"
    return meta


# ---------------------------------------------------------------------------
# 记忆 (Memories)
# ---------------------------------------------------------------------------
@app.get("/api/memories")
def api_list_memories(
    q: str = Query("", description="关键词；为空列出最近"),
    category: str = Query(""),
    include_archived: bool = Query(False),
    limit: int = Query(50, ge=1, le=200),
):
    rows = core.search_memories(q, limit, category, include_archived)
    return _ok(_attach_governance(rows))


@app.post("/api/memories")
def api_create_memory(payload: dict):
    content = (payload.get("content") or "").strip()
    if not content:
        return _err("content 不能为空")
    tags = payload.get("tags")
    if isinstance(tags, str):
        tags = [t for t in tags.split(",") if t.strip()]
    return _run(
        core.save_memory,
        content,
        payload.get("category", "general") or "general",
        payload.get("source", "admin-ui") or "admin-ui",
        float(payload.get("confidence", 0.8) or 0.8),
        tags,
    )


# 必须放在 /api/memories/{memory_id} 之前：否则 "timeline" 会被当成 id。
@app.get("/api/memories/timeline")
def api_memories_timeline(axis: str = Query("created", pattern="^(created|updated)$"),
                          include_archived: bool = Query(False)):
    """全库按时间分档：今天 / 昨天 / 近 7 天 / 近 30 天 / 近 90 天 / 更早。

    返回每档的条数与日期范围。axis 选按创建时间还是更新时间分组——
    前者回答「什么时候记的」，后者回答「最近在用的有哪些」。
    """
    from app.db import connect
    col = "created_at" if axis == "created" else "updated_at"
    statuses = "('active','archived')" if include_archived else "('active')"
    with connect() as conn:
        rows = conn.execute(
            f"SELECT {col} AS ts, category FROM memories "
            f"WHERE status IN {statuses} AND memory_status != 'pending_review'"
        ).fetchall()
    now = datetime.now()
    buckets = [
        ("today", "今天", 0, 1),
        ("yesterday", "昨天", 1, 2),
        ("week", "近 7 天", 2, 7),
        ("month", "近 30 天", 7, 30),
        ("quarter", "近 90 天", 30, 90),
        ("older", "更早", 90, None),
    ]
    out = {key: {"key": key, "label": label, "count": 0,
                 "oldest": None, "newest": None, "categories": {}}
           for key, label, _lo, _hi in buckets}
    unparsed = 0
    for r in rows:
        ts = _parse_ts(r["ts"])
        if ts is None:
            unparsed += 1
            continue
        days = (now - ts).total_seconds() / 86400.0
        if days < 0:
            days = 0.0  # 时钟漂移导致的"未来"时间按今天算，别让它凭空消失
        for key, _label, lo, hi in buckets:
            if days >= lo and (hi is None or days < hi):
                b = out[key]
                b["count"] += 1
                cat = (r["category"] or "general")
                b["categories"][cat] = b["categories"].get(cat, 0) + 1
                iso = ts.strftime("%Y-%m-%d")
                if b["oldest"] is None or iso < b["oldest"]:
                    b["oldest"] = iso
                if b["newest"] is None or iso > b["newest"]:
                    b["newest"] = iso
                break
    ordered = [out[k] for k, _l, _lo, _hi in buckets]

    # 按自然日聚合，最新在前。相对档回答"整体新鲜度"，日聚合才回答
    # "哪天记了什么" —— 前者太粗（36 条会全压在"近 30 天"一档），
    # 真正能定位的是日期。
    by_day: dict[str, dict] = {}
    for r in rows:
        ts = _parse_ts(r["ts"])
        if ts is None:
            continue
        # 和上面的相对档保持同一口径：未来时间按今天算。否则同一条记忆在
        # 「今天 3 条」和 by_day 的日期列表里会给出两种说法（一个是今天、
        # 一个是明天），而两个数字都出现在同一块界面上。
        if ts > now:
            ts = now
        day = ts.strftime("%Y-%m-%d")
        slot = by_day.get(day)
        if slot is None:
            slot = by_day[day] = {"date": day, "count": 0, "categories": {},
                                  "ids": []}
        slot["count"] += 1
        cat = (r["category"] or "general")
        slot["categories"][cat] = slot["categories"].get(cat, 0) + 1
    days = sorted(by_day.values(), key=lambda x: x["date"], reverse=True)

    return _ok({
        "axis": axis,
        "total": len(rows) - unparsed,
        "buckets": ordered,
        "by_day": days,
        "days": len(days),
        "unparsed": unparsed,
        "note": "分档按相对天数算，所以「今天/昨天」会随时间推移而移动，"
                "不是固定的日期区间。",
    })


# 同样必须在 /{memory_id} 之前。
@app.get("/api/memories/breakdown")
def api_memories_breakdown(include_archived: bool = Query(False)):
    """全库按分类 / 访问热度 / 时间三个维度统计。

    统一走这一个接口而不是三个，是因为三个维度用的是同一批行；
    分三次查会读到三个不同瞬间的库，数字对不上时排查成本很高。
    """
    from app.db import connect
    statuses = "('active','archived')" if include_archived else "('active')"
    with connect() as conn:
        rows = conn.execute(
            f"SELECT id, category, access_count, last_accessed_at, created_at, "
            f"updated_at, importance_score FROM memories "
            f"WHERE status IN {statuses} AND memory_status != 'pending_review'"
        ).fetchall()

    total = len(rows)

    # --- 分类 ---
    cats: dict[str, dict] = {}
    for r in rows:
        c = (r["category"] or "general")
        slot = cats.setdefault(c, {"key": c, "label": c, "count": 0,
                                   "never_accessed": 0, "newest": None})
        slot["count"] += 1
        if not (r["access_count"] or 0):
            slot["never_accessed"] += 1
        day = str(r["created_at"] or "")[:10]
        if day and (slot["newest"] is None or day > slot["newest"]):
            slot["newest"] = day
    by_category = sorted(cats.values(), key=lambda x: (-x["count"], x["key"]))

    # --- 访问热度 ---
    # 分档依据 access_count 的绝对值，而不是分位数：分位数在"全库都是 0"
    # 的时候仍然会造出四档，看起来像有数据，其实全是 0。
    buckets = [
        ("hot",      "常翻的",   10, None),
        ("warm",     "偶尔翻",   3,  10),
        ("cold",     "冷门",     1,  3),
        ("untouched", "从没翻过", 0,  1),
    ]
    heat = {k: {"key": k, "label": lb, "count": 0, "ids": []}
            for k, lb, _lo, _hi in buckets}
    for r in rows:
        n = int(r["access_count"] or 0)
        for k, _lb, lo, hi in buckets:
            if n >= lo and (hi is None or n < hi):
                heat[k]["count"] += 1
                if len(heat[k]["ids"]) < 20:
                    heat[k]["ids"].append(r["id"])
                break
    by_heat = [heat[k] for k, _lb, _lo, _hi in buckets]
    touched = total - heat["untouched"]["count"]

    return _ok({
        "total": total,
        "by_category": by_category,
        "by_heat": by_heat,
        "heat_note": (
            "全部 %d 条都还没有访问记录 —— 访问统计是这一版才接上的，"
            "以前的召回不会记账。这里的「从没翻过」并不代表没人关心它们。"
            % total if touched == 0 else
            "有访问记录的 %d 条，没有的 %d 条。" % (touched, total - touched)
        ),
        "touched": touched,
    })


@app.get("/api/memories/{memory_id}")
def api_get_memory(memory_id: int, include_archived: bool = Query(True)):
    """打开详情算一次真实访问；列表接口不算。

    列表一屏 60 条，若列表也记，"常翻的"就等于"最近列出来的"——那个数字
    只会反映 UI 布局。
    """
    data = _run(core.get_memory, memory_id, include_archived)
    try:
        gov.touch_access([int(memory_id)])
    except Exception:  # noqa: BLE001 —— 记账失败不该让详情打不开
        pass
    return data


@app.put("/api/memories/{memory_id}")
def api_update_memory(memory_id: int, payload: dict):
    tags = payload.get("tags")
    if isinstance(tags, str):
        tags = [t for t in tags.split(",") if t.strip()]
    return _run(
        core.update_memory,
        memory_id,
        payload.get("content", "") or "",
        payload.get("category", "") or "",
        payload.get("source", "") or "",
        payload.get("confidence"),
        tags,
    )


@app.post("/api/memories/{memory_id}/archive")
def api_archive(memory_id: int):
    return _run(core.archive_memory, memory_id)


@app.post("/api/memories/{memory_id}/restore")
def api_restore(memory_id: int):
    return _run(core.restore_memory, memory_id)


@app.post("/api/memories/{memory_id}/protect")
def api_protect(memory_id: int, payload: dict):
    return _run(gov.set_protected, memory_id, bool(payload.get("protected", True)),
               payload.get("reason", "") or "")


@app.post("/api/memories/{memory_id}/review")
def api_set_review(memory_id: int, payload: dict):
    return _run(gov.set_review_status, memory_id, payload.get("reason", "") or "",
               payload.get("approver", "admin-ui"))


@app.post("/api/memories/{memory_id}/review/release")
def api_release_review(memory_id: int, payload: dict):
    """解除 pending_review —— 没有这个出口，被标记的记忆就再也检索不到。"""
    payload = payload or {}
    return _run(gov.release_review_status, memory_id,
               payload.get("reason", "") or "",
               payload.get("approver", "admin-ui"))


@app.get("/api/memories/{memory_id}/governance")
def api_gov_status(memory_id: int):
    return _run(gov.get_governance_status, memory_id)


@app.get("/api/memories/{memory_id}/details")
def api_details(memory_id: int):
    return _run(life.get_memory_details, memory_id)


@app.get("/api/memories/{memory_id}/related")
def api_related(memory_id: int, max_depth: int = Query(2, ge=1, le=3)):
    return _run(life.get_related_memories, memory_id, max_depth, "")


@app.get("/api/memories/{memory_id}/usage")
def api_usage(memory_id: int):
    return _run(life.get_memory_usage_stats, memory_id)


@app.post("/api/memories/{memory_id}/feedback")
def api_feedback(memory_id: int, payload: dict):
    return _run(life.record_memory_feedback, memory_id,
               payload.get("feedback_type", "helpful"), payload.get("note", "") or "")


@app.post("/api/memories/{memory_id}/governance-archive")
def api_gov_archive(memory_id: int, payload: dict):
    return _run(gov.archive_memory_governance, memory_id, payload.get("reason", "") or "")


@app.post("/api/memories/{memory_id}/governance-restore")
def api_gov_restore(memory_id: int, payload: dict):
    return _run(gov.restore_memory_governance, memory_id, payload.get("reason", "") or "")


@app.get("/api/memories/search/semantic")
def api_semantic(q: str = Query(""), limit: int = Query(10, ge=1, le=50),
                 category: str = Query(""), min_score: float = Query(-1.0)):
    if not q.strip():
        return _err("q 不能为空")
    result = core.semantic_search_memories(q, limit, category, min_score)
    # 模型未就绪时会降级成关键词检索（返回 dict），统一成 {results, degraded}
    if isinstance(result, dict) and ("degraded" in result or "error" in result):
        return _ok({"degraded": result.get("degraded", False),
                    "reason": result.get("reason") or result.get("error"),
                    "message": result.get("message"),
                    "results": result.get("results", [])})
    return _ok({"degraded": False, "results": result})


# ---------------------------------------------------------------------------
# 关系 (Links)
# ---------------------------------------------------------------------------
@app.post("/api/links")
def api_create_link(payload: dict):
    return _run(core.link_memories,
               int(payload.get("source_memory_id", 0)),
               int(payload.get("target_memory_id", 0)),
               payload.get("relation", "related") or "related",
               payload.get("note", "") or "")


@app.get("/api/memories/{memory_id}/links")
def api_links(memory_id: int):
    return _run(core.get_memory_links, memory_id)


# ---------------------------------------------------------------------------
# 任务 (Tasks)
# ---------------------------------------------------------------------------
@app.get("/api/tasks")
def api_tasks():
    return _ok(core.list_tasks())


@app.post("/api/tasks")
def api_create_task(payload: dict):
    title = (payload.get("title") or "").strip()
    goal = (payload.get("goal") or "").strip()
    if not title or not goal:
        return _err("title 与 goal 不能为空")
    return _run(core.create_task, title, goal)


@app.get("/api/tasks/{task_id}")
def api_get_task(task_id: int):
    return _run(core.get_task, task_id)


@app.post("/api/tasks/{task_id}/snapshot")
def api_snapshot(task_id: int, payload: dict):
    return _run(core.save_stage_snapshot, task_id, payload.get("summary", "") or "",
               payload.get("state_json", "{}") or "{}")


@app.post("/api/tasks/{task_id}/handoff")
def api_handoff(task_id: int, payload: dict):
    return _run(core.handoff_task, task_id, payload.get("summary", "") or "",
               payload.get("state_json", "{}") or "{}")


@app.post("/api/tasks/{task_id}/complete")
def api_complete(task_id: int, payload: dict):
    return _run(core.complete_task, task_id, payload.get("result", "") or "")


@app.post("/api/tasks/{task_id}/resume")
def api_resume(task_id: int):
    return _run(core.resume_task, task_id)


# ---------------------------------------------------------------------------
# 治理 (Governance)
# ---------------------------------------------------------------------------
@app.get("/api/embedding_status")
def api_embedding_status():
    return _ok(core.embedding_status())


# ---------------------------------------------------------------------------
# 记忆星图 (Star map)
# ---------------------------------------------------------------------------
@app.get("/api/starmap")
def api_starmap(limit: int = Query(600, ge=1, le=3000),
                include_archived: bool = Query(False),
                threshold: float = Query(0.62, ge=0.0, le=1.0)):
    """返回 {nodes, edges, meta} 供前端 Canvas 画 3D 星图。

    走 star_map.build_graph —— 纯 Python、无 numpy，拷走就能跑。
    """
    from app import star_map  # 延迟导入：没用到星图时不必加载

    try:
        graph = star_map.build_graph(
            limit=limit,
            include_archived=include_archived,
            semantic_threshold=threshold,
        )
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            status_code=500,
            content={"ok": False, "message": f"{type(exc).__name__}: {exc}"},
        )
    return _ok(graph)


@app.post("/api/embedding/warmup")
def api_warmup():
    emb.ensure_warmup(emb.DEFAULT_MODEL)
    return _ok(emb.model_state())


@app.get("/api/governance/score")
def api_score(category: str = Query(""), limit: int = Query(50, ge=1, le=200)):
    # 直接调服务函数并自己包一层，而不是先包成 JSONResponse 再反序列化回来改。
    # score_memories 只返回评分列、不含 category，界面要显示分类所以这里补齐。
    try:
        result = gov.score_memories(None, limit, category, True)
    except Exception as exc:
        return _err(f"{type(exc).__name__}: {exc}", status=500)
    if not isinstance(result, dict) or result.get("ok") is False:
        return _err(result.get("message", "评分失败") if isinstance(result, dict) else "评分失败",
                    status=404)
    rows = result.get("results") or []
    ids = [r.get("memory_id") for r in rows if isinstance(r, dict) and r.get("memory_id")]
    if ids:
        from app.db import connect
        placeholders = ",".join("?" * len(ids))
        with connect() as conn:
            cats = {row[0]: (row[1] or "") for row in conn.execute(
                f"SELECT id, category FROM memories WHERE id IN ({placeholders})", ids)}
        for r in rows:
            if isinstance(r, dict) and not r.get("category"):
                r["category"] = cats.get(r.get("memory_id"), "")
    return _ok(result)


@app.post("/api/governance/detect")
def api_detect(payload: dict):
    # 注意：detect_conflicts(memory_ids, category, candidate_count, persist)
    # 第三个位置参数是 candidate_count（int），不能传 "" 占位，否则 int("") 抛错。
    return _run(gov.detect_conflicts, None, payload.get("category", "") or "",
                payload.get("candidate_count", 8), True)


@app.get("/api/governance/conflicts")
def api_conflicts(resolution: str = Query(""), memory_id: int = Query(0),
                  limit: int = Query(50, ge=1, le=200)):
    # list_conflicts(memory_id, resolution, limit) —— 这里第三个参数是 limit(int)。
    return _run(gov.list_conflicts, memory_id or None, resolution, limit)


@app.post("/api/governance/conflicts/{conflict_id}/review")
def api_review_conflict(conflict_id: int, payload: dict):
    return _run(gov.review_conflict, conflict_id, payload.get("resolution", "not_conflict"),
               payload.get("note", "") or "")


@app.get("/api/governance/maintenance")
def api_maintenance_preview(mode: str = Query("dry_run"),
                            category: str = Query(""),
                            limit: int = Query(50, ge=1, le=200)):
    return _run(core.run_memory_maintenance, mode, limit, category)


@app.post("/api/governance/maintenance/apply")
def api_maintenance_apply(payload: dict):
    return _run(core.run_memory_maintenance, "apply_approved",
               int(payload.get("limit", 50)), payload.get("category", "") or "")


@app.get("/api/governance/consolidation")
def api_consolidation(status: str = Query(""), limit: int = Query(50, ge=1, le=200)):
    return _run(life.list_consolidation_candidates, status, limit)


@app.get("/api/governance/duplicates")
def api_duplicates(category: str = Query(""), candidate_count: int = Query(8, ge=1, le=50)):
    # find_duplicate_memories(memory_ids, category, candidate_count)
    return _run(life.find_duplicate_memories, None, category, candidate_count)


# ---------------------------------------------------------------------------
# 快照（整库备份 / 回滚）
# ---------------------------------------------------------------------------
@app.get("/api/snapshots")
def api_snapshots(limit: int = Query(20, ge=1, le=200)):
    return _run(snap.list_snapshots, limit)


@app.post("/api/snapshots")
def api_snapshot_create(payload: dict):
    return _run(snap.create_snapshot, payload.get("label", "") or "",
                "manual", payload.get("note", "") or "")


@app.post("/api/snapshots/{snapshot_id}/restore")
def api_snapshot_restore(snapshot_id: int, payload: dict):
    return _run(snap.restore_snapshot, snapshot_id, payload.get("note", "") or "")


@app.post("/api/snapshots/prune")
def api_snapshot_prune(payload: dict):
    return _run(snap.prune_snapshots, int(payload.get("keep", 10)))


@app.get("/api/snapshots/vector-check")
def api_snapshot_vector_check():
    return _run(snap.check_vector_consistency_after_restore)


# ---------------------------------------------------------------------------
# S4 备份与恢复中心 —— 写接口：导出与删除（Batch C）
# ---------------------------------------------------------------------------
@app.post("/api/backup/export")
def api_backup_export(payload: dict):
    """创建备份：只接受四模式白名单，直接转调 S1 export_portable。

    不接受任何路径参数（source_root/db_path/package_dir 一律不传），全部走
    S1 受管默认。前端不参与 ZIP 打包与 SHA-256。
    """
    bt = (payload.get("backup_type") or "").strip()
    if bt not in pbackup.BACKUP_TYPES:
        return _err(f"未知导出模式：{bt}（可用：{', '.join(pbackup.BACKUP_TYPES)}）", status=400)
    # source_version_override 只允许内部固定值，不接受前端任意字符串（防伪造版本元数据）
    res = pbackup.export_portable(bt)
    if not res.get("ok"):
        return _err("；".join(res.get("errors") or ["导出失败"]), status=400)
    return _ok({
        "package_id": res.get("package_id"),
        "archive_name": res.get("archive_name"),
        "backup_type": res.get("backup_type"),
        "file_count": res.get("file_count"),
        "total_size_bytes": res.get("total_size_bytes"),
        "archive_size_bytes": res.get("archive_size_bytes"),
        "validation": res.get("validation") or {},
        "warnings": res.get("warnings") or [],
    })


@app.delete("/api/backup/packages/{package_id}")
def api_backup_package_delete(package_id: str):
    """受管删除：先拒绝正在恢复的包，再转调 S1 delete_package。"""
    path, why = _backup_pkg_path(package_id)
    if path is None:
        return _err(why, status=400)
    if package_id in _active_restore_package_ids():
        return _err("该备份包正被一个未完成的恢复任务使用，禁止删除", status=409)
    res = pbackup.delete_package(package_id)
    if not res.get("ok"):
        # 删除失败保留文件，如实报错（绝不伪装成功）
        return _err("；".join(res.get("errors") or ["删除失败"]), status=400)
    return _ok({
        "package_id": package_id,
        "deleted": True,
        "warnings": res.get("warnings") or [],
    })


# ---------------------------------------------------------------------------
# S4 备份与恢复中心 —— 恢复预览（Batch D，转调 S2，面板不重写校验/差异/令牌）
# ---------------------------------------------------------------------------
@app.post("/api/backup/preview")
def api_backup_preview(payload: dict):
    """生成恢复预览与一次性令牌。

    面板不重新实现任何包校验/差异比对/令牌逻辑，全部由 S2 preview_restore 提供
    （其内部三道门：S1 校验器 + 目标检查 + 模式矩阵 + 兼容性）。令牌只在 HTTP
    响应里返回给前端内存持有，后端不写日志、不持久化到磁盘。
    """
    pid = str(payload.get("package_id") or "").strip()
    path, why = _backup_pkg_path(pid)
    if path is None:
        return _err(why, status=400)

    restore_mode = str(payload.get("restore_mode") or "merge").strip()
    conflict_policy = str(payload.get("conflict_policy") or "keep_target").strip()
    if restore_mode not in prestore.RESTORE_MODES:
        return _err(f"未知恢复模式：{restore_mode}", status=400)
    if conflict_policy not in prestore.CONFLICT_POLICIES:
        return _err(f"未知冲突策略：{conflict_policy}", status=400)
    delete_extras = bool(payload.get("delete_extras", False))

    res = prestore.preview_restore(
        path, restore_mode=restore_mode, conflict_policy=conflict_policy,
        delete_extras=delete_extras,
    )
    if not isinstance(res, dict):
        return _err("预览返回格式异常", status=500)

    # 只回传页面需要的字段；package_path 等绝对路径一律不带出。
    out = {
        "ok": bool(res.get("ok")),
        "restore_id": res.get("restore_id"),
        "package_id": res.get("package_id"),
        "package_sha256": res.get("package_sha256"),
        "backup_type": res.get("backup_type"),
        "restore_mode": res.get("restore_mode"),
        "conflict_policy": res.get("conflict_policy"),
        "delete_extras": delete_extras,
        "summary": res.get("summary") or {},
        "deletions": res.get("deletions") or [],
        "compatibility": res.get("compatibility") or {},
        "warnings": res.get("warnings") or [],
        "blockers": res.get("blockers") or [],
        "validation": res.get("validation") or {},
        "backup_required": res.get("backup_required", True),
        "required_space_bytes": res.get("required_space_bytes", 0),
        "target": _safe_target_info(res.get("target") or {}),
        "message": res.get("message") or "",
    }
    # 令牌仅在预览成功（无 blocker）时随响应返回；绝不写入任何日志。
    out["confirmation_token"] = res.get("confirmation_token") if res.get("ok") else ""
    out["token_expire_at"] = res.get("token_expire_at") if res.get("ok") else ""
    return _ok(out)


# ---------------------------------------------------------------------------
# S4 备份与恢复中心 —— 确认与执行（Batch E）
# 确认消费一次性令牌；执行转调 S2 execute_restore，绝不传 fail_inject。
# 面板不重写令牌/校验/回滚逻辑，也不自行生成令牌。
# ---------------------------------------------------------------------------
_S4_FULL_PHRASE = "确认完整替换"


def _scrub_paths(value):
    """脱敏文本中可能出现的本机绝对路径（Windows 盘符 / POSIX 根路径）。"""
    if not isinstance(value, str):
        return value
    s = re.sub(r"[A-Za-z]:[\\/][^\s，,；;\"']+", ".../", value)
    s = re.sub(r"(?<![\w])/(?:Users|home|root)/[^\s，,；;\"']+", ".../", s)
    return s


def _s4_restore_args(payload):
    """从请求体取恢复参数并校验枚举；返回 (pid, path, mode, policy, extras, err)。"""
    pid = str(payload.get("package_id") or "").strip()
    path, why = _backup_pkg_path(pid)
    if path is None:
        return None, None, None, None, None, why
    mode = str(payload.get("restore_mode") or "merge").strip()
    policy = str(payload.get("conflict_policy") or "keep_target").strip()
    if mode not in prestore.RESTORE_MODES:
        return None, None, None, None, None, f"未知恢复模式：{mode}"
    if policy not in prestore.CONFLICT_POLICIES:
        return None, None, None, None, None, f"未知冲突策略：{policy}"
    return pid, path, mode, policy, bool(payload.get("delete_extras", False)), ""


@app.post("/api/backup/confirm")
def api_backup_confirm(payload: dict):
    """显式确认：full_replace 需强确认短语；令牌由 S2 校验并一次性消费。"""
    pid, path, mode, policy, extras, err = _s4_restore_args(payload)
    if err:
        return _err(err, status=400)
    if mode == "full_replace" and str(payload.get("confirm_phrase") or "").strip() != _S4_FULL_PHRASE:
        return _err(f"完整替换必须输入确认短语：{_S4_FULL_PHRASE}", status=400)

    restore_id = str(payload.get("restore_id") or "").strip()
    token = str(payload.get("confirmation_token") or "").strip()
    if not restore_id or not token:
        return _err("缺少 restore_id 或 confirmation_token", status=400)

    res = prestore.confirm_restore(
        restore_id, token, package_path=path, restore_mode=mode,
        conflict_policy=policy, delete_extras=extras,
    )
    if not isinstance(res, dict) or not res.get("ok"):
        msg = (res or {}).get("message") or "确认失败"
        return _err(_scrub_paths(str(msg)), status=400)
    return _ok({
        "restore_id": restore_id,
        "state": res.get("state"),
        "message": _scrub_paths(str(res.get("message") or "")),
    })


@app.post("/api/backup/execute")
def api_backup_execute(payload: dict):
    """执行恢复：确认 state==CONFIRMED 后转调 S2；绝不传 fail_inject。"""
    pid, path, mode, policy, extras, err = _s4_restore_args(payload)
    if err:
        return _err(err, status=400)
    restore_id = str(payload.get("restore_id") or "").strip()
    if not restore_id:
        return _err("缺少 restore_id", status=400)

    # 状态闸门：必须先 confirm（state==CONFIRMED）。重复点击时状态已变，S2 也会拒。
    st = prestore.get_restore_status(restore_id)
    state = ((st or {}).get("status") or {}).get("state")
    if state != "CONFIRMED":
        return _err(f"当前状态 {state or '未知'} 不可执行（需先确认）", status=409)

    # fail_inject / do_reanchor / plan_files 一律不接受前端输入
    res = prestore.execute_restore(
        restore_id, "", package_path=path, restore_mode=mode,
        conflict_policy=policy, delete_extras=extras,
    )
    if not isinstance(res, dict):
        return _err("执行返回格式异常", status=500)
    out = {
        "restore_id": restore_id,
        "ok": bool(res.get("ok")),
        "state": res.get("state"),
        "stage_results": res.get("stage_results") or {},
        "warnings": [_scrub_paths(str(w)) for w in (res.get("warnings") or [])],
        "errors": [_scrub_paths(str(e)) for e in (res.get("errors") or [])],
        "message": _scrub_paths(str(res.get("message") or "")),
    }
    # 失败/回滚不是 HTTP 错误——如实呈现状态，让页面展示 ROLLBACK_*
    return _ok(out)


@app.post("/api/backup/restores/{restore_id}/rollback")
def api_backup_restore_rollback(restore_id: str):
    """回滚结果查询 + 人工介入指引。

    面板不执行真正的回滚：S2 的 rollback_restore 需要 file_undo（仅在 execute
    内存中、未持久化），且 execute_restore 异常时已自动回滚。本接口只读取当前
    回滚状态并给出指引；ROLLBACK_FAILED 标记为严重故障。
    """
    st = prestore.get_restore_status(restore_id)
    if not isinstance(st, dict) or not st.get("ok"):
        return _err("恢复记录不存在", status=404)
    rec = st.get("status") or {}
    state = rec.get("state")
    severe = state == "ROLLBACK_FAILED"
    guidance = ""
    if severe:
        guidance = ("自动回滚未完成；保护性备份与 staging 已保留，"
                    "请勿重复点击恢复，需人工介入处理。")
    elif state in ("ROLLBACK_REQUIRED", "ROLLBACK_STARTED"):
        guidance = "回滚进行中或待回滚，请稍后刷新查看。"
    elif state == "ROLLED_BACK":
        guidance = "已回滚到恢复前状态。"
    else:
        guidance = "当前无待处理回滚。"
    return _ok({
        "restore_id": restore_id,
        "state": state,
        "severe": severe,
        "auto_rollback_by_engine": True,
        "guidance": guidance,
        "errors": [_scrub_paths(str(e)) for e in (rec.get("errors") or [])],
    })


# ---------------------------------------------------------------------------
# S4 备份与恢复中心 —— 只读接口（Batch B）
# ---------------------------------------------------------------------------
@app.get("/api/backup/packages")
def api_backup_packages():
    """备份包列表：真实包信息 + manifest 摘要，不含绝对路径。"""
    res = pbackup.list_packages()
    if not res.get("ok"):
        return _err("；".join(res.get("errors") or ["包列表读取失败"]), status=400)
    items = []
    for pkg in res.get("packages") or []:
        pid = pkg.get("package_id", "")
        path, why = _backup_pkg_path(pid)
        if path is None:
            continue
        meta = _package_meta(pid, path)
        items.append({
            "package_id": pid,
            "name": pkg.get("name"),
            "backup_type": meta["backup_type"],
            "created_at": pkg.get("created_at") or meta["created_at"],
            "size_bytes": pkg.get("size_bytes"),
            "format_version": meta["format_version"],
            "check_status": meta["check_status"],
            "supports_full_restore": meta["supports_full_restore"],
            "warnings": meta["warnings"],
        })
    return _ok({"packages": items, "count": len(items)})


@app.get("/api/backup/packages/{package_id}")
def api_backup_package_detail(package_id: str):
    """包详情：复用 S1 inspect_package，文件清单摘要限长，不暴露记忆正文。"""
    path, why = _backup_pkg_path(package_id)
    if path is None:
        return _err(why, status=400)
    detail = pbackup.inspect_package(path)
    if not detail.get("ok") and not (detail.get("manifest") or {}):
        return _err("包不可读或校验失败", status=400)
    manifest = detail.get("manifest") or {}
    # 只给文件数量与类别摘要，绝不给逐文件完整清单（避免超大包拖垮页面/泄露正文）
    val = detail.get("validation") or {}
    out = {
        "package_id": package_id,
        "ok": detail.get("ok"),
        "manifest": manifest,
        "file_count": detail.get("file_count"),
        "total_size_bytes": detail.get("total_size_bytes"),
        "validation": {
            "ok": val.get("ok"),
            "errors": (val.get("errors") or [])[:20],
            "warnings": (val.get("warnings") or [])[:20],
            "checks": val.get("checks") or {},
        },
        "blockers": (val.get("errors") or []) if not val.get("ok") else [],
    }
    return _ok(out)


@app.post("/api/backup/packages/{package_id}/validate")
def api_backup_package_validate(package_id: str):
    """完整性校验：复用 S1 validate_package_archive 全 14 项。"""
    path, why = _backup_pkg_path(package_id)
    if path is None:
        return _err(why, status=400)
    val = pbackup.validate_package_archive(path)
    return _ok({
        "package_id": package_id,
        "ok": val.get("ok"),
        "errors": (val.get("errors") or [])[:30],
        "warnings": (val.get("warnings") or [])[:30],
        "checks": val.get("checks") or {},
        "file_count": val.get("file_count"),
        "total_size_bytes": val.get("total_size_bytes"),
    })


@app.get("/api/backup/target")
def api_backup_target():
    """恢复目标检查：复用 S2 inspect_restore_target，字段脱敏后返回。"""
    target = prestore.inspect_restore_target()
    return _ok(_safe_target_info(target))


@app.get("/api/backup/restores")
def api_backup_restores(limit: int = Query(50, ge=1, le=200)):
    """恢复历史：复用 S2 list_restore_history。"""
    return _ok(prestore.list_restore_history(limit=limit))


@app.get("/api/backup/restores/{restore_id}")
def api_backup_restore_detail(restore_id: str):
    """恢复详情：复用 S2 get_restore_status（已剔除令牌密钥），只做脱敏。"""
    res = prestore.get_restore_status(restore_id)
    if not res.get("ok"):
        return _err(res.get("message", "恢复记录不存在"), status=404)
    status = dict(res.get("status") or {})
    for k in ("package_path", "backup_dir", "source_root", "db_path"):
        if k in status:
            status[k] = _redact_path(status[k])
    stages = status.get("stages")
    if isinstance(stages, list):
        cleaned = []
        for s in stages:
            sd = dict(s)
            detail = sd.get("detail")
            if isinstance(detail, dict):
                detail = {dk: (_redact_path(dv) if isinstance(dv, str) and (
                    "path" in dk or "dir" in dk or "root" in dk) else dv)
                    for dk, dv in detail.items()}
                sd["detail"] = detail
            cleaned.append(sd)
        status["stages"] = cleaned
    return _ok({"status": status})


# ---------------------------------------------------------------------------
# 分档保护
# ---------------------------------------------------------------------------
@app.get("/api/protection")
def api_protection_summary(category: str = Query(""),
                           include_archived: bool = Query(False)):
    from app.db import connect
    sql = "SELECT * FROM memories WHERE 1=1"
    args: list = []
    if category.strip():
        sql += " AND category=?"
        args.append(category.strip())
    if not include_archived:
        sql += " AND status='active'"
    with connect() as conn:
        rows = [db_rowdict(r) for r in conn.execute(sql, args).fetchall()]
    return _ok(protect.summarize(rows))


@app.get("/api/protection/{memory_id}")
def api_protection_one(memory_id: int):
    from app.db import connect
    with connect() as conn:
        row = conn.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
    if row is None:
        return _err(f"记忆 #{memory_id} 不存在", status=404)
    return _ok(protect.assess(db_rowdict(row)))


# ---------------------------------------------------------------------------
# 反馈闭环
# ---------------------------------------------------------------------------
@app.get("/api/feedback")
def api_feedback_summary(limit: int = Query(20, ge=1, le=200)):
    return _run(feedback.summarize_feedback, limit)


@app.post("/api/feedback/recall")
def api_feedback_recall(payload: dict):
    ids = payload.get("memory_ids") or []
    if not isinstance(ids, list):
        return _err("memory_ids 必须是数组")
    return _run(feedback.record_retrieved, [int(x) for x in ids])


# ---------------------------------------------------------------------------
# 记忆提炼
# ---------------------------------------------------------------------------
@app.get("/api/distillations")
def api_distillations(limit: int = Query(50, ge=1, le=200)):
    return _run(distill.list_distillations, limit)


@app.get("/api/distillations/candidates")
def api_distill_candidates(limit: int = Query(20, ge=1, le=100),
                           category: str = Query(""),
                           similarity: float = Query(0.52, ge=0, le=1),
                           include_covered: bool = Query(False)):
    return _run(distill.find_distillation_candidates, limit, category,
                similarity, include_covered)


@app.post("/api/distillations")
def api_distill_apply(payload: dict):
    ids = payload.get("source_memory_ids") or []
    if not isinstance(ids, list) or not ids:
        return _err("source_memory_ids 必须是非空数组")
    return _run(distill.apply_distillation,
                payload.get("content", "") or "",
                [int(x) for x in ids],
                payload.get("category", "reflection") or "reflection",
                float(payload.get("confidence", 0.7)),
                payload.get("note", "") or "")


# ---------------------------------------------------------------------------
# 任务续跑
# ---------------------------------------------------------------------------
# 注意路径：不能写成 /api/tasks/stuck —— `/api/tasks/{task_id}` 声明在前面，
# FastAPI 按声明顺序匹配，"stuck" 会被当成 task_id 解析成 int 后 422。
# 所以续跑相关接口统一放在 /api/task-resume 下，与 tasks 动态路由家族分离。
@app.get("/api/task-resume/stuck")
def api_task_resume_stuck(stuck_hours: float = Query(24.0, ge=0.1, le=8760),
                          limit: int = Query(50, ge=1, le=200)):
    """卡住的任务。只报告不自动重试——该不该继续跑是人的决定，不是时间的函数。"""
    return _run(resume_mod.find_stuck_tasks, stuck_hours, limit)


@app.get("/api/task-resume/{task_id}/packet")
def api_task_resume_packet(task_id: int):
    """续跑包。带不到上下文时 context_available 会是 false，而不是假装有。"""
    return _run(resume_mod.build_resume_packet, task_id)


# ---------------------------------------------------------------------------
# 记忆有效期
# ---------------------------------------------------------------------------
@app.get("/api/expiry/scan")
def api_expiry_scan(limit: int = Query(200, ge=1, le=1000),
                    include_protected: bool = Query(False)):
    """列出已过期的记忆。只是清单，不改任何数据。"""
    return _run(expiry_mod.scan_expired, limit, include_protected)


@app.post("/api/expiry/run")
def api_expiry_run(payload: dict | None = None):
    """默认 dry_run；archive=True 也只送 pending_review，不直接归档。

    注意：这个接口声明在 `/api/memories/{memory_id}...` 之前，且 /api/expiry
    与 memories 家族不冲突。
    """
    payload = payload or {}
    return _run(expiry_mod.run_expiry,
                bool(payload.get("dry_run", True)),
                int(payload.get("limit", 200)),
                bool(payload.get("archive", False)))


@app.post("/api/memories/{memory_id}/expiry")
def api_set_expiry(memory_id: int, payload: dict | None = None):
    """给一条记忆设有效期。长期类（preference/fact 等）会被明确拒绝并说明原因。"""
    payload = payload or {}
    return _run(core.set_memory_expiry, memory_id,
                float(payload.get("days", 0) or 0),
                str(payload.get("expires_at", "") or ""))


# ---------------------------------------------------------------------------
# 召回画像（预览，不查库）
# ---------------------------------------------------------------------------
@app.get("/api/recall/profile")
def api_recall_profile(query: str = "", memory_type: str = ""):
    info = recall_prof.weights_for(memory_type=memory_type, query=query)
    return _ok({
        "rule_version": recall_prof.PROFILE_RULE_VERSION,
        "shape": info["shape"],
        "profile": info["profile"],
        "weights": info["weights"],
        "reasons": info["reasons"],
        # 默认表就是 life.RECALL_WEIGHTS（TYPE_PROFILES 里没有 default 键）。
        # 显式返回它，前端才能把"本次权重"和"默认权重"并排比，看出画像到底动了什么。
        "default_weights": dict(life.RECALL_WEIGHTS),
        "type_profiles": recall_prof.TYPE_PROFILES,
        "shape_shift": recall_prof.SHAPE_WEIGHT_SHIFT,
    })


@app.post("/api/recall/backfill-types")
def api_backfill_types(payload: dict | None = None):
    """补 memory_type。默认干跑——它只改排序，但改了就是改了。"""
    payload = payload or {}
    return _run(core.backfill_memory_types,
                bool(payload.get("dry_run", True)),
                int(payload.get("limit", 500)))


# ---------------------------------------------------------------------------
# 取值冲突（同一件事的两种取值，建议都保留）
# ---------------------------------------------------------------------------
@app.get("/api/conflicts/value")
def api_value_conflicts(limit: int = Query(50, ge=1, le=200)):
    return _run(gov.list_conflicts, None, "", limit)


# ---------------------------------------------------------------------------
# 统计 + 首页
# ---------------------------------------------------------------------------
@app.get("/api/stats")
def api_stats():
    from app.db import connect
    with connect() as conn:
        total = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        active = conn.execute("SELECT COUNT(*) FROM memories WHERE status='active'").fetchone()[0]
        archived = conn.execute("SELECT COUNT(*) FROM memories WHERE status='archived'").fetchone()[0]
        tasks = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        links = conn.execute("SELECT COUNT(*) FROM memory_links").fetchone()[0]
        events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    return _ok({"memories_total": total, "memories_active": active,
                "memories_archived": archived, "tasks": tasks,
                "links": links, "events": events, "boot": BOOT_ID})


@app.get("/api/ping")
def api_ping(request: Request):
    """被 iframe 页面本身调用，用来证明"面板真的在浏览器里跑起来了"。

    为什么要这个接口：宿主半的 data/panel-boot.log 只能证明 Node 侧加载了插件，
    证明不了浏览器侧真的求值了 client.js、真的挂上了 iframe。两边都加载成功但
    中间某处静默失败时，只靠宿主痕迹会误判成"已经好了"。这里落一行
    data/panel-client.log，重启后一眼就能分清故障在哪一半。
    """
    from datetime import datetime, timezone
    record = {
        "at": datetime.now(timezone.utc).isoformat(),
        "boot": BOOT_ID,
        # source=client-boot 由 client.js 顶层发出（插件一被宿主求值就打），
        # 其余（空）是面板 iframe 里页面自己发的。这个区别很关键：
        # 前者证明「浏览器半真的加载了」，后者证明「面板真的被打开了」。
        "source": request.query_params.get("source", ""),
        "referer": request.headers.get("referer", ""),
        "origin": request.headers.get("origin", ""),
        "user_agent": request.headers.get("user-agent", "")[:120],
        # Sec-Fetch-Dest 是浏览器自己加的、页面脚本改不了：iframe 内嵌是 "iframe"，
        # 直接打开页面是 "document"，页面自身 fetch 是 "empty"。
        # 有了它，「宿主真的把面板嵌进去了」和「有人拿浏览器直接开了这个地址」就不会再混在一起。
        "dest": request.headers.get("sec-fetch-dest", ""),
    }
    try:
        log_path = Path(__file__).resolve().parent.parent / "data" / "panel-client.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 —— 记账失败绝不能影响面板本身
        pass
    return _ok({"pong": True, "at": record["at"]})


@app.get("/")
def index():
    if HTML_PATH.exists():
        return FileResponse(HTML_PATH, media_type="text/html")
    return _err("找不到 admin_ui.html（请确认它和 admin_ui.py 在同一目录）", status=500)


# 星图用的 3D 库（3d-force-graph，MIT，自包含 three.js）放在 app/static/ 里
# 本地托管而不是走 CDN：面板的设计前提是「拷走就能跑、离线可用」，
# 而 DSH 里是 iframe 同源加载，外链还会额外受网络与 CSP 影响。
STATIC_DIR = Path(__file__).resolve().with_name("static")


@app.get("/static/{name}")
def static_file(name: str):
    # 只允许白名单后缀，避免把目录里别的东西读出去（/static/../admin_ui.py）
    if "/" in name or "\\" in name or ".." in name:
        return _err("非法文件名", status=400)
    if not name.endswith((".js", ".css", ".svg")):
        return _err("不支持的文件类型", status=400)
    target = STATIC_DIR / name
    if not target.is_file():
        return _err(f"找不到 {name}", status=404)
    media = "text/javascript" if name.endswith(".js") else (
        "text/css" if name.endswith(".css") else "image/svg+xml")
    return FileResponse(target, media_type=media)


def is_local_bind_host(host: str) -> bool:
    """判断一个 `--host` 值是否只可能接受本机连接。

    允许的情况（都是「只有本机能连上」）：
      - 回环地址：127.0.0.0/8 里除 127.0.0.1 之外的写法，以及 `localhost`、`::1`
      - 回环域名的等价写法（大小写、末尾点）

    拒绝的情况：
      - 通配地址 `0.0.0.0` / `::`（监听所有网卡）
      - 私网/公网 IP（192.168.x.x、10.x.x.x、8.8.8.8 …）
      - 机器名（即便它今天解析到 127.0.0.1，也可能被 hosts 或 DNS 改掉，
        不把它当成可信任的静态事实）

    空字符串走「关闭监听」语义（uvicorn 不接受），这里按不安全处理，
    交给调用方拒绝——宁可拒绝也不猜。
    """
    if not isinstance(host, str):
        return False
    value = host.strip().lower().rstrip(".")
    if not value:
        return False
    if value == "localhost":
        return True
    # 去掉 IPv6 的 [::1] 方括号写法
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    try:
        addr = ipaddress.ip_address(value)
    except ValueError:
        return False
    return addr.is_loopback


def check_bind_safety(host: str, allow_remote: bool) -> tuple[bool, str]:
    """启动前的绑定安全检查。返回 (是否允许启动, 面向用户的提示文本)。

    纯函数、不碰数据库、不读文件——所以测试可以直接调用它，
    不需要真的起一个 uvicorn 服务。

    规则只有一条：**非本机地址必须显式授权**。
    `allow_remote=True` 时放行，但提示文本会变成安全警告；
    它不是认证机制，只是「用户自己承认知道风险」的一个签名位。
    """
    if is_local_bind_host(host):
        if allow_remote:
            # 本机地址 + 明确授权：不冲突，但没必要，提醒一句即可
            return True, (
                f"[bionic-agent-admin] 提示：--host {host} 本来就只监听本机，"
                f"--allow-remote 在这里没有作用。"
            )
        return True, ""

    if allow_remote:
        return True, (
            f"[bionic-agent-admin] ⚠ 安全警告：管理面板正在绑定 {host}，已暴露到远程网络。\n"
            f"[bionic-agent-admin] ⚠ 当前管理面板【没有任何身份认证】。\n"
            f"[bionic-agent-admin] ⚠ 能访问这个地址的人可以删除、修改、恢复记忆，\n"
            f"[bionic-agent-admin] ⚠ 可以恢复整库快照、导出备份包、改动治理状态。\n"
            f"[bionic-agent-admin] ⚠ 请确认你处在可信网络（例如自己的隔离内网），\n"
            f"[bionic-agent-admin] ⚠ 并且不要把这个端口暴露到公网。\n"
            f"[bionic-agent-admin] ⚠ --allow-remote 只是你的授权声明，不是认证。"
        )

    return False, (
        f"[bionic-agent-admin] 拒绝启动：--host {host} 不是本机地址。\n"
        f"[bionic-agent-admin] 管理面板没有任何身份认证，直接绑定到非本机地址，\n"
        f"[bionic-agent-admin] 等于把「删记忆 / 恢复整库 / 导出备份」的接口交给同网段所有人。\n"
        f"[bionic-agent-admin] \n"
        f"[bionic-agent-admin] 如果你只是想换个端口：--port 9000（host 保持默认 127.0.0.1）。\n"
        f"[bionic-agent-admin] 如果你确实要暴露到局域网，请显式加 --allow-remote：\n"
        f"[bionic-agent-admin]     python -m app.admin_ui --host {host} --allow-remote"
    )


def _run_cli():
    parser = argparse.ArgumentParser(description="Bionic Agent 管理面板")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8899)
    parser.add_argument(
        "--allow-remote",
        action="store_true",
        help="允许绑定非本机地址（面板无身份认证，暴露到局域网前请确认风险）",
    )
    args = parser.parse_args()

    # 这一步必须发生在任何数据库动作之前：_lifespan 里的 _ensure_db() 会建库/迁移，
    # 所以在 uvicorn.run 之前就把"不该起来"的情况挡掉，被拒绝时进程里没有过任何
    # 数据库初始化，也就不会有任何生产数据被触碰。
    allowed, message = check_bind_safety(args.host, args.allow_remote)
    if not allowed:
        print(message, file=sys.stderr)
        sys.exit(2)
    if message:
        print(message, file=sys.stderr)

    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    _run_cli()
