"""MCP 工具层冒烟：把 49 个「零测试调用」的工具各跑一遍，卡住返回契约。

为什么需要这个文件
------------------
`tests/verify_mcp_split_test.py` 只核对 **名字清单**（36+49=85），它证明"工具还在"，
但不证明"调用还能跑"。实测按别名统计：85 个工具里 **49 个**在 `tests/` 里一次都没被
调用过（58%）。而 `docs/ARCHITECTURE_OPTIMIZATION_TASKS.md:244` 把"补工具冒烟"列为
OPT-03（把 67 处 SQL 从工具函数下沉到 stores/）的**硬前置**——没有这层安全网，下沉
SQL 等于在零测试的代码上动手术。

本文件只做一件事：**用真实参数调用每个工具，断言它不抛异常、返回类型对、关键字段在**。
不追业务细节（那些是各域自己的测试），只守"契约没有被改坏"。

断言依据是跑出来的真实返回，不是猜的：
- 返回类型**不统一**：`list_tasks` / `list_events` / `search_memories` / `get_memory_links`
  / `find_related_memories` 返回 `list`；其余多数返回 `dict`；便携包/恢复那批返回
  `_tool_result` 外壳（`{"ok","operation","result","warnings","errors"}`）。
- `get_knowledge_doc` 在空库上必然 `{"ok": False, "error": "文档不存在：..."}`——空知识
  库是合法状态，所以这里断言的是"结构对"而不是"成功"。

隔离
----
`import _env_guard` 必须在任何 `app.*` 之前（`app/db.py` 的 `DB_PATH` 在模块导入时定死）。
闸门把库指到 `var/test_db/<label>-<pid>-<uuid>/agent.db`——**必须在项目根内**，因为
`app/portable_backup.py` 的 `_resolve_package_dir()` 要求包目录 `relative_to(项目根)`，
放系统 temp 会让包相关工具直接报"包目录必须位于项目根内"。
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import _env_guard  # noqa: E402,F401  OPT-04a：数据库隔离闸门（必须在任何 app.* 之前）

from app import db  # noqa: E402
from app import mcp_server as mcp  # noqa: E402

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return ok


def call(tool, *args, **kwargs):
    """调用工具，把异常转成可断言的元组，避免一个炸掉整份套件。"""
    fn = getattr(mcp, tool, None)
    if fn is None:
        return False, None, f"工具不存在：{tool}"
    try:
        return True, fn(*args, **kwargs), ""
    except Exception as exc:  # noqa: BLE001 —— 冒烟要的是"会不会炸"，不是异常类型
        return False, None, f"{type(exc).__name__}: {exc}"


def smoke(tool, *args, **kwargs):
    """统一断言：不抛异常 + 返回类型属于 {dict, list} + 可 JSON 序列化。

    这三个是所有 85 个工具的共同底线（FastMCP 要求返回值可序列化，
    所以"能 json.dumps"本身就是一条真契约）。
    """
    ok, out, err = call(tool, *args, **kwargs)
    if not ok:
        return check(f"{tool} 可调用", False, err), None
    if not isinstance(out, (dict, list)):
        return check(f"{tool} 可调用", False, f"返回类型 {type(out).__name__}"), None
    try:
        json.dumps(out, ensure_ascii=False, default=str)
    except (TypeError, ValueError) as exc:
        return check(f"{tool} 可调用", False, f"不可序列化：{exc}"), None
    return True, out


def has(d, *keys):
    """dict 里同时有这些键（工具返回契约的最小断言）。"""
    return isinstance(d, dict) and all(k in d for k in keys)


def shell(d):
    """`_tool_result` 外壳：{"ok","operation","result","warnings","errors"}。"""
    return has(d, "ok", "operation", "result", "warnings", "errors") and d["operation"] != ""


def main():
    db.init_db()

    # ---------- 0. 前置：造 3 条记忆（蒸馏要求 ≥3 条来源） ----------
    ids = []
    for i, text in enumerate(("甲 冒烟 记忆 关于 缓存", "乙 冒烟 记忆 关于 代理", "丙 冒烟 记忆 关于 重启")):
        ok, out = smoke("save_memory", content=text, category="test", source="smoke",
                        confidence=0.9, tags=[f"t{i}"])
        if ok and isinstance(out, dict) and "id" in out:
            ids.append(out["id"])
    check("前置：造出 3 条记忆", len(ids) == 3, f"ids={ids}")
    if len(ids) < 3:
        return report()

    m1, m2, m3 = ids

    # ---------- 1. 记忆读写 ----------
    ok, out = smoke("get_memory", m1)
    check("get_memory 返回该条", ok and has(out, "id", "content") and out["id"] == m1,
          f"id={out.get('id') if isinstance(out, dict) else out}")

    ok, out = smoke("update_memory", m1, content="甲 冒烟 记忆 已改写", tags=["改"])
    check("update_memory 内容与标签都变了",
          ok and isinstance(out, dict) and out.get("content") == "甲 冒烟 记忆 已改写"
          and out.get("tags") == ["改"], str(out)[:120] if isinstance(out, dict) else str(out))

    ok, out = smoke("archive_memory", m2)
    check("archive_memory 置为 archived",
          ok and isinstance(out, dict) and out.get("status") == "archived",
          str(out)[:100] if isinstance(out, dict) else str(out))

    ok, out = smoke("restore_memory", m2)
    check("restore_memory 回到 active",
          ok and isinstance(out, dict) and out.get("status") == "active",
          str(out)[:100] if isinstance(out, dict) else str(out))

    ok, out = smoke("supersede_memory", m3, "丙 冒烟 记忆 新版本")
    check("supersede_memory 新条 supersedes_id 指回旧条",
          ok and isinstance(out, dict) and out.get("supersedes_id") == m3,
          f"supersedes_id={out.get('supersedes_id') if isinstance(out, dict) else out}")
    m_new = out["id"] if isinstance(out, dict) and "id" in out else None

    # ---------- 2. 检索 ----------
    ok, out = smoke("search_memories", query="冒烟", limit=5)
    check("search_memories 返回 list", ok and isinstance(out, list), f"{type(out).__name__}")

    ok, out = smoke("find_related_memories", query="缓存", limit=5)
    check("find_related_memories 返回 list", ok and isinstance(out, list),
          f"{type(out).__name__}")

    ok, out = smoke("search_knowledge", query="记忆", limit=3)
    check("search_knowledge 返回 dict 且含 results 列表",
          ok and has(out, "ok", "results") and isinstance(out["results"], list),
          str(out)[:120] if isinstance(out, dict) else str(out))

    # ★ 知识库**不受** BIONIC_DB_PATH 隔离：文档来自磁盘上的 dsh-memory 目录，
    # 只有分块/向量在库里。所以沙箱里 doc_id=1 仍能取到真实文档（实测返回
    # path=C:\Users\xing\.dsh\dsh-memory\knowledge\Agent\...）。两条都测：
    # 存在的必须成功，不存在的必须给出 ok=False + error。
    ok, out = smoke("get_knowledge_doc", doc_id=1, max_chars=200)
    check("get_knowledge_doc 取到文档主体",
          ok and has(out, "ok", "doc") and out["ok"] is True
          and isinstance(out["doc"], dict),
          str(out)[:140] if isinstance(out, dict) else str(out))

    ok, out = smoke("get_knowledge_doc", doc_id=99999999, max_chars=200)
    check("get_knowledge_doc 不存在的 doc_id 给 ok=False + error",
          ok and has(out, "ok", "error") and out["ok"] is False,
          str(out)[:120] if isinstance(out, dict) else str(out))

    ok, out = smoke("knowledge_status")
    check("knowledge_status 返回计数结构",
          ok and has(out, "ok", "docs", "chunks", "vectors"),
          str(out)[:120] if isinstance(out, dict) else str(out))

    ok, out = smoke("recall_all", query="冒烟", limit=3)
    check("recall_all 同时给 memories 与 knowledge",
          ok and has(out, "ok", "memories", "knowledge", "counts")
          and isinstance(out["counts"], dict),
          str(out)[:140] if isinstance(out, dict) else str(out))

    # ---------- 3. 关系 ----------
    ok, out = smoke("link_memories", m1, m2, relation="related", note="冒烟关系")
    check("link_memories 返回 link_id 与两端 id",
          ok and has(out, "ok", "link_id", "source_memory_id", "target_memory_id"),
          str(out)[:120] if isinstance(out, dict) else str(out))

    ok, out = smoke("get_memory_links", m1)
    check("get_memory_links 返回 list", ok and isinstance(out, list),
          f"{type(out).__name__}")

    ok, out = smoke("list_memory_relations", m1)
    check("list_memory_relations 返回 relations + count",
          ok and has(out, "ok", "relations", "count") and out["count"] >= 1,
          f"count={out.get('count') if isinstance(out, dict) else out}")

    ok, out = smoke("get_related_memories", m1)
    check("get_related_memories 返回 nodes + edges",
          ok and has(out, "ok", "nodes", "edges")
          and isinstance(out["nodes"], list) and isinstance(out["edges"], list),
          str(out)[:120] if isinstance(out, dict) else str(out))

    # ---------- 4. 任务 ----------
    ok, out = smoke("list_tasks")
    check("list_tasks 返回 list", ok and isinstance(out, list), f"{type(out).__name__}")

    ok, out = smoke("create_task", title="冒烟任务", goal="验证工具可调用")
    tid = out["id"] if ok and isinstance(out, dict) and "id" in out else None
    check("create_task 返回 id 与 plan", ok and tid is not None and has(out, "plan"),
          f"id={tid}")

    if tid is not None:
        ok, out = smoke("get_task", tid)
        check("get_task 返回该任务", ok and isinstance(out, dict) and out.get("id") == tid,
              f"id={out.get('id') if isinstance(out, dict) else out}")

        ok, out = smoke("save_stage_snapshot", tid, summary="冒烟阶段快照")
        check("save_stage_snapshot 返回快照行",
              ok and has(out, "id", "task_id", "stage", "summary"),
              str(out)[:120] if isinstance(out, dict) else str(out))

        ok, out = smoke("handoff_task", tid, summary="冒烟交接")
        check("handoff_task 返回 snapshot + next_stage",
              ok and has(out, "ok", "snapshot", "status", "next_stage")
              and out["next_stage"] == 2,
              f"next_stage={out.get('next_stage') if isinstance(out, dict) else out}")

    # ---------- 5. 事件 ----------
    ok, out = smoke("list_events", limit=5)
    check("list_events 返回 list", ok and isinstance(out, list), f"{type(out).__name__}")

    # ---------- 6. 向量 / 嵌入 ----------
    ok, out = smoke("embedding_status")
    check("embedding_status 返回模型与计数",
          ok and has(out, "model", "total_active_memories", "indexed_active_memories"),
          str(out)[:140] if isinstance(out, dict) else str(out))

    ok, out = smoke("warmup_embeddings")
    check("warmup_embeddings 返回 status", ok and has(out, "ok", "status"),
          str(out)[:120] if isinstance(out, dict) else str(out))

    ok, out = smoke("index_memories", batch_size=8)
    check("index_memories 返回 indexed_this_run",
          ok and has(out, "ok", "indexed_this_run"),
          str(out)[:120] if isinstance(out, dict) else str(out))

    ok, out = smoke("get_vector_consistency")
    check("get_vector_consistency 返回一致性计数",
          ok and has(out, "ok", "active_without_vector", "active_stale_vector"),
          str(out)[:120] if isinstance(out, dict) else str(out))

    ok, out = smoke("check_vector_consistency")
    check("check_vector_consistency 多给 needs_reindex",
          ok and has(out, "ok", "needs_reindex", "missing_vectors", "stale_vectors"),
          str(out)[:120] if isinstance(out, dict) else str(out))

    # ---------- 7. 评分 / 治理 ----------
    ok, out = smoke("score_memories", limit=5, persist=False)
    check("score_memories 返回 results 且未落库",
          ok and has(out, "ok", "scored", "results", "persisted")
          and out["persisted"] is False,
          f"scored={out.get('scored') if isinstance(out, dict) else out}")

    ok, out = smoke("get_memory_governance_status", m1)
    check("get_memory_governance_status 返回存储与保护态",
          ok and has(out, "ok", "memory_id", "storage_status", "protected"),
          str(out)[:120] if isinstance(out, dict) else str(out))

    ok, out = smoke("archive_memory_governance", m_new or m1, reason="冒烟")
    check("archive_memory_governance 返回 status",
          ok and has(out, "ok", "memory_id", "status"),
          str(out)[:120] if isinstance(out, dict) else str(out))

    ok, out = smoke("restore_memory_governance", m_new or m1, reason="冒烟")
    check("restore_memory_governance 回到 active",
          ok and has(out, "ok", "memory_id", "status") and out["status"] == "active",
          str(out)[:120] if isinstance(out, dict) else str(out))

    # 治理动作 id 是库内自增，**不保证从 1 开始**（沙箱复制的是生产库，历史 id 不连续），
    # 所以先从库里取一个真实存在的 id 再断言正面路径；负向路径另外测。
    gid = None
    try:
        with db.connect() as _c:
            _row = _c.execute("SELECT MIN(id) AS i FROM governance_actions").fetchone()
        gid = _row["i"] if _row else None
    except Exception as exc:  # noqa: BLE001
        check("取治理动作 id 失败", False, f"{type(exc).__name__}: {exc}")

    if gid is not None:
        ok, out = smoke("get_governance_action", gid)
        check("get_governance_action 返回动作行",
              ok and has(out, "id", "action_type", "memory_id", "old_value_json"),
              str(out)[:120] if isinstance(out, dict) else str(out))

    ok, out = smoke("get_governance_action", 99999999)
    check("get_governance_action 不存在时 ok=False + message",
          ok and has(out, "ok", "message") and out["ok"] is False,
          str(out)[:120] if isinstance(out, dict) else str(out))

    ok, out = smoke("plan_memory_cleanup", limit=5, dry_run=True)
    check("plan_memory_cleanup 返回 stats + plan",
          ok and has(out, "ok", "mode", "stats", "plan"),
          str(out)[:120] if isinstance(out, dict) else str(out))

    ok, out = smoke("run_memory_maintenance", mode="dry_run", limit=5)
    check("run_memory_maintenance 返回 stats",
          ok and has(out, "ok", "mode", "stats"),
          str(out)[:120] if isinstance(out, dict) else str(out))

    # 不存在的 run 是合法输入：断言错误结构而不是抛异常
    ok, out = smoke("get_governance_run", 999999)
    check("get_governance_run 不存在时 ok=False + message",
          ok and has(out, "ok", "message") and out["ok"] is False,
          str(out)[:120] if isinstance(out, dict) else str(out))

    ok, out = smoke("supersede_memory_from_conflict", 999999, new_content="x")
    check("supersede_memory_from_conflict 不存在冲突时 ok=False",
          ok and has(out, "ok", "message") and out["ok"] is False,
          str(out)[:120] if isinstance(out, dict) else str(out))

    ok, out = smoke("find_duplicate_memories")
    check("find_duplicate_memories 返回 duplicate_count + duplicates",
          ok and has(out, "ok", "duplicate_count", "duplicates"),
          str(out)[:120] if isinstance(out, dict) else str(out))

    # ---------- 8. 生命周期 / 用法统计 ----------
    ok, out = smoke("get_memory_lifecycle", m1)
    check("get_memory_lifecycle 返回状态与合法转移",
          ok and has(out, "ok", "lifecycle_state", "allowed_transitions")
          and isinstance(out["allowed_transitions"], list),
          str(out)[:120] if isinstance(out, dict) else str(out))

    ok, out = smoke("get_memory_details", m1)
    check("get_memory_details 返回 memory 主体",
          ok and has(out, "ok", "memory") and isinstance(out["memory"], dict),
          str(out)[:120] if isinstance(out, dict) else str(out))

    ok, out = smoke("get_memory_usage_stats", 0)
    check("get_memory_usage_stats 返回 usage 列表",
          ok and has(out, "ok", "usage", "count") and isinstance(out["usage"], list),
          str(out)[:120] if isinstance(out, dict) else str(out))

    ok, out = smoke("classify_memory", memory_id=m1)
    check("classify_memory 返回 memory_type + confidence",
          ok and has(out, "memory_type", "confidence", "reasons"),
          str(out)[:120] if isinstance(out, dict) else str(out))

    # ---------- 9. 激活扩散 ----------
    ok, out = smoke("spread_activation", memory_ids=[m1])
    check("spread_activation 返回 activation + seeds",
          ok and has(out, "ok", "activation", "seeds")
          and isinstance(out["activation"], dict),
          str(out)[:120] if isinstance(out, dict) else str(out))

    ok, out = smoke("record_memory_activation", m1, reason="冒烟")
    check("record_memory_activation 返回 ok + memory_id",
          ok and has(out, "ok", "memory_id"),
          str(out)[:120] if isinstance(out, dict) else str(out))

    # ---------- 10. 巩固 / 蒸馏 ----------
    ok, out = smoke("consolidate_memory_candidates", persist=False, limit=5)
    check("consolidate_memory_candidates 返回 candidates（不落库）",
          ok and has(out, "ok", "persisted", "candidates") and out["persisted"] is False,
          str(out)[:120] if isinstance(out, dict) else str(out))

    ok, out = smoke("run_memory_consolidation", dry_run=True)
    check("run_memory_consolidation 返回 planned + candidates",
          ok and has(out, "ok", "dry_run", "planned", "candidates"),
          str(out)[:120] if isinstance(out, dict) else str(out))

    ok, out = smoke("apply_distillation", content="冒烟蒸馏结论",
                    source_memory_ids=[m1, m2, m3])
    check("apply_distillation 3 条来源时成功并挂 derived_from",
          ok and has(out, "ok", "memory_id", "relation") and out["relation"] == "derived_from",
          str(out)[:140] if isinstance(out, dict) else str(out))

    # 少于 3 条必须被拒（README 第 8 条刻意设计），这里顺带守一下
    ok, out = smoke("apply_distillation", content="冒烟蒸馏 单来源",
                    source_memory_ids=[m1])
    check("apply_distillation 少于 3 条来源被拒",
          ok and has(out, "ok", "message") and out["ok"] is False,
          str(out)[:140] if isinstance(out, dict) else str(out))

    # ---------- 11. 快照 ----------
    ok, out = smoke("create_db_snapshot", label="冒烟快照")
    sid = out["snapshot_id"] if ok and isinstance(out, dict) else None
    check("create_db_snapshot 返回 snapshot_id + sha256 + bytes",
          ok and sid is not None and has(out, "sha256", "bytes", "path"),
          f"id={sid}")

    ok, out = smoke("list_db_snapshots")
    check("list_db_snapshots 返回 snapshots 列表",
          ok and has(out, "ok", "snapshots") and isinstance(out["snapshots"], list),
          str(out)[:120] if isinstance(out, dict) else str(out))

    if sid is not None:
        ok, out = smoke("restore_db_snapshot", sid)
        check("restore_db_snapshot 返回 restored_tables",
              ok and has(out, "ok", "snapshot_id", "restored_tables")
              and isinstance(out["restored_tables"], dict),
              str(out)[:120] if isinstance(out, dict) else str(out))

    ok, out = smoke("prune_db_snapshots", keep=10)
    check("prune_db_snapshots 返回 removed + kept",
          ok and has(out, "ok", "removed", "kept"),
          str(out)[:120] if isinstance(out, dict) else str(out))

    # ---------- 12. 便携包（受管目录，只读/删除不存在包） ----------
    ok, out = smoke("list_memory_packages")
    check("list_memory_packages 返回外壳 + packages",
          ok and shell(out) and isinstance(out["result"], dict)
          and "packages" in out["result"],
          str(out)[:160] if isinstance(out, dict) else str(out))

    missing = "bionic-memory-smoke-20260101T000000-deadbeef"
    ok, out = smoke("inspect_memory_package", missing)
    check("inspect_memory_package 不存在时走外壳且 ok=False",
          ok and shell(out) and out["ok"] is False,
          str(out)[:160] if isinstance(out, dict) else str(out))

    ok, out = smoke("validate_memory_package", missing)
    check("validate_memory_package 不存在时走外壳且 ok=False",
          ok and shell(out) and out["ok"] is False,
          str(out)[:160] if isinstance(out, dict) else str(out))

    ok, out = smoke("delete_memory_package", missing)
    check("delete_memory_package 不存在时走外壳且 ok=False",
          ok and shell(out) and out["ok"] is False,
          str(out)[:160] if isinstance(out, dict) else str(out))

    # ---------- 13. 自检：本文件必须覆盖那 49 个零调用工具 ----------
    check("本文件覆盖了 49 个零覆盖工具", *verify_coverage())
    return report()


def verify_coverage():
    """静态核对：49 个零覆盖工具在本文件里都有调用点。

    这条断言是为了防止以后有人"精简"这个文件时静默丢掉几个工具——
    丢掉的工具会重新变回零覆盖，而没人会收到通知。
    """
    import ast

    expect = set("""search_memories find_related_memories link_memories get_memory_links
    supersede_memory get_memory update_memory restore_memory list_tasks get_task handoff_task
    list_events embedding_status index_memories search_knowledge get_knowledge_doc
    knowledge_status recall_all list_memory_packages inspect_memory_package
    validate_memory_package warmup_embeddings score_memories get_memory_governance_status
    supersede_memory_from_conflict archive_memory_governance restore_memory_governance
    get_governance_action run_memory_maintenance plan_memory_cleanup get_governance_run
    get_vector_consistency get_memory_details get_related_memories classify_memory
    find_duplicate_memories list_memory_relations get_memory_lifecycle
    consolidate_memory_candidates run_memory_consolidation get_memory_usage_stats
    spread_activation record_memory_activation create_db_snapshot restore_db_snapshot
    check_vector_consistency prune_db_snapshots apply_distillation
    delete_memory_package""".split())
    src = Path(__file__).read_text(encoding="utf-8")
    called = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id == "smoke" and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    called.add(first.value)
    missing = sorted(expect - called)
    return (not missing, f"缺失={missing}" if missing else f"{len(expect)} 个全在")


def report():
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = len(RESULTS) - passed
    print(f"\n=== MCP 工具冒烟 {passed}/{len(RESULTS)} 通过，失败 {failed} ===")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
