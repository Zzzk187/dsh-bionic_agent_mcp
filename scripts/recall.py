"""统一检索入口：一条命令同时查记忆和知识。

这是"两套系统合一"落地的地方。以前有两个入口：
  - `~/.dsh/dsh-memory/查.ps1`   走 PowerShell 词法（慢，888ms，认死字面）
  - MCP 的 recall_memories / search_knowledge（快，但得先想起调工具）

现在只有一个：记忆走 `memory_lifecycle.recall_memories`（词法 0.43 + 语义 0.12
的融合），知识走 `knowledge.search`（关键词主导 + 向量过地板才加分）。
两边都是"关键词主导、向量补漏"，所以同一句话在两个库里不会被排成两种逻辑。

用法：
    python scripts/recall.py "唤醒预算被截断"
    python scripts/recall.py "刷B站" --limit 3 --full
    python scripts/recall.py "file-state-cache" --no-knowledge   # 只查记忆
    python scripts/recall.py --eval                              # 回归自测

输出刻意沿用 `[A01]` / `[1] 2026-11-07-xxx.md` 的老格式：
`查.ps1 -Eval` 和 `记忆检索.ps1` 的正则都靠它，改了格式那些自测就瞎了。
"""
from __future__ import annotations

import argparse
import io
import sys
import time as _time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def _anchor(m: dict) -> str:
    """记忆的对外 ID：优先 src:A01 这种源锚点，没有就退回库内 id。"""
    for t in (m.get("tags") or []):
        t = str(t)
        if t.startswith("src:"):
            return t[4:]
    return str(m.get("id"))


def _title(m: dict) -> str:
    first = (m.get("content") or "").split("\n", 1)[0]
    return first.lstrip("#").strip() or "(无标题)"


def _body(m: dict) -> str:
    parts = (m.get("content") or "").split("\n", 1)
    return parts[1].strip() if len(parts) > 1 else ""


def search_memories(query: str, limit: int, record_access: bool = True) -> list[dict]:
    from app import memory_lifecycle as life

    # record_access=False 让自测可重复；正常查询要记（frequency 分量靠它，
    # 也让"哪条记忆真被用过"有据可查）。
    return life.recall_memories(query, top_k=limit, record_access=record_access)


def search_knowledge(query: str, limit: int) -> dict:
    from app import knowledge

    return knowledge.search(query, limit=limit)


def _log_query(scope: str, query: str, hits: int, best: str, ms: int, code: int) -> None:
    """往本地 `var/检索日志.tsv` 追加一行，格式必须是 7 列 tab。

    `bin/检索统计.ps1` 按 tab 切成 7 段来统计，少一列会静默丢掉整行。
    这个日志在 2026-11-08 00:10 断过一次：`查.ps1` 改成转调本脚本后，
    写日志的只剩旧的 `bin/记忆检索.ps1` / `bin/知识检索.ps1`，而统一入口
    从此一条都不记——于是"最近 7 天查询 N 次"那一项永远在骗人。
    写失败一律忽略：检索不能因为记不了账就失败。
    """
    try:
        from datetime import datetime as _dt
        from app.knowledge import DSH_MEMORY_ROOT
        log = Path(DSH_MEMORY_ROOT) / "var" / "检索日志.tsv"
        log.parent.mkdir(parents=True, exist_ok=True)
        best = (best or "(无)").replace("\t", " ").replace("\n", " ")[:120]
        q = (query or "").replace("\t", " ").replace("\n", " ")[:160]
        line = "\t".join([_dt.now().isoformat(timespec="seconds"),
                          scope, q, str(hits), best, str(ms), str(code)])
        with io.open(log, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description="统一检索：记忆 + 知识")
    ap.add_argument("query", nargs="?", default="")
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--full", action="store_true", help="打印正文而不是摘要")
    ap.add_argument("--no-memory", action="store_true")
    ap.add_argument("--no-knowledge", action="store_true")
    ap.add_argument("--eval", action="store_true", help="回归自测")
    args = ap.parse_args()

    if args.eval:
        # 复用 tests/lexical_test.py，免得两处各写一套基准
        import subprocess

        test = PROJECT_ROOT / "tests" / "lexical_test.py"
        return subprocess.run([sys.executable, "-X", "utf8", str(test)]).returncode

    query = (args.query or "").strip()
    if not query:
        ap.print_help()
        return 1

    print(f"== 查「{query}」==")

    if not args.no_memory:
        print("--- 记忆 ---")
        _t0 = _time.time()
        hits = search_memories(query, args.limit)
        _best = _anchor(hits[0]) if hits else "(无)"
        _log_query("记忆", query, len(hits), str(_best),
                   int((_time.time() - _t0) * 1000), 0)
        if not hits:
            print("  （无命中。试试更短的关键词）")
        for h in hits:
            print(f"  [{_anchor(h)}] {_title(h)}")
            if args.full:
                body = _body(h)
                if body:
                    print("      " + body.replace("\n", "\n      "))
            else:
                tags = ",".join(str(t) for t in (h.get("tags") or []))
                # recall_memories 的内部字段一律下划线前缀，分数是 _score 不是 score
                score = h.get("score", h.get("_score", 0.0)) or 0.0
                print(f"      tags={tags} score={score:.3f}")

    if not args.no_knowledge:
        print("--- 知识 ---")
        _t1 = _time.time()
        res = search_knowledge(query, args.limit)
        items = res.get("results") or res.get("hits") or []
        _b1 = (items[0].get("source") or "(无)") if items else "(无)"
        if items and items[0].get("heading"):
            _b1 = f"{_b1}: {items[0]['heading']}"
        _log_query("knowledge", query, len(items), str(_b1),
                   int((_time.time() - _t1) * 1000), 0)
        if not items:
            print("  （无命中）")
        for it in items:
            # 知识命中可能是一篇文档的某个段落，rel_path 才是能打开的位置
            # knowledge.search 返回 source（相对 knowledge/ 或 mine/ 的路径），
            # 没有 rel_path 这个键
            path = it.get("source") or it.get("rel_path") or "?"
            if it.get("heading"):
                path = f"{path}  § {it['heading']}"
            score = it.get("score", 0)
            print(f"  [{it.get('doc_id', '?')}] {path}  score={score:.2f}")
            if args.full:
                text = (it.get("snippet") or it.get("text") or "").strip()
                if text:
                    print("      " + text.replace("\n", "\n      "))
            else:
                head = (it.get("snippet") or it.get("text") or "").strip().replace("\n", " ")[:90]
                if head:
                    print(f"      {head}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
