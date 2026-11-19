"""一条命令把本地 dsh-memory 同步进 Bionic Agent 库，并报告是否一致。

分工的落点：本地 `~/.dsh/dsh-memory` 是唯一数据源（可 diff、可 git、唤醒时
常驻注入），MCP 库是它的检索层 + 治理层。所以"同步"永远是单向的：
本地 → MCP。改了记忆/笔记就跑这一个脚本，不要手抄两遍。

用法：
    python scripts/sync_all.py           # 同步记忆 + 知识 + 向量，最后报状态
    python scripts/sync_all.py --dry-run # 只报会改什么，不落库

退出码：0 = 两侧一致；1 = 有东西没同步上（drift 未清空）。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

PY = sys.executable


def _run(script: str, *args: str) -> dict:
    """跑一个导入脚本，返回它最后那行 JSON（脚本按约定都打印 JSON）。"""
    cmd = [PY, "-X", "utf8", str(PROJECT_ROOT / "scripts" / script), *args]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", cwd=str(PROJECT_ROOT))
    if proc.returncode != 0:
        print(f"[FAIL] {script} 退出码 {proc.returncode}")
        tail = (proc.stdout or "")[-2000:] + (proc.stderr or "")[-2000:]
        print(tail)
        return {"ok": False}
    text = (proc.stdout or "").strip()
    # 整个 stdout 就是一个 JSON（知识导入脚本是缩进多行）时直接解析；
    # 否则退回"从后往前找最后一行以 { 开头的 JSON"。
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    out = text.splitlines()
    # 知识脚本用 indent=2 输出多行 JSON、末尾还跟中文提示行：逐行找单行
    # '{' 拼不出完整对象。找到第一个顶格 '{' 后用 raw_decode 解析整块（它会
    # 在匹配的 '}' 处停下，自动忽略后面的中文提示）。
    for i, ln in enumerate(out):
        if ln.startswith("{") and not ln.startswith(" "):
            try:
                obj, _end = json.JSONDecoder().raw_decode("\n".join(out[i:]))
                return obj
            except json.JSONDecodeError:
                break
    for line in reversed(out):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return {"ok": True, "raw": out[-1] if out else ""}


def _index_memory_vectors() -> dict:
    """按 content_hash 增量补记忆向量（与 MCP 的 index_memories 同一判据）。"""
    try:
        from app import db as _db, embeddings as _emb
        sha = _emb.content_hash
        with _db.connect() as conn:
            rows = conn.execute(
                "SELECT m.id, m.content, e.content_hash AS ih, e.model_name AS im "
                "FROM memories m LEFT JOIN memory_embeddings e ON e.memory_id=m.id "
                "WHERE m.status='active' ORDER BY m.id").fetchall()
        pending = [r for r in rows
                   if r["ih"] != sha(r["content"]) or r["im"] != _emb.DEFAULT_MODEL]
        if not pending:
            return {"ok": True, "indexed": 0, "pending": 0}
        vectors = _emb.embed_passages([r["content"] for r in pending])
        import json as _json
        with _db.transaction() as conn:
            for row, vec in zip(pending, vectors):
                conn.execute(
                    "INSERT INTO memory_embeddings"
                    "(memory_id,model_name,content_hash,dimension,vector_json,updated_at) "
                    "VALUES(?,?,?,?,?,CURRENT_TIMESTAMP) "
                    "ON CONFLICT(memory_id) DO UPDATE SET model_name=excluded.model_name,"
                    "content_hash=excluded.content_hash,dimension=excluded.dimension,"
                    "vector_json=excluded.vector_json,updated_at=CURRENT_TIMESTAMP",
                    (row["id"], _emb.DEFAULT_MODEL, sha(row["content"]),
                     len(vec), _json.dumps(vec)))
        return {"ok": True, "indexed": len(pending), "pending": len(pending)}
    except Exception as exc:
        return {"ok": False, "indexed": 0, "pending": 0,
                "message": f"{type(exc).__name__}: {exc}"}


def _rescore_governance() -> dict:
    """重算并持久化治理分（importance/confidence/freshness）。

    这三个分量是**存进表里的列**，召回时直接读列而不现算，所以长期不重算
    就等于排序基于旧快照。半衰期 120 天意味着每天只掉千分之几，正因为慢，
    停摆才不容易被发现——但十天不动，freshness 就会退化成几个常数。
    """
    try:
        from app import governance_service as _gov
        res = _gov.score_memories(limit=500, persist=True)
        return {"ok": True, "scored": res.get("scored", 0)}
    except Exception as exc:
        return {"ok": False, "scored": 0,
                "message": f"{type(exc).__name__}: {exc}"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-memory", action="store_true")
    ap.add_argument("--skip-knowledge", action="store_true")
    args = ap.parse_args()

    extra = ["--dry-run"] if args.dry_run else []
    rc = 0

    if not args.skip_memory:
        print("== 记忆 ==")
        res = _run("import_dsh_memory.py", *extra)
        print(f"  源 {res.get('source_total', '?')} 条 → 新增 {res.get('inserted', '?')}"
              f" / 更新 {res.get('updated', '?')} / 跳过 {res.get('skipped', '?')}")
        if not res.get("ok"):
            rc = 1
        elif not args.dry_run:
            # 导入只写 memories 表，向量要单独补。漏掉这步的后果很隐蔽：
            # 正文改了但向量还是旧的，语义检索照着上一条内容打分，
            # 而关键词分量照样命中，于是"看起来能查到、实际排得不对"。
            idx = _index_memory_vectors()
            print(f"  向量：补建 {idx.get('indexed', 0)} 条"
                  f"（待建 {idx.get('pending', 0)}）")
            if not idx.get("ok"):
                print(f"  错误：{idx.get('message')}")
                rc = 1
            # 治理分（importance/confidence/freshness）是存起来的列，不是每次
            # 现算的。召回直接读这三个列，所以它们一旦停止重算，排序就一直
            # 基于旧值——实测曾停摆 10 天，freshness 退化成 5 个常数值。
            sc = _rescore_governance()
            print(f"  治理分：重算 {sc.get('scored', 0)} 条")
            if not sc.get("ok"):
                print(f"  错误：{sc.get('message')}")
                rc = 1

    if not args.skip_knowledge:
        print("== 知识 ==")
        res = _run("import_dsh_knowledge.py", *extra)
        print(f"  文档 {res.get('docs_imported', '?')} 导 / {res.get('docs_unchanged', '?')} 未变"
              f" / 块 {res.get('chunks_written', '?')} 写 / 删 {res.get('chunks_deleted', '?')}"
              f" / 向量 {res.get('vectors_written', '?')}")
        if res.get("errors"):
            print(f"  错误：{res['errors']}")
            rc = 1

    if args.dry_run:
        print("（dry-run，未落库）")
        return rc

    # 同步完立刻自己验一遍：不一致要让调用方看见，而不是等下次查询发现少了东西
    print("== 校验 ==")
    from app import knowledge
    from app import db as _db

    drift = knowledge.drift()
    print(f"  知识：in_sync={drift.get('in_sync')} on_disk={drift.get('on_disk')}"
          f" in_db={drift.get('in_db')}")
    if not drift.get("in_sync"):
        print(f"  需导入 {len(drift.get('needs_import') or [])} 篇、"
              f"需重导 {len(drift.get('needs_reimport') or [])} 篇")
        rc = 1

    with _db.connect() as conn:
        mem = conn.execute(
            "SELECT COUNT(*) n FROM memories WHERE status='active'").fetchone()["n"]
        kn = conn.execute("SELECT COUNT(*) n FROM knowledge_docs").fetchone()["n"]
        ch = conn.execute("SELECT COUNT(*) n FROM knowledge_chunks").fetchone()["n"]
        vec = conn.execute("SELECT COUNT(*) n FROM knowledge_embeddings").fetchone()["n"]
    print(f"  库：memories(active)={mem} knowledge_docs={kn} chunks={ch} vectors={vec}")
    if ch != vec:
        print(f"  ⚠ 有 {abs(ch - vec)} 个块没有向量，检索会漏")
        rc = 1

    print("OK 两侧一致" if rc == 0 else "有不一致，见上")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
