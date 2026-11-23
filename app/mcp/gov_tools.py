"""MCP governance 实例的 49 个工具（治理 / 生命周期 / 快照 / 便携包恢复）。

OPT-03 3.1 从 app/mcp_server.py 按域拆出；函数体逐字搬移，装饰器与签名未改。
host 只在需要治理操作时才挂这个实例，所以它单独成文件；两个实例共享同一份
runtime 与同一个 SQLite 库，没有状态分叉。
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
from app.governance_rules import load_rules, RULE_VERSION
from app import governance_service as gov
from app import governance_maintenance as maint
from app import memory_lifecycle as life
from app import feedback_loop
from app import snapshots
from app import protection
from app import distillation
from app import activation_spreading as spreading
from app import portable_backup as _pbackup
from app import portable_restore as _prestore
from app.mcp.runtime import (
    mcp_gov, _ensure_db, _pkg_path, _tool_result, _tool_error,
)
from app.mcp.stores import memory_store as _memstore


@mcp_gov.tool()
def warmup_embeddings() -> dict:
    """后台预加载嵌入模型；立即返回当前加载状态。"""
    from app.embeddings import model_state
    ensure_warmup(DEFAULT_MODEL)
    return {"ok": True, "model": DEFAULT_MODEL, **model_state()}



@mcp_gov.tool()
def score_memories(memory_ids: list[int] | None = None, limit: int = 50,
                   category: str = "", persist: bool = True) -> dict:
    """对活跃记忆评分（重要性/置信/时效/稳定）及解释。"""
    _ensure_db()
    if memory_ids is not None:
        if not isinstance(memory_ids, list):
            return {"ok": False, "message": "memory_ids must be a list of integers"}
        cleaned = []
        for item in memory_ids:
            try:
                cleaned.append(int(item))
            except (TypeError, ValueError):
                return {"ok": False, "message": f"memory_ids contains non-integer value: {item!r}"}
        memory_ids = cleaned
    try:
        limit = max(1, min(int(limit), 500))
    except (TypeError, ValueError):
        return {"ok": False, "message": "limit must be an integer"}
    return gov.score_memories(memory_ids=memory_ids, limit=limit,
                              category=str(category or ""), persist=bool(persist))



@mcp_gov.tool()
def get_memory_governance_status(memory_id: int) -> dict:
    """某记忆的治理视图：评分、保护、归档候选理由、向量状态。"""
    _ensure_db()
    try:
        memory_id = int(memory_id)
    except (TypeError, ValueError):
        return {"ok": False, "message": "memory_id must be an integer"}
    return gov.get_governance_status(memory_id)



@mcp_gov.tool()
def protect_memory(memory_id: int, protected: bool = True, reason: str = "") -> dict:
    """设置/清除保护标志；受保护记忆不被自动归档/替代/覆盖。"""
    _ensure_db()
    try:
        memory_id = int(memory_id)
    except (TypeError, ValueError):
        return {"ok": False, "message": "memory_id must be an integer"}
    return gov.set_protected(memory_id, bool(protected), str(reason or ""))



@mcp_gov.tool()
def detect_memory_conflicts(memory_ids: list[int] | None = None, category: str = "",
                            candidate_count: int | None = None,
                            persist: bool = True) -> dict:
    """对活跃记忆做重复/冲突检测；返回双方、理由、置信度、推荐动作。"""
    _ensure_db()
    try:
        ids = [int(i) for i in (memory_ids or [])]
    except (TypeError, ValueError):
        return {"ok": False, "message": "memory_ids must be a list of integers"}
    return gov.detect_conflicts(memory_ids=ids or None, category=category,
                                 candidate_count=candidate_count, persist=persist)



@mcp_gov.tool()
def list_memory_conflicts(memory_id: int | None = None, resolution: str = "",
                          limit: int = 50) -> dict:
    """列出已记录的重复/冲突发现（可按记忆/状态过滤）。"""
    _ensure_db()
    return gov.list_conflicts(memory_id=memory_id, resolution=resolution, limit=limit)



@mcp_gov.tool()
def review_memory_conflict(conflict_id: int, resolution: str, note: str = "") -> dict:
    """人工裁决一条冲突（duplicate_confirmed/not_conflict/superseded/kept_both/pending）。"""
    _ensure_db()
    try:
        conflict_id = int(conflict_id)
    except (TypeError, ValueError):
        return {"ok": False, "message": "conflict_id must be an integer"}
    return gov.review_conflict(conflict_id, resolution=resolution, note=note,
                               resolved_by="user")



@mcp_gov.tool()
def supersede_memory_from_conflict(conflict_id: int, new_content: str = "",
                                   source: str = "user-confirmed",
                                   confidence: float = 0.95, note: str = "") -> dict:
    """应用明确纠正：建新版本、旧记忆 superseded、保留 derived_from。"""
    _ensure_db()
    try:
        conflict_id = int(conflict_id)
    except (TypeError, ValueError):
        return {"ok": False, "message": "conflict_id must be an integer"}
    return gov.supersede_from_conflict(conflict_id, new_content=new_content,
                                       source=source, confidence=confidence, note=note)



@mcp_gov.tool()
def archive_memory_governance(memory_id: int, reason: str = "",
                             approver: str = "user") -> dict:
    """受控软归档；服务端校验仅活跃+非保护可归档；审计可逆。"""
    _ensure_db()
    try:
        memory_id = int(memory_id)
    except (TypeError, ValueError):
        return {"ok": False, "message": "memory_id must be an integer"}
    return gov.archive_memory_governance(memory_id, reason=str(reason or ""),
                                         approver=str(approver or "user"))



@mcp_gov.tool()
def restore_memory_governance(memory_id: int, reason: str = "",
                             approver: str = "user") -> dict:
    """受控恢复归档记忆到活跃检索；审计可逆。"""
    _ensure_db()
    try:
        memory_id = int(memory_id)
    except (TypeError, ValueError):
        return {"ok": False, "message": "memory_id must be an integer"}
    return gov.restore_memory_governance(memory_id, reason=str(reason or ""),
                                         approver=str(approver or "user"))



@mcp_gov.tool()
def get_governance_action(action_id: int) -> dict:
    """按 id 取治理动作及其回滚信息。"""
    _ensure_db()
    try:
        action_id = int(action_id)
    except (TypeError, ValueError):
        return {"ok": False, "message": "action_id must be an integer"}
    return gov.get_governance_action(action_id)



@mcp_gov.tool()
def rollback_governance_action(action_id: int, reason: str = "") -> dict:
    """回滚已执行的可逆治理动作（archive/restore/protect/mark_review/supersede）。"""
    _ensure_db()
    try:
        action_id = int(action_id)
    except (TypeError, ValueError):
        return {"ok": False, "message": "action_id must be an integer"}
    return gov.rollback_governance_action(action_id, reason=str(reason or ""))



@mcp_gov.tool()
def create_db_snapshot(label: str = "", note: str = "", actor: str = "user") -> dict:
    """整库快照：把所有表 dump 成文件 + 在 db_snapshots 记元数据（含 sha256）。

    单个治理动作有 rollback，但一次批量维护/误跑的爆炸半径往往超出单动作能
    回滚的范围。动批量操作之前先拍一张，出事时整个库回到那一刻。
    """
    _ensure_db()
    return snapshots.create_snapshot(label=str(label or ""), kind="manual",
                                     actor=str(actor or "user"), note=str(note or ""))



@mcp_gov.tool()
def list_db_snapshots(limit: int = 20) -> dict:
    """列出可用快照（含文件是否还在、各表行数）。"""
    _ensure_db()
    return snapshots.list_snapshots(limit=limit)



@mcp_gov.tool()
def restore_db_snapshot(snapshot_id: int, note: str = "", actor: str = "user") -> dict:
    """把整个库恢复到某个快照（单事务，全成或全不成）。

    恢复前会自动给当前状态再拍一张（pre-restore），所以这一步本身也可逆。
    恢复后不自动重建向量，只报告 vector_check.需要重建。
    """
    _ensure_db()
    try:
        snapshot_id = int(snapshot_id)
    except (TypeError, ValueError):
        return {"ok": False, "message": "snapshot_id must be an integer"}
    return snapshots.restore_snapshot(snapshot_id, actor=str(actor or "user"),
                                      note=str(note or ""))



@mcp_gov.tool()
def check_vector_consistency() -> dict:
    """检查向量索引与内容一致性；needs_reindex=True 表示恢复/编辑后该重建。"""
    _ensure_db()
    return snapshots.check_vector_consistency_after_restore()



@mcp_gov.tool()
def prune_db_snapshots(keep: int = 10) -> dict:
    """只保留最近 N 个快照，更早的连同文件一起删。"""
    _ensure_db()
    return snapshots.prune_snapshots(keep=keep)



@mcp_gov.tool()
def assess_memory_protection(memory_id: int) -> dict:
    """某条记忆的保护分档（critical/high/normal/low）及被禁止的自动动作。"""
    _ensure_db()
    try:
        memory_id = int(memory_id)
    except (TypeError, ValueError):
        return {"ok": False, "message": "memory_id must be an integer"}
    with connect() as conn:
        row = rowdict(_memstore.fetch_memory(conn, memory_id))
    if not row:
        return {"ok": False, "message": "Memory not found", "memory_id": memory_id}
    verdict = protection.assess(row)
    verdict.update({"ok": True, "memory_id": memory_id,
                    "content": (row.get("content") or "")[:120]})
    return verdict



@mcp_gov.tool()
def summarize_protection(category: str = "", include_archived: bool = False) -> dict:
    """整库保护分档分布（几条 critical、几条会被自动归档）。"""
    _ensure_db()
    with connect() as conn:
        rows = [rowdict(r) for r in _memstore.select_all_for_protection(
            conn,
            category=category.strip(),
            include_archived=bool(include_archived),
        )]
    return protection.summarize(rows)



@mcp_gov.tool()
def find_distillation_candidates(limit: int = 20, category: str = "",
                                 similarity: float = 0.52,
                                 include_covered: bool = False) -> dict:
    """找值得提炼的「多次具体经历」（只读，不生成结论文字）。

    规则能可靠地找出同一主题下的多条经历并给出证据，但写不出抽象结论——
    硬编只会复制原句。所以这里产出候选 + 证据 + 应用方式，结论由调用方写。
    """
    _ensure_db()
    return distillation.find_distillation_candidates(
        limit=limit, category=category, similarity=similarity,
        include_covered=include_covered)



@mcp_gov.tool()
def apply_distillation(content: str, source_memory_ids: list[int],
                       category: str = "reflection", confidence: float = 0.7,
                       note: str = "", approver: str = "user") -> dict:
    """把一条抽象结论记成记忆，并用 derived_from 挂回它的证据。

    至少要有 3 条来源记忆：更少就不是「提炼」而是「断言新事实」，
    那种情况用 save_memory。源记忆不会被改写或删除。
    """
    _ensure_db()
    return distillation.apply_distillation(
        content=content, source_memory_ids=source_memory_ids, category=category,
        confidence=confidence, note=note, approver=approver)



@mcp_gov.tool()
def list_distillations(limit: int = 50) -> dict:
    """列出所有提炼结果、它们的证据，以及是否已被后续记忆反驳。"""
    _ensure_db()
    return distillation.list_distillations(limit=limit)



@mcp_gov.tool()
def set_review_status(memory_id: int, reason: str = "", approver: str = "user") -> dict:
    """把记忆标 pending_review（人工裁决保留）；排除自动归档候选。"""
    _ensure_db()
    try:
        memory_id = int(memory_id)
    except (TypeError, ValueError):
        return {"ok": False, "message": "memory_id must be an integer"}
    return gov.set_review_status(memory_id, reason=str(reason or ""),
                                 approver=str(approver or "user"))



@mcp_gov.tool()
def release_review_status(memory_id: int, reason: str = "",
                          approver: str = "user") -> dict:
    """解除 pending_review（待审核保留），让记忆重新进入检索。

    pending_review 是**排除标记**：召回、语义检索、关键词检索都会把它过滤掉。
    所以在没有解除路径的情况下，一次 mark_review 就等于把这条记忆永久藏起来。
    人工裁决冲突会顺带自动解除；这个工具用于单独放行。
    """
    _ensure_db()
    try:
        memory_id = int(memory_id)
    except (TypeError, ValueError):
        return {"ok": False, "message": "memory_id must be an integer"}
    return gov.release_review_status(memory_id, reason=str(reason or ""),
                                     approver=str(approver or "user"))



@mcp_gov.tool()
def run_memory_maintenance(mode: str = "dry_run", limit: int = 50,
                          category: str = "", approver: str = "user") -> dict:
    """治理闭环：dry_run 预览 / review 待批 / apply_approved 执行。"""
    _ensure_db()
    if mode not in ("dry_run", "review", "apply_approved"):
        return {"ok": False, "message": "mode 必须是 dry_run / review / apply_approved"}
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        return {"ok": False, "message": "limit must be an integer"}
    return maint.run_memory_maintenance(
        mode=str(mode), limit=limit, category=str(category or ""),
        approver=str(approver or "user"))



@mcp_gov.tool()
def plan_memory_cleanup(limit: int = 50, category: str = "",
                       dry_run: bool = True) -> dict:
    """生成整理计划（dry_run 预览 / 持久化为待批候选）。"""
    _ensure_db()
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        return {"ok": False, "message": "limit must be an integer"}
    return maint.plan_memory_cleanup(limit=limit, category=str(category or ""),
                                     dry_run=bool(dry_run))



@mcp_gov.tool()
def review_governance_actions(approval_status: str = "", execution_status: str = "",
                             run_id: int = None, limit: int = 50) -> dict:
    """按状态列出治理动作供人工审查。"""
    _ensure_db()
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        return {"ok": False, "message": "limit must be an integer"}
    rid = None
    if run_id is not None:
        try:
            rid = int(run_id)
        except (TypeError, ValueError):
            return {"ok": False, "message": "run_id must be an integer"}
    return maint.review_governance_actions(
        approval_status=str(approval_status or ""),
        execution_status=str(execution_status or ""),
        run_id=rid, limit=limit)



@mcp_gov.tool()
def apply_governance_actions(action_ids: list, approver: str = "user") -> dict:
    """审批并执行指定动作（服务端重验权限）。"""
    _ensure_db()
    if not isinstance(action_ids, (list, tuple)):
        return {"ok": False, "message": "action_ids must be a list"}
    try:
        ids = [int(x) for x in action_ids]
    except (TypeError, ValueError):
        return {"ok": False, "message": "action_ids 含非整数值"}
    return maint.apply_governance_actions(ids, approver=str(approver or "user"))



@mcp_gov.tool()
def get_governance_run(run_id: int) -> dict:
    """取一次治理运行的动作汇总与每动作状态。"""
    _ensure_db()
    try:
        run_id = int(run_id)
    except (TypeError, ValueError):
        return {"ok": False, "message": "run_id must be an integer"}
    return maint.get_governance_run(run_id)



@mcp_gov.tool()
def get_vector_consistency() -> dict:
    """检查向量索引与内容一致性（缺失/过期/归档保留）。"""
    _ensure_db()
    return maint.get_vector_consistency()



@mcp_gov.tool()
def recall_memories(query: str = "", top_k: int = 5, category: str = "",
                    memory_type: str = "", include_related: bool = False,
                    explain: bool = False, min_score: float | None = None) -> dict:
    """融合语义+关键词+治理分的可解释检索；含关联子图与激活扩散；稳定排序。

    `min_score` 是**默认关闭**的相关性门槛：本次实测（45 题）证明固定分数阈值
    无法区分相关与无关查询（相关题 top1 最低 0.3707 比 3 道无关题都低），因此
    不设默认值。调用方显式给出时，若全部候选低于门槛则返回空列表并附
    `rejected` 说明，绝不把低分结果包装成有效记忆。
    """
    _ensure_db()
    if not isinstance(top_k, int):
        try:
            top_k = int(top_k)
        except (TypeError, ValueError):
            top_k = 5
    top_k = max(1, min(top_k, 50))
    from app.embeddings import is_ready
    model_ready = is_ready()
    # Ensure the model is warming up even if a request is the first trigger.
    ensure_warmup(DEFAULT_MODEL)
    with connect() as conn:
        rows = life.recall_memories(
            query=query, top_k=top_k, category=category, memory_type=memory_type,
            include_related=include_related, explain=explain, conn=conn,
            min_score=min_score)
    # A rejection is not a memory: pull it out so `results` only ever contains
    # real rows. `count == 0` plus `rejected` is the explicit "nothing relevant"
    # answer, never a short-but-nonempty list.
    rejected = None
    real = []
    for r in rows:
        if isinstance(r, dict) and r.get("_rejected"):
            rejected = r["_rejected"]
        else:
            real.append(r)
    result = {"results": real, "count": len(real),
              "weights": life.RECALL_WEIGHTS if explain else None}
    if rejected is not None:
        result["rejected"] = rejected
        result["empty"] = True
    # Surface whether semantic relevance was actually applied (vs keyword-only
    # degradation while the model is loading) so the caller can decide to retry.
    if query.strip() and not model_ready:
        result["semantic_status"] = "degraded"
        result["semantic_note"] = ("嵌入模型仍在后台加载/下载，本次结果按关键词相关性返回；"
                                   "模型就绪后语义排序将生效。可稍后重试或先调用 warmup_embeddings。")
    return result



@mcp_gov.tool()
def get_memory_details(memory_id: int) -> dict:
    """某记忆完整视图：基础字段+生命周期+治理+反馈+关系。"""
    _ensure_db()
    if not isinstance(memory_id, int):
        try:
            memory_id = int(memory_id)
        except (TypeError, ValueError):
            return {"ok": False, "message": "memory_id must be an integer"}
    return life.get_memory_details(memory_id)



@mcp_gov.tool()
def get_related_memories(memory_id: int, max_depth: int = 2, relation: str = "",
                        max_nodes: int = 30) -> dict:
    """返回某记忆的关系子图；防环、限深限节点。"""
    _ensure_db()
    if not isinstance(memory_id, int):
        try:
            memory_id = int(memory_id)
        except (TypeError, ValueError):
            return {"ok": False, "message": "memory_id must be an integer"}
    return life.get_related_memories(memory_id, max_depth=max_depth,
                                     relation=relation, max_nodes=max_nodes)



@mcp_gov.tool()
def record_memory_feedback(memory_id: int, feedback_type: str, note: str = "",
                          approver: str = "user") -> dict:
    """记录一条使用反馈信号（retrieved/used/helpful/not_relevant/corrected）。

    反馈会回流到评分：helpful/used 抬可信度，corrected/not_relevant 压降，
    幅度封顶 ±25%。retrieved 只记录曝光、**不参与计分**——曝光不等于有用，
    否则被召回最多的会自我强化成最高分。至少要有 2 条带方向的反馈才生效。
    """
    _ensure_db()
    if not isinstance(memory_id, int):
        try:
            memory_id = int(memory_id)
        except (TypeError, ValueError):
            return {"ok": False, "message": "memory_id must be an integer"}
    result = life.record_memory_feedback(memory_id, feedback_type, note=note, approver=approver)
    # Tell the caller what this feedback did to the score, rather than making
    # them run a separate scored query to find out.
    if result.get("ok"):
        try:
            with connect() as conn:
                fb = feedback_loop.feedback_for(conn, memory_id)
            result["signal"] = fb.get("signal", 0.0)
            result["signal_applied"] = fb.get("applied", False)
            result["signal_reason"] = fb.get("reason", "")
        except Exception:
            pass
    return result



@mcp_gov.tool()
def summarize_memory_feedback(limit: int = 20) -> dict:
    """查看反馈信号排行：哪些记忆被反复证明有用、哪些被纠正过。

    只有真正影响评分的记忆才会出现在 top_positive / top_negative 里
    （至少要 2 条带方向反馈）。retrieved 数量单独列出，仅作曝光参考。
    """
    _ensure_db()
    return feedback_loop.summarize_feedback(limit=limit)



@mcp_gov.tool()
def record_recall(memory_ids: list[int] | None = None) -> dict:
    """手动记录一批记忆被召回（曝光）。只留痕，不改变任何评分。"""
    _ensure_db()
    return feedback_loop.record_retrieved(memory_ids or [])



@mcp_gov.tool()
def classify_memory(memory_id: int = 0, content: str = "", category: str = "",
                   source: str = "") -> dict:
    """用确定性信号把记忆归入类型分类（猜测，不写库）。"""
    _ensure_db()
    if memory_id and memory_id > 0:
        with connect() as conn:
            row = _memstore.fetch_type_fields(conn, memory_id)
            if not row:
                return {"ok": False, "message": "memory not found"}
            content = row["content"] or ""
            category = row["category"] or ""
            source = row["source"] or ""
    return life.classify_memory_type(content, category, source)



@mcp_gov.tool()
def find_duplicate_memories(memory_ids: list = None, category: str = "",
                           candidate_count: int = 8) -> dict:
    """在活跃记忆中找重复/近似/纠正对（复用冲突检测）。"""
    _ensure_db()
    ids = None
    if memory_ids:
        if not isinstance(memory_ids, (list, tuple)):
            return {"ok": False, "message": "memory_ids must be a list"}
        ids = [int(x) for x in memory_ids]
    if not isinstance(candidate_count, int):
        candidate_count = 8
    candidate_count = max(5, min(candidate_count, 10))
    return life.find_duplicate_memories(memory_ids=ids, category=category,
                                        candidate_count=candidate_count)



@mcp_gov.tool()
def create_memory_relation(source_memory_id: int, target_memory_id: int,
                           relation: str = "related_to", note: str = "",
                           approver: str = "user") -> dict:
    """在 memory_links 建关系；防环、幂等、校验合法性。"""
    _ensure_db()
    try:
        s, t = int(source_memory_id), int(target_memory_id)
    except (TypeError, ValueError):
        return {"ok": False, "message": "source/target memory ids must be integers"}
    return life.create_memory_relation(s, t, relation=relation, note=note, approver=approver)



@mcp_gov.tool()
def list_memory_relations(memory_id: int) -> dict:
    """列出某记忆的全部关系（双向，只读）。"""
    _ensure_db()
    if not isinstance(memory_id, int):
        try:
            memory_id = int(memory_id)
        except (TypeError, ValueError):
            return {"ok": False, "message": "memory_id must be an integer"}
    return life.list_memory_relations(memory_id)



@mcp_gov.tool()
def get_memory_lifecycle(memory_id: int) -> dict:
    """返回记忆生命周期状态及合法转移。"""
    _ensure_db()
    if not isinstance(memory_id, int):
        try:
            memory_id = int(memory_id)
        except (TypeError, ValueError):
            return {"ok": False, "message": "memory_id must be an integer"}
    with connect() as conn:
        row = _memstore.fetch_memory(conn, memory_id)
        if not row:
            return {"ok": False, "message": "memory not found"}
        d = dict(row)
        return {"ok": True, "memory_id": memory_id,
                "lifecycle_state": life.lifecycle_state_of(d),
                "allowed_transitions": life.lifecycle_transitions(d),
                "status": d.get("status"), "memory_status": d.get("memory_status")}



@mcp_gov.tool()
def consolidate_memory_candidates(persist: bool = False, limit: int = 50,
                                 category: str = "", memory_type: str = "",
                                 approver: str = "user") -> dict:
    """仅扫描生成巩固候选（不改动记忆）。"""
    _ensure_db()
    if not isinstance(limit, int):
        limit = 50
    return life.consolidate_memory_candidates(
        persist=persist, limit=limit, category=category, memory_type=memory_type,
        approver=approver)



@mcp_gov.tool()
def run_memory_consolidation(candidate_ids: list = None, dry_run: bool = False,
                            approver: str = "user", rule_version: str = "") -> dict:
    """逐候选独立事务应用已批准巩固候选；审计可逆。"""
    _ensure_db()
    ids = None
    if candidate_ids is not None:
        if not isinstance(candidate_ids, (list, tuple)):
            return {"ok": False, "message": "candidate_ids must be a list"}
        ids = [int(x) for x in candidate_ids]
    return life.run_memory_consolidation(candidate_ids=ids, dry_run=dry_run,
                                         approver=approver, rule_version=rule_version)



@mcp_gov.tool()
def get_memory_usage_stats(memory_id: int = 0) -> dict:
    """返回记忆使用反馈统计（单条或全活跃排名）。"""
    _ensure_db()
    return life.get_memory_usage_stats(memory_id=int(memory_id or 0))



@mcp_gov.tool()
def spread_activation(memory_ids: list = None, decay: float = 0.5, max_depth: int = 2,
                      include_recent_events: bool = True, window_days: int = 30) -> dict:
    """沿关系图传播激活值（纯读、确定性、防环、衰减），供检索重排。"""
    _ensure_db()
    ids = None
    if memory_ids:
        ids = [int(x) for x in memory_ids]
    return spreading.compute_activation(
        memory_ids=ids, decay=float(decay), max_depth=int(max_depth),
        include_recent_events=bool(include_recent_events), window_days=int(window_days))



@mcp_gov.tool()
def record_memory_activation(memory_id: int, reason: str = "", approver: str = "user") -> dict:
    """仅写审计事件记录人工再激活（不改状态）。"""
    _ensure_db()
    return spreading.record_activation(int(memory_id), reason=reason, approver=approver)



@mcp_gov.tool()
def preview_memory_restore(package_id: str, restore_mode: str = "merge",
                           conflict_policy: str = "keep_target",
                           delete_extras: bool = False, ttl_sec: int = 300) -> dict:
    """只读预览恢复：真实差异统计 + 冲突/删除项 + compatibility + 一次性确认令牌。"""
    op = "preview_memory_restore"
    path, err = _pkg_path(package_id)
    if err:
        return _tool_error(op, err)
    rm = str(restore_mode or "").strip()
    cp = str(conflict_policy or "").strip()
    if rm not in _prestore.RESTORE_MODES:
        return _tool_error(op, f"非法恢复模式：{restore_mode or '(空)'}")
    if cp not in _prestore.CONFLICT_POLICIES:
        return _tool_error(op, f"非法冲突策略：{conflict_policy or '(空)'}")
    try:
        ttl = int(ttl_sec)
    except (TypeError, ValueError):
        return _tool_error(op, "ttl_sec 必须是整数")
    if not (1 <= ttl <= 86400):
        return _tool_error(op, "ttl_sec 必须在 1..86400 之间")
    try:
        inner = _prestore.preview_restore(
            path, restore_mode=rm, conflict_policy=cp,
            delete_extras=bool(delete_extras), ttl_sec=ttl)
        out = _tool_result(op, inner, restore_id=inner.get("restore_id"))
        if inner.get("blockers"):
            out["blockers"] = list(inner["blockers"])
        return out
    except Exception as exc:  # noqa: BLE001
        return _tool_error(op, f"预览异常：{type(exc).__name__}: {exc}")



@mcp_gov.tool()
def restore_memory_package(restore_id: str, confirmation_token: str, package_id: str,
                           restore_mode: str = "merge",
                           conflict_policy: str = "keep_target",
                           delete_extras: bool = False) -> dict:
    """执行恢复：必须显式传预览生成的 restore_id 与一次性令牌，先确认后执行；工具绝不自行生成令牌。"""
    op = "restore_memory_package"
    rid = str(restore_id or "").strip()
    tok = str(confirmation_token or "").strip()
    if not rid:
        return _tool_error(op, "缺少 restore_id（请先调用 preview_memory_restore）")
    if not tok:
        return _tool_error(op, "缺少 confirmation_token（禁止跳过确认；请先预览）")
    path, err = _pkg_path(package_id)
    if err:
        return _tool_error(op, err)
    rm = str(restore_mode or "").strip()
    cp = str(conflict_policy or "").strip()
    if rm not in _prestore.RESTORE_MODES:
        return _tool_error(op, f"非法恢复模式：{restore_mode or '(空)'}")
    if cp not in _prestore.CONFLICT_POLICIES:
        return _tool_error(op, f"非法冲突策略：{conflict_policy or '(空)'}")
    try:
        # 第一步：显式确认（S2 内部重算绑定指纹 + 一次性消费令牌）
        cf = _prestore.confirm_restore(
            rid, tok, package_path=path, restore_mode=rm,
            conflict_policy=cp, delete_extras=bool(delete_extras))
        if not cf.get("ok"):
            out = _tool_result(op, cf, restore_id=rid)
            out["stage"] = "confirm"
            return out
        # 第二步：执行（令牌已消费；S2 内部再次比对确认时指纹；不传 fail_inject）
        ex = _prestore.execute_restore(
            rid, tok, package_path=path, restore_mode=rm,
            conflict_policy=cp, delete_extras=bool(delete_extras))
        out = _tool_result(op, ex, restore_id=rid)
        out["stage"] = "execute"
        out["confirmation"] = cf
        return out
    except Exception as exc:  # noqa: BLE001
        return _tool_error(op, f"恢复异常：{type(exc).__name__}: {exc}")



@mcp_gov.tool()
def delete_memory_package(package_id: str) -> dict:
    """删除一个受管便携包（只删 data/memory_packages 内的包，不删保护性备份与 staging）。"""
    op = "delete_memory_package"
    pid = str(package_id or "").strip()
    if not pid:
        return _tool_error(op, "缺少 package_id")
    try:
        return _tool_result(op, _pbackup.delete_package(pid))
    except Exception as exc:  # noqa: BLE001
        return _tool_error(op, f"删除异常：{type(exc).__name__}: {exc}")
