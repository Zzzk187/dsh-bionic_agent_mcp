"""Read-only knowledge base over dsh-memory's `knowledge/` and `mine/` trees.

Why this module exists
----------------------
dsh-memory keeps two layers that MCP could never see:

    knowledge/   82 md, ~400K chars -- researched, deduplicated notes
    mine/        24 md              -- the assistant's own day notes

They were unreachable because bionic's schema only had `memories` (facts with a
lifecycle) and there is no honest way to store a 10K-char document as a
"memory" -- it would enter conflict detection, expiry, and recall scoring, none
of which mean anything for a note. Phase 7 adds dedicated tables instead.

Design decisions worth keeping
------------------------------
* **Chunk by `## `, not by characters.** dsh-memory's own `知识检索.ps1` splits
  on `^## `, so a chunk here lines up with what that tool would show. Measured
  on the real tree: 628 sections, median 569 chars, p90 1158 -- exactly the size
  an embedding handles well. Only 23 sections exceed 1500 chars and get a second
  split at paragraph boundaries.
* **`path` is the idempotency anchor, `content_hash` is the change detector.**
  Re-importing must not duplicate rows, and an unchanged file must not pay for
  new vectors (that is 82 documents x several chunks of ONNX inference).
* **Documents never enter `memories`.** No lifecycle, no governance, no
  conflicts. Mixing them would flood the review queue with 82 non-conflicts.
* **Deletion is explicit, never implied.** A file that disappears from disk is
  reported, not silently dropped: the source tree is the authority, and this
  module must not be the one to lose a note.

The module is intentionally dependency-free beyond stdlib + `app.embeddings`,
so the import script can run without the MCP server being up.
"""
from __future__ import annotations

import hashlib
import io
import os
import re
from math import log as _math_log
from typing import Iterable

from . import db
from . import embeddings

# Where dsh-memory lives. Overridable because a second machine (or a test) may
# keep it elsewhere, and hardcoding a Windows path into a module is how test
# suites end up unable to run.
DSH_MEMORY_ROOT = os.getenv("DSH_MEMORY_ROOT", r"C:\Users\xing\.dsh\dsh-memory")

# Files in knowledge/ that are indexes, not notes. Importing them would create
# chunks whose text is a table of links -- semantically noisy and never the
# answer to a question.
INDEX_FILENAMES = ("INDEX.md", "资产索引.md")

# Sections longer than this get a second split at blank-line boundaries.
# 1500 chars is roughly 700 Chinese tokens: past that, a single vector averages
# over too many unrelated facts and retrieval gets vague.
MAX_CHUNK_CHARS = 1500

# Very short sections ("## 参考" with two links) carry almost no signal; they
# stay attached to the document but are not worth a vector.
MIN_CHUNK_CHARS = 40

MODEL_NAME = embeddings.DEFAULT_MODEL

# ---------------------------------------------------------------------------
# The system's own skeleton (added 2026-11-08, when the local dsh-memory system
# was merged into MCP wholesale).
#
# These files were deliberately left out of the first import, and the reason was
# sound at the time: 卷/ is mirrored row-by-row into `memories`, so importing it
# here puts the same text in two tables with two origins. But the merge exposed
# the cost of that choice -- a `memories` table answers a question only if you
# already know which row to open. "我有哪些硬约定" and "记忆库怎么备份" got zero
# hits, because the files that answer them were not in the index at all.
#
# So the trade-off is now the other way round, with the duplication accepted and
# named: the `memories` row stays authoritative (that is what 记忆.jsonl syncs
# into), and these documents are the retrieval-facing view of the same text.
# They are separate roots, not notes, because they are not notes -- they are the
# machine, and a query that wants a note usually does not want the machinery.
# Paths are relative to the dsh-memory root, with forward slashes. They were
# flat filenames until 2026-11-08, when the directory was modularised into
# bin/ lib/ data/ docs/ var/; keeping the old names here silently turned 22
# indexed documents into missing_on_disk -- the index still answered queries
# with text that no longer existed on disk, which is worse than a plain miss.
SKELETON_FILES = (
    "约定.md", "性格.md", "记忆.md", "记忆索引.md",      # 唤醒面：故意留在顶层
    "docs/体系.md", "docs/MAP.md", "docs/README.md",
)
SKELETON_DIRS = ("卷",)
SKELETON_SCRIPTS = (
    "bin/查.ps1", "bin/检查记忆.ps1", "bin/待办.ps1", "bin/备份.ps1",
    "bin/检索统计.ps1", "bin/生成索引.ps1", "bin/知识检索.ps1",
    "bin/自检.ps1", "bin/记忆写入.ps1", "bin/记忆检索.ps1",
    # 2026-11-08 模块化后新增：这两个文件才是"路径与 I/O 从哪来"的答案
    "config.ps1", "lib/Common.ps1",
)
SKELETON_ROOT = "memory"
SCRIPT_ROOT = "script"


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

def _split_long(text: str, limit: int = MAX_CHUNK_CHARS) -> list[str]:
    """Split an over-long section at paragraph boundaries, never mid-word.

    Falls back to hard slicing only when a single paragraph is itself longer
    than the limit (tables and code blocks are the usual offenders) -- losing a
    row boundary is better than dropping content.
    """
    if len(text) <= limit:
        return [text]
    out: list[str] = []
    buf: list[str] = []
    size = 0
    for para in text.split("\n\n"):
        plen = len(para) + 2
        if size and size + plen > limit:
            out.append("\n\n".join(buf).strip())
            buf, size = [], 0
        if plen > limit:
            for i in range(0, len(para), limit):
                piece = para[i:i + limit]
                if len(piece) >= MIN_CHUNK_CHARS:
                    out.append(piece.strip())
            continue
        buf.append(para)
        size += plen
    if buf:
        out.append("\n\n".join(buf).strip())
    return [c for c in out if c.strip()]


def _title_of(text: str) -> str:
    for line in text.splitlines():
        m = re.match(r"^#\s+(.+)$", line)
        if m:
            return m.group(1).strip()
        if line.strip():
            break        # first non-empty line is not an H1 -> no title
    return ""


def split_sections(text: str) -> list[tuple[str, str]]:
    """Return [(heading, body), ...] using dsh-memory's own `^## ` rule.

    The text before the first `## ` becomes a section headed by the document
    title (or '' when the file has no H1), so nothing is ever dropped.
    """
    lines = text.splitlines()
    sections: list[tuple[str, list[str]]] = []
    # The run-in before the first `## ` is `(前言)`, NOT the document title.
    # `New-Doc` in 知识检索.ps1 names it exactly that, and the distinction is
    # load-bearing: the heading scores 3w per matching term, so titling the
    # preamble after the document made every file whose subject words appear in
    # its own title collect a second 3w that the PowerShell never pays. Two
    # unrelated hardware files outranked the right answer by 2 points that way.
    cur_heading = "(前言)"
    cur: list[str] = []
    for line in lines:
        if re.match(r"^##\s+", line):
            sections.append((cur_heading, cur))
            cur_heading = re.sub(r"^##\s+", "", line).strip()
            cur = [line]
        else:
            cur.append(line)
    sections.append((cur_heading, cur))

    out: list[tuple[str, str]] = []
    for heading, body in sections:
        body_text = "\n".join(body).strip()
        if not body_text:
            continue
        for piece in _split_long(body_text):
            if len(piece) >= MIN_CHUNK_CHARS:
                out.append((heading, piece))
    return out


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def _skeleton_docs(root: str) -> list[dict]:
    """The system's own files: wake-chain pages, 卷/, AGENTS.md, and the scripts.

    Kept out of the knowledge/mine walk for two reasons: they are outside those
    two directories, and mixing them into the same root would let the machinery
    outrank the notes (a query about 备份 should be able to find 备份.ps1, but a
    query about 显存 should never be answered by it).
    """
    out: list[dict] = []

    def _add(full: str, rel: str, root_name: str, domain: str) -> None:
        try:
            st = os.stat(full)
        except OSError:
            return
        out.append({
            "path": os.path.abspath(full),
            "rel_path": rel,
            "root": root_name,
            "domain": domain,
            # [S0/P1-1] 稳定的源根 + 相对路径：跨设备重锚靠这两个字段，
            # 绝对路径只作展示与读取入口，不再当幂等键。
            "source_root": os.path.abspath(root),
            "source_rel": rel,
            "byte_size": st.st_size,
            "mtime": int(st.st_mtime),
        })

    def _rel(rel: str) -> str:
        return os.path.join(root, *rel.split("/"))   # 清单里写 '/'，落盘要本地分隔符

    for name in SKELETON_FILES:
        _add(_rel(name), name, SKELETON_ROOT, "")
    # AGENTS.md sits one level up from dsh-memory; it is the file the host
    # injects into every request, so it is the single most load-bearing text in
    # the whole system and must be searchable.
    # [S0/P1-1] rel_path 显式写成 ../AGENTS.md，如实反映它在 dsh-memory
    # 上一级；既避免与卷/ 下的同名文件混淆，也让重锚端能算回真实位置。
    _add(os.path.join(os.path.dirname(root), "AGENTS.md"), "../AGENTS.md", SKELETON_ROOT, "")
    for sub in SKELETON_DIRS:
        base = os.path.join(root, sub)
        if not os.path.isdir(base):
            continue
        for dirpath, _dirnames, filenames in os.walk(base):
            for name in sorted(filenames):
                if not name.lower().endswith(".md"):
                    continue
                full = os.path.join(dirpath, name)
                _add(full, os.path.join(sub, os.path.relpath(full, base)), SKELETON_ROOT, sub)
    for name in SKELETON_SCRIPTS:
        _add(_rel(name), name, SCRIPT_ROOT, "")
    return out


def discover(root: str | None = None) -> list[dict]:
    """Walk knowledge/, mine/, and the system skeleton; return the importable list.

    The original version deliberately did NOT read 卷/ -- those four files are
    mirrored row-by-row into `memories`. That decision was overturned on
    2026-11-08 (see SKELETON_FILES): the mirror answers a question only when you
    already know which row to open, so the same text is now also indexed here.
    """
    root = root or DSH_MEMORY_ROOT
    docs: list[dict] = []
    for sub in ("knowledge", "mine"):
        base = os.path.join(root, sub)
        if not os.path.isdir(base):
            continue
        for dirpath, _dirnames, filenames in os.walk(base):
            for name in sorted(filenames):
                if not name.lower().endswith(".md"):
                    continue
                if sub == "knowledge" and name in INDEX_FILENAMES:
                    continue
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, base)
                domain = ""
                if sub == "knowledge" and os.sep in rel:
                    domain = rel.split(os.sep)[0]
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                docs.append({
                    "path": os.path.abspath(full),
                    "rel_path": rel,
                    "root": sub,
                    "domain": domain,
                    # [S0/P1-1] 与 _skeleton_docs 同一口径：相对 dsh-memory 根。
                    # 这样重锚时 new_path = new_root / source_rel 对两类文档都成立。
                    "source_root": os.path.abspath(root),
                    "source_rel": os.path.relpath(full, root),
                    "byte_size": st.st_size,
                    "mtime": int(st.st_mtime),
                })
    # Skeleton last: if a rel_path ever collides, the note-shaped import wins.
    return docs + _skeleton_docs(root)


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

def _read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


def import_docs(root: str | None = None, *, dry_run: bool = False,
                embed: bool = True, limit: int | None = None) -> dict:
    """Import (or refresh) every document under knowledge/ and mine/.

    Returns a report instead of throwing on individual file errors: one
    unreadable note must not abort a 106-document import, but it must be
    visible in the result (`errors`) rather than swallowed.

    `dry_run=True` does all the reading and chunking but writes nothing -- the
    numbers it reports are the numbers a real run would produce.
    """
    docs = discover(root)
    if limit is not None:
        docs = docs[:limit]

    stats = {
        "ok": True,
        "root": root or DSH_MEMORY_ROOT,
        "dry_run": dry_run,
        "embed": embed,
        "docs_seen": len(docs),
        "docs_imported": 0,
        "docs_unchanged": 0,
        "chunks_written": 0,
        "chunks_deleted": 0,
        "vectors_written": 0,
        "errors": [],
    }

    # Read and chunk first, before touching the database: if a file is
    # unreadable we want to know before a transaction opens.
    prepared: list[dict] = []
    for meta in docs:
        try:
            text = _read_text(meta["path"])
        except OSError as exc:
            stats["errors"].append({"path": meta["path"], "error": str(exc)[:200]})
            continue
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        prepared.append({
            **meta,
            "title": _title_of(text) or os.path.splitext(os.path.basename(meta["path"]))[0],
            "content": text,
            "content_hash": digest,
            "sections": split_sections(text),
        })

    if dry_run:
        stats["docs_imported"] = len(prepared)
        stats["chunks_written"] = sum(len(p["sections"]) for p in prepared)
        return stats

    with db.connect() as conn:
        for doc in prepared:
            # [S0/P1-1] 稳定键 (root, rel_path) 优先；绝对路径只作兼容回退。
            # 换机器/换盘符后同一文档识别为"更新"而非"新增+孤儿"；回退同时
            # 平滑迁移 AGENTS.md 的 rel_path "AGENTS.md" → "../AGENTS.md"
            # （命中后下面的 UPDATE 会把两列都刷新为新值，不产生重复行）。
            row = conn.execute(
                "SELECT id, content_hash FROM knowledge_docs WHERE root = ? AND rel_path = ?",
                (doc["root"], doc["rel_path"]),
            ).fetchone()
            if row is None:
                row = conn.execute(
                    "SELECT id, content_hash FROM knowledge_docs WHERE path = ?",
                    (doc["path"],),
                ).fetchone()
            if row is not None and row["content_hash"] == doc["content_hash"]:
                stats["docs_unchanged"] += 1
                continue

            if row is None:
                cur = conn.execute(
                    "INSERT INTO knowledge_docs (path, rel_path, root, domain, title, content,"
                    " content_hash, byte_size, mtime, chunk_count)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (doc["path"], doc["rel_path"], doc["root"], doc["domain"], doc["title"],
                     doc["content"], doc["content_hash"], doc["byte_size"], doc["mtime"],
                     len(doc["sections"])),
                )
                doc_id = cur.lastrowid
            else:
                doc_id = row["id"]
                # Delete vectors explicitly: ON DELETE CASCADE on knowledge_chunks
                # does not fire for the embeddings table when rows are removed by
                # an explicit DELETE on chunks (SQLite cascades one level).
                conn.execute(
                    "DELETE FROM knowledge_embeddings WHERE chunk_id IN"
                    " (SELECT id FROM knowledge_chunks WHERE doc_id = ?)",
                    (doc_id,),
                )
                old = conn.execute(
                    "SELECT COUNT(*) AS n FROM knowledge_chunks WHERE doc_id = ?", (doc_id,)
                ).fetchone()["n"]
                stats["chunks_deleted"] += int(old)
                conn.execute("DELETE FROM knowledge_chunks WHERE doc_id = ?", (doc_id,))
                conn.execute(
                    # [S0/P1-1] 同时刷新 path：稳定键命中但路径已变（换机器）时，
                    # 把绝对路径重锚到本机；内容未变也更新，避免下次仍判 changed。
                    "UPDATE knowledge_docs SET path=?, rel_path=?, root=?, domain=?, title=?,"
                    " content=?, content_hash=?, byte_size=?, mtime=?, chunk_count=?,"
                    " updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (doc["path"], doc["rel_path"], doc["root"], doc["domain"], doc["title"],
                     doc["content"], doc["content_hash"], doc["byte_size"], doc["mtime"],
                     len(doc["sections"]), doc_id),
                )

            chunk_ids: list[int] = []
            for ord_, (heading, body) in enumerate(doc["sections"]):
                cur = conn.execute(
                    "INSERT INTO knowledge_chunks (doc_id, ord, heading, content, content_hash,"
                    " char_count) VALUES (?,?,?,?,?,?)",
                    (doc_id, ord_, heading, body,
                     hashlib.sha256(body.encode("utf-8")).hexdigest(), len(body)),
                )
                chunk_ids.append(cur.lastrowid)
                stats["chunks_written"] += 1

            if embed and chunk_ids:
                stats["vectors_written"] += _embed_chunks(conn, doc_id, doc["sections"], chunk_ids)
            stats["docs_imported"] += 1

        conn.execute(
            "INSERT INTO events (task_id, event_type, detail) VALUES (0,?,?)",
            ("knowledge.imported",
             f"seen={stats['docs_seen']} imported={stats['docs_imported']}"
             f" unchanged={stats['docs_unchanged']} chunks={stats['chunks_written']}"),
        )
    return stats


def _embed_chunks(conn, doc_id: int, sections: list[tuple[str, str]], chunk_ids: list[int]) -> int:
    """Embed every chunk of one document in a single batch.

    Batching matters: ONNX inference has a fixed per-call cost that dominates
    when called once per 500-char chunk. One call per document keeps the import
    of 106 documents under a couple of minutes.

    A model failure here is not fatal to the import -- the chunks are already
    written, so text search still works and `embed_missing` can backfill later.
    """
    texts = []
    for heading, body in sections:
        # Prefix the heading so "## 显存门槛" is retrievable by its own name even
        # when the body never repeats those words.
        texts.append(f"{heading}\n{body}" if heading else body)
    try:
        vectors = embeddings.embed_passages(texts)
    except Exception:
        return 0
    written = 0
    for chunk_id, (heading, body), vec in zip(chunk_ids, sections, vectors):
        conn.execute(
            "INSERT OR REPLACE INTO knowledge_embeddings"
            " (chunk_id, model_name, content_hash, dimension, vector_json)"
            " VALUES (?,?,?,?,?)",
            (chunk_id, MODEL_NAME, hashlib.sha256(body.encode("utf-8")).hexdigest(),
             len(vec), _vec_json(vec)),
        )
        written += 1
    return written


def _vec_json(vec: Iterable[float]) -> str:
    import json
    return json.dumps([round(float(x), 7) for x in vec], separators=(",", ":"))


def _vec_load(raw: str) -> list[float]:
    import json
    return json.loads(raw)


def embed_missing(limit: int | None = None) -> dict:
    """Backfill vectors for chunks that have none (or were made by another model).

    Separated from the import so a failed warmup does not require re-reading
    every markdown file: text is already in the database.
    """
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT c.id, c.heading, c.content FROM knowledge_chunks c"
            " LEFT JOIN knowledge_embeddings e ON e.chunk_id = c.id AND e.model_name = ?"
            " WHERE e.chunk_id IS NULL",
            (MODEL_NAME,),
        ).fetchall()
        if limit is not None:
            rows = rows[:limit]
        batch, written = [], 0
        for r in rows:
            batch.append(r)
            if len(batch) >= 32:
                written += _embed_batch(conn, batch)
                batch = []
        if batch:
            written += _embed_batch(conn, batch)
        if written:
            conn.execute(
                "INSERT INTO events (task_id, event_type, detail) VALUES (0,?,?)",
                ("knowledge.embedded", f"vectors={written}"),
            )
    return {"ok": True, "chunks_missing": len(rows), "vectors_written": written}


def _embed_batch(conn, rows) -> int:
    texts = [f"{r['heading']}\n{r['content']}" if r["heading"] else r["content"] for r in rows]
    try:
        vectors = embeddings.embed_passages(texts)
    except Exception:
        return 0
    for r, vec in zip(rows, vectors):
        conn.execute(
            "INSERT OR REPLACE INTO knowledge_embeddings"
            " (chunk_id, model_name, content_hash, dimension, vector_json) VALUES (?,?,?,?,?)",
            (r["id"], MODEL_NAME,
             hashlib.sha256(r["content"].encode("utf-8")).hexdigest(), len(vec), _vec_json(vec)),
        )
    return len(rows)


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

# 同义词表与分词函数已抽到 `app.text_tokens`（记忆侧也要用同一份）。
# 这里保留同名别名，本模块其余代码与外部引用点都不用改。
from app.text_tokens import SYNONYM_GROUPS as _SYNONYM_GROUPS, segments as _segments  # noqa: E402,F401


def _parse_query(text: str) -> tuple[list[str], list[str]]:
    """Return (original_terms, all_terms) for one query.

    A straight port of `ConvertTo-Terms` in dsh-memory's `知识检索.ps1`. It exists
    here for one measured reason: the MCP side has to answer the *same* questions
    the PowerShell side already answers at recall@3 12/12, and a different
    tokenizer means two systems that disagree in front of the user.

    Three sources of terms:
      1. the query split on whitespace / commas -- the words the user typed
      2. the synonym table, so "压缩" also tries "compact" / "摘要" / "剪枝"
      3. CJK bigrams, as a fallback when the table has no entry
    """
    q = (text or "").strip()
    originals = [p.strip() for p in re.split(r"[\s,，、/|;；]+", q) if p.strip()]
    terms: list[str] = list(originals)

    # Match on the table's KEY, not on its members -- exactly as `ConvertTo-Terms`
    # does: `$Text -like "*$k*" -or $k -like "*$Text*"` iterates `$Synonyms.Keys`.
    # Matching any member instead fires every group a common word appears in:
    # "本地模型 V100 显存" contains 显存, which sits in the 部署 / 私有化 / 缩放
    # groups, so twelve unrelated terms were added and a section about vLLM
    # quantization outscored the article actually about V100 hardware.
    low = q.lower()
    for group in _SYNONYM_GROUPS:
        key = group[0].lower()
        if key in low or low in key:
            terms.extend(group)

    # CJK bigrams from *short* queries are deliberately NOT terms:
    # `知识检索.ps1` uses them only as the `min(6, bg) * 1.6` bonus below.
    # Listing them as terms too scored every bigram twice and cost 2 of 38
    # first-place hits when measured.
    #
    # That measurement was taken on the 38-case benchmark, which is made of
    # short keyword queries ("上下文坍缩", "切分 召回"). Real questions are
    # whole sentences, and a sentence is one whitespace-delimited token, so
    # the tokenizer above yields exactly one term for it and every section
    # containing that missing substring scores zero -- "插件装完没反应是为什么"
    # ranked `多 Agent 编排` first and `DSH 插件开发` third, because all of
    # them scored 1.08 and nothing separated them. So: expand bigrams only
    # from parts long enough to be a sentence (>= 6 CJK chars). Short keyword
    # queries keep the old path untouched, which is why the benchmark stays
    # identical (measured: 34/38 by single-file criterion, same before and
    # after; the four misses are cases whose accepted answer is a *set* of
    # files and this criterion only reads the first).
    for part in originals:
        if len(part) < 6 or not re.search(r"[\u4e00-\u9fff]", part):
            continue
        chars = list(part)
        for i in range(len(chars) - 1):
            bg = chars[i] + chars[i + 1]
            if re.fullmatch(r"[\u4e00-\u9fff]{2}", bg) and bg not in terms:
                terms.append(bg)

    seen: list[str] = []
    for t in terms:
        t = t.strip()
        if t and t not in seen:
            seen.append(t)
    return originals, seen


def _term_weights(queries: list[str], docs: list[str]) -> tuple[dict[str, float], list[str]]:
    """IDF weights computed over whole documents, exactly like 知识检索.ps1.

    ``w = min(4, 1 + log(N/df))``, and terms the user typed themselves get
    ``w * 2.5`` capped at 10. That doubling is what makes a question phrased in
    the user's own words beat a section that merely repeats common vocabulary.

    Document-level df (not chunk-level) is deliberate: a word that appears in
    one file is rare and informative regardless of how many sections repeat it.
    """
    n_docs = max(1, len(docs))
    low_docs = [d.lower() for d in docs]
    originals: list[str] = []
    terms: list[str] = []
    for q in queries:
        o, t = _parse_query(q)
        originals.extend(o)
        for term in t:
            if term not in terms:
                terms.append(term)
    low_orig = {o.lower() for o in originals}

    weights: dict[str, float] = {}
    for t in terms:
        tl = t.lower()
        df = sum(1 for d in low_docs if tl in d)
        w = min(4.0, 1.0 + _math_log(n_docs / max(1, df)))
        if tl in low_orig:
            w = min(10.0, w * 2.5)
        weights[t] = w
    return weights, terms


def _index_summaries() -> dict[str, str]:
    """`{file name: INDEX.md summary}`, lowercased -- `Get-IndexMap` from the PS.

    Read once per process; the file only changes when the knowledge base is
    regenerated. Absent or unreadable INDEX.md just means no index bonus.
    """
    global _INDEX_CACHE
    if _INDEX_CACHE is not None:
        return _INDEX_CACHE
    out: dict[str, str] = {}
    path = os.path.join(DSH_MEMORY_ROOT, "knowledge", "INDEX.md")
    try:
        text = io.open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        _INDEX_CACHE = out
        return out
    for line in text.split("\n"):
        m = re.match(r"^\|\s*\[[^\]]+\]\(([^)]+\.md)\)\s*\|\s*(.+?)\s*\|", line)
        if m:
            name = m.group(1).strip().replace("\\", "/").split("/")[-1].lower()
            out[name] = m.group(2).strip().lower()
    _INDEX_CACHE = out
    return out


_INDEX_CACHE: dict | None = None


def _keyword_scores(queries: list[str], chunks: list[dict]) -> list[float]:
    """Section scoring, ported statement-for-statement from `知识检索.ps1`.

    That PowerShell implementation scores 38/38 on the benchmark below; this is
    that algorithm rather than a fresh design. Six earlier attempts on this side
    -- weighted sum, RRF, bigram-IDF with common terms dropped, chunk-level
    scoring, a document-level tie-breaker, and a post-hoc section merge -- topped
    out at 87%, because each was a guess about what *should* matter. Copying a
    tuned result is not a guess; re-deriving one and calling it equal is.

    **The scoring unit is the `## ` section, never the chunk.** A section longer
    than `MAX_CHUNK_CHARS` is stored as several chunks, and scoring each piece
    separately splits the evidence: the heading bonus lands on the first piece
    while the sentence the user remembers sits in the third. Chunks therefore
    carry a `section` key and are merged back before scoring, with the section's
    score handed to every chunk of it.

    Two details look like fussiness and are not:

    * comparisons are case-insensitive on **both** sides, because PowerShell's
      `-like` is. Lowercasing only the haystack silently broke every query naming
      a capitalised product -- "Obsidian", "Claudian", "LoRA" -- and cost six of
      thirty-eight first-place hits.
    * term frequency counts **matching lines**, not characters. Counting
      characters hands the win to whichever section happens to be longest.

    A chunk scoring 0 shares no term with the query: a real answer, not a miss.
    """
    if not chunks:
        return []

    # Merge chunks back into their section. Without a `section` key each chunk is
    # its own section, i.e. the old, worse behaviour.
    sec_of: list[str] = []
    sec_heading: dict[str, str] = {}
    sec_body: dict[str, str] = {}
    sec_file: dict[str, str] = {}
    sec_title: dict[str, str] = {}
    for i, c in enumerate(chunks):
        key = c.get("section") or f"#chunk{i}"
        sec_of.append(key)
        if key not in sec_heading:
            sec_heading[key] = (c.get("heading") or "").lower()
            sec_body[key] = c.get("content") or ""
            # Bare file name, not the relative path: a query word that names the
            # folder would otherwise score 4w on every file in that domain.
            rel = (c.get("source") or "").replace("\\", "/")
            sec_file[key] = rel.split("/")[-1].lower()
            sec_title[key] = (c.get("title") or "").lower()
        else:
            sec_body[key] = sec_body[key] + "\n" + (c.get("content") or "")

    keys = list(sec_heading.keys())

    # df is measured over whole documents, so collapse sections back to their file.
    by_file: dict[str, str] = {}
    for k in keys:
        f = sec_file[k] or k
        by_file[f] = by_file.get(f, "") + "\n" + sec_heading[k] + "\n" + sec_body[k]
    doc_texts = list(by_file.values()) or [""]

    weights, terms = _term_weights(queries, doc_texts)
    if not terms:
        return [0.0] * len(chunks)

    qtext = " ".join(q.strip().lower() for q in queries if q and q.strip())
    cjk = re.sub(r"[^\u4e00-\u9fff]", "", qtext)
    bigrams = list(dict.fromkeys(cjk[i:i + 2] for i in range(len(cjk) - 1)))

    summaries = _index_summaries()
    index_hit: dict[str, bool] = {}
    for k in keys:
        summary = summaries.get(sec_file[k])
        index_hit[k] = bool(summary) and any(t.lower() in summary for t in terms)

    sec_score: dict[str, float] = {}
    for k in keys:
        head = sec_heading[k]
        body_low = sec_body[k].lower()
        file_low = sec_file[k]
        title_low = sec_title[k]
        # The section's own `## ` heading is part of its text in the PowerShell,
        # so it must be part of `joined` here for the verbatim check to agree.
        joined = head + "\n" + body_low
        lines = body_low.split("\n")

        score = 0.0
        matched = 0
        rare = 0
        for t in terms:
            tl = t.lower()
            w = weights.get(t, 1.0)
            hit = False
            if tl and tl in file_low:
                score += 4 * w
                hit = True
            if tl and tl in title_low:
                score += 3 * w
                hit = True
            if tl and tl in head:
                score += 3 * w
                hit = True
            if tl:
                n = sum(1 for line in lines if tl in line)
                if n:
                    score += min(n, 6) * w
                    hit = True
            if hit:
                matched += 1
                if w >= 6:
                    rare += 1

        if index_hit[k]:
            score += 2
        if matched and terms:
            score = round(score * (0.75 + 0.25 * (matched / len(terms))))
        if rare:
            score += 15 * rare

        if bigrams:
            bg = sum(1 for b in bigrams if b in joined)
            if bg:
                score += min(6, bg) * 1.6

        # The query verbatim inside the section means it is *about* the query.
        if qtext and qtext in joined:
            score += 40

        # Index / map files mention every keyword by construction: they say where
        # the answer lives, they are not the answer.
        if re.search(r"主题地图|索引|index|重复与空白|资产索引|总图|速查", file_low):
            score = round(score * 0.55)
        # Prefaces and summaries cover everything at low resolution.
        if re.search(r"一句话|总结|速览|前言|结论|摘要", head):
            score = round(score * 0.7)
        # A bare source list has no content of its own.
        if re.search(r"引用来源|来源表|视频清单|目录", head):
            score = round(score * 0.6)

        sec_score[k] = score

    peak = max(sec_score.values()) if sec_score else 0.0
    if peak <= 0:
        return [0.0] * len(chunks)
    return [sec_score[k] / peak for k in sec_of]


# How the two signals combine. This was tuned against the user's own 38-case
# benchmark (`knowledge/` only, the same cases dsh-memory's 知识检索.ps1 -Eval
# runs), not guessed:
#
#     keyword + 1.0*max(0, vector-0.30)    recall@1 32/38  (84%)  recall@3 38/38
#     RRF over the two ranked lists        recall@1 29/38  (76%)  recall@3 35/38
#
# Those numbers were measured while the keyword half was still a from-scratch
# scorer. Once it became a faithful port of 知识检索.ps1 it reached 38/38 alone,
# and the vector half's only remaining job is paraphrase tolerance that does not
# disturb the order:
#
#     VECTOR_ALPHA 0.0 / 0.2 / 0.4         recall@1 38/38  (100%)  recall@3 38/38
#     VECTOR_ALPHA 0.6 / 1.0               recall@1 37/38   (97%)  recall@3 38/38
#
# 0.4 keeps a real vector contribution -- it can break a tie the keyword scorer
# leaves open -- while leaving the ceiling intact. Parity with `-Eval` is the
# point: two systems that answer the user differently are worse than one system
# that is merely good.
#
# RRF actually *hurt* here. Its premise is two lists of comparable quality, but
# the vector list has no useful head on this corpus: all 781 sections score
# 0.20-0.46, so vector rank 1 is barely better than rank 200. Promoting it by
# rank displaces a genuinely best keyword hit.
#
# Treating the vector score as a bonus above a floor keeps the part that works
# (paraphrase tolerance --- it is what surfaces 上下文工程与多Agent之争 for
# "上下文压缩什么时候触发") while never letting a flat, undifferentiated vector
# score outrank a verbatim match.
VECTOR_ALPHA = 0.4
VECTOR_FLOOR = 0.30

# At most this many sections from one document, so a single long note cannot
# occupy the whole result list. Same rule as 知识检索.ps1's per-file cap.
MAX_PER_DOC = 2



def search(query: str, limit: int = 5, root: str = "", domain: str = "") -> dict:
    """Search knowledge notes: keyword scoring, plus vector similarity as a bonus.

    The keyword half is a port of `知识检索.ps1` (see `_keyword_scores`); the
    vector half is a floor-guarded bonus. See `VECTOR_ALPHA` for the measured
    reason a plain rank fusion was rejected.

    When the embedding model is unavailable the search degrades to keyword only
    and still answers -- paraphrase tolerance is what is lost, not correctness.
    """
    q = (query or "").strip()
    if not q:
        return {"ok": False, "error": "query 不能为空", "results": []}

    where, params = [], []
    if root:
        where.append("d.root = ?")
        params.append(root)
    if domain:
        where.append("d.domain = ?")
        params.append(domain)
    clause = (" WHERE " + " AND ".join(where)) if where else ""

    use_vector = embeddings.is_ready(MODEL_NAME)
    qvec = None
    if use_vector:
        try:
            qvec = embeddings.embed_query(q)
        except Exception:
            use_vector = False

    with db.connect() as conn:
        rows = conn.execute(
            "SELECT c.id AS chunk_id, c.doc_id, c.ord, c.heading, c.content,"
            " d.path, d.rel_path, d.root, d.domain, d.title, e.vector_json"
            " FROM knowledge_chunks c JOIN knowledge_docs d ON d.id = c.doc_id"
            " LEFT JOIN knowledge_embeddings e ON e.chunk_id = c.id AND e.model_name = ?"
            + clause,
            [MODEL_NAME] + params,
        ).fetchall()

        chunk_dicts = [
            {
                "heading": r["heading"],
                "content": r["content"],
                "source": r["rel_path"],
                "title": r["title"],
                # Sections are the scoring unit; a >1500-char section is stored
                # as several chunks and must be merged back before scoring.
                "section": f'{r["doc_id"]}|{r["heading"]}',
            }
            for r in rows
        ]
        kw_scores = _keyword_scores([q], chunk_dicts)

        sims: list[float] = []
        for r in rows:
            if use_vector and r["vector_json"]:
                try:
                    sims.append(embeddings.cosine_similarity(qvec, _vec_load(r["vector_json"])))
                except Exception:
                    sims.append(0.0)
            else:
                sims.append(0.0)
        if not use_vector:
            sims = [0.0] * len(rows)

        scored = []
        for i, r in enumerate(rows):
            kw = kw_scores[i]
            vec = sims[i]
            bonus = VECTOR_ALPHA * max(0.0, vec - VECTOR_FLOOR) if use_vector else 0.0
            score = kw + bonus
            if score <= 0:
                continue
            matched_by = []
            if kw > 0:
                matched_by.append("keyword")
            if bonus > 0:
                matched_by.append("vector")
            scored.append({
                "chunk_id": r["chunk_id"],
                "doc_id": r["doc_id"],
                "heading": r["heading"],
                "score": round(score, 6),
                "keyword_score": round(kw, 4),
                "vector_score": round(vec, 4),
                "matched_by": matched_by,
                "source": r["rel_path"],
                "root": r["root"],
                "domain": r["domain"],
                "title": r["title"],
                "ord": r["ord"],
                "snippet": _snippet(r["content"], q),
            })
    # Ties break on file name, then section order -- the same keys as the
    # PowerShell's `Sort-Object Score -Descending, File -Ascending`. Without the
    # file-name key the winner among equal scores came from insertion order:
    # stable, but arbitrary, and wrong on two benchmark cases.
    scored.sort(key=lambda x: (-x["score"], x["source"], x["ord"]))

    capped: list[dict] = []
    for item in scored:
        same = sum(1 for kept in capped if kept["source"] == item["source"])
        if same >= MAX_PER_DOC:
            continue
        capped.append(item)

    return {
        "ok": True,
        "query": q,
        "mode": "hybrid" if use_vector else "keyword",
        "vector_alpha": VECTOR_ALPHA,
        "vector_floor": VECTOR_FLOOR,
        "candidates": len(scored),
        "results": capped[:max(1, min(int(limit or 5), 50))],
    }


def _snippet(content: str, query: str, width: int = 220) -> str:
    """Return a window around the first keyword hit, so the caller can judge
    relevance without fetching the whole chunk."""
    flat = re.sub(r"\s+", " ", content).strip()
    if len(flat) <= width:
        return flat
    low = flat.lower()
    q = (query or "").strip().lower()
    pos = low.find(q) if q else -1
    if pos < 0:
        for part in re.split(r"[\s，。、；：？！]+", q):
            if len(part) >= 2:
                pos = low.find(part)
                if pos >= 0:
                    break
    if pos < 0:
        return flat[:width] + "…"
    start = max(0, pos - width // 3)
    end = min(len(flat), start + width)
    return ("…" if start else "") + flat[start:end] + ("…" if end < len(flat) else "")


def get_doc(doc_id: int = 0, path: str = "", max_chars: int = 12000) -> dict:
    """Full document plus its chunk list.

    Look up by `doc_id` or by `path` (`rel_path`, which is what search results
    return) -- a caller that just got a snippet has the path, not the id, and
    making it do a second query to translate is friction for no benefit.

    `max_chars` guards against a caller pulling a 20K-char note into context by
    accident; the truncation is reported so it is never silent.
    """
    path = (path or "").strip().replace("\\", "/")
    if not doc_id and not path:
        return {"ok": False, "error": "doc_id 与 path 至少给一个"}
    with db.connect() as conn:
        if doc_id:
            row = conn.execute(
                "SELECT * FROM knowledge_docs WHERE id = ?", (doc_id,)
            ).fetchone()
        else:
            # Accept the bare file name too: results are frequently quoted by
            # name alone, and a strict path match would force the caller to
            # reconstruct the domain folder. `rel_path` stores the OS separator,
            # so match both spellings rather than making the caller care.
            name = path.split("/")[-1]
            row = conn.execute(
                "SELECT * FROM knowledge_docs WHERE rel_path IN (?, ?)"
                " OR rel_path LIKE ? OR rel_path LIKE ? ORDER BY id LIMIT 1",
                (path, path.replace("/", "\\"), f"%/{name}", f"%\\{name}"),
            ).fetchone()
        if row is None:
            return {"ok": False, "error": f"文档不存在：{path or doc_id}"}
        # `row["id"]`, not the `doc_id` argument: when the caller looked up by
        # path, `doc_id` is still 0 and this query would silently return an
        # empty chunk list -- a document claiming `chunk_count: 9` next to
        # `chunks: []` is exactly the kind of half-truth this table exists to
        # avoid. (Caught by calling the MCP tool by path; no test had passed a
        # path through to this branch.)
        chunks = conn.execute(
            "SELECT id, ord, heading, char_count FROM knowledge_chunks"
            " WHERE doc_id = ? ORDER BY ord", (row["id"],),
        ).fetchall()
    content = row["content"]
    truncated = len(content) > max_chars
    return {
        "ok": True,
        "doc": {
            "id": row["id"],
            "path": row["path"],
            "rel_path": row["rel_path"],
            "root": row["root"],
            "domain": row["domain"],
            "title": row["title"],
            "byte_size": row["byte_size"],
            "chunk_count": row["chunk_count"],
            "imported_at": row["imported_at"],
            "updated_at": row["updated_at"],
        },
        "chunks": [dict(c) for c in chunks],
        "content": content[:max_chars],
        "truncated": truncated,
        "content_total_chars": len(content),
    }


def get_chunk(chunk_id: int) -> dict:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT c.*, d.rel_path, d.title, d.root, d.domain FROM knowledge_chunks c"
            " JOIN knowledge_docs d ON d.id = c.doc_id WHERE c.id = ?",
            (chunk_id,),
        ).fetchone()
    if row is None:
        return {"ok": False, "error": f"知识片段不存在：{chunk_id}"}
    return {"ok": True, "chunk": dict(row)}


def prune_missing(root: str | None = None, *, dry_run: bool = True) -> dict:
    """Delete knowledge_docs rows whose file is gone AND whose text survives elsewhere.

    A moved file (体系.md -> docs/体系.md) leaves the old path behind: `import_docs`
    is keyed on `path`, so it inserts the new row and never touches the old one.
    Leaving it means search still surfaces a document whose source no longer exists
    -- worse than a plain miss, because the snippet looks authoritative.

    But "in db, not on disk" is also what a genuinely deleted note looks like, and
    that note may be the only copy. So deletion requires proof of a move: a live
    document with the same basename. No such twin -> report only, never delete.
    """
    root = root or DSH_MEMORY_ROOT

    def _norm_rel(r, rel):
        # [S0/P1-1] legacy AGENTS.md 归一化（同 drift）。
        rel = rel or ""
        if r == "memory" and rel == "AGENTS.md":
            rel = "../AGENTS.md"
        return rel

    # [S0/P1-1] 用稳定键判定"文档还在不在"，不再用绝对路径：换机器/盘符后
    # 路径全变但 (root, rel_path) 不变，不应把整套库误判成 missing。
    on_disk = {
        (d["root"], _norm_rel(d["root"], d["rel_path"])) for d in discover(root)
    }
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, path, rel_path, root, content_hash FROM knowledge_docs ORDER BY id"
        ).fetchall()

    # 判据是"同名文件搬了家"，不是"内容一字没变"：模块化那次连内容一起改了，
    # 用 hash 比会把每个改过的脚本都判成唯一副本，一个也删不掉。
    # 证据要两条同时成立：root 相同（同一类资产内部搬动）+ basename 相同（同一个文件）。
    # 只比 basename 会撞巧合：memory/README.md 搬去 docs/ 的同时，mine/ 下也有个
    # README.md，它的 rel_path 恰好也是 "README.md"，于是旧行被"证明"搬去了那儿。
    key = lambda root, rel: (root, os.path.basename(rel or ""))
    live_home: dict[tuple, list[str]] = {}
    for r in rows:
        if (r["root"], _norm_rel(r["root"], r["rel_path"])) in on_disk:
            live_home.setdefault(key(r["root"], r["rel_path"]), []).append(r["rel_path"])

    missing = [
        r for r in rows
        if (r["root"], _norm_rel(r["root"], r["rel_path"])) not in on_disk
    ]
    removable, kept = [], []
    for r in missing:
        rel = r["rel_path"] or os.path.basename(r["path"])
        homes = live_home.get(key(r["root"], rel), [])
        if not homes:
            kept.append({"doc_id": r["id"], "path": r["path"], "rel_path": rel,
                         "reason": "同 root 下已没有同名文件，可能是唯一副本，不删"})
            continue
        removable.append({"doc_id": r["id"], "path": r["path"], "rel_path": rel,
                          "why": "同名文件已搬到 " + "、".join(homes)})

    stats = {
        "ok": True,
        "dry_run": dry_run,
        "missing_total": len(missing),
        "removable": removable,
        "kept": kept,
        "deleted_docs": 0,
        "deleted_chunks": 0,
        "deleted_vectors": 0,
    }
    if dry_run or not removable:
        return stats

    with db.connect() as conn:
        for item in removable:
            doc_id = item["doc_id"]
            vec_n = conn.execute(
                "SELECT COUNT(*) AS n FROM knowledge_embeddings WHERE chunk_id IN"
                " (SELECT id FROM knowledge_chunks WHERE doc_id = ?)", (doc_id,)
            ).fetchone()["n"]
            chunk_n = conn.execute(
                "SELECT COUNT(*) AS n FROM knowledge_chunks WHERE doc_id = ?", (doc_id,)
            ).fetchone()["n"]
            conn.execute(
                "DELETE FROM knowledge_embeddings WHERE chunk_id IN"
                " (SELECT id FROM knowledge_chunks WHERE doc_id = ?)", (doc_id,))
            conn.execute("DELETE FROM knowledge_chunks WHERE doc_id = ?", (doc_id,))
            conn.execute("DELETE FROM knowledge_docs WHERE id = ?", (doc_id,))
            stats["deleted_vectors"] += int(vec_n)
            stats["deleted_chunks"] += int(chunk_n)
            stats["deleted_docs"] += 1
        conn.execute(
            "INSERT INTO events (task_id, event_type, detail) VALUES (0,?,?)",
            ("knowledge.pruned",
             f"missing={len(missing)} deleted={stats['deleted_docs']}"
             f" kept={len(kept)} chunks={stats['deleted_chunks']}"),
        )
    return stats


def status() -> dict:
    """Coverage report: what is imported, what has vectors, what drifted."""
    with db.connect() as conn:
        docs = conn.execute("SELECT COUNT(*) AS n FROM knowledge_docs").fetchone()["n"]
        chunks = conn.execute("SELECT COUNT(*) AS n FROM knowledge_chunks").fetchone()["n"]
        vecs = conn.execute(
            "SELECT COUNT(*) AS n FROM knowledge_embeddings WHERE model_name = ?", (MODEL_NAME,)
        ).fetchone()["n"]
        by_root = conn.execute(
            "SELECT root, COUNT(*) AS n, SUM(chunk_count) AS c FROM knowledge_docs GROUP BY root"
        ).fetchall()
        by_domain = conn.execute(
            "SELECT domain, COUNT(*) AS n FROM knowledge_docs"
            " WHERE root='knowledge' GROUP BY domain ORDER BY n DESC"
        ).fetchall()
    return {
        "ok": True,
        "root": DSH_MEMORY_ROOT,
        "model": MODEL_NAME,
        "model_ready": embeddings.is_ready(MODEL_NAME),
        "docs": docs,
        "chunks": chunks,
        "vectors": vecs,
        "chunks_without_vector": max(0, chunks - vecs),
        "by_root": {r["root"]: {"docs": r["n"], "chunks": r["c"] or 0} for r in by_root},
        "knowledge_domains": {r["domain"] or "(根)": r["n"] for r in by_domain},
    }


def drift(root: str | None = None) -> dict:
    """Compare the database against the files on disk.

    Three cases matter and they are NOT symmetric:
      * on disk, not in db     -> needs import
      * in db, not on disk     -> the file was deleted or moved; report it, do
                                  not auto-delete (the note may be the only copy)
      * hash differs           -> the file changed, needs re-import
    """
    root = root or DSH_MEMORY_ROOT

    def _key(r, rel):
        # [S0/P1-1] 稳定键 (root, rel_path)；legacy AGENTS.md 曾以
        # "AGENTS.md" 入库，归一化到当前 "../AGENTS.md"，避免误判 missing+added。
        rel = rel or ""
        if r == "memory" and rel == "AGENTS.md":
            rel = "../AGENTS.md"
        return (r, rel)

    on_disk = {_key(d["root"], d["rel_path"]): d for d in discover(root)}
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, path, root, rel_path, content_hash FROM knowledge_docs"
        ).fetchall()
    in_db = {
        _key(r["root"], r["rel_path"]):
            {"id": r["id"], "content_hash": r["content_hash"], "path": r["path"]}
        for r in rows
    }

    added, changed, missing = [], [], []
    for key, meta in on_disk.items():
        if key not in in_db:
            added.append(meta["rel_path"])
            continue
        try:
            text = _read_text(meta["path"])   # 读本机当前路径，换盘符后依然读得到
        except OSError:
            continue
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != in_db[key]["content_hash"]:
            changed.append(meta["rel_path"])
    for key, info in in_db.items():
        if key not in on_disk:
            missing.append({"doc_id": info["id"], "path": info["path"]})

    return {
        "ok": True,
        "on_disk": len(on_disk),
        "in_db": len(in_db),
        "needs_import": sorted(added),
        "needs_reimport": sorted(changed),
        "missing_on_disk": missing,
        "in_sync": not added and not changed and not missing,
    }


def reanchor_paths(new_root: str, *, dry_run: bool = True) -> dict:
    """[S0/P1-1] 把知识文档的绝对路径从旧 dsh-memory 根重锚到 ``new_root``。

    跨设备迁移后，库里 ``knowledge_docs.path`` 仍是旧机器的绝对路径（历史幂等
    锚点），会导致 ``drift()`` 把整套库报成 missing_on_disk。本函数按稳定键
    ``(root, rel_path)`` 找到新根下的对应文件，校验内容 hash 未变后，只重写
    ``path`` 这一列——不碰内容、rel_path、root、chunk、向量和治理状态。

    四类结果：reanchored（路径已改）、unchanged（本来就指向新路径）、
    conflicts（新路径存在但 hash 变了，应 reimport 而非重锚）、
    missing（新根下找不到该文件）。写入走单个 ``db.transaction()``，任何
    异常整单回滚；``dry_run=True`` 只计算不落库。
    """
    new_root = os.path.abspath(new_root)
    report: dict = {
        "ok": True,
        "dry_run": dry_run,
        "new_root": new_root,
        "reanchored": [],
        "unchanged": [],
        "conflicts": [],
        "missing": [],
        "errors": [],
    }
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, path, root, rel_path, content_hash FROM knowledge_docs ORDER BY id"
        ).fetchall()

    updates: list[tuple] = []
    for r in rows:
        root = r["root"] or ""
        rel = r["rel_path"] or ""
        if root == "memory" and rel == "AGENTS.md":  # legacy 归一化
            rel = "../AGENTS.md"
        if root in ("knowledge", "mine"):
            target = os.path.abspath(os.path.join(new_root, root, rel))
        elif root in ("memory", "script"):
            # skeleton / scripts 的 rel_path 相对 dsh-memory 根；
            # ../AGENTS.md 自然解析到父目录，与 _skeleton_docs 一致。
            target = os.path.abspath(os.path.join(new_root, rel))
        else:
            report["conflicts"].append({"doc_id": r["id"], "path": r["path"],
                                        "reason": "未知 root: %s" % root})
            continue
        if not os.path.isfile(target):
            report["missing"].append({"doc_id": r["id"], "old_path": r["path"],
                                      "expected": target, "rel_path": rel})
            continue
        try:
            text = _read_text(target)
        except OSError as exc:
            report["errors"].append({"doc_id": r["id"], "path": target,
                                     "error": str(exc)[:200]})
            continue
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if digest != r["content_hash"]:
            report["conflicts"].append({
                "doc_id": r["id"], "path": target, "rel_path": rel,
                "reason": "内容 hash 已变，应 reimport 而非重锚",
            })
            continue
        if os.path.abspath(r["path"]) == target:
            report["unchanged"].append({"doc_id": r["id"], "path": target})
            continue
        updates.append((r["id"], target))
        report["reanchored"].append({"doc_id": r["id"], "old_path": r["path"],
                                     "new_path": target, "rel_path": rel})

    report["planned"] = len(updates)
    if dry_run or not updates:
        return report
    try:
        with db.transaction() as conn:
            for doc_id, target in updates:
                conn.execute("UPDATE knowledge_docs SET path=? WHERE id=?", (target, doc_id))
    except Exception as exc:
        report["ok"] = False
        report["reanchored"] = []
        report["errors"].append({"error": "重锚事务失败，已回滚: " + str(exc)[:200]})
    return report
