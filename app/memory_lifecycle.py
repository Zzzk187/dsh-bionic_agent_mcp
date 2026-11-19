"""Phase 6 + 7 memory lifecycle, types, unified recall, feedback and consolidation.

Design rules (must hold for every change in this file):
  * Purely additive on top of Phases 1-5. No column drops, no table rebuilds,
    no rewrite of existing rows, no change to Phase 1-4 tool semantics.
  * `memories.status` stays the single retrieval filter (active/archived);
    `memories.memory_status` is the governance VIEW field
    (active/pending_review/superseded/archived). The two are NEVER merged.
  * Recall never returns archived, pending_review or superseded memories unless
    explicitly asked (via the original, unchanged tools).
  * Consolidation only GENERATES candidates; turning a candidate into a real
    memory change requires explicit approval + execution and is fully audited
    and reversible. Unverified inference is never promoted to a fact.
  * All multi-table mutations run inside `db.transaction()` so a failure leaves
    no half-written state.
"""
from __future__ import annotations

import json
import math
import re
from typing import Optional

from app import db
from app.embeddings import EmbeddingUnavailable
from app import feedback_loop
from app import expiry
from app import recall_profile
from app import protection
from app import governance_service as gov
from app import governance_rules as rules
from app import governance_scoring as scoring
from app import governance_conflicts as conflicts
from app import activation_spreading as spreading
# S6-FINAL-FIX P2-3：seed 阈值只在 memory_lexical 定义一次。
# 已验证无循环导入：memory_lexical 只 import knowledge，knowledge 只
# import db/embeddings，都不回指本模块。
from app import memory_lexical

# Governance columns are stripped from recall output so the fused retrieval
# stays compatible with the Phase 1-4 response shape. Mirrors mcp_server._memory_out.
_GOV_COLUMNS = {
    "importance_score", "confidence_score", "freshness_score", "access_count",
    "last_accessed_at", "last_evaluated_at", "memory_status", "protected",
}


def _clean(row: dict) -> dict:
    result = dict(row)
    if "tags_json" in result:
        try:
            result["tags"] = json.loads(result.pop("tags_json") or "[]")
        except (TypeError, ValueError):
            result["tags"] = []
            result.pop("tags_json", None)
    for k in _GOV_COLUMNS:
        result.pop(k, None)
    return result

# --------------------------------------------------------------------------
# Versioned configuration (deterministic, not data-validated weights)
# --------------------------------------------------------------------------
LIFECYCLE_RULE_VERSION = "phase6-lifecycle-v1"

# Recall fusion weights (sum-normalised at query time). These are stable rules,
# not adaptive weights: the task forbids adaptive ranking unless enough feedback
# exists AND it is validated; we ship stable rules so recall is reproducible.
#
# 2026-11-16 (S5-E): relevance share raised 0.55 -> 0.8652, measured on the
# 41-answer set through the real `recall_profile` clamp path:
#   R@1 0.6585 -> 0.7317 (+0.0732), MRR@5 0.7724 -> 0.8211 (+0.0487),
#   nDCG@5 0.8118 -> 0.8484 (+0.0366), R@3/R@5 unchanged (0.9268 both).
# The gain does not come from "adding relevance" but from *not double counting*
# the governance columns: `importance_score` already folds frequency (0.25) and
# confidence (0.20) into itself, so keeping both the aggregate and its inputs at
# full weight fed the same signal in twice. Every component keeps a non-zero
# weight -- relevance dominates, the priors only break ties. See
# S5E_IMPLEMENTATION_REPORT.md for the full A/T1..T4 ablation table.
RECALL_WEIGHTS = {
    "semantic": 0.27,
    "keyword": 0.60,
    "importance": 0.03,
    "confidence": 0.03,
    "freshness": 0.03,
    "frequency": 0.02,
    "activation": 0.02,
}

# Feedback nudge for the fused score. Deliberately tiny and its own component
# rather than folded into "confidence" (which already absorbs feedback through
# governance scoring): folding it in would double-count the same signal, and
# keeping it separate keeps `_explain` honest about where a ranking came from.
# `retrieved` never reaches here -- see app/feedback_loop.py.
RECALL_FEEDBACK_WEIGHT = 0.06

# Memory-type taxonomy. `unknown` means the row has no type yet (legacy data).
MEMORY_TYPES = ("episodic", "semantic", "preference", "procedural", "goal", "reflection", "unknown")

# Allowed relation vocabulary for the richer graph (reuses existing semantics).
ALLOWED_RELATIONS = (
    "related_to", "supports", "contradicts", "supersedes", "derived_from",
    "part_of", "occurred_before", "caused_by", "similar_to",
)
# Directed relations that must not form a cycle.
DIRECTED_RELATIONS = ("derived_from", "part_of", "occurred_before", "caused_by", "supersedes")

# Legal lifecycle transitions. Key = from-state, value = set of to-states.
# States map onto (status, memory_status) pairs; see LIFECYCLE_STATE_OF.
LEGAL_TRANSITIONS = {
    "retrievable": {"pending_review", "archived", "superseded"},
    "pending_review": {"retrievable", "archived"},
    "superseded": {"archived"},
    "archived": {"retrievable"},
}

LIFECYCLE_STATES = ("retrievable", "pending_review", "superseded", "archived")


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _json(x, default="{}"):
    if x is None or x == "":
        return default
    if isinstance(x, (dict, list)):
        return json.dumps(x, ensure_ascii=False)
    return x


def _json_loads(x, default=None):
    if x is None or x == "":
        return default if default is not None else {}
    try:
        return json.loads(x)
    except (TypeError, ValueError):
        return default if default is not None else {}


# --------------------------------------------------------------------------
# Lifecycle state mapping (status/memory_status -> lifecycle state)
# --------------------------------------------------------------------------
def lifecycle_state_of(row) -> str:
    """Map a memory row to one of the four lifecycle states.

    NOTE: sqlite3.Row does NOT support `"key" in row` (the `in` operator tests
    values, not keys), so we read keys via row.keys() once.
    """
    keys = set(row.keys())
    status = (row["status"] if "status" in keys else None) or "active"
    mstatus = (row["memory_status"] if "memory_status" in keys else None) or status
    # memory_status (the governance view) takes precedence over the raw
    # retrieval status, so a superseded/archived row keeps its real state.
    if mstatus == "superseded":
        return "superseded"
    if mstatus == "pending_review":
        return "pending_review"
    if status == "archived":
        return "archived"
    return "retrievable"


def lifecycle_transitions(row) -> list[str]:
    return sorted(LEGAL_TRANSITIONS.get(lifecycle_state_of(row), set()))


# --------------------------------------------------------------------------
# Memory-type classification (deterministic; never elevates inference to fact)
# --------------------------------------------------------------------------
_TYPE_HINTS = [
    # English terms keep \b (so "plan" does not match "planet"); CJK terms are
    # bare, because \b never fires between two Han characters and wrapping
    # them in \b silently disabled every Chinese keyword.
    ("preference", re.compile(
        r"(?:\b(?:prefer|preference|like|dislike|want|need|wish|would rather|"
        r"favorite|favourite|avoid)\b|"
        r"偏好|喜欢|讨厌|不喜欢|不想|习惯|口味|风格|要求|希望|倾向)", re.I)),
    ("procedural", re.compile(
        r"(?:\b(?:how to|step|steps|procedure|workflow|run|execute|command|"
        r"script|recipe|config|configure|setup|install)\b|"
        r"流程|步骤|如何|怎么|怎样|命令|脚本|配置|执行|安装|部署|操作)", re.I)),
    ("goal", re.compile(
        r"(?:\b(?:goal|objective|plan|target|aim|milestone|roadmap|todo)\b|"
        r"目标|计划|任务|打算|里程碑|待办|规划)", re.I)),
    ("reflection", re.compile(
        r"(?:\b(?:lesson|learned|reflection|retrospective|insight|mistake|"
        r"takeaway|conclusion)\b|"
        r"教训|反思|复盘|学到|结论|总结|经验|踩坑|坑)", re.I)),
    ("episodic", re.compile(
        r"(?:\b(?:yesterday|last time|happened|meeting|on monday|we discussed|"
        r"earlier|today)\b|"
        r"昨天|前天|上次|刚才|那次|当时|会议|讨论|发生|今天)", re.I)),
]

#: Non-English category names -> memory type.
#:
#: dsh-memory (the system this instance mirrors) files memories under Chinese
#: section names, so `category` arrives as 环境 / 项目 / 教训 / 授权. The check
#: below used to be `cat in MEMORY_TYPES` alone, which silently rejected every
#: one of them: the category signal — documented as "stronger than free text" —
#: contributed nothing at all on imported rows. Keep this map explicit rather
#: than guessing from the characters, so each mapping can be argued with.
_CATEGORY_TYPE = {
    "环境": "semantic",     # facts about this machine/setup
    "项目": "semantic",     # project inventory and status
    "教训": "reflection",   # lessons learned / post-mortems
    "授权": "preference",   # standing authorizations and user profile
}


def classify_memory_type(content: str, category: str = "", source: str = "") -> dict:
    """Deterministic, explainable type guess. Never returns a forced bucket.

    Returns {memory_type, confidence, reasons, needs_review}. Low-confidence or
    ambiguous classifications set needs_review=True so a human can adjudicate;
    the data is never lost and the memory stays fully searchable.
    """
    text = f"{category} {content} {source}".strip()
    hits = []
    for t, pat in _TYPE_HINTS:
        if pat.search(text):
            hits.append(t)
    # Category is a stronger signal than free text. Resolve it through
    # _CATEGORY_TYPE so non-English section names count too (see the note
    # there); an English type name is accepted as-is.
    cat = (category or "").lower().strip()
    cat_type = ""
    raw_cat = (category or "").strip()
    if raw_cat in _CATEGORY_TYPE:
        cat_type = _CATEGORY_TYPE[raw_cat]
    elif cat in MEMORY_TYPES and cat != "unknown":
        cat_type = cat
    if cat_type and cat_type not in hits:
        hits.insert(0, cat_type)
    reasons = [f"matched '{h}' signals" for h in hits]
    if not hits:
        return {
            "memory_type": "unknown",
            "confidence": 0.0,
            "reasons": ["no type signal matched; legacy/untyped memory kept as 'unknown'"],
            "needs_review": False,
        }
    # Multiple distinct type hints => ambiguous.
    distinct = sorted(set(hits))
    if len(distinct) > 1:
        # Pick the category-derived type, not `distinct[0]`. Sorted order is
        # alphabetical, so the old code let the spelling of a type name decide
        # the outcome: a 教训 row matching both "procedural" and "reflection"
        # always came out procedural even though its category said reflection.
        # The category is the stated strong signal, so it wins; with no
        # category signal we fall back to the hint order in _TYPE_HINTS
        # (most-specific first), which is stable and explainable.
        chosen = cat_type or next(t for t, _ in _TYPE_HINTS if t in distinct)
        if chosen not in distinct:
            chosen = distinct[0]
        by_category = bool(cat_type) and chosen == cat_type
        return {
            "memory_type": chosen,
            # A category that decided the tie is NOT a guess: the category
            # records which volume a human filed the memory under, while the
            # extra hits are keyword coincidences. Flagging that as 0.4 /
            # needs_review would queue dozens of import rows for adjudication
            # that has already been made once, at filing time. Only a tie
            # broken by hint order (no category at all) is genuinely uncertain.
            "confidence": 0.75 if by_category else 0.4,
            "reasons": reasons + [f"ambiguous: also matched {[d for d in distinct if d != chosen]}",
                                  f"chosen by {'category' if by_category else 'hint order'}"],
            "needs_review": not by_category,
        }
    conf = 0.85 if cat_type == distinct[0] else 0.6
    return {
        "memory_type": distinct[0],
        "confidence": conf,
        "reasons": reasons,
        "needs_review": conf < 0.6,
    }


# --------------------------------------------------------------------------
# Unified recall / retrieval orchestration
# --------------------------------------------------------------------------
def _tokenize(text: str) -> set[str]:
    text = (text or "").lower()
    # latin words + CJK unigrams (cheap, dependency-free)
    toks = set(re.findall(r"[a-z0-9]+", text))
    cjk = re.findall(r"[\u4e00-\u9fff]", text)
    toks.update(cjk)
    return toks


def _keyword_score(query: str, content: str) -> float:
    """旧的字符余弦打分，**已不参与召回排序**。

    保留它只为兼容既有调用点与 `_explain` 里的旧口径；量纲（0..1）看着
    像新的归一化词法分但其含义不同。新代码一律走 `_lexical_scores`。
    实测差距（2026-11-08，8 题自然语言集）：这套 recall@1 3/8，移植版 6/8。
    """
    q = _tokenize(query)
    c = _tokenize(content)
    if not q:
        return 0.0
    inter = q & c
    if not inter:
        return 0.0
    return len(inter) / math.sqrt(len(q) * max(1, len(c)))


def _lexical_scores(query: str, rows, diagnostics: dict | None = None) -> dict[int, float]:
    """字段加权的词法分（0..1），按批归一化。见 `app.memory_lexical`。

    必须按批算而不是逐条算：归一化的分母是本批 peak，逐条拿不到它，
    而峰值恰恰决定「这条是不是这批里最像的」。

    `diagnostics`（S6-FIX-06）：可选出参，透传给 `score_all` 让概念桥把
    「本次命中几条 / 哪些 id / 有几条跨过 seed 阈值」写进来。它只记账，
    **不影响返回的分数**；`None` 时整条诊断路径不执行。
    """
    from app import memory_lexical
    mems = [dict(r) for r in rows]
    return memory_lexical.score_all(query, mems, diagnostics)


def _soft_freq(access_count: int) -> float:
    n = max(0, int(access_count or 0))
    # mirrors governance_scoring soft ceiling to avoid inflation.
    raw = 100.0 * math.log1p(n) / math.log1p(rules.FREQUENCY_SATURATION_ACCESSES)
    knee = 90.0
    if raw <= knee:
        return raw
    excess = raw - knee
    return knee + rules.FREQUENCY_OVERFLOW_HEADROOM * (excess / (excess + rules.FREQUENCY_OVERFLOW_HEADROOM))


def _usage_freq(fb_counts: dict | None) -> float:
    """Frequency component from *real* use, not from exposure.

    2026-11-16 (S5-E): `access_count` was being used here, but the only writer is
    `governance_service.touch_access`, which `recall_memories` calls on every
    returned row. That made the column an exposure counter, not a usage counter --
    measured on the live DB, 47/49 active rows sat at 0 while the one memory that
    every query returns ("文档一律用中文") had reached 260. Scoring on it therefore
    rewarded being *returned*, which is exactly the self-reinforcing loop
    `app/feedback_loop.py` forbids (retrieved must contribute 0.0).

    So the component now reads only directional usage feedback (`used` /
    `helpful`), which a caller must assert explicitly. With no such feedback the
    value is 0.0 and the component simply does not participate; when real feedback
    arrives it starts to count. `access_count` is left untouched as an audit
    trail -- we do not rewrite stored statistics.
    """
    if not fb_counts:
        return 0.0
    used = max(0, int(fb_counts.get("used") or 0))
    helpful = max(0, int(fb_counts.get("helpful") or 0))
    # `helpful` counts double: it is the stronger endorsement of the two.
    effective = used + 2 * helpful
    if effective <= 0:
        return 0.0
    return _soft_freq(effective)


def _keyword_fallback(query: str, limit: int = 10, category: str = "") -> list:
    """Return active, non-excluded memories ranked by keyword overlap only.

    Used as the immediate-degradation path when the embedding model is still
    loading, so a semantic search / recall never blocks on a model download.
    Never returns archived, pending_review or superseded memories.
    """
    with db.connect() as conn:
        sql = ("SELECT * FROM memories WHERE status='active' "
               "AND memory_status NOT IN ('pending_review', 'superseded')")
        args: list = []
        if category.strip():
            sql += " AND category=?"
            args.append(category.strip()[:100])
        rows = conn.execute(sql + " ORDER BY id", args).fetchall()
    # 降级路径也用同一套词法分：模型没就绪时如果退回字符余弦，用户在同一个
    # 查询上会得到两套完全不同的排序，而他无法知道当前是哪一条路径。
    lex = _lexical_scores(query, rows)
    scored = [(dict(r), lex[r["id"]]) for r in rows if r["id"] in lex]
    scored.sort(key=lambda x: (x[1], x[0].get("updated_at") or "", x[0]["id"]), reverse=True)
    return [d for d, _ in scored[:max(1, min(int(limit), 50))]]


def _semantic_scores(conn, query: str) -> dict[int, float]:
    """Return {memory_id: cosine similarity} for active, indexed memories.

    Degrades to an empty map (caller falls back to keyword) only while the
    embedding model is actively downloading on first use, so a semantic search
    never blocks on the network fetch and trips the MCP client timeout. If the
    model is already cached/loaded, embed_query returns in ~1s and semantic
    ranking applies normally.
    """
    out: dict[int, float] = {}
    if not query.strip():
        return out
    from app import embeddings
    # 模型正在后台加载/下载时，征得一个小小的等待窗口再降级。
    # 直接返回空 dict 会让「刚启动后的第一次召回」和之后每一次
    # 不一样（同一句话两个排序，调用方无从得知）；而模型从本地缓存
    # 加载只需 ~1.5s，等这一下比给出第二种排序划算。
    if embeddings.model_state().get("status") == "pending":
        import time as _time
        _deadline = _time.time() + 3.0
        while _time.time() < _deadline:
            if embeddings.model_state().get("status") != "pending":
                break
            _time.sleep(0.05)
    if embeddings.model_state().get("status") == "pending":
        # A first-use download is in progress in the background worker; do not
        # block this request — degrade to keyword and let the caller retry.
        return out
    try:
        qv = embeddings.embed_query(query)
    except EmbeddingUnavailable:
        return out
    except Exception:
        return out
    rows = conn.execute(
        """SELECT m.id, e.vector_json, e.content_hash, m.content
             FROM memories m JOIN memory_embeddings e ON e.memory_id=m.id
            WHERE m.status='active' AND m.memory_status NOT IN ('pending_review', 'superseded')
              AND e.model_name=?""",
        (embeddings.DEFAULT_MODEL,),
    ).fetchall()
    for r in rows:
        if r["content_hash"] != embeddings.content_hash(r["content"]):
            continue
        try:
            vec = json.loads(r["vector_json"])
        except (TypeError, ValueError):
            continue
        out[r["id"]] = embeddings.cosine_similarity(qv, vec)
    return out


def recall_memories(query: str = "", top_k: int = 5, category: str = "",
                    memory_type: str = "", include_related: bool = False,
                    explain: bool = False, conn=None,
                    record_access: bool = True,
                    min_score: float | None = None) -> list[dict]:
    """Fused, explainable, reproducible retrieval.

    Combines semantic + keyword + governance scores (importance/confidence/
    freshness/frequency) and optional relation boosting. Never returns
    archived, pending_review or superseded memories. Stable ordering: tie-break
    by (updated_at desc, id desc).
    """
    top_k = max(1, min(int(top_k), 50))

    # Chosen once per query, not per candidate: the weights are a property of
    # the question being asked. The result carries the profile name so a
    # surprising ranking can be traced to "which profile" instead of guesswork.
    profile = recall_profile.weights_for(memory_type=memory_type, query=query)
    weights = profile["weights"]
    shape = profile["shape"]

    def _run(c):
        sql = ("SELECT * FROM memories WHERE status='active' "
               "AND memory_status NOT IN ('pending_review', 'superseded')")
        args: list = []
        if category.strip():
            sql += " AND category=?"
            args.append(category.strip()[:100])
        if memory_type.strip() and memory_type.strip() != "unknown":
            sql += " AND memory_type=?"
            args.append(memory_type.strip())
        sql += " ORDER BY id"
        rows = c.execute(sql, args).fetchall()

        sem = _semantic_scores(c, query)
        # 词法分换成字段加权版（标题×6 / 标签×4 / 正文×2，移植自
        # `记忆检索.ps1`）。它按批归一化，所以先对全体候选算一次。
        #
        # S6-FIX-06：`lex_diag` 是概念桥的只读诊断出参（命中数/命中 id/
        # 跨 seed 阈值数）。它**只被写、不被读来改分**：`lex` 的取值路径与
        # 加诊断前完全相同。仅在 explain=True 时随结果回显。
        #
        # S6-FINAL-FIX P2-2：explain=False 时传 None 而不是空 dict。以前
        # 无条件建 {} 并传下去，等于每次检索都白跑一遍诊断记账、只是不回显；
        # 现在 None 让 apply_concept_bridge 里的 `want_diag` 直接为假。
        lex_diag: dict | None = {} if explain else None
        lex = _lexical_scores(query, rows, lex_diag)
        seeds = [d["id"] for d in rows
                 if (sem.get(d["id"]) is not None and sem.get(d["id"]) >= 0.45)
                 or lex.get(d["id"], 0.0) >= memory_lexical.LEX_SEED_THRESHOLD]
        # Even when nothing in the query hits hard enough to be a seed, usage
        # history still has something to say: a memory that keeps getting read
        # seeds its neighbours. Short-circuiting on an empty seed list, as this
        # used to, made the usage-seed path dead code for precisely the vague
        # queries that need it most -- only well-matched queries ever spread.
        spread = (spreading.activation_for_seed_query(c, seeds) if seeds
                  else spreading.compute_activation(conn=c).get("activation", {}))
        # One query for every feedback row instead of one per candidate: the
        # feedback table is small and N+1 lookups here would be the hot path.
        fb_rows = {}
        try:
            for fb in c.execute("SELECT * FROM memory_feedback").fetchall():
                fb_rows[fb["memory_id"]] = feedback_loop._counts(fb)
        except Exception:
            fb_rows = {}
        scored = []
        for row in rows:
            d = dict(row)
            kw = lex.get(d["id"], 0.0)
            sm = sem.get(d["id"])
            imp = float(d.get("importance_score") or 0.0)
            conf = float(d.get("confidence_score") or 0.0)
            fr = float(d.get("freshness_score") or 0.0)
            freq = _usage_freq(fb_rows.get(d["id"]))
            act = spread.get(d["id"], {}).get("activation", 0.0)
            if d["id"] in fb_rows:
                fb_info = feedback_loop.compute_signal(fb_rows[d["id"]])
                fb_term = feedback_loop.recall_bonus(fb_info["signal"]) if fb_info["applied"] else 0.0
            else:
                fb_info = None
                fb_term = 0.0
            parts = {
                "semantic": (sm if sm is not None else 0.0),
                "keyword": kw,
                "importance": imp / 100.0,
                "confidence": conf / 100.0,
                "freshness": fr / 100.0,
                "frequency": freq / 100.0,
                "activation": act,
                "feedback": fb_term,
            }
            total = sum(parts[k] * weights[k] for k in weights)
            total += parts["feedback"] * RECALL_FEEDBACK_WEIGHT
            # Per-candidate soft prior: a "how do I" question gives a memory
            # typed `procedural` a nudge; it never excludes anything.
            mtype_mult = recall_profile.type_multiplier(d.get("memory_type") or "", shape)
            if mtype_mult != 1.0:
                total *= mtype_mult
            # Expiry demotes, never removes. A memory past its stated shelf life
            # is usually still *true*, just no longer current -- so it keeps its
            # content and just ranks lower, and the caller is told why.
            exp_penalty = expiry.recall_penalty(d.get("expires_at"))
            if exp_penalty:
                total -= exp_penalty
            rec = _clean(d)
            rec["_score"] = round(total, 6)
            if exp_penalty:
                rec["_expired"] = True
                rec["_expiry_penalty"] = exp_penalty
            if explain:
                # 6dp, not 4dp: `_score` is rounded to 6dp, so explain has to be
                # at least as precise for a caller to recompute it. At 4dp the
                # rounding error was ~2e-5 once a single component carried a
                # large weight (keyword 0.61), which made `_explain` unable to
                # reproduce `_score` -- i.e. the ranking stopped being auditable.
                rec["_explain"] = {k: round(v, 6) for k, v in parts.items()}
                rec["_weights"] = dict(weights, feedback=RECALL_FEEDBACK_WEIGHT)
                rec["_profile"] = profile["profile"]
                rec["_profile_reasons"] = profile["reasons"]
                rec["_frequency_source"] = (
                    "usage_feedback" if freq > 0 else "none"
                )
                rec["_access_count_observed"] = int(d.get("access_count") or 0)
                rec["_type_multiplier"] = mtype_mult
                rec["_semantic_available"] = sm is not None
                # S6-FIX-06：概念桥只读诊断。整批一份，每条结果回显同一份，
                # 与 `_weights` / `_profile` 的处理方式一致（它们也是每次查询
                # 一份）。不含任何查询原文，只说「命中了几条、是哪几条」。
                if lex_diag:
                    rec["_bridge"] = dict(lex_diag)
                rec["_activation"] = {
                    "amount": round(act, 4),
                    "contributions": spread.get(d["id"], {}).get("contributions", []),
                }
                if fb_info is not None:
                    rec["_feedback"] = {
                        "signal": fb_info["signal"],
                        "applied": fb_info["applied"],
                        "counts": fb_rows[d["id"]],
                        "reason": fb_info["reason"],
                    }
            scored.append(rec)
        # Stable sort: highest score first, then recency, then id.
        scored.sort(key=lambda x: (x["_score"], x.get("updated_at") or "", x["id"]),
                    reverse=True)
        result = scored[:top_k]
        # Optional relevance floor. DELIBERATELY OPT-IN (default None = off).
        #
        # S5-E measured whether a fixed score threshold can tell related from
        # unrelated queries on the 45-query set: it cannot. Related top1 scores
        # span 0.3707..0.7765 and unrelated top1 span 0.3583..0.4813 -- a real
        # preference query ("我是什么样的人设") scores 0.3707, BELOW three of the
        # four unrelated queries. Every threshold that suppressed the unrelated
        # ones also rejected 6..10 legitimate queries; the best joint rule still
        # rejected 1/41 related while keeping only 2/4 unrelated. Shipping any of
        # them as a default would silently drop good memories, so we ship the
        # mechanism and leave it disarmed rather than pick a number that looks
        # good on 4 negative samples.
        #
        # When a caller does arm it, the rejection is *explicit*: an empty list
        # plus a `_rejected` audit block, never a silently short list.
        if min_score is not None and result:
            kept = [r for r in result if r["_score"] >= min_score]
            if not kept:
                top = result[0]
                rejected = {
                    "reason": "below_min_score",
                    "min_score": min_score,
                    "best_score": top["_score"],
                    "best_id": top["id"],
                    "considered": len(result),
                    "message": (
                        "没有足够相关的记忆：最佳候选分数 %.4f 低于门槛 %.4f。"
                        % (top["_score"], min_score)
                    ),
                }
                if explain:
                    rejected["best_explain"] = {
                        k: v for k, v in top.items() if k.startswith("_")
                    }
                return [{"_rejected": rejected}] if explain else []
            result = kept
        if include_related:
            ids = [r["id"] for r in result]
            related = _related_subgraph(c, ids, max_depth=1)
            for r in result:
                r["_related"] = related.get(r["id"], [])
        # 记一次真实访问。只加 access_count / last_accessed_at（频次分量的
        # 原料），不改 confidence、不写反馈信号、不 rescore —— "曝光不能让
        # 记忆升格"这条规矩不受影响。
        #
        # 放在拿到 result 之后：记的是"真正返回给调用方的那些"，而不是
        # "参与打分的全部候选"。
        # `record_access=False` 是给评测用的：access_count 会反过来
        # 喂 frequency 分量，同一套题跑两遍第二遍就是在用上一遍刷
        # 出来的数字打分（实测 @1 6/8 → 4/8，差别只在跑过什么）。
        if result and record_access:
            try:
                from app import governance_service as _gov
                _gov.touch_access([r["id"] for r in result])
            except Exception:  # noqa: BLE001 —— 记账失败不该让召回本身失败
                pass
        return result

    if conn is not None:
        return _run(conn)
    with db.connect() as conn:
        return _run(conn)


# --------------------------------------------------------------------------
# Relation graph
# --------------------------------------------------------------------------
def _related_subgraph(conn, seed_ids: list[int], max_depth: int = 1) -> dict[int, list[dict]]:
    """BFS over memory_links up to max_depth (cycle-safe via visited set)."""
    out: dict[int, list[dict]] = {sid: [] for sid in seed_ids}
    visited = set(seed_ids)
    frontier = list(seed_ids)
    depth = 0
    while frontier and depth < max_depth:
        nxt = []
        for mid in frontier:
            links = conn.execute(
                "SELECT * FROM memory_links WHERE source_memory_id=? OR target_memory_id=? ORDER BY id",
                (mid, mid),
            ).fetchall()
            for lk in links:
                l = dict(lk)
                other = l["target_memory_id"] if l["source_memory_id"] == mid else l["source_memory_id"]
                direction = "out" if l["source_memory_id"] == mid else "in"
                node = {"memory_id": other, "relation": l["relation"],
                        "direction": direction, "note": l["note"], "depth": depth + 1}
                if other not in visited:
                    visited.add(other)
                    nxt.append(other)
                out.setdefault(mid, []).append(node)
        frontier = nxt
        depth += 1
    return out


def get_related_memories(memory_id: int, max_depth: int = 2, relation: str = "",
                         max_nodes: int = 30) -> dict:
    """Return the relation subgraph around a memory (depth/cycle limited)."""
    max_depth = max(1, min(int(max_depth), 3))
    max_nodes = max(1, min(int(max_nodes), 200))
    with db.connect() as conn:
        row = conn.execute("SELECT id FROM memories WHERE id=?", (memory_id,)).fetchone()
        if not row:
            return {"ok": False, "message": "memory not found"}
        visited = {memory_id}
        frontier = [memory_id]
        nodes: list[dict] = []
        edges: list[dict] = []
        depth = 0
        while frontier and depth < max_depth and len(nodes) < max_nodes:
            nxt = []
            for mid in frontier:
                links = conn.execute(
                    "SELECT * FROM memory_links WHERE source_memory_id=? OR target_memory_id=? ORDER BY id",
                    (mid, mid),
                ).fetchall()
                for lk in links:
                    l = dict(lk)
                    if relation and l["relation"] != relation:
                        continue
                    other = l["target_memory_id"] if l["source_memory_id"] == mid else l["source_memory_id"]
                    direction = "out" if l["source_memory_id"] == mid else "in"
                    edges.append({"from": mid, "to": other, "relation": l["relation"],
                                  "direction": direction, "note": l["note"]})
                    if other not in visited:
                        visited.add(other)
                        nxt.append(other)
                        if len(nodes) < max_nodes:
                            nr = conn.execute(
                                "SELECT id, content, category, memory_status, status FROM memories WHERE id=?",
                                (other,)).fetchone()
                            nodes.append(dict(nr) if nr else {"id": other})
            frontier = nxt
            depth += 1
        return {"ok": True, "memory_id": memory_id, "depth": max_depth,
                "nodes": nodes, "edges": edges, "node_count": len(nodes)}


def create_memory_relation(source_memory_id: int, target_memory_id: int,
                           relation: str = "related_to", note: str = "",
                           approver: str = "user") -> dict:
    """Create a typed relation with dedup + cycle prevention for directed edges."""
    relation = (relation or "related_to").strip()
    if relation not in ALLOWED_RELATIONS:
        return {"ok": False, "message": f"unknown relation '{relation}'; allowed: {ALLOWED_RELATIONS}"}
    if source_memory_id == target_memory_id:
        return {"ok": False, "message": "self-loop relations are not allowed"}
    with db.transaction() as conn:
        s = conn.execute("SELECT id FROM memories WHERE id=?", (source_memory_id,)).fetchone()
        t = conn.execute("SELECT id FROM memories WHERE id=?", (target_memory_id,)).fetchone()
        if not s or not t:
            return {"ok": False, "message": "source or target memory not found"}
        dup = conn.execute(
            "SELECT id FROM memory_links WHERE source_memory_id=? AND target_memory_id=? AND relation=?",
            (source_memory_id, target_memory_id, relation)).fetchone()
        if dup:
            return {"ok": True, "message": "relation already exists", "link_id": dup["id"],
                    "already_existed": True}
        if relation in DIRECTED_RELATIONS:
            # Prevent creating a cycle: target must not already reach source.
            reach = _reachable(conn, target_memory_id, relation, {source_memory_id})
            if source_memory_id in reach:
                return {"ok": False, "message": f"would create a cycle for directed relation '{relation}'"}
        cur = conn.execute(
            "INSERT INTO memory_links(source_memory_id, target_memory_id, relation, note, created_at) "
            "VALUES(?,?,?,?,?)",
            (source_memory_id, target_memory_id, relation, note or "", _now()))
        link_id = cur.lastrowid
        conn.execute(
            "INSERT INTO events(task_id, event_type, detail) VALUES(?,?,?)",
            (None, "memory.relation.created",
             json.dumps({"source": source_memory_id, "target": target_memory_id,
                         "relation": relation, "by": approver}, ensure_ascii=False)))
    return {"ok": True, "link_id": link_id, "already_existed": False}


def _reachable(conn, start: int, relation: str, stop: set[int]) -> set[int]:
    seen = set()
    frontier = [start]
    while frontier:
        nxt = []
        for mid in frontier:
            links = conn.execute(
                "SELECT target_memory_id FROM memory_links WHERE source_memory_id=? AND relation=?",
                (mid, relation)).fetchall()
            for r in links:
                o = r["target_memory_id"]
                if o in stop:
                    seen.add(o)
                    continue
                if o not in seen:
                    seen.add(o)
                    nxt.append(o)
        frontier = nxt
    return seen


def list_memory_relations(memory_id: int) -> dict:
    with db.connect() as conn:
        row = conn.execute("SELECT id FROM memories WHERE id=?", (memory_id,)).fetchone()
        if not row:
            return {"ok": False, "message": "memory not found"}
        links = conn.execute(
            "SELECT * FROM memory_links WHERE source_memory_id=? OR target_memory_id=? ORDER BY id",
            (memory_id, memory_id)).fetchall()
        out = []
        for lk in links:
            l = dict(lk)
            l["direction"] = "out" if l["source_memory_id"] == memory_id else "in"
            out.append(l)
        return {"ok": True, "memory_id": memory_id, "relations": out, "count": len(out)}


# --------------------------------------------------------------------------
# Memory detail view (governance + feedback + relations + lifecycle)
# --------------------------------------------------------------------------
def get_memory_details(memory_id: int) -> dict:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
        if not row:
            return {"ok": False, "message": "memory not found"}
        d = dict(row)
        gov_status = gov.get_governance_status(memory_id)
        fb = conn.execute("SELECT * FROM memory_feedback WHERE memory_id=?", (memory_id,)).fetchone()
        rel = list_memory_relations(memory_id)
        from app import mcp_server as _mcp  # local import to avoid cycle at module load
        base = _mcp._memory_out(d)
        detail = {
            "ok": True,
            "memory": base,
            "lifecycle_state": lifecycle_state_of(d),
            "allowed_transitions": lifecycle_transitions(d),
            "governance": gov_status,
            "feedback": dict(fb) if fb else None,
            "relations": rel.get("relations", []),
        }
        return detail


# --------------------------------------------------------------------------
# Usage feedback (traceable, no score inflation)
# --------------------------------------------------------------------------
FEEDBACK_TYPES = ("retrieved", "used", "helpful", "not_relevant", "corrected")


def record_memory_feedback(memory_id: int, feedback_type: str, note: str = "",
                           approver: str = "user") -> dict:
    if feedback_type not in FEEDBACK_TYPES:
        return {"ok": False, "message": f"invalid feedback_type '{feedback_type}'; allowed: {FEEDBACK_TYPES}"}
    col = f"{feedback_type}_count"
    # Only last_retrieved_at / last_used_at are stored as dedicated columns
    # (the rest share last_feedback_at); per-type last_* history is in history_json.
    last_col = "last_retrieved_at" if feedback_type == "retrieved" else (
        "last_used_at" if feedback_type == "used" else None)
    now = _now()
    with db.transaction() as conn:
        row = conn.execute("SELECT id FROM memories WHERE id=?", (memory_id,)).fetchone()
        if not row:
            return {"ok": False, "message": "memory not found"}
        if last_col:
            conn.execute(
                f"INSERT INTO memory_feedback(memory_id, {col}, {last_col}, last_feedback_at, history_json) "
                "VALUES(?,1,?,?,?) "
                "ON CONFLICT(memory_id) DO UPDATE SET "
                f"{col}={col}+1, {last_col}=excluded.{last_col}, "
                "last_feedback_at=excluded.last_feedback_at, "
                "history_json=excluded.history_json",
                (memory_id, now, now, json.dumps([{"t": feedback_type, "at": now}], ensure_ascii=False)))
        else:
            conn.execute(
                f"INSERT INTO memory_feedback(memory_id, {col}, last_feedback_at, history_json) "
                "VALUES(?,1,?,?) "
                "ON CONFLICT(memory_id) DO UPDATE SET "
                f"{col}={col}+1, last_feedback_at=excluded.last_feedback_at, "
                "history_json=excluded.history_json",
                (memory_id, now, json.dumps([{"t": feedback_type, "at": now}], ensure_ascii=False)))
        # Cap history to last 50 entries to avoid unbounded growth.
        fb = conn.execute("SELECT history_json FROM memory_feedback WHERE memory_id=?", (memory_id,)).fetchone()
        hist = _json_loads(fb["history_json"], []) if fb else []
        if len(hist) > 50:
            hist = hist[-50:]
            conn.execute("UPDATE memory_feedback SET history_json=? WHERE memory_id=?",
                         (json.dumps(hist, ensure_ascii=False), memory_id))
        conn.execute(
            "INSERT INTO events(task_id, event_type, detail) VALUES(?,?,?)",
            (None, "memory.feedback",
             json.dumps({"memory_id": memory_id, "type": feedback_type, "by": approver}, ensure_ascii=False)))
    return {"ok": True, "memory_id": memory_id, "feedback_type": feedback_type}


def get_memory_usage_stats(memory_id: int = 0) -> dict:
    with db.connect() as conn:
        if memory_id:
            fb = conn.execute("SELECT * FROM memory_feedback WHERE memory_id=?", (memory_id,)).fetchone()
            return {"ok": True, "memory_id": memory_id, "stats": dict(fb) if fb else {}}
        rows = conn.execute(
            "SELECT m.id, m.content, m.category, f.retrieved_count, f.used_count, "
            "f.helpful_count, f.not_relevant_count, f.corrected_count "
            "FROM memories m LEFT JOIN memory_feedback f ON f.memory_id=m.id "
            "WHERE m.status='active' ORDER BY (f.used_count+f.helpful_count) DESC, m.id"
        ).fetchall()
        return {"ok": True, "count": len(rows),
                "usage": [dict(r) for r in rows]}


# --------------------------------------------------------------------------
# Duplicate / near-duplicate discovery (reuses Phase 5 conflict detection)
# --------------------------------------------------------------------------
def find_duplicate_memories(memory_ids: Optional[list[int]] = None, category: str = "",
                           candidate_count: int = 8) -> dict:
    det = gov.detect_conflicts(memory_ids=memory_ids, category=category,
                                    candidate_count=candidate_count, persist=False)
    findings = det.get("findings", []) if isinstance(det, dict) else det
    dups = [f for f in findings if isinstance(f, dict)
            and f.get("conflict_type") in ("duplicate", "correction")
            and not f.get("prior_resolution")]
    return {"ok": True, "duplicate_count": len(dups), "duplicates": dups}


# --------------------------------------------------------------------------
# Consolidation candidates (generate only; never applied silently)
# --------------------------------------------------------------------------
CONSOL_KINDS = (
    "merge_duplicate", "extract_semantic", "extract_procedural",
    "stale_fact", "missing_source", "conflict_review", "archive_review",
)


def consolidate_memory_candidates(persist: bool = False, limit: int = 50,
                                  category: str = "", memory_type: str = "",
                                  approver: str = "user") -> dict:
    """Scan active memories and emit consolidation CANDIDATES only.

    persist=False  -> preview, no database write (safe default).
    persist=True   -> insert rows into consolidation_candidates (status 'pending').
    Nothing here mutates an existing memory; applying a candidate is a separate,
    approved, audited, reversible step (run_memory_consolidation).
    """
    limit = max(1, min(int(limit), 500))
    rule = rules.load_rules()
    with db.connect() as conn:
        sql = ("SELECT * FROM memories WHERE status='active' "
               "AND memory_status NOT IN ('pending_review', 'superseded')")
        args: list = []
        if category.strip():
            sql += " AND category=?"
            args.append(category.strip()[:100])
        if memory_type.strip() and memory_type.strip() != "unknown":
            sql += " AND memory_type=?"
            args.append(memory_type.strip())
        sql += " ORDER BY id"
        rows = [dict(r) for r in conn.execute(sql, args).fetchall()]

        candidates: list[dict] = []
        # 1) duplicates from existing conflict findings
        det = gov.detect_conflicts(category=category, candidate_count=rule.get("candidate_count", 8),
                                   persist=False)
        findings = det.get("findings", []) if isinstance(det, dict) else det
        # A single protection verdict per row, reused by every heuristic below.
        # Recomputed here (rather than trusting the stored `protected` flag)
        # because the tiers are derived from scores, which move over time.
        def _tier(d):
            return protection.assess(d, {"importance": float(d.get("importance_score") or 0.0)})

        for f in findings:
            # A pair a human already judged is settled data, not a fresh lead:
            # re-emitting it every scan is how a triaged queue silently turns
            # back into an untriaged one.
            if f.get("prior_resolution"):
                continue
            if f.get("conflict_type") in ("duplicate", "correction"):
                pair_ids = [f.get("memory_id"), f.get("candidate_memory_id")]
                if any(_tier(next((r for r in rows if r["id"] == pid), {}))["blocks"]["auto_merge"]
                       for pid in pair_ids if pid is not None):
                    candidates.append({
                        "kind": "merge_needs_review",
                        "title": f"合并需人工确认 {pair_ids[0]} / {pair_ids[1]}",
                        "detail": (f.get("reason", "") +
                                   "（涉及受保护/高分记忆，自动合并会绕过保护，改为人工裁决）"),
                        "source_memory_ids": pair_ids,
                        "confidence": f.get("confidence", 0.0),
                        "rule_version": f.get("rule_version", rule["rule_version"]),
                    })
                    continue
                candidates.append({
                    "kind": "merge_duplicate",
                    "title": f"合并重复记忆 {f.get('memory_id')} / {f.get('candidate_memory_id')}",
                    "detail": f.get("reason", ""),
                    "source_memory_ids": pair_ids,
                    "confidence": f.get("confidence", 0.0),
                    "rule_version": f.get("rule_version", rule["rule_version"]),
                })
            elif f.get("conflict_type") in ("possible_conflict", "undetermined"):
                candidates.append({
                    "kind": "conflict_review",
                    "title": f"冲突待裁决 {f.get('memory_id')} / {f.get('candidate_memory_id')}",
                    "detail": f.get("reason", ""),
                    "source_memory_ids": [f.get("memory_id"), f.get("candidate_memory_id")],
                    "confidence": f.get("confidence", 0.0),
                    "rule_version": f.get("rule_version", rule["rule_version"]),
                })
        # 2) per-memory heuristics
        for d in rows:
            mid = d["id"]
            mtype = d.get("memory_type") or "unknown"
            imp = float(d.get("importance_score") or 0.0)
            fr = float(d.get("freshness_score") or 0.0)
            acc = int(d.get("access_count") or 0)
            src = (d.get("source") or "").lower()
            protected = int(d.get("protected") or 0)
            tier = _tier(d)
            tier_blocks = tier["blocks"]
            # archive review (never for protected / preference / protected tiers)
            is_pref = mtype == "preference"
            if (not protected and not is_pref and not tier_blocks["auto_archive"]
                    and imp <= rule["archive_importance_threshold"]
                    and fr <= rule["archive_freshness_threshold"]):
                candidates.append({
                    "kind": "archive_review",
                    "title": f"归档候选 {mid}",
                    "detail": f"importance={imp} freshness={fr} tier={tier['tier']}",
                    "source_memory_ids": [mid],
                    "confidence": 0.5,
                    "rule_version": rule["rule_version"],
                })
            # stale fact (low freshness, low access, not protected, not a
            # protected tier — a critical memory that merely went stale is a
            # reason to revisit it, not a reason to call the fact wrong)
            if (not protected and not tier_blocks["auto_archive"]
                    and fr <= rule["archive_freshness_threshold"] and acc == 0):
                candidates.append({
                    "kind": "stale_fact",
                    "title": f"可能过时事实 {mid}",
                    "detail": f"freshness={fr}, access_count={acc}",
                    "source_memory_ids": [mid],
                    "confidence": 0.4,
                    "rule_version": rule["rule_version"],
                })
            # missing source
            if src in ("inferred", "guess", ""):
                candidates.append({
                    "kind": "missing_source",
                    "title": f"缺来源记忆 {mid}",
                    "detail": f"source='{src or 'empty'}'",
                    "source_memory_ids": [mid],
                    "confidence": 0.3,
                    "rule_version": rule["rule_version"],
                })
            # extract procedural from how-to content
            if mtype == "episodic" and re.search(r"\b(how to|step|流程|步骤|命令|command)\b", d.get("content", ""), re.I):
                candidates.append({
                    "kind": "extract_procedural",
                    "title": f"可从情景提取程序性经验 {mid}",
                    "detail": d.get("content", "")[:120],
                    "source_memory_ids": [mid],
                    "confidence": 0.45,
                    "rule_version": rule["rule_version"],
                })
        candidates = candidates[:limit]
        if persist:
            with db.transaction() as tconn:
                for c in candidates:
                    tconn.execute(
                        "INSERT INTO consolidation_candidates(kind,status,title,detail,source_memory_ids,"
                        "confidence,rule_version,created_at) VALUES(?,?,?,?,?,?,?,?)",
                        (c["kind"], "pending", c["title"], c["detail"],
                         json.dumps(c["source_memory_ids"], ensure_ascii=False),
                         c["confidence"], c["rule_version"], _now()))
                tconn.execute(
                    "INSERT INTO events(task_id,event_type,detail) VALUES(?,?,?)",
                    (None, "consolidation.candidates",
                     json.dumps({"count": len(candidates), "by": approver}, ensure_ascii=False)))
            return {"ok": True, "persisted": True, "candidate_count": len(candidates),
                    "candidates": candidates}
        return {"ok": True, "persisted": False, "candidate_count": len(candidates),
                "candidates": candidates}


def run_memory_consolidation(candidate_ids: Optional[list[int]] = None, dry_run: bool = False,
                             approver: str = "user", rule_version: str = "") -> dict:
    """Apply approved consolidation candidates.

    Each action is wrapped in its own transaction and recorded in
    governance_actions so it is auditable and reversible. Failure of one
    candidate never leaves a half-written transformation for the others.
    """
    if candidate_ids is not None and not isinstance(candidate_ids, (list, tuple)):
        return {"ok": False, "message": "candidate_ids must be a list of ints"}
    # Fetch pending candidates first (read-only connection).
    with db.connect() as rconn:
        sql = "SELECT * FROM consolidation_candidates WHERE status='pending'"
        args: list = []
        if candidate_ids:
            placeholders = ",".join("?" * len(candidate_ids))
            sql += f" AND id IN ({placeholders})"
            args.extend(int(x) for x in candidate_ids)
        sql += " ORDER BY id"
        cands = [dict(c) for c in rconn.execute(sql, args).fetchall()]
    if dry_run:
        return {"ok": True, "dry_run": True, "planned": len(cands), "candidates": cands}
    results = []
    for c in cands:
        # Each candidate is its own transaction: gov archive functions open their
        # own transaction, so we must reuse a single connection here rather than
        # nesting. The candidate-status UPDATE shares that transaction.
        try:
            with db.transaction() as conn:
                res = _apply_candidate(conn, c, approver, rule_version or c["rule_version"])
                if res.get("ok"):
                    conn.execute("UPDATE consolidation_candidates SET status='applied', resolved_at=?, "
                                 "resolved_by=?, notes_json=? WHERE id=?",
                                 (_now(), approver, json.dumps(res, ensure_ascii=False), c["id"]))
                else:
                    conn.execute("UPDATE consolidation_candidates SET status='skipped', resolved_at=?, "
                                 "resolved_by=?, notes_json=? WHERE id=?",
                                 (_now(), approver, json.dumps(res, ensure_ascii=False), c["id"]))
                results.append(res)
        except Exception as exc:  # one failure must not abort the batch
            try:
                with db.connect() as fconn:
                    fconn.execute("UPDATE consolidation_candidates SET status='skipped', resolved_at=?, "
                                  "resolved_by=?, notes_json=? WHERE id=?",
                                  (_now(), approver, json.dumps({"error": str(exc)}, ensure_ascii=False), c["id"]))
            except Exception:
                pass
            results.append({"ok": False, "candidate_id": c["id"], "error": str(exc)})
    return {"ok": True, "dry_run": False, "applied": len([r for r in results if r.get("ok")]),
            "skipped": len([r for r in results if not r.get("ok")]), "results": results}


def _apply_candidate(conn, cand: dict, approver: str, rule_version: str) -> dict:
    kind = cand["kind"]
    ids = _json_loads(cand["source_memory_ids"], [])
    if kind == "archive_review" and ids:
        mid = ids[0]
        before, err = gov._archive_memory(conn, mid, f"consolidation:{kind}",
                                          gov.RULE_VERSION, approver)
        if err:
            return {"ok": False, "candidate_id": cand["id"], "error": err}
        return {"ok": True, "candidate_id": cand["id"], "action": "archive", "memory_id": mid}
    if kind == "merge_duplicate" and len(ids) >= 2:
        # Keep the higher-importance one; archive the loser (reuse governance archive).
        rows = [dict(r) for r in conn.execute(
            "SELECT id, importance_score, access_count, updated_at FROM memories WHERE id IN (?,?)",
            (ids[0], ids[1])).fetchall()]
        if len(rows) < 2:
            return {"ok": False, "candidate_id": cand["id"], "error": "source memories missing"}
        loser = min(rows, key=lambda r: (float(r["importance_score"] or 0), float(r["access_count"] or 0)))
        before, err = gov._archive_memory(conn, loser["id"], f"consolidation:{kind}",
                                          gov.RULE_VERSION, approver)
        if err:
            return {"ok": False, "candidate_id": cand["id"], "error": err}
        return {"ok": True, "candidate_id": cand["id"], "action": "archive_duplicate",
                "archived_memory_id": loser["id"]}
    if kind in ("extract_procedural", "extract_semantic") and ids:
        # Create a NEW derived memory, linked via derived_from; never overwrite source.
        src = conn.execute("SELECT * FROM memories WHERE id=?", (ids[0],)).fetchone()
        if not src:
            return {"ok": False, "candidate_id": cand["id"], "error": "source memory missing"}
        s = dict(src)
        new_type = "procedural" if kind == "extract_procedural" else "semantic"
        content = f"[derived from #{s['id']}] {s['content']}"[:4000]
        cur = conn.execute(
            "INSERT INTO memories(content,category,source,confidence,status,memory_status,memory_type) "
            "VALUES(?,?,?,?,?,?,?)",
            (content, s.get("category", "general"), "consolidation-derived",
             float(s.get("confidence") or 0.7), "active", "active", new_type))
        new_id = cur.lastrowid
        conn.execute(
            "INSERT INTO memory_links(source_memory_id,target_memory_id,relation,note,created_at) "
            "VALUES(?,?,?,?,?)",
            (new_id, ids[0], "derived_from", f"consolidation:{kind}", _now()))
        conn.execute(
            "INSERT INTO events(task_id,event_type,detail) VALUES(?,?,?)",
            (None, "consolidation.applied",
             json.dumps({"kind": kind, "source": ids[0], "derived": new_id, "by": approver}, ensure_ascii=False)))
        return {"ok": True, "candidate_id": cand["id"], "action": "create_derived",
                "new_memory_id": new_id, "derived_from": ids[0]}
    # conservative default: do not auto-apply review-only candidates (conflict_review,
    # stale_fact, missing_source) without explicit human handling.
    return {"ok": False, "candidate_id": cand["id"],
            "error": f"kind '{kind}' requires explicit human review; not auto-applied"}


def list_consolidation_candidates(status: str = "", limit: int = 50) -> dict:
    with db.connect() as conn:
        sql = "SELECT * FROM consolidation_candidates"
        args: list = []
        if status.strip():
            sql += " WHERE status=?"
            args.append(status.strip())
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(int(limit))
        rows = conn.execute(sql, args).fetchall()
        return {"ok": True, "count": len(rows), "candidates": [dict(r) for r in rows]}
