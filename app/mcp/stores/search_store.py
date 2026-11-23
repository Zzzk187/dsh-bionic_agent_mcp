"""搜索与向量域 SQL（OPT-03 3.2 三期：从 core_tools.py 下沉）。

设计红线同前两个 store：**函数一律接收调用方传入的 conn，自己不 open、不 commit、
不开事务**。这里的工具都是「先查、再打分、可选回写」，事务边界本来就只在单个
`with connect() as conn:` 里，下沉后仍由工具函数掌握。

`search_memories` 的 SQL 是**动态拼**的（AND 词条件 + 可选的 category），所以把
拼装整体搬进来而不是只搬一句 SQL —— 只搬一句就得让工具层继续拼 SQL，等于没下沉。
返回 `(rows, base_clauses, base_params)`：后两项给「AND 全不中时退回词法兜底」用，
与下沉前工具里的 base_clauses/base_params 逐字同源。
"""
from __future__ import annotations

from app.embeddings import DEFAULT_MODEL

# ---- search_memories ----

_SELECT_MEMORY_BY_CATEGORY = (
    "SELECT * FROM memories WHERE status='active' AND memory_status != 'pending_review' "
    "AND category=? ORDER BY updated_at DESC LIMIT 500"
)
_SELECT_MEMORY_RECENT = (
    "SELECT * FROM memories WHERE status='active' AND memory_status != 'pending_review' "
    "ORDER BY updated_at DESC LIMIT 500"
)

# ---- get_memory_links ----

_SELECT_LINKS_FOR_MEMORY = (
    "SELECT l.*, a.content AS source_content, b.content AS target_content "
    "FROM memory_links l "
    "JOIN memories a ON a.id=l.source_memory_id "
    "JOIN memories b ON b.id=l.target_memory_id "
    "WHERE l.source_memory_id=? OR l.target_memory_id=? "
    "ORDER BY l.id DESC LIMIT 100"
)

# ---- embedding_status ----

_COUNT_ACTIVE_MEMORIES = "SELECT COUNT(*) FROM memories WHERE status='active'"
_COUNT_INDEXED_ACTIVE = (
    "SELECT COUNT(*) FROM memory_embeddings e JOIN memories m ON m.id=e.memory_id "
    "WHERE m.status='active' AND e.model_name=?"
)
_COUNT_STALE_ACTIVE = (
    "SELECT COUNT(*) FROM memories m LEFT JOIN memory_embeddings e ON e.memory_id=m.id "
    "WHERE m.status='active' AND (e.memory_id IS NULL OR e.model_name!=? OR e.content_hash!=?)"
)

# ---- index_memories ----

_SELECT_INDEX_STATE = (
    "SELECT m.id,m.content,e.content_hash AS indexed_hash,e.model_name AS indexed_model "
    "FROM memories m LEFT JOIN memory_embeddings e ON e.memory_id=m.id "
    "WHERE m.status='active' ORDER BY m.id"
)
_UPSERT_EMBEDDING = (
    "INSERT INTO memory_embeddings(memory_id,model_name,content_hash,dimension,vector_json,updated_at) "
    "VALUES(?,?,?,?,?,CURRENT_TIMESTAMP) "
    "ON CONFLICT(memory_id) DO UPDATE SET model_name=excluded.model_name, "
    "content_hash=excluded.content_hash,dimension=excluded.dimension, "
    "vector_json=excluded.vector_json,updated_at=CURRENT_TIMESTAMP"
)

# ---- semantic_search_memories ----

_SELECT_VECTORS_BY_CATEGORY = (
    "SELECT m.*,e.vector_json,e.model_name,e.content_hash FROM memories m "
    "JOIN memory_embeddings e ON e.memory_id=m.id "
    "WHERE m.status='active' AND m.memory_status != 'pending_review' AND m.category=? "
    "AND e.model_name=? ORDER BY m.id"
)
_SELECT_VECTORS_ALL = (
    "SELECT m.*,e.vector_json,e.model_name,e.content_hash FROM memories m "
    "JOIN memory_embeddings e ON e.memory_id=m.id "
    "WHERE m.status='active' AND m.memory_status != 'pending_review' "
    "AND e.model_name=? ORDER BY m.id"
)


def search_rows(conn, *, include_archived: bool, category: str,
                terms: list[str], limit: int):
    """关键词检索主查询。

    返回 `(rows, base_clauses, base_params)`；后两者是不含 AND 词条件、不含 LIMIT
    的那份条件与参数，供调用方在「AND 全不中」时退回词法兜底复用。

    注意 base_* 必须从 clauses/params 的**副本**取，且必须在追加 word 条件之前 ——
    下沉前工具里也是这么写的，注释里记过「反推漏掉 LIMIT 那个 ? 会报
    sqlite3.ProgrammingError: Incorrect number of bindings」。
    """
    statuses = "('active','archived')" if include_archived else "('active')"
    params: list = []
    clauses = [f"status IN {statuses}", "memory_status != 'pending_review'"]
    if category:
        clauses.append("category = ?")
        params.append(category)
    base_clauses = list(clauses)
    base_params = list(params)
    for term in terms:
        clauses.append("(content LIKE ? OR tags_json LIKE ?)")
        params.extend([f"%{term}%", f"%{term}%"])
    sql = ("SELECT * FROM memories WHERE " + " AND ".join(clauses)
           + " ORDER BY updated_at DESC, id DESC LIMIT ?")
    params.append(limit)
    return conn.execute(sql, params).fetchall(), base_clauses, base_params


def search_all_rows(conn, base_clauses: list[str], base_params: list):
    sql = ("SELECT * FROM memories WHERE " + " AND ".join(base_clauses)
           + " ORDER BY updated_at DESC, id DESC")
    return conn.execute(sql, base_params).fetchall()


def related_rows(conn, category: str):
    if category:
        return conn.execute(_SELECT_MEMORY_BY_CATEGORY, (category,)).fetchall()
    return conn.execute(_SELECT_MEMORY_RECENT).fetchall()


def links_for_memory(conn, memory_id: int):
    return conn.execute(_SELECT_LINKS_FOR_MEMORY, (memory_id, memory_id)).fetchall()


def count_active(conn) -> int:
    return int(conn.execute(_COUNT_ACTIVE_MEMORIES).fetchone()[0])


def count_indexed_active(conn) -> int:
    return int(conn.execute(_COUNT_INDEXED_ACTIVE, (DEFAULT_MODEL,)).fetchone()[0])


def count_stale_active(conn) -> int:
    return int(conn.execute(_COUNT_STALE_ACTIVE, (DEFAULT_MODEL, "")).fetchone()[0])


def index_state_rows(conn):
    return conn.execute(_SELECT_INDEX_STATE).fetchall()


def upsert_embedding(conn, memory_id: int, model_name: str, digest: str,
                     dimension: int, vector_json: str) -> None:
    conn.execute(_UPSERT_EMBEDDING,
                 (memory_id, model_name, digest, dimension, vector_json))


def vector_rows(conn, category: str):
    if category:
        return conn.execute(_SELECT_VECTORS_BY_CATEGORY,
                            (category, DEFAULT_MODEL)).fetchall()
    return conn.execute(_SELECT_VECTORS_ALL, (DEFAULT_MODEL,)).fetchall()
