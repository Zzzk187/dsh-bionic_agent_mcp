"""Vector-index consistency checks (infrastructure layer).

Why this is its own module
--------------------------
`snapshots` (backup/restore) needs to answer "after putting the rows back, do the
stored vectors still describe the current text?". That question only touches the
memory table and the embedding table, so it belongs to the infrastructure layer --
not to `governance_maintenance`, which is the *upper* orchestration layer (it
imports governance_service / governance_scoring / governance_conflicts /
governance_rules). Having a low-level backup module import a high-level
governance orchestrator just to count mismatched rows is a reversed dependency.

Behaviour is identical to the implementation that used to live in
`governance_maintenance`: archived rows keep their vectors (we never delete on
archive), and this deliberately does NOT re-embed -- re-embedding needs the model,
takes time, and would turn a fast rollback into a long silent operation.
"""
from __future__ import annotations

from app import db

try:
    from app import embeddings as emb
except Exception:  # pragma: no cover
    emb = None


def get_vector_consistency() -> dict:
    """Verify vector-index consistency with current content.

    - Active memories missing a vector -> pending re-index.
    - Active memories whose stored vector hash != current content -> stale.
    - Archived memories must KEEP their vectors (we never delete on archive).
    """
    from app import embeddings as _emb
    with db.connect() as conn:
        rows = [db.rowdict(r) for r in conn.execute(
            "SELECT m.id,m.status,m.content,e.content_hash AS stored_hash,"
            "e.model_name FROM memories m LEFT JOIN memory_embeddings e "
            "ON e.memory_id=m.id ORDER BY m.id").fetchall()]
    active_no_vector = 0
    stale = 0
    archived_with_vector = 0
    archived_no_vector = 0
    for r in rows:
        if r["status"] == "active":
            if r["stored_hash"] is None:
                active_no_vector += 1
            elif _emb is not None and r["stored_hash"] != _emb.content_hash(r["content"] or ""):
                stale += 1
        elif r["status"] == "archived":
            if r["stored_hash"] is not None:
                archived_with_vector += 1
            else:
                archived_no_vector += 1
    return {
        "ok": True,
        "active_without_vector": active_no_vector,
        "active_stale_vector": stale,
        "archived_with_vector_retained": archived_with_vector,
        "archived_without_vector": archived_no_vector,
        "embedding_available": emb is not None,
        "message": "归档记忆的向量应被保留；活跃检索按 status 过滤，无需重算。",
    }
