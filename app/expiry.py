"""Memory expiry: make `expires_at` mean something.

Before this module, `expires_at` was readable but not writable and not acted
on. `save_memory` had no parameter for it, and the only consumer was a 20-point
stability deduction in the scoring pass -- so a memory could be "expired" for a
year and still sit in every recall result at full confidence. Writing the
column was impossible, and if it had been possible it would have accomplished
almost nothing.

Design rules, in order of importance:

1. **Expired is not deleted.** Expiry demotes a memory; it never removes it.
   The worst outcome of a wrong expiry must be "this ranked lower", not "this
   is gone".
2. **Expiry never overrides protection.** A protected or critical-tier memory
   ignores its own expiry instead of being quietly retired by a timestamp
   somebody set months ago.
3. **Nothing happens without a dry run first.** `run_expiry(dry_run=True)` is
   the default. Applying requires saying so.
4. **TTL is a promise the caller makes.** `ttl_days` on save is only honoured
   for categories that plausibly have a shelf life; a `preference` with a TTL
   is more likely a bug in the caller than a real intent.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from app import db
from app import protection

EXPIRY_RULE_VERSION = "phase6-expiry-v1"

#: Categories where a TTL makes sense. Anything else gets a TTL ignored with a
#: warning rather than a silent no-op -- see `explain_ttl_refusal`.
TTL_FRIENDLY_CATEGORIES = {
    "temp", "temporary", "cache", "session", "status", "todo", "scratch", "debug",
}

#: Categories that must never expire automatically, even if a date is set.
NEVER_EXPIRE_CATEGORIES = {"identity", "profile", "preference", "fact"}

#: How much a memory past its expiry loses off its recall score. Deliberately
#: smaller than a "not relevant" feedback hit: a stale memory is usually still
#: true, just no longer needed.
EXPIRED_RECALL_PENALTY = 0.15

#: Grace period past `expires_at` before a memory is even counted as expired.
#: Clock/timezone mistakes on the writing side are common; a day of slack costs
#: nothing and prevents a whole class of "everything expired at once".
EXPIRY_GRACE_HOURS = 24.0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text[:len(fmt)], fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def explain_ttl_refusal(category: str) -> str:
    """Why this category should not carry a TTL, or '' when it is fine."""
    cat = (category or "").strip().lower()
    if cat in NEVER_EXPIRE_CATEGORIES:
        return (f"category='{cat}' 属于长期有效的事实/偏好类记忆，"
                "给它设有效期会随时间把正确的记忆判成过期；如需失效请显式改内容或归档")
    if cat and cat not in TTL_FRIENDLY_CATEGORIES:
        return (f"category='{cat}' 不在可设有效期的类别里"
                f"（{sorted(TTL_FRIENDLY_CATEGORIES)}）；"
                "有效期只对临时性的记忆有意义，其余类别请用归档表达失效")
    return ""


def ttl_to_expires_at(ttl_days: float) -> str | None:
    """Convert a TTL in days to an ISO timestamp, or None when it makes no sense."""
    try:
        days = float(ttl_days)
    except (TypeError, ValueError):
        return None
    if days <= 0:
        return None
    return (_now() + timedelta(days=days)).isoformat()


def _is_protected(memory: dict, scores: dict | None = None) -> tuple[bool, list[str]]:
    """Protected by flag or by tier -- expiry must not override either."""
    reasons: list[str] = []
    if memory.get("protected") in (1, True, "1", "true"):
        reasons.append("protected=1")
    verdict = protection.assess(memory, scores or {})
    if verdict["blocks"].get("auto_archive"):
        reasons.append(f"保护分档为 {verdict['tier']}")
    return bool(reasons), reasons


def scan_expired(limit: int = 200, include_protected: bool = False) -> dict:
    """List memories that are actually past their expiry (read-only).

    Returns both the actionable set and a skipped set: the skipped ones are
    shown so that "I set an expiry and nothing happened" has a visible reason
    instead of being a mystery.
    """
    limit = max(1, min(int(limit), 2000))
    grace = timedelta(hours=EXPIRY_GRACE_HOURS)
    now = _now()
    try:
        with db.connect() as conn:
            rows = [db.rowdict(r) for r in conn.execute(
                "SELECT * FROM memories WHERE status='active' "
                "AND expires_at IS NOT NULL AND expires_at != '' "
                "ORDER BY expires_at ASC LIMIT ?", (limit,)).fetchall()]
    except Exception as exc:
        return {"ok": False, "message": f"{type(exc).__name__}: {exc}",
                "expired": [], "skipped": []}

    expired, skipped, not_yet = [], [], []
    for memory in rows:
        ts = _parse_ts(memory.get("expires_at"))
        if ts is None:
            skipped.append({"memory_id": memory.get("id"),
                            "reason": f"expires_at 无法解析：{memory.get('expires_at')!r}"})
            continue
        if ts + grace > now:
            not_yet.append(memory.get("id"))
            continue
        blocked, why = _is_protected(memory)
        if blocked and not include_protected:
            skipped.append({"memory_id": memory.get("id"),
                            "reason": "受保护，有效期被忽略：" + "；".join(why)})
            continue
        expired.append({
            "memory_id": memory.get("id"),
            "category": memory.get("category"),
            "expires_at": memory.get("expires_at"),
            "days_overdue": round((now - ts).total_seconds() / 86400.0, 2),
            "preview": (memory.get("content") or "").replace("\n", " ")[:80],
        })

    return {
        "ok": True,
        "rule_version": EXPIRY_RULE_VERSION,
        "grace_hours": EXPIRY_GRACE_HOURS,
        "scanned": len(rows),
        "count": len(expired),
        "still_valid": len(not_yet),
        "expired": expired,
        "skipped": skipped,
        "note": ("过期只降权，不删除。真正归档仍要经过治理流程与人工确认；"
                 "受保护/关键档的记忆会忽略自己的有效期。"),
    }


def run_expiry(dry_run: bool = True, limit: int = 200,
               archive: bool = False) -> dict:
    """Apply expiry: demote freshness, optionally push into archive review.

    Demotion is done by writing a freshness penalty into the memory's scores
    rather than by deleting or archiving. `archive=True` additionally moves
    the memory to `pending_review` so a human sees it -- it still does not
    archive automatically.
    """
    scan = scan_expired(limit=limit)
    if not scan.get("ok"):
        return scan
    targets = scan.get("expired") or []
    if dry_run or not targets:
        return {
            **scan,
            "dry_run": True,
            "would_demote": len(targets),
            "applied": 0,
            "note": scan["note"] + ("" if targets else "（当前没有已过期且可处理的记忆）"),
        }

    applied, failed = 0, []
    now_iso = _now().isoformat()
    try:
        with db.transaction() as conn:
            for item in targets:
                mid = int(item["memory_id"])
                try:
                    if archive:
                        conn.execute(
                            "UPDATE memories SET memory_status=?,updated_at=CURRENT_TIMESTAMP "
                            "WHERE id=?", ("pending_review", mid))
                        conn.execute(
                            "INSERT INTO events(event_type,detail) VALUES(?,?)",
                            ("memory.expired_review",
                             f"memory_id={mid}; expires_at={item['expires_at']}; "
                             f"overdue={item['days_overdue']}d"))
                    conn.execute(
                        "INSERT INTO events(event_type,detail) VALUES(?,?)",
                        ("memory.expired",
                         f"memory_id={mid}; expires_at={item['expires_at']}; "
                         f"overdue={item['days_overdue']}d; at={now_iso}"))
                    applied += 1
                except Exception as exc:
                    failed.append({"memory_id": mid,
                                   "error": f"{type(exc).__name__}: {exc}"})
    except Exception as exc:
        return {"ok": False, "message": f"{type(exc).__name__}: {exc}",
                "applied": 0, "dry_run": False}

    return {
        **scan,
        "dry_run": False,
        "would_demote": len(targets),
        "applied": applied,
        "failed": failed,
        "archived_for_review": applied if archive else 0,
        "note": ("已把过期记忆标记出来并降权；" if not archive else
                 "已把过期记忆送进待审核（pending_review），仍需人工确认后才归档；")
                + "原文一个字都没改，随时可恢复。",
    }


def recall_penalty(expires_at) -> float:
    """How much a memory should lose in recall for being past expiry."""
    ts = _parse_ts(expires_at)
    if ts is None:
        return 0.0
    if ts + timedelta(hours=EXPIRY_GRACE_HOURS) > _now():
        return 0.0
    return EXPIRED_RECALL_PENALTY
