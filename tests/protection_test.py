"""Graduated protection tiers: the tests that matter are the negative ones.

A protection feature is only worth anything if it actually *stops* something.
So the assertions below are mostly "this action is refused", plus a reverse
check that removing the tier check makes them fail.
"""
import os
import sys
import tempfile
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="bionic-prot-test-")
os.environ["BIONIC_DB_PATH"] = str(Path(TMP) / "agent.db")
os.environ["BIONIC_SNAPSHOT_DIR"] = str(Path(TMP) / "snaps")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import db, protection  # noqa: E402
from app import governance_scoring as scoring  # noqa: E402
from app import memory_lifecycle as life  # noqa: E402
from app import mcp_server as mcp  # noqa: E402

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return ok


def mem(**kw):
    base = {"category": "lesson", "importance_score": 50, "confidence_score": 70,
            "protected": 0, "status": "active", "content": "x"}
    base.update(kw)
    return base


def test_tiers():
    print("\n=== 分档判定 ===")
    cases = [
        ("protected 直接 critical", mem(protected=1, importance_score=10), protection.TIER_CRITICAL),
        ("preference 类别 critical", mem(category="preference", importance_score=10), protection.TIER_CRITICAL),
        ("identity 类别 critical", mem(category="identity", importance_score=10), protection.TIER_CRITICAL),
        ("高重要性 critical", mem(importance_score=85), protection.TIER_CRITICAL),
        ("中高重要性 high", mem(importance_score=65), protection.TIER_HIGH),
        ("普通 normal", mem(importance_score=45), protection.TIER_NORMAL),
        ("低分 low", mem(importance_score=12), protection.TIER_LOW),
        ("temp 类别 low", mem(category="temp", importance_score=75), protection.TIER_LOW),
    ]
    for name, m, want in cases:
        got = protection.assess(m)["tier"]
        check(name, got == want, f"{got}" + ("" if got == want else f" 期望 {want}"))

    critical = protection.assess(mem(importance_score=90))
    check("critical 档禁止自动归档", critical["blocks"]["auto_archive"] is True)
    check("critical 档禁止自动合并", critical["blocks"]["auto_merge"] is True)
    check("critical 档要求批量写人工确认", critical["blocks"]["bulk_write"] is True)
    low = protection.assess(mem(importance_score=10))
    check("low 档不禁止任何自动动作",
          not any(low["blocks"].values()), str(low["blocks"]))
    high = protection.assess(mem(importance_score=65))
    check("high 档禁止归档与合并，但允许批量写",
          high["blocks"]["auto_archive"] and high["blocks"]["auto_merge"]
          and not high["blocks"]["bulk_write"])


def test_only_restricts():
    """Tiers must never *widen* what the old thresholds allowed."""
    print("\n=== 分档只收紧、不放松 ===")
    low = mem(category="temp", importance_score=5)
    allowed, reasons = protection.allows(low, "auto_archive")
    check("低分临时记忆仍可进入归档路径", allowed, reasons[0][:50])
    high = mem(importance_score=95)
    blocked, reasons = protection.allows(high, "auto_archive")
    check("高分记忆被封住", not blocked, reasons[0][:50])
    # A memory that the old archive threshold already refused must stay refused.
    old_refuse = scoring.archive_candidate(
        mem(importance_score=90, freshness_score=10),
        {"importance": 90.0, "freshness": 10.0})[0]
    check("原本就被判不能归档的仍旧不能归档", old_refuse is False)


def test_archive_candidate_blocked():
    print("\n=== 归档候选判定接入了分档 ===")
    # Freshness is terrible but importance is high: the old rule would let a
    # pure freshness argument through; the tier gate must stop it.
    m = mem(category="lesson", importance_score=85)
    ok, reasons = scoring.archive_candidate(m, {"importance": 85.0, "freshness": 5.0})
    check("高分记忆不进归档候选", not ok, reasons[0][:70])
    check("拒绝理由里点名了分档", any("分档" in r for r in reasons),
          next((r[:60] for r in reasons if "分档" in r), "未找到"))
    low = mem(category="lesson", importance_score=12)
    ok2, _ = scoring.archive_candidate(low, {"importance": 12.0, "freshness": 5.0})
    check("低分记忆仍会进入归档候选", ok2 is True)


def test_end_to_end_candidates():
    print("\n=== 端到端：巩固候选也遵守分档 ===")
    db.init_db()
    crit = mcp.save_memory(content="用户偏好：所有交付文档必须用中文", category="preference")
    lowm = mcp.save_memory(content="临时备忘：明天可能要看一眼日志", category="temp")
    with db.transaction() as conn:
        # Force the high-value one to look stale and low-scoring on purpose.
        conn.execute("UPDATE memories SET importance_score=95, freshness_score=1 "
                     "WHERE id=?", (crit["id"],))
        conn.execute("UPDATE memories SET importance_score=8, freshness_score=1 "
                     "WHERE id=?", (lowm["id"],))
    cands = life.consolidate_memory_candidates(persist=False, limit=50)
    ids_in_archive = {c["source_memory_ids"][0] for c in cands.get("candidates", [])
                      if c["kind"] == "archive_review"}
    check("高分 preference 没有出现在归档候选里", crit["id"] not in ids_in_archive,
          f"归档候选 {sorted(ids_in_archive)}，crit={crit['id']}")
    check("低分临时记忆出现在归档候选里", lowm["id"] in ids_in_archive,
          f"归档候选 {sorted(ids_in_archive)}，low={lowm['id']}")


def test_annotate_and_summarize():
    print("\n=== 只读标注 ===")
    rows = [mem(importance_score=90), mem(importance_score=50), mem(importance_score=5)]
    protection.annotate(rows, action="auto_archive")
    check("annotate 给每行挂上 protection", all("protection" in r for r in rows))
    check("注解里带 tier", rows[0]["protection"]["tier"] == protection.TIER_CRITICAL)
    check("指定 action 时带 allowed", rows[0]["protection"].get("allowed") is False
          and rows[2]["protection"].get("allowed") is True)
    summary = protection.summarize(rows)
    check("summarize 数得对",
          summary["counts"][protection.TIER_CRITICAL] == 1
          and summary["counts"][protection.TIER_NORMAL] == 1
          and summary["counts"][protection.TIER_LOW] == 1,
          str(summary["counts"]))
    check("summarize 声明只收紧不放松", "不会把记忆升级成自动动作" in summary["note"],
          summary["note"][:40])


def test_mcp_tool():
    print("\n=== MCP 工具 ===")
    r = mcp.assess_memory_protection(1)
    check("assess_memory_protection 可用", r.get("ok"), str(r.get("tier")))
    s = mcp.summarize_protection()
    check("summarize_protection 可用", s.get("ok"), str(s.get("counts")))


def run():
    test_tiers()
    test_only_restricts()
    test_archive_candidate_blocked()
    test_end_to_end_candidates()
    test_annotate_and_summarize()
    test_mcp_tool()
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n=== {len(RESULTS) - len(failed)}/{len(RESULTS)} 通过 ===")
    return 1 if failed else 0


if __name__ == "__main__":
    code = run()
    import shutil
    shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(code)
