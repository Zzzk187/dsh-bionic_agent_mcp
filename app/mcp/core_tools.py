"""MCP core 实例的 36 个工具（日常主力：记忆读写 / 任务 / 知识 / 便携包）。

OPT-03 3.1 从 app/mcp_server.py 按域拆出；函数体逐字搬移，装饰器与签名未改。
`mcp` 实例与共享 helper 都从 app.mcp.runtime 绑名过来，所以 `@mcp.tool()` 照抄即可。
"""
from __future__ import annotations

import sys
from pathlib import Path
import json
import re

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.db import init_db, connect, rowdict
from app.embeddings import (DEFAULT_MODEL, EmbeddingUnavailable, content_hash,
                            embed_passages, embed_query, cosine_similarity,
                            ensure_warmup, model_state)
from app import expiry
from app import task_resume
from app import knowledge
from app import memory_lifecycle as life
from app import portable_backup as _pbackup
from app.mcp.runtime import (
    mcp, _ensure_db, _keyword_only_search, _touch_retrieved,
    _memory_out, _pkg_path, _tool_result, _tool_error,
)
from app.mcp.stores import memory_store as _memstore
from app.mcp.stores import task_store as _taskstore
from app.mcp.stores import search_store as _searchstore


@mcp.tool()
def save_memory(content: str, category: str = "general", source: str = "deepseek-harness",
                confidence: float = 0.8, tags: list[str] | None = None,
                ttl_days: float = 0) -> dict:
    """保存一条持久记忆；写审计，返回新记忆 id。

    `ttl_days` 只对临时类记忆生效（temp/cache/session/todo 等）。对
    preference/fact 这类长期有效的记忆传入会被**明确拒绝并说明原因**，
    而不是静默忽略——否则调用方会以为自己设了有效期，实际上永远不过期。
    """
    _ensure_db()
    content = content.strip()
    if not content:
        return {"ok": False, "message": "content must not be empty"}
    confidence = max(0.0, min(float(confidence), 1.0))
    safe_tags = list(dict.fromkeys(str(t).strip()[:50] for t in (tags or []) if str(t).strip()))[:20]

    expires_at = None
    ttl_warning = ""
    if ttl_days:
        refusal = expiry.explain_ttl_refusal(category)
        if refusal:
            ttl_warning = refusal
        else:
            expires_at = expiry.ttl_to_expires_at(ttl_days)
            if expires_at is None:
                ttl_warning = f"ttl_days={ttl_days!r} 无法转换为有效期（必须 > 0），已忽略"

    # Classify on write. Leaving memory_type NULL "for later" would make the
    # recall profiles dead code on every row; and a wrong guess here only
    # re-orders results, never hides them.
    type_guess = life.classify_memory_type(content, category, source)
    guessed_type = type_guess.get("memory_type") or "unknown"
    stored_type = None if guessed_type == "unknown" else guessed_type

    with connect() as conn:
        memory_id = _memstore.insert_memory(
            conn,
            content=content[:10000],
            category=category[:100],
            source=source[:200],
            confidence=confidence,
            tags_json=json.dumps(safe_tags, ensure_ascii=False),
            expires_at=expires_at,
            memory_type=stored_type,
        )
        detail = f"memory_id={memory_id}; category={category[:100]}; source={source[:200]}"
        if stored_type:
            detail += f"; memory_type={stored_type}"
        if expires_at:
            detail += f"; expires_at={expires_at}"
        _memstore.insert_event(conn, "memory.created", detail)
        if ttl_warning:
            _memstore.insert_event(
                conn, "memory.ttl_refused", f"memory_id={memory_id}; {ttl_warning}"
            )
        row = _memstore.fetch_memory(conn, memory_id)
    out = _memory_out(row)
    out["memory_type"] = stored_type
    out["type_confidence"] = type_guess.get("confidence")
    if type_guess.get("needs_review"):
        out["type_needs_review"] = True
        out["type_reasons"] = type_guess.get("reasons")
    if expires_at:
        out["expires_at"] = expires_at
        out["ttl_days"] = float(ttl_days)
    if ttl_warning:
        out["ttl_warning"] = ttl_warning
        out["ttl_applied"] = False
    elif ttl_days:
        out["ttl_applied"] = True
    return out



@mcp.tool()
def set_memory_expiry(memory_id: int, days: float = 0, expires_at: str = "") -> dict:
    """给已有记忆设/清有效期。days>0 按天数算；显式 expires_at 优先。days<=0 且无 expires_at 表示清除。"""
    _ensure_db()
    if expires_at.strip():
        ts = expiry._parse_ts(expires_at.strip())
        if ts is None:
            return {"ok": False, "message": f"expires_at 无法解析：{expires_at!r}"}
        value = ts.isoformat()
    elif days and float(days) > 0:
        value = expiry.ttl_to_expires_at(days)
    else:
        value = None
    with connect() as conn:
        row = _memstore.fetch_memory(conn, int(memory_id))
        if not row:
            return {"ok": False, "message": "Memory not found", "memory_id": int(memory_id)}
        memory = rowdict(row)
        if value is not None:
            refusal = expiry.explain_ttl_refusal(memory.get("category") or "")
            if refusal:
                return {"ok": False, "message": refusal, "memory_id": int(memory_id)}
        _memstore.set_expires_at(conn, int(memory_id), value)
        _memstore.insert_event(
            conn, "memory.expiry_set",
            f"memory_id={memory_id}; expires_at={value}",
        )
    return {"ok": True, "memory_id": int(memory_id), "expires_at": value}



@mcp.tool()
def scan_expired_memories(limit: int = 200, include_protected: bool = False) -> dict:
    """列出真正过期的记忆（只读）。过保护档的记忆会进 skipped 并说明原因。"""
    _ensure_db()
    return expiry.scan_expired(limit=limit, include_protected=include_protected)



@mcp.tool()
def run_expiry_policy(dry_run: bool = True, limit: int = 200, archive: bool = False) -> dict:
    """执行过期策略：标记 + 降权，可选送人工审核。默认 dry_run，不会自动归档。"""
    _ensure_db()
    return expiry.run_expiry(dry_run=dry_run, limit=limit, archive=archive)



@mcp.tool()
def search_memories(query: str = "", limit: int = 20, category: str = "", include_archived: bool = False) -> list[dict]:
    """按关键词/分类/标签检索记忆（非语义）。空查询列出最近记忆。"""
    _ensure_db()
    limit = max(1, min(int(limit), 100))
    query = query.strip()
    terms = [t for t in re.split(r"\s+", query) if t][:8] if query else []
    with connect() as conn:
        # 不含 AND 词条件、不含 LIMIT 的那份条件与参数（base_*）由 store 一并返回，
        # 给下面的词法兜底复用。手写而不是从 params 反推：反推漏掉 LIMIT 那个 ?
        # 就会报 "sqlite3.ProgrammingError: Incorrect number of bindings"。
        rows, base_clauses, base_params = _searchstore.search_rows(
            conn,
            include_archived=bool(include_archived),
            category=category.strip()[:100],
            terms=terms,
            limit=limit,
        )
    if rows or not query:
        return [_memory_out(r) for r in rows]

    # AND 全不中。这不一定是"没有相关内容"——中文口语长句只要有一个词在库里
    # 不存在，整条 SQL 就返回 0 行，调用方无法区分"真的没有"和"有一个词没对
    # 上"。退回字段加权词法打分（标题×6 / 标签×4 / 正文×2 + bigram/trigram
    # 展开 + 同义词），让部分命中也能排出来。
    # 结果带 fuzzy=True 与 score：这是放宽后的排序，不是精确命中。
    from app import memory_lexical
    with connect() as conn:
        allrows = _searchstore.search_all_rows(conn, base_clauses, base_params)
    scored = memory_lexical.score_all(query, [dict(r) for r in allrows])
    if not scored:
        return []
    by_id = {r["id"]: r for r in allrows}
    out = []
    for rid, score in sorted(scored.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]:
        item = _memory_out(by_id[rid])
        item["score"] = round(float(score), 4)
        item["fuzzy"] = True
        out.append(item)
    return out



@mcp.tool()
def find_related_memories(query: str, limit: int = 10, category: str = "") -> list[dict]:
    """按词重叠+短语加成对相关记忆排序（轻量，非向量）。"""
    _ensure_db()
    query = query.strip()
    if not query:
        return []
    terms = list(dict.fromkeys(t.lower() for t in re.findall(r"[\w\u4e00-\u9fff]+", query) if len(t) > 1))[:20]
    if not terms:
        return []
    limit = max(1, min(int(limit), 50))
    with connect() as conn:
        rows = _searchstore.related_rows(conn, category.strip()[:100])
    scored = []
    for row in rows:
        item = _memory_out(row)
        text = (item.get("content", "") + " " + " ".join(item.get("tags", []))).lower()
        hits = sum(1 for term in terms if term in text)
        if hits == 0:
            continue
        score = hits / max(1, len(terms))
        if query.lower() in text:
            score += 0.5
        score += min(float(item.get("confidence", 0.5)), 1.0) * 0.05
        item["relevance_score"] = round(score, 4)
        scored.append(item)
    scored.sort(key=lambda x: (x["relevance_score"], x.get("updated_at", "")), reverse=True)
    return scored[:limit]



@mcp.tool()
def link_memories(source_memory_id: int, target_memory_id: int, relation: str = "related", note: str = "") -> dict:
    """在 memory_links 建立两条记忆的有类型关系。"""
    _ensure_db()
    if int(source_memory_id) == int(target_memory_id):
        return {"ok": False, "message": "A memory cannot link to itself"}
    allowed = {"related", "supports", "contradicts", "supersedes"}
    relation = relation.strip().lower()
    if relation not in allowed:
        return {"ok": False, "message": "relation must be one of: related, supports, contradicts, supersedes"}
    with connect() as conn:
        a = _memstore.memory_exists(conn, int(source_memory_id))
        b = _memstore.memory_exists(conn, int(target_memory_id))
        if not a or not b:
            return {"ok": False, "message": "Both memories must exist"}
        link_id = _memstore.insert_memory_link(
            conn, int(source_memory_id), int(target_memory_id), relation, note[:1000]
        )
        _memstore.insert_event(
            conn, "memory.linked",
            f"link_id={link_id}; source={source_memory_id}; target={target_memory_id}; relation={relation}",
        )
        return {"ok": True, "link_id": link_id, "source_memory_id": int(source_memory_id),
                "target_memory_id": int(target_memory_id), "relation": relation}



@mcp.tool()
def get_memory_links(memory_id: int) -> list[dict]:
    """列出某记忆的全部双向关系。"""
    _ensure_db()
    with connect() as conn:
        if not _memstore.memory_exists(conn, int(memory_id)):
            return []
        rows = _searchstore.links_for_memory(conn, int(memory_id))
    return [rowdict(r) for r in rows]



@mcp.tool()
def supersede_memory(old_memory_id: int, new_content: str, category: str = "",
                     source: str = "user-confirmed", confidence: float = 0.95,
                     note: str = "") -> dict:
    """记录新事实并把旧记忆标 superseded/archived（需用户确认）。"""
    _ensure_db()
    new_content = new_content.strip()
    if not new_content:
        return {"ok": False, "message": "new_content must not be empty"}
    confidence = max(0.0, min(float(confidence), 1.0))
    with connect() as conn:
        old = _memstore.fetch_active_memory(conn, int(old_memory_id))
        if not old:
            return {"ok": False, "message": "Active old memory not found", "memory_id": int(old_memory_id)}
        cat = category.strip()[:100] or old["category"]
        new_id = _memstore.insert_memory_superseding(
            conn,
            content=new_content[:10000],
            category=cat,
            source=source[:200],
            confidence=confidence,
            tags_json=old["tags_json"] if "tags_json" in old.keys() else "[]",
            supersedes_id=int(old_memory_id),
        )
        _memstore.force_archive_memory_row(conn, int(old_memory_id))
        _memstore.insert_memory_link(
            conn, new_id, int(old_memory_id), "supersedes", note[:1000]
        )
        _memstore.insert_event(
            conn,
            "memory.superseded",
            f"old_id={old_memory_id}; new_id={new_id}; note={note[:300]}",
        )
        row = _memstore.fetch_memory(conn, new_id)
    return _memory_out(row)



@mcp.tool()
def get_memory(memory_id: int, include_archived: bool = True) -> dict:
    """按 id 取一条记忆（含来源/置信度/时间戳/标签）。"""
    _ensure_db()
    with connect() as conn:
        row = _memstore.fetch_memory(conn, int(memory_id))
    if not row:
        return {"ok": False, "message": "Memory not found", "memory_id": int(memory_id)}
    result = _memory_out(row)
    # Phase 6.3: ordinary retrieval (include_archived=False) excludes anything
    # that is not actively retrievable — that is status='active' AND a non
    # hold-state memory_status. pending_review is a human-adjudication hold and
    # must never surface in ordinary retrieval. Read memory_status from the raw
    # row because _memory_out() scrubs governance columns by default.
    if not include_archived and (
        row["status"] != "active" or (row["memory_status"] or "active") == "pending_review"
    ):
        return {"ok": False, "message": "Memory is not active", "memory_id": int(memory_id)}
    return result



@mcp.tool()
def update_memory(memory_id: int, content: str = "", category: str = "", source: str = "",
                  confidence: float | None = None, tags: list[str] | None = None) -> dict:
    """更新记忆指定字段，省略字段不变；保留 provenance。"""
    _ensure_db()
    updates, values = [], []
    if content.strip():
        updates.append("content=?"); values.append(content.strip()[:10000])
    if category.strip():
        updates.append("category=?"); values.append(category.strip()[:100])
    if source.strip():
        updates.append("source=?"); values.append(source.strip()[:200])
    if confidence is not None:
        updates.append("confidence=?"); values.append(max(0.0, min(float(confidence), 1.0)))
    if tags is not None:
        safe_tags = list(dict.fromkeys(str(t).strip()[:50] for t in tags if str(t).strip()))[:20]
        updates.append("tags_json=?"); values.append(json.dumps(safe_tags, ensure_ascii=False))
    if not updates:
        return {"ok": False, "message": "No fields provided to update"}
    updates.extend(["updated_at=CURRENT_TIMESTAMP"])
    with connect() as conn:
        # memory_id 由 store 追加在最后，与下沉前的 values 顺序逐字一致。
        cur_rowcount = _memstore.update_memory_fields(conn, int(memory_id), updates, values)
        if not cur_rowcount:
            return {"ok": False, "message": "Active memory not found", "memory_id": int(memory_id)}
        _memstore.insert_event(
            conn,
            "memory.updated",
            f"memory_id={memory_id}; fields={','.join(u.split('=')[0] for u in updates)}",
        )
        row = _memstore.fetch_memory(conn, int(memory_id))
    return _memory_out(row)



@mcp.tool()
def archive_memory(memory_id: int) -> dict:
    """软归档记忆（不物理删除）。"""
    _ensure_db()
    with connect() as conn:
        if not _memstore.archive_memory_row(conn, int(memory_id)):
            return {"ok": False, "message": "Memory not found or already archived"}
        _memstore.insert_event(
            conn, "memory.archived", f"memory_id={memory_id}; source=mcp"
        )
    return {"ok": True, "memory_id": int(memory_id), "status": "archived"}



@mcp.tool()
def restore_memory(memory_id: int) -> dict:
    """把归档记忆恢复为活跃。"""
    _ensure_db()
    with connect() as conn:
        if not _memstore.restore_memory_row(conn, int(memory_id)):
            return {"ok": False, "message": "Archived memory not found", "memory_id": int(memory_id)}
        _memstore.insert_event(
            conn, "memory.restored", f"memory_id={memory_id}; source=mcp"
        )
        row = _memstore.fetch_memory(conn, int(memory_id))
    return _memory_out(row)



@mcp.tool()
def create_task(title: str, goal: str) -> dict:
    """创建持久任务（含初始计划），不自动执行。"""
    _ensure_db()
    plan = [
        {"step": 1, "title": "Clarify objective and constraints", "status": "pending"},
        {"step": 2, "title": "Create execution plan", "status": "pending"},
        {"step": 3, "title": "Execute and verify", "status": "pending"},
    ]
    with connect() as conn:
        task_id = _taskstore.insert_task(
            conn, title[:200], goal[:10000], json.dumps(plan, ensure_ascii=False)
        )
        _taskstore.insert_task_event(conn, task_id, "task.created", title[:200])
        row = _taskstore.fetch_task(conn, task_id)
    result = rowdict(row)
    result["plan"] = json.loads(result.pop("plan_json"))
    return result



@mcp.tool()
def list_tasks() -> list[dict]:
    """列出最近任务及状态。"""
    _ensure_db()
    with connect() as conn:
        rows = _taskstore.fetch_recent_tasks(conn)
    results = []
    for row in rows:
        d = rowdict(row); d["plan"] = json.loads(d.pop("plan_json")); results.append(d)
    return results



@mcp.tool()
def get_task(task_id: int) -> dict:
    """取任务及其阶段快照。"""
    _ensure_db()
    with connect() as conn:
        row = _taskstore.fetch_task(conn, int(task_id))
        if not row:
            return {"ok": False, "message": "Task not found", "task_id": int(task_id)}
        snaps = _taskstore.fetch_snapshots(conn, int(task_id))
    d = rowdict(row); d["plan"] = json.loads(d.pop("plan_json")); d["snapshots"] = [rowdict(s) for s in snaps]
    return d



@mcp.tool()
def save_stage_snapshot(task_id: int, summary: str, state_json: str = "{}") -> dict:
    """仅保存检查点，不改任务状态/不推进阶段。"""
    _ensure_db()
    try:
        state = json.loads(state_json)
    except json.JSONDecodeError:
        return {"ok": False, "message": "state_json must be valid JSON"}
    with connect() as conn:
        task = _taskstore.fetch_task(conn, int(task_id))
        if not task:
            return {"ok": False, "message": "Task not found", "task_id": int(task_id)}
        stage = int(task["current_stage"])
        snap_id = _taskstore.insert_snapshot(
            conn, int(task_id), stage, summary[:20000],
            json.dumps(state, ensure_ascii=False),
        )
        _taskstore.insert_task_event(
            conn, int(task_id), "task.snapshot_saved",
            f"snapshot_id={snap_id}; stage={stage}",
        )
        snap = _taskstore.fetch_snapshot(conn, snap_id)
    return rowdict(snap)



@mcp.tool()
def handoff_task(task_id: int, summary: str, state_json: str = "{}") -> dict:
    """保存交接快照并推进到下一阶段（同一事务）。"""
    _ensure_db()
    try:
        state = json.loads(state_json)
    except json.JSONDecodeError:
        return {"ok": False, "message": "state_json must be valid JSON"}
    with connect() as conn:
        task = _taskstore.fetch_task(conn, int(task_id))
        if not task:
            return {"ok": False, "message": "Task not found", "task_id": int(task_id)}
        stage = int(task["current_stage"])
        snap_id = _taskstore.insert_snapshot(
            conn, int(task_id), stage, summary[:20000],
            json.dumps(state, ensure_ascii=False),
        )
        _taskstore.mark_handoff(conn, int(task_id))
        _taskstore.insert_task_event(
            conn, int(task_id), "task.handoff",
            f"snapshot_id={snap_id}; next_stage={stage+1}",
        )
        snap = _taskstore.fetch_snapshot(conn, snap_id)
        trow = _taskstore.fetch_task(conn, int(task_id))
    out = {"ok": True, "snapshot": rowdict(snap), "status": "handoff", "next_stage": stage + 1}
    td = rowdict(trow)
    td["plan"] = json.loads(td.pop("plan_json"))
    out["task"] = td
    return out



@mcp.tool()
def resume_task(task_id: int) -> dict:
    """续跑任务：把状态改回 active，并返回可用的执行上下文。

    以前这里只是 `UPDATE status='active'`，快照从来没被读过——调用方按文档
    续跑，拿到的是一个状态写着 active、上下文全丢的空壳。现在返回值里带
    最近一次阶段快照的 state、摘要和下一步，并且在没有快照时明确写
    context_available=false，而不是假装恢复成功。
    """
    _ensure_db()
    return task_resume.resume(int(task_id))



@mcp.tool()
def get_resume_packet(task_id: int) -> dict:
    """查看任务的续跑上下文（只读，不改状态）。重启后想知道从哪继续就用它。"""
    _ensure_db()
    return task_resume.build_resume_packet(int(task_id))



@mcp.tool()
def find_stuck_tasks(stuck_hours: float = 24.0, limit: int = 50) -> dict:
    """找出「声称在跑但很久没动」和「已标记失败」的任务。只读，不自动重试。"""
    _ensure_db()
    return task_resume.find_stuck_tasks(stuck_hours=stuck_hours, limit=limit)



@mcp.tool()
def fail_task(task_id: int, reason: str = "") -> dict:
    """把任务标记为失败（保留快照与事件，之后仍可带上下文重试）。"""
    _ensure_db()
    return task_resume.mark_failed(int(task_id), reason)



@mcp.tool()
def complete_task(task_id: int, result: str = "") -> dict:
    """标记任务完成并记录结果。"""
    _ensure_db()
    with connect() as conn:
        changed = _taskstore.mark_completed(conn, int(task_id), result[:20000])
        if not changed:
            return {"ok": False, "message": "Task not found", "task_id": int(task_id)}
        _taskstore.insert_task_event(
            conn, int(task_id), "task.completed", result[:500]
        )
    return {"ok": True, "task_id": int(task_id), "status": "completed"}



@mcp.tool()
def list_events(task_id: int = 0, limit: int = 50) -> list[dict]:
    """查看审计轨迹；task_id=0 表示全部。"""
    _ensure_db()
    limit = max(1, min(int(limit), 200))
    with connect() as conn:
        if int(task_id) > 0:
            rows = _taskstore.fetch_events_by_task(conn, int(task_id), limit)
        else:
            rows = _taskstore.fetch_events_all(conn, limit)
    return [rowdict(r) for r in rows]



@mcp.tool()
def embedding_status() -> dict:
    """查看嵌入模型与向量索引覆盖（不下载模型）。"""
    _ensure_db()
    from app.embeddings import model_state, is_ready
    with connect() as conn:
        total = _searchstore.count_active(conn)
        indexed = _searchstore.count_indexed_active(conn)
        stale = _searchstore.count_stale_active(conn)
    st = model_state()
    status = st.get("status")
    note = ("首次生成向量时会下载模型（已在后台预热）；下载期间语义检索会即时降级为关键词检索，"
            "不会阻塞或超时。语义检索使用本地向量，不调用外部 API。")
    if status == "pending":
        note = "模型正在后台下载/加载，暂未完成；此期间语义检索即时降级为关键词检索，不会阻塞。"
    elif status == "failed":
        note = f"模型加载失败：{st.get('error','')}。语义检索将降级为关键词检索；请检查网络/依赖后重试。"
    elif status == "ready":
        note = "模型已就绪；语义检索使用本地向量，不调用外部 API。"
    return {"model": DEFAULT_MODEL, "backend": "FastEmbed / ONNX Runtime", "total_active_memories": total,
            "indexed_active_memories": indexed, "needs_indexing_or_refresh": max(0, total-indexed),
            "model_load_status": status, "model_ready": bool(is_ready()), "note": note}



@mcp.tool()
def index_memories(batch_size: int = 32, force_rebuild: bool = False) -> dict:
    """批量建立/刷新向量索引；可重复、安全。"""
    _ensure_db()
    batch_size = max(1, min(int(batch_size), 128))
    with connect() as conn:
        rows = _searchstore.index_state_rows(conn)
    pending = [r for r in rows if force_rebuild or r['indexed_hash'] != content_hash(r['content']) or r['indexed_model'] != DEFAULT_MODEL]
    indexed_now = 0
    try:
        for offset in range(0, len(pending), batch_size):
            batch = pending[offset:offset+batch_size]
            vectors = embed_passages([r['content'] for r in batch])
            with connect() as conn:
                for row, vector in zip(batch, vectors):
                    _searchstore.upsert_embedding(
                        conn, row['id'], DEFAULT_MODEL,
                        content_hash(row['content']), len(vector),
                        json.dumps(vector),
                    )
            indexed_now += len(batch)
    except EmbeddingUnavailable as exc:
        return {"ok": False, "error": str(exc), "indexed_this_run": indexed_now,
                "pending_at_start": len(pending), "hint": "安装依赖并确保首次模型下载可访问后重试。"}
    return {"ok": True, "model": DEFAULT_MODEL, "indexed_this_run": indexed_now,
            "pending_at_start": len(pending), "message": "向量索引已更新；原 memories 数据未修改。"}



@mcp.tool()
def semantic_search_memories(query: str, limit: int = 10, category: str = "",
                             min_score: float = -1.0, auto_index: bool = True) -> list[dict] | dict:
    """按多语言向量语义检索活跃记忆；默认自动补齐新向量。"""
    _ensure_db()
    query = query.strip()
    if not query:
        return []
    limit = max(1, min(int(limit), 50))
    min_score = max(-1.0, min(float(min_score), 1.0))
    if auto_index:
        result = index_memories(batch_size=32, force_rebuild=False)
        if not result.get('ok'):
            return result
    try:
        query_vector = embed_query(query)
    except EmbeddingUnavailable as exc:
        # Model still downloading (first use) or load failed: degrade to keyword
        # immediately instead of blocking on the MCP client timeout.
        from app.embeddings import is_ready
        if not is_ready():
            degraded = _keyword_only_search(query, limit, category)
            _touch_retrieved(degraded)
            return {"ok": True, "degraded": True, "reason": "model_not_ready",
                    "message": "嵌入模型仍在后台加载/下载，已降级为关键词检索。可稍后重试或先调用 warmup_embeddings。",
                    "results": degraded}
        return {"ok": False, "error": str(exc), "hint": "检查 fastembed 依赖和模型下载。"}
    with connect() as conn:
        rows = _searchstore.vector_rows(conn, category.strip()[:100])
    scored = []
    for row in rows:
        if row['content_hash'] != content_hash(row['content']):
            continue
        try:
            vector = json.loads(row['vector_json'])
        except (TypeError, json.JSONDecodeError):
            continue
        score = cosine_similarity(query_vector, vector)
        if score < min_score:
            continue
        item = _memory_out(row)
        for key in ('vector_json', 'model_name', 'content_hash'):
            item.pop(key, None)
        item['similarity'] = round(score, 6)
        scored.append(item)
    scored.sort(key=lambda x: (x['similarity'], x.get('updated_at', '')), reverse=True)
    result = scored[:limit]
    # Record a real retrieval. Mirrors memory_lifecycle.recall_memories, which
    # has always done this; without it access_count stayed at 0 forever and
    # "was this memory ever used?" could not be answered.
    # Placed after slicing: we count what was actually returned to the caller,
    # not every candidate that happened to be scored.
    _touch_retrieved(result)
    return result



@mcp.tool()
def backfill_memory_types(dry_run: bool = True, limit: int = 500) -> dict:
    """给还没有 memory_type 的记忆补上类型（默认 dry_run 只报告）。

    单独做一个工具是因为：老数据在写入时没有类型，而召回画像依赖类型。
    补类型是纯增量操作（只写 memory_type 一列，不动内容/状态），但它会
    改变排序行为，所以默认 dry_run——先看清楚改了哪些，再决定要不要落库。
    """
    _ensure_db()
    limit = max(1, min(int(limit), 2000))
    with connect() as conn:
        rows = _memstore.untyped_active_rows(conn, limit)
        planned = []
        for r in rows:
            guess = life.classify_memory_type(r["content"] or "",
                                              r["category"] or "", r["source"] or "")
            mtype = guess.get("memory_type") or "unknown"
            if mtype == "unknown":
                continue
            planned.append({"memory_id": r["id"], "memory_type": mtype,
                            "confidence": guess.get("confidence"),
                            "needs_review": bool(guess.get("needs_review"))})
        if not dry_run:
            for p in planned:
                _memstore.update_memory_type(conn, p["memory_id"], p["memory_type"])
            if planned:
                _memstore.insert_event(
                    conn, "memory.types_backfilled",
                    f"count={len(planned)}; sample_ids={[p['memory_id'] for p in planned[:10]]}",
                )
    return {
        "ok": True,
        "dry_run": bool(dry_run),
        "rule_version": life.LIFECYCLE_RULE_VERSION,
        "candidates_scanned": len(rows),
        "would_set": len(planned),
        "applied": 0 if dry_run else len(planned),
        "need_review": sum(1 for p in planned if p["needs_review"]),
        "items": planned,
        "note": ("干跑：只报告不落库" if dry_run
                 else "已写回；召回排序会随之变化，但没有任何记忆被删除或隐藏"),
    }



@mcp.tool()
def search_knowledge(query: str, limit: int = 5, root: str = "", domain: str = "") -> dict:
    """在知识库里检索（全文分块，关键词主导 + 向量补漏）。

    `root` 可填 knowledge / mine / memory / script 只看一侧：
      * knowledge / mine —— 知识笔记
      * memory  —— 系统骨架（AGENTS.md、约定、性格、体系、记忆目录、卷/）
      * script  —— 10 个 .ps1 工具脚本
    `domain` 按知识域过滤（Agent / 产品 / 工程 / 语音 / 部署 / 安全 / 知识）。
    想一次同时查记忆，用 recall_all。
    """
    _ensure_db()
    limit = max(1, min(int(limit), 20))
    return knowledge.search(query, limit=limit, root=root or "", domain=domain or "")



@mcp.tool()
def get_knowledge_doc(doc_id: int = 0, path: str = "", max_chars: int = 12000) -> dict:
    """按 id 或相对路径取一篇知识笔记的全文（可截断）。

    检索命中只给摘要行时，想看上下文用这个；`path` 用检索结果里的 rel_path。
    """
    _ensure_db()
    max_chars = max(500, min(int(max_chars), 60000))
    if not doc_id and not path:
        return {"ok": False, "error": "doc_id 与 path 至少给一个"}
    return knowledge.get_doc(doc_id=doc_id, path=path or "", max_chars=max_chars)



@mcp.tool()
def knowledge_status() -> dict:
    """知识库索引概况：文档数 / 分块数 / 向量覆盖 / 各知识域分布。

    回答「知识库导进来了没有、向量建全了没有」时先看这个，不要靠猜。
    """
    _ensure_db()
    return knowledge.status()



@mcp.tool()
def recall_all(query: str, limit: int = 5, memory_limit: int = 0,
               knowledge_limit: int = 0, root: str = "",
               include_knowledge: bool = True) -> dict:
    """统一检索：一句话同时查记忆与知识（本地 dsh-memory 融入 MCP 的接缝）。

    这是 scripts/recall.py 的 MCP 版本。两个库都是"关键词主导 + 向量补漏"，
    所以同一句话不会在两边被排成两套逻辑；这里把两边结果放进同一个返回体，
    调用方拿到的是"关于这个问题，系统一共有哪些东西"。

    - 记忆走 recall_memories（词法 0.43 + 语义 0.12 融合，含治理分）
    - 知识走 knowledge.search（关键词主导，向量过地板才加分）

    `root` 只作用于知识侧：knowledge / mine / memory / script。
    传 "" 表示全部。想只看骨架（约定、性格、体系、卷/、脚本）传 memory 或 script。
    """
    _ensure_db()
    query = (query or "").strip()
    if not query:
        return {"ok": False, "error": "query 不能为空", "memories": [], "knowledge": []}
    limit = max(1, min(int(limit), 20))
    mem_lim = max(1, min(int(memory_limit or limit), 20))
    kn_lim = max(1, min(int(knowledge_limit or limit), 20))

    from app.memory_lifecycle import recall_memories as _recall

    memories = _recall(query, top_k=mem_lim)
    out_mem = []
    for m in memories:
        anchor = next((str(t)[4:] for t in (m.get("tags") or [])
                       if str(t).startswith("src:")), str(m.get("id")))
        content = m.get("content") or ""
        head, _, body_text = content.partition("\n")
        out_mem.append({
            "anchor": anchor,
            "id": m.get("id"),
            "title": head.lstrip("#").strip() or "(无标题)",
            "body": body_text.strip()[:600],
            "tags": [str(t) for t in (m.get("tags") or []) if not str(t).startswith("src:")],
            "score": round(float(m.get("_score") or 0.0), 4),
        })

    out_kn: list[dict] = []
    if include_knowledge:
        res = knowledge.search(query, limit=kn_lim, root=root or "")
        for it in (res.get("results") or []):
            out_kn.append({
                "doc_id": it.get("doc_id"),
                "root": it.get("root"),
                "path": it.get("source") or it.get("rel_path") or "",
                "heading": it.get("heading") or "",
                "snippet": (it.get("snippet") or "").strip()[:400],
                "score": round(float(it.get("score") or 0.0), 4),
            })

    return {"ok": True, "query": query, "memories": out_mem, "knowledge": out_kn,
            "counts": {"memories": len(out_mem), "knowledge": len(out_kn)}}



@mcp.tool()
def export_memory_package(backup_type: str = "source_only") -> dict:
    """按四模式导出一个便携记忆包到受管目录：source_only / source_with_db / full / knowledge_tasks。"""
    op = "export_memory_package"
    bt = str(backup_type or "").strip()
    if bt not in _pbackup.BACKUP_TYPES:
        return _tool_error(op, f"非法导出模式：{backup_type or '(空)'}（可选：{', '.join(_pbackup.BACKUP_TYPES)}）")
    try:
        return _tool_result(op, _pbackup.export_portable(bt))
    except Exception as exc:  # noqa: BLE001 — MCP 边界统一转结构化错误
        return _tool_error(op, f"导出异常：{type(exc).__name__}: {exc}")



@mcp.tool()
def list_memory_packages() -> dict:
    """列出受管目录中已有的便携记忆包（只读；只看文件名/大小/时间）。"""
    op = "list_memory_packages"
    try:
        return _tool_result(op, _pbackup.list_packages())
    except Exception as exc:  # noqa: BLE001
        return _tool_error(op, f"列包异常：{type(exc).__name__}: {exc}")



@mcp.tool()
def inspect_memory_package(package_id: str, expected_type: str = "") -> dict:
    """只读查看一个便携包的 manifest 摘要与校验结果。"""
    op = "inspect_memory_package"
    path, err = _pkg_path(package_id)
    if err:
        return _tool_error(op, err)
    et = str(expected_type or "").strip() or None
    try:
        return _tool_result(op, _pbackup.inspect_package(path, expected_type=et))
    except Exception as exc:  # noqa: BLE001
        return _tool_error(op, f"查看异常：{type(exc).__name__}: {exc}")



@mcp.tool()
def validate_memory_package(package_id: str, expected_type: str = "") -> dict:
    """对一个便携包执行 14 项安全与完整性校验（只读，不解包到正式目录）。"""
    op = "validate_memory_package"
    path, err = _pkg_path(package_id)
    if err:
        return _tool_error(op, err)
    et = str(expected_type or "").strip() or None
    try:
        return _tool_result(op, _pbackup.validate_package_archive(path, expected_type=et))
    except Exception as exc:  # noqa: BLE001
        return _tool_error(op, f"校验异常：{type(exc).__name__}: {exc}")
