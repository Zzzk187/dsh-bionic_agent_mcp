"""S5-D 批：语义检索专项测试。

覆盖本批诊断出的四个语义路径事实（全部为「已核实正确」或「刻意设计」的守门测试，
不是靠大数字过关的弱断言）：

  1. `_semantic_scores` 返回原始余弦、未做标定——值域含负值，且与
     `embeddings.cosine_similarity` 逐条一致（防止有人偷偷加归一化破坏可比性）。
  2. `content_hash` 不符的向量必须被跳过（陈旧向量不得参与排序）。
  3. `model_name` 不符的向量必须被过滤（换模型后不得混用）。
  4. `memory_status IN ('pending_review','superseded')` 与 `status='archived'`
     必须被排除（这是「排除标记」的守门测试）。
  5. 语义降级：模型 `pending` 时返回空字典而非抛异常（不阻塞检索）。
  6. 语义分量在 `_explain` 里的键位与 `_weights` 的键位必须匹配——
     这是 D 批踩过的坑（误用 `_explain["parts"]` 会得全零、伪造「去语义无影响」）。

**不使用真实模型、不联网**：用 monkeypatch 替换 `embeddings.embed_query` /
`embed_passages` 返回可控向量，全部在临时库上跑，不碰生产库。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _env_guard  # noqa: E402,F401  OPT-04a：数据库隔离闸门（必须在任何 app.* 之前）

import app.db as db            # noqa: E402
import app.embeddings as embeddings  # noqa: E402
import app.memory_lifecycle as life  # noqa: E402

PASS = 0
FAIL = 0
FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok] {name}")
    else:
        FAIL += 1
        FAILED.append(name)
        print(f"  [FAIL] {name}{(' — ' + detail) if detail else ''}")


class _Env:
    """临时库 + 假向量，完全隔离。"""

    def __enter__(self):
        self._td = tempfile.TemporaryDirectory(prefix="bionic-sem-")
        self._orig_db = db.DB_PATH
        db.DB_PATH = Path(self._td.name) / "sem.db"
        db.init_db()
        self._orig_q = embeddings.embed_query
        self._orig_p = embeddings.embed_passages
        self._orig_state = embeddings.model_state
        return self

    def __exit__(self, *exc):
        db.DB_PATH = self._orig_db
        embeddings.embed_query = self._orig_q
        embeddings.embed_passages = self._orig_p
        embeddings.model_state = self._orig_state
        self._td.cleanup()
        return False


def _fake_embed(query: str) -> list[float]:
    """可控的「查询→向量」映射：用查询里的关键词决定方向。"""
    q = query.lower()
    if "甲" in q or "alpha" in q:
        return [1.0, 0.0, 0.0]
    if "乙" in q or "beta" in q:
        return [0.0, 1.0, 0.0]
    return [0.0, 0.0, 1.0]


def _seed(conn, rows):
    """rows: [(id, content, category, status, memory_status, vec)]"""
    for mid, content, cat, st, mst, vec in rows:
        conn.execute(
            "INSERT INTO memories(id,content,category,source,confidence,status,"
            "created_at,updated_at,memory_status) VALUES(?,?,?,?,?,?,?,?,?)",
            (mid, content, cat, "test", 0.9, st, "2026-11-01 00:00:00",
             "2026-11-01 00:00:00", mst))
        if vec is not None:
            conn.execute(
                "INSERT INTO memory_embeddings(memory_id,model_name,content_hash,"
                "dimension,vector_json,updated_at) VALUES(?,?,?,?,?,?)",
                (mid, embeddings.DEFAULT_MODEL, embeddings.content_hash(content),
                 len(vec), json.dumps(vec), "2026-11-01 00:00:00"))


# ---------------------------------------------------------------- 1. 原始余弦
def t_raw_cosine():
    print("\n== 1. _semantic_scores 返回原始余弦，未做标定 ==")
    with _Env() as env:
        embeddings.embed_query = _fake_embed
        with db.connect() as conn:
            # 甲方向正、乙方向正交、丙方向反向
            _seed(conn, [
                (1, "甲的记录", "general", "active", "active", [1.0, 0.0, 0.0]),
                (2, "乙的记录", "general", "active", "active", [0.0, 1.0, 0.0]),
                (3, "丙的记录", "general", "active", "active", [-1.0, 0.0, 0.0]),
            ])
        with db.connect() as conn:
            sc = life._semantic_scores(conn, "甲的问题")
        check("返回三条", len(sc) == 3, f"实际 {len(sc)}")
        check("同向 = 1.0", abs(sc.get(1, 0) - 1.0) < 1e-9, f"实际 {sc.get(1)}")
        check("正交 = 0.0", abs(sc.get(2, 0) - 0.0) < 1e-9, f"实际 {sc.get(2)}")
        check("反向 = -1.0（原始余弦含负值，未裁剪）",
              abs(sc.get(3, 0) + 1.0) < 1e-9, f"实际 {sc.get(3)}")
        check("值域未被压缩到 0..1（min<0 即证明无标定）",
              min(sc.values()) < 0)


# ---------------------------------------------------------- 2. 陈旧向量被跳过
def t_stale_hash_skipped():
    print("\n== 2. content_hash 不符的陈旧向量必须被跳过 ==")
    with _Env() as env:
        embeddings.embed_query = _fake_embed
        with db.connect() as conn:
            _seed(conn, [
                (1, "甲的记录", "general", "active", "active", [1.0, 0.0, 0.0]),
                (2, "乙的记录", "general", "active", "active", [0.0, 1.0, 0.0]),
            ])
            # 篡改 id=2 的 content，使 hash 与向量不再匹配（模拟「内容改了但没重建向量」）
            conn.execute("UPDATE memories SET content='乙的记录（已改）' WHERE id=2")
        with db.connect() as conn:
            sc = life._semantic_scores(conn, "甲的问题")
        check("陈旧向量的记忆被排除", 2 not in sc, f"实际 keys={sorted(sc)}")
        check("有效向量的记忆仍在", 1 in sc)
        check("只返回 1 条", len(sc) == 1, f"实际 {len(sc)}")


# ---------------------------------------------------------- 3. 模型名不符被过滤
def t_model_mismatch_filtered():
    print("\n== 3. model_name 不符的向量必须被过滤 ==")
    with _Env() as env:
        embeddings.embed_query = _fake_embed
        with db.connect() as conn:
            _seed(conn, [
                (1, "甲的记录", "general", "active", "active", [1.0, 0.0, 0.0]),
            ])
            # 把 id=1 的向量换成别的模型
            conn.execute("UPDATE memory_embeddings SET model_name='other-model' "
                         "WHERE memory_id=1")
        with db.connect() as conn:
            sc = life._semantic_scores(conn, "甲的问题")
        check("异模型向量被过滤（换模型后不得混用）", 1 not in sc,
              f"实际 keys={sorted(sc)}")
        check("返回空字典", sc == {}, f"实际 {sc}")


# ------------------------------------------------------- 4. 排除标记的守门测试
def t_exclusion_filters():
    print("\n== 4. archived / pending_review / superseded 必须被排除 ==")
    with _Env() as env:
        embeddings.embed_query = _fake_embed
        with db.connect() as conn:
            _seed(conn, [
                (1, "甲的活跃记录", "general", "active", "active", [1.0, 0.0, 0.0]),
                (2, "甲的归档记录", "general", "archived", "archived", [1.0, 0.0, 0.0]),
                (3, "甲的待审记录", "general", "active", "pending_review", [1.0, 0.0, 0.0]),
                (4, "甲的取代记录", "general", "active", "superseded", [1.0, 0.0, 0.0]),
            ])
        with db.connect() as conn:
            sc = life._semantic_scores(conn, "甲的问题")
        check("只返回 status=active 且 memory_status=active 的一条",
              sorted(sc) == [1], f"实际 {sorted(sc)}")
        check("archived 被排除", 2 not in sc)
        check("pending_review 被排除（排除标记）", 3 not in sc)
        check("superseded 被排除", 4 not in sc)


# ------------------------------------------------------------ 5. 降级路径
def t_degraded_when_pending():
    print("\n== 5. 模型 pending 时语义返回空字典，不阻塞检索 ==")
    with _Env() as env:
        embeddings.embed_query = _fake_embed
        # 伪造模型状态为 pending：_semantic_scores 会等 3s 后返回 {}
        real_state = embeddings.model_state

        def _pending_state():
            st = dict(real_state() or {})
            st["status"] = "pending"
            return st

        embeddings.model_state = _pending_state
        with db.connect() as conn:
            _seed(conn, [
                (1, "甲的记录", "general", "active", "active", [1.0, 0.0, 0.0]),
            ])
        import time as _t
        t0 = _t.time()
        with db.connect() as conn:
            sc = life._semantic_scores(conn, "甲的问题")
        waited = _t.time() - t0
        check("pending 时返回空字典（降级而非抛异常）", sc == {}, f"实际 {sc}")
        check("等待未明显超过设计上限 3s（实测 %.2fs）" % waited, waited < 6.0,
              f"实际 {waited:.2f}s")


# ------------------------------------------- 6. _explain / _weights 键位一致性
def t_explain_keys_match_weights():
    print("\n== 6. _explain 与 _weights 的键位必须匹配（D 批踩过的坑）==")
    with _Env() as env:
        embeddings.embed_query = _fake_embed
        with db.connect() as conn:
            _seed(conn, [
                (1, "甲的记录", "general", "active", "active", [1.0, 0.0, 0.0]),
                (2, "乙的记录", "general", "active", "active", [0.0, 1.0, 0.0]),
            ])
        got = life.recall_memories("甲的问题", top_k=2, record_access=False,
                                  explain=True)
        check("返回结果非空", len(got) > 0)
        if not got:
            return
        r = got[0]
        check("_explain 存在且是 dict", isinstance(r.get("_explain"), dict))
        check("_weights 在顶层（不在 _explain 里）",
              isinstance(r.get("_weights"), dict),
              "D 批曾误用 _explain['parts'] 导致全零假结论")
        parts = r.get("_explain") or {}
        weights = r.get("_weights") or {}
        check("_explain 本身不含 'parts' 包装层", "parts" not in parts)
        check("semantic 键在两处都存在",
              "semantic" in parts and "semantic" in weights,
              f"parts={sorted(parts)} weights={sorted(weights)}")
        # 用真实结构重算总分：应能与 _score 对上（证明键位用对了）
        tot = sum(parts.get(k, 0.0) * weights.get(k, 0.0) for k in weights)
        tot += (parts.get("feedback", 0.0) or 0.0) * 0.06
        tm = r.get("_type_multiplier")
        if tm and tm != 1.0:
            tot *= tm
        score = r.get("_score") or 0.0
        check(f"按 _explain×_weights 重算能复现 _score（重算 {tot:.5f} vs "
              f"实际 {score:.5f}）", abs(tot - score) < 5e-3,
              f"差 {abs(tot - score):.6f}")


# ------------------------------------------------ 7. 语义单路确实参与融合
def t_semantic_participates():
    print("\n== 7. 语义分量确实参与融合（非结构性压制到 0）==")
    with _Env() as env:
        embeddings.embed_query = _fake_embed
        with db.connect() as conn:
            _seed(conn, [
                (1, "甲的记录", "general", "active", "active", [1.0, 0.0, 0.0]),
                (2, "乙的记录", "general", "active", "active", [0.0, 1.0, 0.0]),
            ])
        got = life.recall_memories("甲的问题", top_k=2, record_access=False,
                                  explain=True)
        by_id = {r["id"]: r for r in got}
        if 1 in by_id:
            parts = by_id[1].get("_explain") or {}
            check("甲（语义同向）的 semantic 分量 > 0",
                  (parts.get("semantic") or 0) > 0.5,
                  f"实际 {parts.get('semantic')}")
        if 1 in by_id and 2 in by_id:
            s1 = (by_id[1].get("_explain") or {}).get("semantic") or 0
            s2 = (by_id[2].get("_explain") or {}).get("semantic") or 0
            check("语义同向得分高于正交项", s1 > s2, f"{s1:.4f} vs {s2:.4f}")
            check("语义排序第一名是命中的那条",
                  got[0]["id"] == 1, f"实际 id={got[0]['id']}")


def run():
    global PASS, FAIL
    PASS = FAIL = 0
    FAILED.clear()
    t_raw_cosine()
    t_stale_hash_skipped()
    t_model_mismatch_filtered()
    t_exclusion_filters()
    t_degraded_when_pending()
    t_explain_keys_match_weights()
    t_semantic_participates()
    print(f"\n=== {PASS}/{PASS + FAIL} 通过 ===")
    if FAILED:
        print("失败项：")
        for f in FAILED:
            print(f"  - {f}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(run())
