"""Graduated protection tiers.

The `protected` flag is binary: a memory either never gets auto-touched or it
is fully exposed to automated maintenance. In practice the risk is not
uniform. Losing a stated user preference hurts far more than losing a stale
scratch note, yet both are "unprotected" today and both obey the same archive
threshold.

This module adds a second, *derived* axis. A memory's tier comes from its
score and category, not from a flag someone remembered to set, so it applies
to the whole library automatically — including memories written before anyone
thought about governance.

Tiers, and what each one blocks:

    critical  importance >= 80, or protected, or category identity/profile
              → never auto-archive, never auto-merge, never bulk-write
    high      importance >= 60
              → never auto-archive/merge silently; bulk writes need approval
    normal    importance >= 30
              → ordinary thresholds apply
    low       importance < 30
              → eligible for the existing low-score archive path (still gated
                by freshness + observation period)

Design decision: tiers only ever *restrict*. They never promote a memory into
an automated action that the existing thresholds would have refused. A derived
safety axis that can also trigger destructive work is not a safety axis.
"""

from app.governance_rules import load_rules

TIER_CRITICAL = "critical"
TIER_HIGH = "high"
TIER_NORMAL = "normal"
TIER_LOW = "low"

TIER_ORDER = {TIER_LOW: 0, TIER_NORMAL: 1, TIER_HIGH: 2, TIER_CRITICAL: 3}

# Thresholds are deliberately the numbers already used by the archive path, so
# there is one mental model rather than two competing sets of cut-offs.
CRITICAL_IMPORTANCE = 80.0
HIGH_IMPORTANCE = 60.0
NORMAL_IMPORTANCE = 30.0

# Categories that describe who the user is. Losing one of these is not a lost
# row, it is the assistant forgetting the person.
CRITICAL_CATEGORIES = {"identity", "profile", "preference", "fact"}
# Categories whose whole purpose is to be ephemeral.
EPHEMERAL_CATEGORIES = {"temp", "temporary", "session", "cache", "todo", "status"}


def assess(memory: dict, scores: dict | None = None, rules: dict | None = None) -> dict:
    """Return {tier, reasons, blocks} for one memory.

    `blocks` is the machine-readable part: which automated actions this tier
    forbids. Callers must check it rather than comparing tier strings, so that
    adding a tier later does not silently widen permissions.
    """
    rules = rules or load_rules()
    scores = scores or {}
    reasons: list[str] = []

    importance = float(scores.get("importance", memory.get("importance_score") or 50.0)
                       or 50.0)
    confidence = float(scores.get("credibility",
                                  memory.get("confidence_score") or 70.0) or 70.0)
    category = str(memory.get("category") or "").strip().lower()
    protected = memory.get("protected") in (1, True, "1", "true")
    status = str(memory.get("status") or "active")
    superseded = bool(memory.get("supersedes_id")) or status == "superseded"

    tier = TIER_NORMAL
    if protected:
        tier = TIER_CRITICAL
        reasons.append("记忆被显式标记 protected=1")
    elif category in CRITICAL_CATEGORIES:
        tier = TIER_CRITICAL
        reasons.append(f"类别 {category} 属于身份/偏好类，丢失代价高")
    elif importance >= CRITICAL_IMPORTANCE:
        tier = TIER_CRITICAL
        reasons.append(f"重要性 {importance:.1f} ≥ {CRITICAL_IMPORTANCE:.0f}")
    elif category in EPHEMERAL_CATEGORIES:
        # Checked *before* the high/normal score bands on purpose: a scratch
        # note that happens to have accumulated accesses is still a scratch
        # note. Being retrieved a lot is not the same as being worth keeping,
        # and letting score win here would re-create the exact problem the
        # tiers exist to solve.
        tier = TIER_LOW
        reasons.append(f"类别 {category} 本就是临时性内容")
    elif importance >= HIGH_IMPORTANCE:
        tier = TIER_HIGH
        reasons.append(f"重要性 {importance:.1f} ≥ {HIGH_IMPORTANCE:.0f}")
    elif importance >= NORMAL_IMPORTANCE:
        tier = TIER_NORMAL
        reasons.append(f"重要性 {importance:.1f} ≥ {NORMAL_IMPORTANCE:.0f}")
    else:
        tier = TIER_LOW
        reasons.append(f"重要性 {importance:.1f} < {NORMAL_IMPORTANCE:.0f}")

    # A high-confidence memory that is already superseded is still *history*:
    # it must not be silently deleted, but merging it into its successor is
    # exactly what it is for.
    if superseded and tier == TIER_CRITICAL and not protected \
            and category not in CRITICAL_CATEGORIES:
        reasons.append("已被新版本取代；保留作为历史，但允许合并进新版本")
        blocks_override = {"auto_merge": False}
    else:
        blocks_override = {}

    blocks = {
        "auto_archive": tier in (TIER_CRITICAL, TIER_HIGH),
        "auto_merge": tier in (TIER_CRITICAL, TIER_HIGH),
        "bulk_write": tier == TIER_CRITICAL,
    }
    blocks.update(blocks_override)

    if blocks["auto_archive"]:
        reasons.append(f"{tier} 档：禁止自动归档")
    if blocks["auto_merge"]:
        reasons.append(f"{tier} 档：禁止自动合并")
    if blocks["bulk_write"]:
        reasons.append("critical 档：批量写操作需人工确认")

    return {"tier": tier, "reasons": reasons, "blocks": blocks,
            "importance": round(importance, 2), "confidence": round(confidence, 2),
            "category": category, "protected": protected}


def allows(memory: dict, action: str, scores: dict | None = None,
           rules: dict | None = None) -> tuple[bool, list[str]]:
    """Whether `action` (auto_archive / auto_merge / bulk_write) is permitted."""
    verdict = assess(memory, scores, rules)
    blocked = bool(verdict["blocks"].get(action))
    return (not blocked), verdict["reasons"]


def annotate(rows: list[dict], action: str = "") -> list[dict]:
    """Attach `protection` to each row; optionally record whether `action` is
    allowed. Read-only — callers that only want to *show* the tier can skip
    passing `action`."""
    for row in rows:
        verdict = assess(row)
        if action:
            verdict["allowed"] = not verdict["blocks"].get(action)
        row["protection"] = verdict
    return rows


def summarize(rows: list[dict]) -> dict:
    counts = {TIER_CRITICAL: 0, TIER_HIGH: 0, TIER_NORMAL: 0, TIER_LOW: 0}
    for row in rows:
        tier = assess(row)["tier"]
        counts[tier] = counts.get(tier, 0) + 1
    return {
        "ok": True,
        "counts": counts,
        "total": len(rows),
        "thresholds": {"critical": CRITICAL_IMPORTANCE, "high": HIGH_IMPORTANCE,
                       "normal": NORMAL_IMPORTANCE},
        "critical_categories": sorted(CRITICAL_CATEGORIES),
        "ephemeral_categories": sorted(EPHEMERAL_CATEGORIES),
        "note": "分档只用于收紧自动操作，不会把记忆升级成自动动作。",
    }
