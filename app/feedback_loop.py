"""Feedback loop: turn recorded feedback into a scoring signal.

The gap this closes
-------------------
`record_memory_feedback` wrote counters into `memory_feedback`, but nothing
ever read them back: `build_scoring_context` collected links / updates / tags
only, and `recall_memories` fused semantic + keyword + governance scores. So a
memory could be marked "helpful" or "corrected" a hundred times and still be
scored exactly as before. The feedback existed; it just never closed the loop.

The one design rule that matters
--------------------------------
**`retrieved` never adds score.** Exposure is not usefulness. If being recalled
raised a memory's score, the most-recalled memories would keep winning, get
recalled more, and entrench themselves -- a feedback loop that optimises for
being returned rather than being right. So:

  retrieved     recorded, contributes 0.0 to the signal (pure audit trail)
  used          small positive weight; the agent says it actually used it
  helpful       positive; the strongest endorsing signal available
  not_relevant  negative, but weak: a miss may be the query's fault
  corrected     negative and strong: the content was wrong, which is the most
                informative thing a memory can tell us about itself

Everything is a pure function of the feedback table, so scores stay recomputable
and explainable -- the same design rule as the rest of the governance layer.
No feedback is ever applied by mutating a stored score directly; scoring reads
the signal on demand.
"""
from __future__ import annotations

import json
import math

# A signal built from one single event is noise. Require this many *directional*
# observations (everything except `retrieved`) before the signal may shift a
# score at all. Below the threshold the memory is scored exactly as before.
MIN_DIRECTIONAL_SAMPLES = 2

# How hard the signal may pull a confidence score, as a fraction. Kept
# deliberately small: confidence feeds the credibility component (weight 0.20
# of importance) and the recall confidence term (weight 0.10), so a 0.25 swing
# at the signal level moves the final score by a few points, not by tens.
MAX_CONFIDENCE_SHIFT = 0.25

FEEDBACK_RULE_VERSION = "phase6-feedback-loop-v1"


def _counts(row) -> dict:
    """Normalise a memory_feedback row (sqlite3.Row or dict) into plain ints."""
    if row is None:
        return {k: 0 for k in
                ("retrieved", "used", "helpful", "not_relevant", "corrected")}
    d = dict(row)
    out = {}
    for key in ("retrieved", "used", "helpful", "not_relevant", "corrected"):
        try:
            out[key] = max(0, int(d.get(f"{key}_count") or 0))
        except (TypeError, ValueError):
            out[key] = 0
    return out


def compute_signal(counts: dict) -> dict:
    """Turn raw counters into a signed signal in [-1, 1] with its arithmetic.

    Exposed separately from the database so it can be unit-tested on numbers
    alone and so `explain` output can show exactly how a score was reached.
    """
    retrieved = counts.get("retrieved", 0)
    used = counts.get("used", 0)
    helpful = counts.get("helpful", 0)
    not_relevant = counts.get("not_relevant", 0)
    corrected = counts.get("corrected", 0)

    directional = used + helpful + not_relevant + corrected
    if directional < MIN_DIRECTIONAL_SAMPLES:
        return {
            "signal": 0.0,
            "applied": False,
            "samples": directional,
            "reason": (
                f"带方向的反馈只有 {directional} 条，少于 {MIN_DIRECTIONAL_SAMPLES} 条，"
                f"不足以调整评分（retrieved={retrieved} 只记录、不参与计分）"
            ),
            "components": {},
        }

    # Endorsement: helpful is the strongest positive, `used` counts for less
    # because "I used it" does not mean "it was right".
    helpful_ratio = helpful / max(1, helpful + not_relevant)
    used_boost = min(1.0, math.log1p(used) / math.log1p(10.0))

    # Pushback: `corrected` is the strong negative, `not_relevant` the weak one
    # (a miss can be the query's fault, not the memory's).
    corrected_ratio = corrected / (1.0 + corrected)
    not_rel_ratio = not_relevant / max(1, helpful + not_relevant)

    signal = (
        0.50 * helpful_ratio
        + 0.30 * used_boost
        - 0.50 * corrected_ratio
        - 0.30 * not_rel_ratio
    )
    signal = max(-1.0, min(1.0, signal))

    return {
        "signal": round(signal, 4),
        "applied": True,
        "samples": directional,
        "reason": _explain(signal, counts, helpful_ratio, used_boost,
                           corrected_ratio, not_rel_ratio),
        "components": {
            "helpful_ratio": round(helpful_ratio, 4),
            "used_boost": round(used_boost, 4),
            "corrected_ratio": round(corrected_ratio, 4),
            "not_relevant_ratio": round(not_rel_ratio, 4),
        },
    }


def _explain(signal, counts, helpful_ratio, used_boost,
             corrected_ratio, not_rel_ratio) -> str:
    parts = [
        f"helpful={counts['helpful']} → +{0.50 * helpful_ratio:.3f}",
        f"used={counts['used']} → +{0.30 * used_boost:.3f}",
        f"corrected={counts['corrected']} → -{0.50 * corrected_ratio:.3f}",
        f"not_relevant={counts['not_relevant']} → -{0.30 * not_rel_ratio:.3f}",
        f"retrieved={counts['retrieved']} → 不计分（曝光不等于有用）",
    ]
    return f"反馈信号 {signal:+.3f}：" + "，".join(parts)


def load_counts(conn, memory_id: int) -> dict:
    row = conn.execute(
        "SELECT * FROM memory_feedback WHERE memory_id=?", (int(memory_id),)
    ).fetchone()
    return _counts(row)


def feedback_for(conn, memory_id: int) -> dict:
    """Signal for one memory, ready to be merged into a scoring context."""
    return compute_signal(load_counts(conn, memory_id))


def apply_to_confidence(confidence_score: float, signal: float) -> float:
    """Pull a confidence score by at most MAX_CONFIDENCE_SHIFT of itself.

    Multiplicative (not additive) so a memory already at a low confidence is
    not pushed below zero, and a high-confidence memory is not capped out: the
    shift scales with how much confidence there is to shift.
    """
    if not signal:
        return confidence_score
    factor = 1.0 + MAX_CONFIDENCE_SHIFT * signal
    return max(0.0, min(100.0, float(confidence_score) * factor))


def recall_bonus(signal: float) -> float:
    """Small additive term for the recall fusion (0..1 scale).

    Kept tiny and symmetric around zero. Recall already has a `confidence`
    term derived from governance scoring, which itself absorbs this signal --
    this is the second, much weaker nudge so that a strongly-endorsed memory can
    win a near-tie at retrieval time without feedback dominating the ranking.
    """
    return 0.05 * max(-1.0, min(1.0, signal))


def record_retrieved(memory_ids: list[int]) -> dict:
    """Log that these memories were surfaced. Deliberately does NOT rescore.

    Called after a recall so the audit trail knows what was shown. Because
    `retrieved` contributes nothing to `compute_signal`, this can never
    inflate a memory's standing by being returned often.
    """
    from app import db as _db

    ids = [int(m) for m in (memory_ids or []) if str(m).lstrip("-").isdigit()]
    if not ids:
        return {"ok": True, "recorded": 0, "note": "没有需要记录的 id"}

    from datetime import datetime
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    recorded = 0
    with _db.transaction() as conn:
        for mid in ids:
            exists = conn.execute("SELECT id FROM memories WHERE id=?", (mid,)).fetchone()
            if not exists:
                continue
            conn.execute(
                "INSERT INTO memory_feedback(memory_id, retrieved_count, last_retrieved_at, "
                "last_feedback_at) VALUES(?,1,?,?) "
                "ON CONFLICT(memory_id) DO UPDATE SET "
                "retrieved_count=retrieved_count+1, last_retrieved_at=excluded.last_retrieved_at, "
                "last_feedback_at=excluded.last_feedback_at",
                (mid, now, now),
            )
            recorded += 1
    return {
        "ok": True,
        "recorded": recorded,
        "note": "retrieved 只记录曝光，不参与评分（曝光不等于有用）",
    }


def summarize_feedback(limit: int = 50) -> dict:
    """Which memories have the strongest positive/negative signals right now."""
    from app import db as _db

    limit = max(1, min(int(limit), 200))
    with _db.connect() as conn:
        rows = conn.execute(
            "SELECT m.id, m.content, m.category, f.* FROM memories m "
            "JOIN memory_feedback f ON f.memory_id = m.id "
            "WHERE m.status='active'"
        ).fetchall()

    scored = []
    for row in rows:
        d = dict(row)
        counts = _counts(d)
        info = compute_signal(counts)
        scored.append({
            "memory_id": d["id"],
            "content": (d.get("content") or "")[:120],
            "category": d.get("category"),
            "counts": counts,
            "signal": info["signal"],
            "applied": info["applied"],
            "reason": info["reason"],
        })

    scored.sort(key=lambda x: x["signal"], reverse=True)
    applied = [s for s in scored if s["applied"]]
    return {
        "ok": True,
        "rule_version": FEEDBACK_RULE_VERSION,
        "total_with_feedback": len(scored),
        "applied_count": len(applied),
        "min_directional_samples": MIN_DIRECTIONAL_SAMPLES,
        "max_confidence_shift": MAX_CONFIDENCE_SHIFT,
        "top_positive": [s for s in applied if s["signal"] > 0][:limit],
        "top_negative": [s for s in reversed(applied) if s["signal"] < 0][:limit],
        "note": "retrieved 只记录、不计分；至少 "
                f"{MIN_DIRECTIONAL_SAMPLES} 条带方向反馈才生效",
    }
