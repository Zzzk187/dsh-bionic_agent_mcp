"""Memory distillation: find the material, never invent the conclusion.

The gap this fills
------------------
`consolidate_memory_candidates` produces tidy-up work — merge this duplicate,
archive that stale row. None of it produces *new knowledge*. The thing actually
worth doing is turning several concrete experiences into one abstract statement
("three times the user rejected a busy layout" -> "user prefers restrained,
uncluttered UI"), with a `derived_from` edge back to the evidence.

Why this module stops at candidates
-----------------------------------
Rules can find the material with confidence: cluster by embedding, notice the
cluster got denser, notice a contradiction appeared. Rules cannot write the
conclusion. "User prefers minimalist design" is not recoverable by regex from
three sentences about specific layouts — any rule that claims to do it is just
copy-pasting one sentence and calling it a conclusion, which is worse than
nothing because it looks like a summary while duplicating a row.

So this module does the part that is actually trustworthy:
  1. find clusters of related experiences that deserve one abstraction,
  2. hand over the evidence (ids, texts, similarity, why now),
  3. accept a distilled text from a human/model and record it with provenance,
  4. notice when a distillation has been contradicted and lower its confidence.

The conclusion always comes from outside. That keeps the module honest and
keeps the whole project zero-external-dependency.
"""

import json
from datetime import datetime, timezone

from app import db
from app.embeddings import cosine_similarity
from app.memory_lifecycle import ALLOWED_RELATIONS

# A cluster needs at least this many distinct experiences before "several
# observations" is a fair description. Two is a coincidence, not a pattern.
MIN_CLUSTER_SIZE = 3
# Same threshold family as conflict detection, but a little looser: we are
# looking for "same theme", not "same statement".
DEFAULT_CLUSTER_SIMILARITY = 0.52
MAX_DISTILLATIONS_PER_SCAN = 20
DISTILL_RULE_VERSION = "phase6-distill-v1"
DERIVED_SOURCE = "distilled"


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _vectors(conn) -> dict[int, list[float]]:
    out: dict[int, list[float]] = {}
    for r in conn.execute("SELECT memory_id, vector_json FROM memory_embeddings").fetchall():
        try:
            out[int(r[0])] = json.loads(r[1])
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    return out


def _cluster(rows: list[dict], vectors: dict[int, list[float]],
             threshold: float) -> list[list[int]]:
    """Union-find over cosine similarity. Deliberately plain: a fancier
    clustering would change *which* evidence gets grouped, and that decision
    should be inspectable rather than an opaque optimisation."""
    ids = [r["id"] for r in rows if r["id"] in vectors]
    parent = {i: i for i in ids}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            try:
                if cosine_similarity(vectors[a], vectors[b]) >= threshold:
                    ra, rb = find(a), find(b)
                    if ra != rb:
                        parent[rb] = ra
            except Exception:
                continue
    groups: dict[int, list[int]] = {}
    for i in ids:
        groups.setdefault(find(i), []).append(i)
    return sorted(groups.values(), key=len, reverse=True)


def _already_distilled(conn) -> set[int]:
    """Ids already serving as evidence for some distillation.

    `derived_from` points new -> source, so the *evidence* is the target side.
    """
    covered: set[int] = set()
    try:
        rows = conn.execute(
            "SELECT target_memory_id FROM memory_links WHERE relation='derived_from'"
        ).fetchall()
    except Exception:
        return covered
    for (target,) in rows:
        try:
            covered.add(int(target))
        except (TypeError, ValueError):
            continue
    return covered


def find_distillation_candidates(limit: int = 20, category: str = "",
                                 similarity: float | None = None,
                                 include_covered: bool = False) -> dict:
    """Propose clusters of experiences worth one abstraction. Read-only."""
    limit = max(1, min(int(limit), MAX_DISTILLATIONS_PER_SCAN))
    threshold = float(similarity if similarity is not None else DEFAULT_CLUSTER_SIMILARITY)
    threshold = max(0.30, min(threshold, 0.95))
    try:
        with db.connect() as conn:
            sql = "SELECT * FROM memories WHERE status='active'"
            args: list = []
            if category.strip():
                sql += " AND category=?"
                args.append(category.strip())
            rows = [db.rowdict(r) for r in conn.execute(sql + " ORDER BY id", args).fetchall()]
            vectors = _vectors(conn)
            covered = _already_distilled(conn)
    except Exception as exc:
        return {"ok": False, "message": f"{type(exc).__name__}: {exc}", "candidates": []}

    groups = _cluster(rows, vectors, threshold)
    by_id = {r["id"]: r for r in rows}
    candidates = []
    skipped_covered = 0
    for group in groups:
        if len(group) < MIN_CLUSTER_SIZE:
            continue
        if not include_covered and covered and set(group) <= covered:
            skipped_covered += 1
            continue
        members = [by_id[i] for i in group if i in by_id]
        cats = sorted({(m.get("category") or "") for m in members})
        candidates.append({
            "kind": "distill_cluster",
            "title": f"可提炼：{len(members)} 条相关经历（{'/'.join(c for c in cats if c) or '混合'}）",
            "source_memory_ids": sorted(group),
            "member_preview": [
                {"id": m["id"], "category": m.get("category"),
                 "content": (m.get("content") or "").replace("\n", " ")[:90]}
                for m in members[:6]
            ],
            "size": len(members),
            "categories": [c for c in cats if c],
            "similarity_threshold": threshold,
            "why": (f"{len(members)} 条记忆两两语义相似度 ≥ {threshold:.2f}，"
                    "构成同一主题下的多次具体经历；适合概括成一条抽象结论"),
            "how_to_apply": ("把成员里的共同点写成一条抽象记忆，用 "
                             "apply_distillation(content=..., source_memory_ids=[...]) 提交；"
                             "源记忆不会被删除或改写"),
            "rule_version": DISTILL_RULE_VERSION,
        })

    # Existing distillations that have since been contradicted.
    drift = _drifted_distillations()
    return {
        "ok": True,
        "candidates": candidates[:limit],
        "count": min(len(candidates), limit),
        "total_clusters": len(groups),
        "min_cluster_size": MIN_CLUSTER_SIZE,
        "similarity_threshold": threshold,
        "skipped_already_covered": skipped_covered,
        "drifted_distillations": drift,
        "rule_version": DISTILL_RULE_VERSION,
        "note": ("这里只提出「哪些经历值得提炼」和证据，不生成结论文字——"
                 "规则写不出抽象结论，硬编只会复制原句。"),
    }


def _distilled_rows(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT id, content, category, confidence_score, importance_score, memory_status "
        "FROM memories WHERE source LIKE ? AND status='active'", (DERIVED_SOURCE + "%",)
    ).fetchall()
    return [db.rowdict(r) for r in rows]


def _drifted_distillations() -> list[dict]:
    """Distillations that were later contradicted by a `contradicts` edge.

    Direction matters and cannot be assumed. `derived_from` always points
    new -> evidence, but `contradicts` can be written either way round by
    whoever recorded it ("the new observation contradicts the old rule" or
    "the old rule is contradicted by this"). So both sides are inspected and
    whichever one is a distillation is reported as the drifted one — a
    one-sided JOIN silently misses half the contradictions, which is exactly
    the kind of quiet no-op that makes people stop trusting the report.
    """
    out: list[dict] = []
    try:
        with db.connect() as conn:
            rows = conn.execute(
                "SELECT s.id, t.id, s.content, s.source, t.content, t.source "
                "FROM memory_links l "
                "JOIN memories s ON s.id = l.source_memory_id "
                "JOIN memories t ON t.id = l.target_memory_id "
                "WHERE l.relation='contradicts' "
                "  AND s.status='active' AND t.status='active'"
            ).fetchall()
    except Exception:
        return []
    prefix = DERIVED_SOURCE + ":"
    for src_id, tgt_id, src_text, src_source, tgt_text, tgt_source in rows:
        src_is = str(src_source or "").startswith(prefix)
        tgt_is = str(tgt_source or "").startswith(prefix)
        if src_is and not tgt_is:
            distilled_id, distilled_text = int(src_id), src_text
            by_id, by_text = int(tgt_id), tgt_text
        elif tgt_is and not src_is:
            distilled_id, distilled_text = int(tgt_id), tgt_text
            by_id, by_text = int(src_id), src_text
        else:
            # Neither side is a distillation (or both are, which would mean a
            # distillation contradicting another one — not our call to judge).
            continue
        out.append({
            "distilled_memory_id": distilled_id,
            "distilled": (distilled_text or "")[:90],
            "contradicted_by_id": by_id,
            "contradicted_by": (by_text or "")[:90],
            "hint": "该提炼已被后续记忆反驳，建议复核或降低其可信度",
        })
    return out


def apply_distillation(content: str, source_memory_ids: list[int], category: str = "",
                       confidence: float = 0.7, approver: str = "user",
                       note: str = "") -> dict:
    """Record one distilled statement, linked to its evidence.

    Refuses to run without evidence: a "conclusion" with no source memories is
    just a new assertion, and asserting is what save_memory already does.
    """
    text = (content or "").strip()
    if not text:
        return {"ok": False, "message": "content must not be empty"}
    if len(text) > 4000:
        return {"ok": False, "message": "content too long (max 4000 chars)"}
    try:
        ids = sorted({int(i) for i in (source_memory_ids or [])})
    except (TypeError, ValueError):
        return {"ok": False, "message": "source_memory_ids must be a list of integers"}
    if len(ids) < MIN_CLUSTER_SIZE:
        return {"ok": False,
                "message": (f"至少需要 {MIN_CLUSTER_SIZE} 条来源记忆才构成一次提炼；"
                            f"少于这个数就是断言新事实，请用 save_memory")}
    try:
        with db.connect() as conn:
            placeholders = ",".join("?" * len(ids))
            found = {int(r[0]) for r in conn.execute(
                f"SELECT id FROM memories WHERE id IN ({placeholders})", ids).fetchall()}
    except Exception as exc:
        return {"ok": False, "message": f"{type(exc).__name__}: {exc}"}
    missing = [i for i in ids if i not in found]
    if missing:
        return {"ok": False, "message": f"这些来源记忆不存在：{missing}"}

    conf = max(0.0, min(float(confidence), 1.0))
    cat = (category or "").strip() or "reflection"
    try:
        with db.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO memories(content,category,source,confidence,status,"
                "memory_status,memory_type,created_at,updated_at) "
                "VALUES(?,?,?,?,'active','active','reflection',?,?)",
                (text, cat, f"{DERIVED_SOURCE}:{'/'.join(str(i) for i in ids)[:80]}",
                 conf, _now(), _now()))
            new_id = cur.lastrowid
            made = []
            for src_id in ids:
                got = conn.execute(
                    "INSERT INTO memory_links(source_memory_id,target_memory_id,relation,note,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (new_id, src_id, "derived_from", f"distill:{note[:60]}", _now()))
                if got.lastrowid:
                    made.append(src_id)
            conn.execute(
                "INSERT INTO events(task_id,event_type,detail) VALUES(NULL,?,?)",
                ("memory.distilled",
                 f"new_id={new_id}; sources={ids}; approver={approver[:50]}"))
        return {"ok": True, "memory_id": new_id, "sources": made,
                "relation": "derived_from", "category": cat, "confidence": conf,
                "message": "已记录提炼结果并挂上 derived_from 关系；源记忆保持原样"}
    except Exception as exc:
        return {"ok": False, "message": f"{type(exc).__name__}: {exc}"}


def list_distillations(limit: int = 50) -> dict:
    """Every distilled memory, with its evidence and any contradiction."""
    limit = max(1, min(int(limit), 200))
    try:
        with db.connect() as conn:
            rows = _distilled_rows(conn)
            out = []
            for r in rows[:limit]:
                sources = [int(x[0]) for x in conn.execute(
                    "SELECT target_memory_id FROM memory_links "
                    "WHERE source_memory_id=? AND relation='derived_from'",
                    (r["id"],)).fetchall()]
                out.append({
                    "memory_id": r["id"],
                    "content": (r["content"] or "")[:400],
                    "category": r["category"],
                    "confidence_score": r["confidence_score"],
                    "importance_score": r["importance_score"],
                    "source_memory_ids": sources,
                    "source_count": len(sources),
                })
            drift = _drifted_distillations()
        drifted_ids = {d["distilled_memory_id"] for d in drift}
        for item in out:
            item["contradicted"] = item["memory_id"] in drifted_ids
        return {"ok": True, "distillations": out, "count": len(out),
                "drifted": drift, "note": "提炼结果自身也是记忆，可被后续记忆反驳。"}
    except Exception as exc:
        return {"ok": False, "message": f"{type(exc).__name__}: {exc}", "distillations": []}
