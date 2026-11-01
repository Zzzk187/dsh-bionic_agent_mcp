"""Conflict triage: does the rule engine stop calling changed values duplicates?

The failure this guards against is silent and expensive: two memories that state
the same fact with a different number get classified `duplicate` and recommend
`archive_duplicate`. Approving that loses a value that really existed.

So the tests check three things:
1. Numeric mismatches on near-identical text become `value_conflict`, not
   `duplicate`, and never recommend archiving.
2. This must not swallow real duplicates -- byte-identical text stays duplicate.
3. The rule must not fire when it has nothing to compare (no numbers on one
   side), otherwise it becomes a blanket "never dedupe" by accident.
"""
import os
import sys
import tempfile
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="bionic-conflict-test-")
os.environ["BIONIC_DB_PATH"] = str(Path(TMP) / "agent.db")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402
from app import mcp_server as mcp  # noqa: E402
from app.governance_conflicts import (  # noqa: E402
    classify_pair, extract_values, value_skeleton, needs_review,
    lexical_similarity,
)
from app.governance_rules import load_rules  # noqa: E402

RESULTS = []
RULES = load_rules()


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return ok


def row(content, rid=1, category="环境", tags="[]", created="2026-10-20"):
    return {"id": rid, "content": content, "category": category,
            "tags_json": tags, "created_at": created}


def classify(a, b, sim=0.95):
    return classify_pair(a, b, sim, RULES)


def fresh():
    db.init_db()
    with db.transaction() as conn:
        for t in ("memory_embeddings", "memory_links", "memory_feedback",
                  "memory_conflicts", "consolidation_candidates", "governance_actions",
                  "governance_runs", "snapshots", "tasks", "memories", "events"):
            conn.execute(f"DELETE FROM {t}")


def test_value_extraction():
    print("\n=== 数值提取 ===")
    check("端口号", extract_values("端口是 59647") == ["59647"],
          str(extract_values("端口是 59647")))
    check("版本号拆成两段", extract_values("版本 0.2.1") == ["0.2", "1"],
          str(extract_values("版本 0.2.1")))
    check("多个值去重保序", extract_values("8899 和 59647 还有 8899") == ["8899", "59647"],
          str(extract_values("8899 和 59647 还有 8899")))
    check("没有数字返回空", extract_values("没有数字") == [], "[]")
    check("骨架抹掉数字", value_skeleton("端口 8899") == "端口 #",
          value_skeleton("端口 8899"))


def test_value_conflict_detected():
    print("\n=== 同骨架不同取值 -> value_conflict（不是 duplicate）===")
    a = row("DSH 桌面版每次重启端口都会变，这次是 59647", 1)
    b = row("DSH 桌面版每次重启端口都会变，这次是 51366", 2)
    v = classify(a, b)
    check("判定为 value_conflict", v and v["conflict_type"] == "value_conflict",
          (v or {}).get("conflict_type"))
    check("建议两条都保留", v and v["recommendation"] == "keep_both_time_note",
          (v or {}).get("recommendation"))
    # Assert on the machine-readable `recommendation`, not on whether the prose
    # happens to contain the word "归档" -- the hint legitimately says
    # "不要归档", which a substring check reads as a failure.
    check("recommendation 不是归档动作",
          v and v["recommendation"] == "keep_both_time_note",
          (v or {}).get("recommendation"))
    check("recommendation 里没有任何 archive 类取值",
          v and not str(v["recommendation"]).lower().startswith("archive"),
          (v or {}).get("recommendation"))
    check("理由里点出两边取值",
          v and ("59647" in v["reason"] and "51366" in v["reason"]),
          (v or {}).get("reason", "")[:70])
    check("进人工审核", v and needs_review(v), str(v and needs_review(v)))

    v2 = classify(row("项目用 Python 3.11", 3), row("项目用 Python 3.12", 4))
    check("版本号不同也是 value_conflict", v2 and v2["conflict_type"] == "value_conflict",
          (v2 or {}).get("conflict_type"))


def test_real_duplicates_still_caught():
    print("\n=== 真重复不能被新规则放过 ===")
    same = "用户偏好：技术文档和交付物一律用中文撰写"
    v = classify(row(same, 1), row(same, 2))
    check("逐字相同仍是 duplicate", v and v["conflict_type"] == "duplicate",
          (v or {}).get("conflict_type"))
    check("仍然建议归档", v and v["recommendation"] == "archive_duplicate",
          (v or {}).get("recommendation"))

    v2 = classify(row("项目使用 SQLite 存储", 3), row("项目使用 SQLite 存储。", 4))
    check("仅差标点仍算重复", v2 and v2["conflict_type"] == "duplicate",
          (v2 or {}).get("conflict_type"))


def test_no_numbers_no_rule():
    print("\n=== 一边没有数字时不启用该规则（否则等于永不判重）===")
    v = classify(row("用户喜欢咖啡", 1), row("用户喜欢咖啡 但也要茶", 2))
    check("无数字差异 -> 仍走原逻辑", v and v["conflict_type"] != "value_conflict",
          (v or {}).get("conflict_type"))
    values = extract_values("用户喜欢咖啡")
    check("确实抽不到数字", values == [], str(values))


def test_skeleton_threshold():
    print("\n=== 骨架差异过大时不算取值矛盾（是不同的话，不是同一个数的两种取值）===")
    # Requirements for this case to actually exercise the threshold:
    #   1. both sides contain numbers, and the numbers DIFFER (otherwise the
    #      rule never even considers the pair and the threshold is untested);
    #   2. the skeletons are lexically far apart.
    # An earlier version used the same numbers on both sides, so the rule
    # short-circuited before reaching the threshold and the case stayed green
    # even with the threshold disabled.
    a = row("端口 8899 是管理面板", 1)
    b = row("今天下雨了所以 8898 很奇怪", 2)
    check("两边数字不同（规则会考虑这一对）",
          set(extract_values(a["content"])) != set(extract_values(b["content"])),
          f"{extract_values(a['content'])} vs {extract_values(b['content'])}")
    sk_a, sk_b = value_skeleton(a["content"]), value_skeleton(b["content"])
    check("两条骨架确实不够像（阈值该拦住）",
          lexical_similarity(sk_a, sk_b) < 0.80,
          f"{lexical_similarity(sk_a, sk_b):.3f}")
    # `sim=0.95` stands in for the real-world case where vector similarity is
    # high (same topic) while the literal claims differ.
    v = classify(a, b, sim=0.95)
    check("骨架差太多不判 value_conflict",
          v is None or v["conflict_type"] != "value_conflict",
          (v or {}).get("conflict_type"))


def test_end_to_end_through_mcp():
    print("\n=== 端到端：走 MCP 工具面（不是直接调模块函数）===")
    fresh()
    m1 = mcp.save_memory(content="DSH 桌面版重启后端口是 59647", category="环境")
    m2 = mcp.save_memory(content="DSH 桌面版重启后端口是 51366", category="环境")
    with db.transaction() as conn:
        conn.execute("UPDATE memories SET tags_json='[\"dsh\"]'")
    res = mcp.detect_memory_conflicts(persist=False)
    check("检测跑通", res.get("ok") is True, str(res.get("scanned")))
    types = [f["conflict_type"] for f in res.get("findings") or []]
    hit = [f for f in (res.get("findings") or [])
           if {f["memory_id"], f["candidate_memory_id"]} == {m1["id"], m2["id"]}]
    check("这两条被配对并判定", len(hit) == 1, f"findings={types}")
    if hit:
        check("类型是 value_conflict", hit[0]["conflict_type"] == "value_conflict",
              hit[0]["conflict_type"])
        check("计数进 needs_review", hit[0]["needs_review"] is True, "True")

    # 归档候选绝不该包含它
    with db.connect() as conn:
        row2 = conn.execute("SELECT * FROM memories WHERE id=?", (m2["id"],)).fetchone()
    check("记忆仍在库里（没被自动归档）", row2 is not None and row2["status"] == "active",
          str(row2["status"]))


def test_values_do_not_break_persistence():
    print("\n=== 落库：value_conflict 能写进 memory_conflicts 并去重 ===")
    fresh()
    a = mcp.save_memory(content="当前版本 0.2.1", category="环境")
    b = mcp.save_memory(content="当前版本 0.2.5", category="环境")
    with db.transaction() as conn:
        conn.execute("UPDATE memories SET tags_json='[\"ver\"]'")
    first = mcp.detect_memory_conflicts(persist=True)
    check("持久化成功", first.get("written", 0) >= 1, str(first.get("written")))
    second = mcp.detect_memory_conflicts(persist=True)
    with db.connect() as conn:
        n = conn.execute("SELECT COUNT(*) FROM memory_conflicts WHERE conflict_type='value_conflict'").fetchone()[0]
    check("重复跑不重复插", n == len([1]), f"{n} 行")
    listed = mcp.list_memory_conflicts()
    check("能列出", listed.get("ok") is not False, str(listed.get("count")))


def run():
    test_value_extraction()
    test_value_conflict_detected()
    test_real_duplicates_still_caught()
    test_no_numbers_no_rule()
    test_skeleton_threshold()
    test_end_to_end_through_mcp()
    test_values_do_not_break_persistence()
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n=== {len(RESULTS) - len(failed)}/{len(RESULTS)} 通过 ===")
    return 1 if failed else 0


if __name__ == "__main__":
    code = run()
    import shutil
    shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(code)
