"""Verify the MCP server-split: core (31) + governance (46) instances.

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
    # Phase 6 additions that belong on the DAILY instance: each one is a
    # routine upkeep action with short, obvious arguments, called as part of
    # normal memory work (set/clear a TTL, scan what has expired, backfill the
    # type column, resume or give up on a task). Requiring a second mount to
    # perform them would mean the common case pays the governance schema cost.
    # The list lagged behind the code since commit 9f946d4; it is the
    # expectation that was stale, not the registration.
    "set_memory_expiry", "scan_expired_memories", "run_expiry_policy",
    "get_resume_packet", "find_stuck_tasks", "fail_task",
    "backfill_memory_types",
    # Phase 7: the knowledge notes (knowledge/ + mine/) are read daily, in the
    # same session as the memories, and a search tool that needs a second mount
    # would simply not be used. Read-only, three tools, short arguments.
    "search_knowledge", "get_knowledge_doc", "knowledge_status",
]
GOV_EXPECTED = [
    "warmup_embeddings", "score_memories", "get_memory_governance_status",
    "protect_memory", "detect_memory_conflicts", "list_memory_conflicts",
    "review_memory_conflict", "supersede_memory_from_conflict",
    "archive_memory_governance", "restore_memory_governance",
    "get_governance_action", "rollback_governance_action",
    # 待审核保留是带排除语义的标记；set 与 release 必须成对存在，
    # 否则 mark_review 就是单向门（记忆被藏起来但没法放回来）。
    "set_review_status", "release_review_status",
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
    check(f"core 数量={len(CORE_EXPECTED)}", len(core) == len(CORE_EXPECTED), str(len(core)))
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
