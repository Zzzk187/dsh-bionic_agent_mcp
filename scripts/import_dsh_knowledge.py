"""Import dsh-memory's knowledge notes into the MCP knowledge tables.

`knowledge/` and `mine/` are the two places the assistant writes durable notes,
and until now they were reachable only from the PowerShell side: the MCP memory
store knew nothing about them, so a session driven through MCP could not answer
"what did I write about RAG chunking". This script makes them retrievable.

Not imported: `卷/`. Those four files are already in `memories`, one row per
entry, mirrored from `记忆.jsonl`; importing them here as well would put the
same text in two tables with two different lifetimes.

Idempotent: a document is keyed by its absolute `path`, and re-import only
rewrites rows whose `content_hash` changed. Safe to run on a schedule.

    python scripts/import_dsh_knowledge.py              # import + embed, changed only
    python scripts/import_dsh_knowledge.py --dry-run    # report, write nothing
    python scripts/import_dsh_knowledge.py --no-embed   # import text now, vector later
    python scripts/import_dsh_knowledge.py --status     # coverage report, no writes
"""
import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app import db, knowledge


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default="", help="只导入某一侧：knowledge / mine")
    ap.add_argument("--dry-run", action="store_true", help="只报告，不写库")
    ap.add_argument("--no-embed", action="store_true", help="先不建向量")
    ap.add_argument("--limit", type=int, default=0, help="最多处理几篇（调试用）")
    ap.add_argument("--status", action="store_true", help="只看索引概况")
    args = ap.parse_args()

    # [S0/P2-1] --dry-run / --status 只读：不建库、不执行任何 DDL。
    # 表不存在时下游查询返回空结果，不会凭空造出一个 agent.db。
    if not args.dry_run and not args.status:
        db.init_db()

    # [S0/P2-1] 库文件根本不存在时，--dry-run / --status 连连接都不开：
    # sqlite3.connect 对不存在的路径会创建空文件，仍算"凭空建库"。
    db_absent = not Path(db.DB_PATH).is_file()
    if db_absent and args.status:
        print(json.dumps({"ok": True, "status": "db_absent", "docs": 0},
                         ensure_ascii=False, indent=2))
        return 0
    if db_absent and args.dry_run:
        docs = knowledge.discover(args.root or None)
        print(json.dumps({"ok": True, "dry_run": True, "db_absent": True,
                          "docs_seen": len(docs)}, ensure_ascii=False, indent=2))
        return 0

    if args.status:
        print(json.dumps(knowledge.status(), ensure_ascii=False, indent=2))
        return 0

    result = knowledge.import_docs(
        root=args.root or None,
        dry_run=args.dry_run,
        embed=not args.no_embed,
        limit=args.limit or None,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))

    drift = knowledge.drift(root=args.root or None)
    if drift.get("needs_import") or drift.get("needs_reimport"):
        print(f"\n注意：还有 {len(drift['needs_import'])} 篇未导入、"
              f"{len(drift['needs_reimport'])} 篇内容已变，重跑本脚本即可。")
    if drift.get("missing_on_disk"):
        print(f"\n注意：{len(drift['missing_on_disk'])} 篇在库里但磁盘上已不存在"
              f"（只报告，未删除）：{drift['missing_on_disk'][:5]}")

    return 0 if not result.get("errors") else 1


if __name__ == "__main__":
    raise SystemExit(main())
