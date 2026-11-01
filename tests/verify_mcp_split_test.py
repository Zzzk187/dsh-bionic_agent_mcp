"""Verify the MCP server-split: core (21) + governance (45) instances.

Real import of ``app.mcp_server``; asserts the two FastMCP instances register
exactly the intended tool sets with no overlap. Run:
    python tests/verify_mcp_split_test.py
"""
import sys, asyncio
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.mcp_server as srv

CORE_EXPECTED = [
    "save_memory", "search_memories", "find_related_memories", "link_memories",
    "get_memory_links", "supersede_memory", "get_memory", "update_memory",
    "archive_memory", "restore_memory", "create_task", "list_tasks", "get_task",
    "save_stage_snapshot", "handoff_task", "resume_task", "complete_task",
    "list_events", "embedding_status", "index_memories", "semantic_search_memories",
]
GOV_EXPECTED = [
    "warmup_embeddings", "score_memories", "get_memory_governance_status",
    "protect_memory", "detect_memory_conflicts", "list_memory_conflicts",
    "review_memory_conflict", "supersede_memory_from_conflict",
    "archive_memory_governance", "restore_memory_governance",
    "get_governance_action", "rollback_governance_action", "set_review_status",
    "run_memory_maintenance", "plan_memory_cleanup", "review_governance_actions",
    "apply_governance_actions", "get_governance_run", "get_vector_consistency",
    "recall_memories", "get_memory_details", "get_related_memories",
    "record_memory_feedback", "classify_memory", "find_duplicate_memories",
    "create_memory_relation", "list_memory_relations", "get_memory_lifecycle",
    "consolidate_memory_candidates", "run_memory_consolidation",
    "get_memory_usage_stats", "spread_activation", "record_memory_activation",
    # Phase 6: feedback loop / snapshots / protection tiers / distillation.
    "summarize_memory_feedback", "record_recall",
    "create_db_snapshot", "list_db_snapshots", "restore_db_snapshot",
    "check_vector_consistency", "prune_db_snapshots",
    "assess_memory_protection", "summarize_protection",
    "find_distillation_candidates", "apply_distillation", "list_distillations",
]

PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {extra}")


async def main():
    core = {t.name for t in await srv.mcp.list_tools()}
    gov = {t.name for t in await srv.mcp_gov.list_tools()}
    check("core 数量=21", len(core) == 21, str(len(core)))
    check(f"governance 数量={len(GOV_EXPECTED)}", len(gov) == len(GOV_EXPECTED), str(len(gov)))
    check("core 与期望一致", core == set(CORE_EXPECTED),
          f"missing={set(CORE_EXPECTED)-core} extra={core-set(CORE_EXPECTED)}")
    check("governance 与期望一致", gov == set(GOV_EXPECTED),
          f"missing={set(GOV_EXPECTED)-gov} extra={gov-set(GOV_EXPECTED)}")
    check("core 与 governance 无重叠", not (core & gov), str(core & gov))
    check(f"合计={len(CORE_EXPECTED) + len(GOV_EXPECTED)}",
          len(core) + len(gov) == len(CORE_EXPECTED) + len(GOV_EXPECTED),
          str(len(core) + len(gov)))
    print(f"\nMCP 拆分验证通过 {PASS} 项，失败 {FAIL} 项")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
