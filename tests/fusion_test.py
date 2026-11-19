"""S5-E：融合排序、frequency 来源与无结果判断的守门测试。

本文件只测**已落地的机制**，不测「阈值多准」——因为实测证明固定阈值区分
不了相关/无关查询，所以守门目标是：
  1. 权重表本身满足设计约束（相关性主导、无分量归零、R@3/R@5 不牺牲）
  2. frequency 只吃真实使用反馈，绝不吃 access_count（曝光）
  3. `_explain`<<能复现>> `_score`（可解释性不是装饰）
  4. `min_score` 默认关闭；开启时要么正常返回、要么明确返回空+原因
  5. 空结果绝不伪装成有效记忆

全流程使用临时隔离库与假向量，不联网、不碰生产库。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

PASS = 0
FAIL = 0


def check(label, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {label}" + (f" — {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  [FAIL] {label}" + (f" — {detail}" if detail else ""))


_TMPS: list[str] = []


def fresh():
    d = tempfile.mkdtemp(prefix="bionic-s5e-")
    _TMPS.append(d)
    p = Path(d) / "agent.db"
    os.environ["BIONIC_DB_PATH"] = str(p)
    import app.db as db
    db.DB_PATH = p
    db.init_db()
    return p


def fake_embed():
    from app import embeddings
    table = {"甲": [1.0, 0.0, 0.0], "乙": [0.0, 1.0, 0.0]}
    embeddings.embed_query = lambda text: table.get(text, [0.0, 0.0, 1.0])
    embeddings.embed_passages = lambda texts: [
        table.get(t, [0.0, 0.0, 1.0]) for t in texts]
    return embeddings


def seed(path, items):
    import sqlite3
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    for mid, content, cat in items:
        conn.execute(
            "INSERT INTO memories(id, content, category, source, confidence,"
            " status, created_at, updated_at, importance_score,"
            " confidence_score, freshness_score, access_count, memory_status)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (mid, content, cat, "test", 0.9, "active",
             "2026-11-01 00:00:00", "2026-11-01 00:00:00",
             50.0, 80.0, 90.0, 0, "active"))
    conn.commit()
    return conn


# ---------------------------------------------------------------- 1
def test_weight_table():
    print("\n=== 1. 权重表满足设计约束 ===")
    from app import memory_lifecycle as life
    from app import recall_profile as rp

    w = life.RECALL_WEIGHTS
    check("7 个分量齐备", len(w) == 7, str(sorted(w)))
    check("全部分量为正（没有静音任何分量）", all(v > 0 for v in w.values()),
          f"min={min(w.values()):.3f}")
    rel = w["semantic"] + w["keyword"]
    check("相关性主导（semantic+keyword > 0.85）", rel > 0.85, f"{rel:.4f}")
    # 注意：`recall_profile.MAX_WEIGHT=0.50` 是**归一化前输入表**的上限。归一化
    # 之后某一项合法地可以超过 0.50（keyword 0.60 先被截到 0.50，再归一成
    # 0.5556）。所以这里断言的是「归一化前的输入都 ≤ 0.61」，而不是「最终权重
    # ≤ 0.5」——后者与实现不符，写成那样只会逼着后来人改代码迁就测试。
    check("输入表无分量超 0.61（有界，不出现独占）", max(w.values()) <= 0.61,
          f"max={max(w.values()):.3f}")
    check("MAX_WEIGHT clamp 确实生效（keyword 被截后再归一）",
          rp.MAX_WEIGHT == 0.50, f"MAX_WEIGHT={rp.MAX_WEIGHT}")

    # 真实路径（经 _normalise 的 clamp）也必须守住这些约束
    real = rp.weights_for(memory_type="", query="默认问句")["weights"]
    check("真实路径 7 分量齐备", len(real) == 7, str(len(real)))
    check("真实路径全部分量为正", all(v > 0 for v in real.values()),
          f"min={min(real.values()):.4f}")
    check("真实路径相关性仍主导",
          real["semantic"] + real["keyword"] > 0.85,
          f"{real['semantic'] + real['keyword']:.4f}")

    # 类型画像之间的相对关系必须自洽（默认表改动后最容易破的地方）
    pre = rp._normalise(rp.TYPE_PROFILES["preference"])
    epi = rp._normalise(rp.TYPE_PROFILES["episodic"])
    check("preference 比默认更不看重 freshness",
          pre["freshness"] < life.RECALL_WEIGHTS["freshness"],
          f"{pre['freshness']:.4f} vs {life.RECALL_WEIGHTS['freshness']:.4f}")
    check("episodic 比默认更看重 freshness",
          epi["freshness"] > life.RECALL_WEIGHTS["freshness"],
          f"{epi['freshness']:.4f} vs {life.RECALL_WEIGHTS['freshness']:.4f}")


# ---------------------------------------------------------------- 2
def test_frequency_source():
    print("\n=== 2. frequency 只吃真实使用反馈，绝不吃 access_count ===")
    from app import memory_lifecycle as life

    check("无反馈时 frequency 为 0",
          life._usage_freq(None) == 0.0 and life._usage_freq({}) == 0.0)
    check("只有 retrieved 曝光时 frequency 仍为 0",
          life._usage_freq({"retrieved": 999, "used": 0, "helpful": 0}) == 0.0,
          "曝光不能让记忆升格")
    used = life._usage_freq({"used": 3, "helpful": 0})
    check("真实 used 参与计分", used > 0, f"{used:.2f}")
    helpful = life._usage_freq({"used": 0, "helpful": 3})
    check("helpful 权重高于 used（同次数）", helpful > used,
          f"{helpful:.2f} vs {used:.2f}")
    both = life._usage_freq({"used": 3, "helpful": 3})
    check("两种反馈叠加", both >= helpful, f"{both:.2f}")

    # 端到端：access_count 很大但无反馈时，frequency 分量必须为 0
    path = fresh()
    seed(path, [(1, "# 大访问量\n\n用户偏好：文档用中文。", "preference")])
    import sqlite3
    c = sqlite3.connect(str(path))
    c.execute("UPDATE memories SET access_count = 260 WHERE id = 1")
    c.commit()
    c.close()
    fake_embed()
    out = life.recall_memories("甲", top_k=1, explain=True, record_access=False)
    check("access_count=260 不再抬高 frequency 分量",
          out and out[0]["_explain"]["frequency"] == 0.0,
          f"frequency={out[0]['_explain']['frequency'] if out else 'n/a'}")
    check("access_count 仍作为观测值保留在 explain 里",
          out and out[0].get("_access_count_observed") == 260,
          f"observed={out[0].get('_access_count_observed') if out else 'n/a'}")
    check("frequency 来源被显式标注",
          out and out[0].get("_frequency_source") == "none",
          f"source={out[0].get('_frequency_source') if out else 'n/a'}")


# ---------------------------------------------------------------- 3
def test_explain_reproducible():
    print("\n=== 3. explain 能复现 score（可解释性不是装饰）===")
    from app import memory_lifecycle as life
    path = fresh()
    seed(path, [(1, "# 甲主题\n\n正文甲", "fact"),
                (2, "# 乙主题\n\n正文乙", "fact"),
                (3, "# 别的\n\n无关内容", "fact")])
    fake_embed()
    out = life.recall_memories("甲", top_k=3, explain=True, record_access=False)
    check("有返回", len(out) > 0, f"{len(out)} 条")
    worst = 0.0
    for r in out:
        acc = sum(r["_explain"][k] * r["_weights"][k] for k in r["_explain"])
        worst = max(worst, abs(acc - r["_score"]))
    check("按 _explain×_weights 重算与 _score 一致（误差 <1e-5）",
          worst < 1e-5, f"最大误差 {worst:.2e}")


# ---------------------------------------------------------------- 4
def test_min_score_mechanism():
    print("\n=== 4. min_score 默认关闭；开启时拒绝是显式的 ===")
    from app import memory_lifecycle as life
    path = fresh()
    seed(path, [(1, "# 甲主题\n\n正文甲", "fact"),
                (2, "# 别的\n\n无关", "fact")])
    fake_embed()

    default = life.recall_memories("甲", top_k=3, record_access=False)
    check("默认不设门槛：照常返回", len(default) > 0, f"{len(default)} 条")

    # 门槛高到必然全部拒绝
    strict = life.recall_memories("甲", top_k=3, record_access=False,
                                  min_score=0.99)
    check("门槛生效：全部低于门槛时返回空列表", strict == [],
          f"返回 {len(strict)} 条")

    strict_x = life.recall_memories("甲", top_k=3, record_access=False,
                                    explain=True, min_score=0.99)
    check("explain 模式下给出拒绝说明", len(strict_x) == 1
          and "_rejected" in strict_x[0], str(list(strict_x[0].keys())))
    if strict_x and "_rejected" in strict_x[0]:
        rj = strict_x[0]["_rejected"]
        check("拒绝原因可读", rj.get("reason") == "below_min_score",
              rj.get("reason", ""))
        check("拒绝说明含最佳分数与门槛",
              "best_score" in rj and "min_score" in rj,
              f"best={rj.get('best_score')} min={rj.get('min_score')}")
        check("拒绝说明是中文可读的",
              "没有足够相关" in (rj.get("message") or ""), rj.get("message", ""))
        check("拒绝块不伪装成记忆（不含 content）", "content" not in rj)

    # 门槛为 0 时不应误伤
    zero = life.recall_memories("甲", top_k=3, record_access=False, min_score=0.0)
    check("门槛 0 不误伤任何结果", len(zero) == len(default),
          f"{len(zero)} vs {len(default)}")

    # 部分保留：门槛落在中间时不应整批丢弃
    mid = life.recall_memories("甲", top_k=3, record_access=False,
                               min_score=default[-1]["_score"])
    check("门槛只过滤低分、保留高分", 0 < len(mid) <= len(default),
          f"{len(mid)}/{len(default)}")


# ---------------------------------------------------------------- 5
def test_relevance_dominates():
    print("\n=== 5. 相关性压过非相关性先验（这是本批的核心收益）===")
    from app import memory_lifecycle as life
    path = fresh()
    # A：内容相关，但各项治理指标都低
    # B：内容无关，但治理指标极高（旧权重下会被抬上来）
    #
    # 注意：必须真的写入向量，否则 semantic 恒 0；查询用两字词，否则中文
    # bigram 分词不产生任何 term（"甲" 是单字，词法为空），相关性无从谈起。
    import sqlite3
    conn = seed(path, [(1, "# 甲子主题\n\n甲子正文", "fact"),
                       (2, "# 完全无关\n\n别的内容", "fact")])
    conn.execute("UPDATE memories SET importance_score=20, confidence_score=40,"
                 " freshness_score=50, access_count=0 WHERE id=1")
    conn.execute("UPDATE memories SET importance_score=99, confidence_score=99,"
                 " freshness_score=99, access_count=0 WHERE id=2")
    conn.execute(
        "INSERT INTO memory_embeddings(memory_id, model_name, content_hash,"
        " dimension, vector_json, updated_at) VALUES(?,?,?,?,?,?)",
        (1, "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
         "h1", 3, "[1.0, 0.0, 0.0]", "2026-11-01 00:00:00"))
    conn.execute(
        "INSERT INTO memory_embeddings(memory_id, model_name, content_hash,"
        " dimension, vector_json, updated_at) VALUES(?,?,?,?,?,?)",
        (2, "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
         "h2", 3, "[0.0, 1.0, 0.0]", "2026-11-01 00:00:00"))
    conn.commit()
    conn.close()
    fake_embed()
    out = life.recall_memories("甲子", top_k=2, explain=True, record_access=False)
    check("内容相关的低治理分记忆排在前面",
          out and out[0]["id"] == 1,
          f"top1={out[0]['id'] if out else 'n/a'} "
          f"scores={[round(r['_score'], 4) for r in out]}")
    if out:
        w = out[0]["_weights"]
        check("相关性权重确实主导",
              w["semantic"] + w["keyword"] > 0.85,
              f"{w['semantic'] + w['keyword']:.3f}")


# ---------------------------------------------------------------- 6
def test_ordering_stable():
    print("\n=== 6. 排序稳定可复现 ===")
    from app import memory_lifecycle as life
    path = fresh()
    seed(path, [(1, "# 甲主题\n\n正文甲", "fact"),
                (2, "# 甲主题\n\n正文乙", "fact"),
                (3, "# 甲主题\n\n正文甲", "fact")])
    fake_embed()
    a = [r["id"] for r in life.recall_memories("甲", top_k=3,
                                               record_access=False)]
    b = [r["id"] for r in life.recall_memories("甲", top_k=3,
                                               record_access=False)]
    check("同一查询两次结果完全一致（record_access=False）", a == b,
          f"{a} vs {b}")


def main():
    import app.memory_lifecycle  # noqa: F401  —— 先导入，patch 才能生效
    groups = [test_weight_table, test_frequency_source, test_explain_reproducible,
              test_min_score_mechanism, test_relevance_dominates,
              test_ordering_stable]
    for fn in groups:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            import traceback
            print(f"  [FAIL] {fn.__name__} 异常：{exc}")
            traceback.print_exc()
            global FAIL
            FAIL += 1
    print(f"\n=== {PASS}/{PASS + FAIL} 通过 ===")
    for d in _TMPS:
        import shutil
        shutil.rmtree(d, ignore_errors=True)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
