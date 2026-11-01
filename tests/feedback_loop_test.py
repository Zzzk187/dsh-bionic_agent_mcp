"""Tests for the feedback loop: does feedback actually change how memory scores?

The bug being pinned: `record_memory_feedback` wrote counters that nothing ever
read, so "helpful" and "corrected" had zero effect on scoring. A test that only
checks "the counter went up" would have passed on the broken code, so every
assertion here is about the SCORE moving (or provably not moving).

Run from the project root:

    python tests/feedback_loop_test.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app import db  # noqa: E402
from app import feedback_loop as fb  # noqa: E402

_RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, passed: bool, detail: str = "") -> None:
    _RESULTS.append((name, passed, detail))
    print(f"[{'PASS' if passed else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def counts(**kw) -> dict:
    base = {"retrieved": 0, "used": 0, "helpful": 0, "not_relevant": 0, "corrected": 0}
    base.update(kw)
    return base


def test_retrieved_never_scores():
    """Exposure must not be worth any points. This is the anti-echo-chamber rule."""
    heavy = fb.compute_signal(counts(retrieved=1000))
    check("retrieved 一千次仍然不产生信号", heavy["signal"] == 0.0 and not heavy["applied"],
          f"signal={heavy['signal']:.3f} applied={heavy['applied']}")

    # And it must not rescue a memory that has negative directional feedback.
    neg_only = fb.compute_signal(counts(corrected=2))
    neg_plus_views = fb.compute_signal(counts(corrected=2, retrieved=500))
    check("retrieved 不会抵消 corrected 的负信号",
          neg_only["signal"] == neg_plus_views["signal"],
          f"{neg_only['signal']:+.3f} vs {neg_plus_views['signal']:+.3f}")


def test_minimum_samples():
    """One observation is noise; two are a trend."""
    one = fb.compute_signal(counts(helpful=1))
    check("只有 1 条带方向反馈时不生效", not one["applied"] and one["signal"] == 0.0,
          one["reason"][:70])

    two = fb.compute_signal(counts(helpful=2))
    check("2 条带方向反馈开始生效", two["applied"] and two["signal"] > 0,
          f"signal={two['signal']:+.3f}")


def test_direction_is_correct():
    helpful = fb.compute_signal(counts(helpful=5, used=2))
    corrected = fb.compute_signal(counts(corrected=2, used=1))
    not_rel = fb.compute_signal(counts(not_relevant=4))

    check("helpful 产生正信号", helpful["signal"] > 0.2, f"{helpful['signal']:+.3f}")
    check("corrected 产生负信号", corrected["signal"] < 0, f"{corrected['signal']:+.3f}")
    check("not_relevant 产生负信号", not_rel["signal"] < 0, f"{not_rel['signal']:+.3f}")
    check("corrected 比 not_relevant 更伤",
          abs(fb.compute_signal(counts(corrected=4))["signal"])
          > abs(fb.compute_signal(counts(not_relevant=4))["signal"]),
          "记忆被纠正比一次没命中更严重")


def test_confidence_shift_is_bounded():
    """No amount of feedback may push a score to 0 or 100."""
    check("confidence 100 被正向反馈顶不破 100",
          fb.apply_to_confidence(100, 1.0) <= 100.0,
          f"→ {fb.apply_to_confidence(100, 1.0):.2f}")
    check("confidence 0 被负向反馈压不破 0",
          fb.apply_to_confidence(0, -1.0) >= 0.0,
          f"→ {fb.apply_to_confidence(0, -1.0):.2f}")
    check("极端信号也只动 ±25%",
          abs(fb.apply_to_confidence(80, 1.0) - 80) <= 80 * 0.25 + 1e-9,
          f"80 → {fb.apply_to_confidence(80, 1.0):.2f}")
    check("信号为 0 时分数不变",
          fb.apply_to_confidence(63.7, 0.0) == 63.7)


def test_scores_actually_move_end_to_end():
    """The whole loop, against a real temp DB: feedback must move the score."""
    original = db.DB_PATH
    try:
        with tempfile.TemporaryDirectory() as td:
            db.DB_PATH = Path(td) / "test.db"
            db.init_db()

            from app import mcp_server as mcp
            from app import memory_lifecycle as life

            a = mcp.save_memory(content="反馈闭环测试：记忆 A 会被反复证明有用", category="test")
            b = mcp.save_memory(content="反馈闭环测试：记忆 B 会被纠正", category="test")
            a_id, b_id = a["id"], b["id"]

            def credibility_of(mid):
                with db.connect() as conn:
                    ctx = mcp.gov.build_scoring_context(conn, mid)
                    row = conn.execute("SELECT * FROM memories WHERE id=?", (mid,)).fetchone()
                from app.governance_scoring import score_memory
                return score_memory(dict(row), ctx)["credibility"], ctx, score_memory

            base_a, ctx_a, _ = credibility_of(a_id)
            base_b, ctx_b, _ = credibility_of(b_id)
            # .get() rather than [] so that a regression reports a readable
            # FAIL instead of crashing with KeyError partway through the suite.
            check("评分上下文里带上了反馈信号字段",
                  "feedback_signal" in ctx_a and "feedback_applied" in ctx_a,
                  f"字段 {sorted(k for k in ctx_a if k.startswith('feedback'))}")
            check("初始未受反馈影响",
                  ctx_a.get("feedback_signal", 0) == 0
                  and not ctx_a.get("feedback_applied", False),
                  str(ctx_a.get("feedback_reason", "(无反馈字段)"))[:60])

            # A: strongly endorsed. B: corrected twice.
            for _ in range(4):
                mcp.record_memory_feedback(a_id, "helpful")
            for _ in range(3):
                mcp.record_memory_feedback(a_id, "used")
            for _ in range(2):
                mcp.record_memory_feedback(b_id, "corrected")

            after_a, ctx_a2, score_memory = credibility_of(a_id)
            after_b, ctx_b2, _ = credibility_of(b_id)

            check("A 的可信度因正向反馈上升", after_a > base_a,
                  f"{base_a:.2f} → {after_a:.2f}（signal={ctx_a2.get('feedback_signal', 0.0):+.3f}）")
            check("B 的可信度因被纠正下降", after_b < base_b,
                  f"{base_b:.2f} → {after_b:.2f}（signal={ctx_b2.get('feedback_signal', 0.0):+.3f}）")
            with db.connect() as conn:
                row_a = conn.execute("SELECT * FROM memories WHERE id=?", (a_id,)).fetchone()
            reasons_a = score_memory(dict(row_a), ctx_a2)["reasons"]["credibility"]
            check("评分原因里写明了反馈依据",
                  any("反馈信号" in r for r in reasons_a),
                  next((r[:80] for r in reasons_a if "反馈信号" in r), "未找到"))

            # Recall must reflect it too.
            results = life.recall_memories(query="反馈闭环测试", top_k=5, explain=True)
            by_id = {r["id"]: r for r in results}
            check("召回结果里能看到反馈分量",
                  a_id in by_id and by_id[a_id]["_explain"]["feedback"] > 0,
                  f"A 的召回反馈分量 {by_id.get(a_id, {}).get('_explain', {}).get('feedback')}")
            check("B 的召回反馈分量是负的",
                  b_id in by_id and by_id[b_id]["_explain"]["feedback"] < 0,
                  f"B 的召回反馈分量 {by_id.get(b_id, {}).get('_explain', {}).get('feedback')}")

            # Retrieved must be provably inert even in the real DB.
            before, _, _ = credibility_of(a_id)
            for _ in range(20):
                mcp.record_memory_feedback(a_id, "retrieved")
            after, ctx_after, _ = credibility_of(a_id)
            check("真实库里 retrieved 二十次也不动评分",
                  abs(after - before) < 1e-9,
                  f"{before:.2f} → {after:.2f}，signal={ctx_after.get('feedback_signal', 0.0):+.3f}")

            summary = mcp.summarize_memory_feedback(limit=10)
            check("汇总能看到正负两侧",
                  summary["ok"] and summary["applied_count"] == 2,
                  f"applied={summary['applied_count']}")
            check("汇总里 A 在正侧、B 在负侧",
                  any(x["memory_id"] == a_id for x in summary["top_positive"])
                  and any(x["memory_id"] == b_id for x in summary["top_negative"]),
                  f"正 {[x['memory_id'] for x in summary['top_positive']]} / "
                  f"负 {[x['memory_id'] for x in summary['top_negative']]}")
    finally:
        db.DB_PATH = original


def test_record_recall_is_inert():
    """`record_recall` writes the exposure trail without touching scores."""
    original = db.DB_PATH
    try:
        with tempfile.TemporaryDirectory() as td:
            db.DB_PATH = Path(td) / "test.db"
            db.init_db()
            from app import mcp_server as mcp
            m = mcp.save_memory(content="曝光留痕测试", category="test")
            r = mcp.record_recall([m["id"]])
            check("record_recall 记录成功", r["ok"] and r["recorded"] == 1, str(r.get("recorded")))
            with db.connect() as conn:
                row = conn.execute("SELECT * FROM memory_feedback WHERE memory_id=?",
                                   (m["id"],)).fetchone()
                info = fb.feedback_for(conn, m["id"])
            check("retrieved 计数写进了表", row["retrieved_count"] == 1, str(row["retrieved_count"]))
            check("但信号仍然是 0、未生效", info["signal"] == 0.0 and not info["applied"],
                  info["reason"][:60])
            check("不存在的 id 被忽略不报错",
                  mcp.record_recall([999999])["recorded"] == 0)
    finally:
        db.DB_PATH = original


def run() -> int:
    print("=== 反馈闭环 ===")
    test_retrieved_never_scores()
    test_minimum_samples()
    test_direction_is_correct()
    test_confidence_shift_is_bounded()
    test_scores_actually_move_end_to_end()
    test_record_recall_is_inert()
    failed = [r for r in _RESULTS if not r[1]]
    print(f"\n=== {len(_RESULTS) - len(failed)}/{len(_RESULTS)} 通过 ===")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(run())
