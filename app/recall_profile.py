"""Recall weighting by memory type and query shape -- as a *profile*, not a rewrite.

The design constraint that shapes everything here: recall must stay
reproducible and explainable. A ranker that silently re-weights itself per query
is impossible to audit and impossible to regression-test, so instead we ship a
small number of named, inspectable profiles. Every response carries the name of
the profile used and the exact weights, so a surprising ranking can always be
traced to "which profile" rather than "the model felt like it".

Second constraint: a profile may only *shift* emphasis between components. It
never drops a component to zero and never pushes a component to dominance,
because both would let query phrasing alone exclude a memory -- the opposite of
what a recall system is for.
"""
import re

# The default weight table lives in a value-only module so this file never
# imports memory_lifecycle (OPT-02-D; that back-edge used to be a lazy import
# inside weights_for(), i.e. a real cycle papered over by deferral).
from app.recall_weights import RECALL_WEIGHTS

# Query-shape detection. These are deliberately cheap lexical checks rather
# than a classifier: a wrong guess only re-orders results (never filters them),
# so the cost of being wrong is low and the cost of an opaque classifier is high.
_SHAPE_PATTERNS = (
    ("procedural", re.compile(
        r"(怎么|如何|怎样|步骤|流程|命令|脚本|执行|配置|怎么弄|咋弄|"
        r"\bhow\b|\bsteps?\b|\bprocedure\b|\bcommand\b|\brun\b|\bsetup\b)", re.I)),
    ("temporal", re.compile(
        r"(上次|之前|上次那个|刚才|昨天|前天|那次|当时|后来|"
        r"\blast time\b|\bpreviously\b|\bwhen\b|\bback then\b)", re.I)),
    ("preference", re.compile(
        r"(喜欢|偏好|习惯|讨厌|想要|希望|要求|口味|"
        r"\bprefer\b|\blike\b|\bdislike\b|\bwant\b|\bwish\b)", re.I)),
    ("recall_overview", re.compile(
        r"(总结|概览|都有哪些|有哪些|全部|所有|回忆|梳理|"
        r"\bsummar(y|ize)\b|\boverview\b|\ball\b|\blist\b)", re.I)),
)

# Per-type weight profiles. Sums are normalised at query time, so these are
# relative emphasis, not absolute shares.
#
# The reasoning behind each shift:
# * preference memories ("user always wants Chinese docs") are identified by
#   content match, and their value does not decay -- so freshness matters much
#   less than whether they match at all.
# * procedural memories are kept because they worked before; that is their
#   whole point, so confidence and stability outrank recency.
# * episodic memories are the opposite: the value IS the occasion, so freshness
#   and activation carry them and importance carries less.
# * reflection memories are distilled conclusions; they should win on
#   importance/confidence, not on how often they were returned.
TYPE_PROFILES = {
    "preference": {"semantic": 0.423, "keyword": 0.202, "importance": 0.182,
                   "confidence": 0.101, "freshness": 0.020, "frequency": 0.040,
                   "activation": 0.032},
    "episodic": {"semantic": 0.24, "keyword": 0.18,
                 "importance": 0.12, "confidence": 0.16, "freshness": 0.19,
                 "frequency": 0.06, "activation": 0.05},
    "procedural": {"semantic": 0.30, "keyword": 0.34, "importance": 0.13,
                   "confidence": 0.08, "freshness": 0.02, "frequency": 0.07,
                   "activation": 0.06},
    "goal": {"semantic": 0.26, "keyword": 0.22, "importance": 0.20,
             "confidence": 0.14, "freshness": 0.09, "frequency": 0.05,
             "activation": 0.04},
    "reflection": {"semantic": 0.28, "keyword": 0.20, "importance": 0.18,
                   "confidence": 0.10, "freshness": 0.11, "frequency": 0.07,
                   "activation": 0.06},
}

# A query shaped like a "how do I" question prefers procedural knowledge even
# when the memory itself was never typed; and a "what did we do last time"
# query prefers episodic. Applied PER CANDIDATE via type_multiplier(), which is
# the only place memory-type names legally appear.
SHAPE_PREFERENCE = {
    "procedural": {"procedural": 1.25, "semantic": 1.0, "reflection": 0.9},
    "temporal": {"episodic": 1.30, "semantic": 1.0, "procedural": 0.9},
    "preference": {"preference": 1.30, "semantic": 1.0, "episodic": 0.9},
    # An overview question is where a distilled reflection is most useful and
    # where recency is least relevant.
    "recall_overview": {"reflection": 1.25, "semantic": 1.05, "episodic": 0.85},
}

# How a question's SHAPE shifts emphasis between scoring COMPONENTS. This is a
# different axis from SHAPE_PREFERENCE above and must stay that way: component
# names (freshness, frequency) and memory-type names (episodic, procedural) are
# not interchangeable, and conflating them silently produced a no-op.
#
# The reasoning per shift:
# * "how do I" -> what worked before matters more than when it was written, so
#   confidence up and freshness down.
# * "what happened last time" -> the occasion IS the answer, so freshness and
#   activation up.
# * "what do I prefer" -> a stable preference does not decay; content match and
#   importance carry it, recency is nearly irrelevant.
# * "summarize everything" -> broad coverage from widely-linked/distilled
#   memories; frequency and importance up, freshness down.
SHAPE_WEIGHT_SHIFT = {
    "procedural": {"confidence": 1.35, "frequency": 1.15, "freshness": 0.70},
    "temporal": {"freshness": 1.80, "activation": 1.30, "importance": 0.80},
    "preference": {"semantic": 1.15, "keyword": 1.15, "freshness": 0.45},
    "recall_overview": {"importance": 1.30, "frequency": 1.25, "freshness": 0.70},
}

# Bounds that keep any profile from silencing or hijacking a component.
MIN_WEIGHT = 0.02
MAX_WEIGHT = 0.50

PROFILE_RULE_VERSION = "phase6-recall-profile-v1"


def detect_shape(query: str) -> str:
    """Best-effort query shape; 'unknown' when nothing matches."""
    text = (query or "").strip()
    if not text:
        return "unknown"
    for name, pat in _SHAPE_PATTERNS:
        if pat.search(text):
            return name
    return "unknown"


def _normalise(weights: dict) -> dict:
    clamped = {k: max(MIN_WEIGHT, min(MAX_WEIGHT, float(v))) for k, v in weights.items()}
    total = sum(clamped.values())
    if total <= 0:
        return dict(clamped)
    return {k: round(v / total, 6) for k, v in clamped.items()}


def weights_for(memory_type: str = "", query: str = "",
                base: dict | None = None) -> dict:
    """Return {weights, profile, shape, reasons} for one query.

    `base` lets the caller pass its own default table; otherwise the shared
    table from `app.recall_weights` is used (no import back into the ranker).
    """
    base_weights = dict(base or RECALL_WEIGHTS)
    mtype = (memory_type or "").strip().lower()
    shape = detect_shape(query)
    reasons = []

    if shape != "unknown":
        reasons.append(f"query 形态判定为 {shape}")

    if mtype and mtype in TYPE_PROFILES:
        weights = _normalise(TYPE_PROFILES[mtype])
        reasons.append(f"调用方指定 memory_type={mtype}，采用该类型的权重画像")
        return {"weights": weights, "profile": f"type:{mtype}",
                "shape": shape, "reasons": reasons}

    # No explicit type: shift emphasis between COMPONENTS based on the shape of
    # the question. Uses SHAPE_WEIGHT_SHIFT (component names), never
    # SHAPE_PREFERENCE (memory-type names) -- those are a different axis and
    # mixing them makes this whole branch a silent no-op.
    shift = SHAPE_WEIGHT_SHIFT.get(shape, {})
    if shift:
        adjusted = {k: v * shift.get(k, 1.0) for k, v in base_weights.items()}
        weights = _normalise(adjusted)
        changed = [f"{k}×{shift[k]}" for k in shift if k in base_weights]
        reasons.append(f"按问句形态调整成分比重（{', '.join(changed)}，总和归一）")
        return {"weights": weights, "profile": f"shape:{shape}",
                "shape": shape, "reasons": reasons}
    # An explicit "unknown" already stops callers from filtering earlier (that
    # existing pre-filter is unchanged here). We just fall through to the
    # default table, because an explicit request for "unknown" is far more
    # likely "I don't know the type" than "I want only untyped memories".
    reasons.append("未命中任何类型/形态，使用默认权重")
    return {"weights": _normalise(base_weights), "profile": "default",
            "shape": shape, "reasons": reasons}


def type_multiplier(memory_type: str = "", shape: str = "unknown") -> float:
    """Soft prior a memory's own type gets under the current query shape.

    Kept as a multiplier rather than folded into the weights because it applies
    per-candidate (each memory has its own type), while the weights apply
    per-query.
    """
    if not memory_type or not shape:
        return 1.0
    return float(SHAPE_PREFERENCE.get(shape, {}).get(memory_type, 1.0))
