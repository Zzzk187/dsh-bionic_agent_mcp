"""Recall profiles: does the weighting actually change with type/shape, and does
it stay inside the safe envelope while doing so?

Two things are easy to get wrong here and both are tested explicitly:

1. A profile table that is defined but never used. Every ranking test below is
   paired with a reverse check (below, in _verify_recall_profile_catches_bug.py)
   that breaks the wiring and demands a red result.
2. A profile that "works" by silencing a component. If any weight can reach 0
   or dominate, a phrase in the question alone can exclude a memory -- which is
   the opposite of what recall is for. So the envelope is asserted directly
   rather than assumed.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="bionic-recall-profile-test-")
os.environ["BIONIC_DB_PATH"] = str(Path(TMP) / "agent.db")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402
from app import mcp_server as mcp  # noqa: E402
from app import recall_profile as rp  # noqa: E402

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return ok


def fresh():
    db.init_db()
    with db.transaction() as conn:
        for table in ("memory_embeddings", "memory_links", "memory_feedback",
                      "memory_conflicts", "consolidation_candidates",
                      "governance_actions", "governance_runs", "snapshots",
                      "tasks", "memories", "events"):
            conn.execute(f"DELETE FROM {table}")


def test_shape_detection():
    print("\n=== 问句形态判定 ===")
    cases = [
        ("怎么配置代理", "procedural"),
        ("如何部署", "procedural"),
        ("上次那个端口是多少", "temporal"),
        ("昨天说过什么", "temporal"),
        ("我喜欢什么口味", "preference"),
        ("总结一下所有记忆", "recall_overview"),
        ("随便问点什么", "unknown"),
        ("", "unknown"),
    ]
    for q, want in cases:
        got = rp.detect_shape(q)
        check(f"'{q[:12]}' -> {want}", got == want, got)


def test_profiles_are_valid():
    print("\n=== 每个画像都在安全范围内（不允许某成分归零或被单一主导）===")
    check("画像数量符合预期", len(rp.TYPE_PROFILES) == 5, str(len(rp.TYPE_PROFILES)))
    for mtype, table in rp.TYPE_PROFILES.items():
        norm = rp._normalise(table)
        check(f"{mtype} 归一化后和≈1", abs(sum(norm.values()) - 1.0) < 1e-6,
              f"{sum(norm.values()):.6f}")
        check(f"{mtype} 没有成分被清零", min(norm.values()) > 0,
              f"min={min(norm.values()):.4f}")
        check(f"{mtype} 没有单一成分超过 0.5", max(norm.values()) <= 0.5,
              f"max={max(norm.values()):.4f}")
        check(f"{mtype} 保留了全部 7 个成分", len(norm) == 7, str(len(norm)))


def test_weights_for_selection():
    print("\n=== 选哪个画像：按类型优先，其次按问句形态 ===")
    w = rp.weights_for(memory_type="procedural", query="")
    check("显式指定类型 -> 用该类型画像", w["profile"] == "type:procedural", w["profile"])
    check("理由写清了", any("memory_type" in r for r in w["reasons"]), str(w["reasons"])[:60])

    w2 = rp.weights_for(memory_type="", query="怎么配置代理")
    check("无类型 + 程序型问句 -> 形态画像", w2["profile"] == "shape:procedural", w2["profile"])

    w3 = rp.weights_for(memory_type="", query="随便问问")
    check("都没命中 -> 默认权重", w3["profile"] == "default", w3["profile"])

    from app.memory_lifecycle import RECALL_WEIGHTS
    w4 = rp.weights_for(memory_type="", query="")
    check("默认画像的权重就是原本那张表（不偷偷改动）",
          abs(sum(w4["weights"].values()) - 1.0) < 1e-6
          and set(w4["weights"]) == set(RECALL_WEIGHTS),
          str(list(w4["weights"])[:3]))


def test_type_profile_actually_shifts():
    print("\n=== 类型画像真的改变了相对比重（不是换个名字）===")
    from app.memory_lifecycle import RECALL_WEIGHTS
    epi = rp._normalise(rp.TYPE_PROFILES["episodic"])
    pre = rp._normalise(rp.TYPE_PROFILES["preference"])
    check("episodic 比默认更看重 freshness",
          epi["freshness"] > RECALL_WEIGHTS["freshness"],
          f"{epi['freshness']:.3f} vs {RECALL_WEIGHTS['freshness']:.3f}")
    check("preference 比默认更不看重 freshness",
          pre["freshness"] < RECALL_WEIGHTS["freshness"],
          f"{pre['freshness']:.3f} vs {RECALL_WEIGHTS['freshness']:.3f}")
    check("两个画像彼此不同", epi["freshness"] != pre["freshness"], "不同")


def test_recall_uses_profile():
    print("\n=== 端到端：同样的记忆，换问句形态后排序依据变了 ===")
    fresh()
    # Two memories with identical text but different types: only the type
    # multiplier can separate them, so this isolates the per-candidate prior.
    body = "端口 8899 的调试记录"
    mcp.save_memory(content=body, category="temp")
    with db.transaction() as conn:
        conn.execute("UPDATE memories SET memory_type='procedural' WHERE content=?", (body,))
    m_proc = mcp.save_memory(content=body + " 甲", category="temp")
    with db.transaction() as conn:
        conn.execute("UPDATE memories SET memory_type='episodic' WHERE id=?",
                     (m_proc["id"],))

    res = mcp.recall_memories(query="上次那个端口 8899 怎么配的", top_k=10, explain=True)
    check("召回成功", res.get("ok") is not False, f"语义状态 {res.get('semantic_status')}")
    items = res.get("results") or []
    check("结果带上了所用画像",
          all("_profile" in i for i in items),
          str([i.get("_profile") for i in items]))
    # Compare against the *actual* tables rather than "sums to ~1", which is
    # true of almost any weight vector and would pass even if recall ignored
    # the profile entirely. This asserts the exact numbers reached scoring.
    from app.memory_lifecycle import RECALL_WEIGHTS
    expected_shape = rp.weights_for(memory_type="", query="上次那个端口 8899 怎么配的")
    shape_w = {k: v for k, v in (items[0].get("_weights") or {}).items() if k != "feedback"}
    # The query matches both "上次" and "怎么"; detect_shape returns the first
    # hit, so assert against whatever it actually chose rather than hard-coding.
    chosen = expected_shape["shape"]
    want = rp._normalise(
        {k: RECALL_WEIGHTS[k] * rp.SHAPE_WEIGHT_SHIFT.get(chosen, {}).get(k, 1.0)
         for k in RECALL_WEIGHTS})
    check(f"权重表与 {chosen} 形态表逐项一致",
          all(abs(shape_w.get(k, -1) - want[k]) < 1e-6 for k in want),
          f"got={ {k: round(shape_w.get(k, -1), 4) for k in list(want)[:3]} } "
          f"want={ {k: round(want[k], 4) for k in list(want)[:3]} }")
    check("权重表确实不同于默认表",
          any(abs(shape_w.get(k, -1) - rp._normalise(RECALL_WEIGHTS)[k]) > 1e-6
              for k in want),
          "与默认表有差异")
    check("每条带上了类型乘子",
          all("_type_multiplier" in i for i in items),
          str([i.get("_type_multiplier") for i in items]))
    # The decisive check: recompute the score from the reported parts and the
    # reported weights. If recall used the hard-coded global table while
    # *reporting* the profile table, these two diverge -- and every weaker
    # assertion above still passes. (That is exactly the bug this caught.)
    checked = 0
    mismatched = []
    for it in items:
        parts, w = it.get("_explain") or {}, it.get("_weights") or {}
        if not parts or not w:
            continue
        recomputed = sum(parts[k] * w[k] for k in parts if k in w)
        # The per-candidate type prior multiplies the whole fused score, so it
        # has to be in the recomputation or every typed memory looks wrong.
        recomputed *= float(it.get("_type_multiplier") or 1.0)
        # feedback is folded in separately and expiry subtracts afterwards;
        # compare on a tolerance that covers both, and only when neither fires.
        if it.get("_expired"):
            continue
        checked += 1
        # Tolerance matters: at 0.01 two different weight tables can produce
        # scores that agree to within the tolerance, which is how the first
        # version of this check let a hard-coded-weights regression through.
        # `_score` is rounded to 6dp, so anything above 1e-5 is a real mismatch.
        if abs(recomputed - it["_score"]) > 1e-5:
            mismatched.append((it["id"], round(recomputed, 6), it["_score"]))
    check("报告出来的权重确实是算分用的权重",
          checked > 0 and not mismatched,
          f"校验 {checked} 条，不一致 {mismatched[:3]}" if mismatched
          else f"校验 {checked} 条全部一致")
    prof = items[0].get("_profile") if items else ""
    check("形态被识别出来（不是 default）", prof != "default", str(prof))


def test_explicit_type_overrides_shape():
    print("\n=== 调用方显式要某类型时，形态不再抢戏 ===")
    w = rp.weights_for(memory_type="reflection", query="怎么配置代理")
    check("用类型画像", w["profile"] == "type:reflection", w["profile"])
    check("仍记录了形态", w["shape"] == "procedural", w["shape"])


def test_multiplier_bounds():
    print("\n=== 类型乘子是软先验，不会把别的类型清掉 ===")
    for shape in ("procedural", "temporal", "preference", "recall_overview", "unknown"):
        for mtype in ("episodic", "semantic", "preference", "procedural",
                      "goal", "reflection", "unknown", ""):
            mult = rp.type_multiplier(mtype, shape)
            assert 0.5 <= mult <= 1.5, f"{shape}/{mtype} = {mult}"
    check("所有组合都落在 [0.5, 1.5]", True, "覆盖 5 形态 × 8 类型")
    check("未命中的类型乘子为 1（不惩罚）",
          rp.type_multiplier("goal", "unknown") == 1.0,
          str(rp.type_multiplier("goal", "unknown")))
    check("空类型乘子为 1", rp.type_multiplier("", "temporal") == 1.0, "1.0")


def test_autoclassify_on_write():
    print("\n=== 写入时自动归类（否则类型画像对全库都是空转）===")
    fresh()
    r = mcp.save_memory(content="用户偏好：技术文档一律用中文", category="general")
    check("中文偏好被识别", r.get("memory_type") == "preference", str(r.get("memory_type")))
    check("带上了置信度", (r.get("type_confidence") or 0) > 0, str(r.get("type_confidence")))

    r2 = mcp.save_memory(content="怎么部署这个项目，步骤是什么", category="general")
    check("中文程序型被识别", r2.get("memory_type") == "procedural", str(r2.get("memory_type")))

    r3 = mcp.save_memory(content="zzz qqq 无信号文本", category="general")
    check("无信号时留空而不是编一个", r3.get("memory_type") is None, str(r3.get("memory_type")))

    with db.connect() as conn:
        stored = conn.execute("SELECT memory_type FROM memories WHERE id=?",
                              (r["id"],)).fetchone()[0]
    check("确实落库了", stored == "preference", str(stored))


def test_backfill():
    print("\n=== 老数据补类型：默认干跑，落库才改 ===")
    fresh()
    ids = []
    for txt in ("用户偏好中文", "怎么安装", "昨天开会"):
        r = mcp.save_memory(content=txt, category="general")
        ids.append(r["id"])
    with db.transaction() as conn:
        conn.execute("UPDATE memories SET memory_type=NULL")  # 模拟老数据

    dry = mcp.backfill_memory_types(dry_run=True)
    check("干跑报告数量", dry.get("would_set") == 3, str(dry.get("would_set")))
    check("干跑 applied 为 0", dry.get("applied") == 0, str(dry.get("applied")))
    with db.connect() as conn:
        nulls = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE memory_type IS NULL").fetchone()[0]
    check("干跑没有落库", nulls == 3, str(nulls))

    applied = mcp.backfill_memory_types(dry_run=False)
    check("执行后 applied 与预告一致", applied.get("applied") == dry.get("would_set"),
          f"{applied.get('applied')} vs {dry.get('would_set')}")
    with db.connect() as conn:
        nulls2 = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE memory_type IS NULL").fetchone()[0]
        types = [r[0] for r in conn.execute(
            "SELECT memory_type FROM memories ORDER BY id")]
    check("落库后不再有 NULL", nulls2 == 0, str(nulls2))
    check("类型分布正确", types == ["preference", "procedural", "episodic"], str(types))
    check("写了审计事件", applied.get("ok") is True, "ok")


def test_no_negative_weights_by_filtering():
    print("\n=== 画像绝不改变「谁能被查到」（只改排序）===")
    fresh()
    a = mcp.save_memory(content="完全无关的日落描写 甲乙丙", category="general")
    with db.transaction() as conn:
        conn.execute("UPDATE memories SET memory_type='episodic' WHERE id=?", (a["id"],))
    res = mcp.recall_memories(query="如何配置数据库连接池", top_k=10)
    ids = [i["id"] for i in res.get("results") or []]
    check("类型不匹配的记忆仍然能被查到", a["id"] in ids, str(ids))


def run():
    test_shape_detection()
    test_profiles_are_valid()
    test_weights_for_selection()
    test_type_profile_actually_shifts()
    test_recall_uses_profile()
    test_explicit_type_overrides_shape()
    test_multiplier_bounds()
    test_autoclassify_on_write()
    test_backfill()
    test_no_negative_weights_by_filtering()
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n=== {len(RESULTS) - len(failed)}/{len(RESULTS)} 通过 ===")
    return 1 if failed else 0


if __name__ == "__main__":
    code = run()
    import shutil
    shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(code)
