# -*- coding: utf-8 -*-
"""S6-FIX-06：概念桥只读诊断的开关一致性与数据准确性测试。

三件事必须同时成立，否则诊断就是新的风险源：

1. **不改分**：开关开/关、传/不传 `diagnostics` 时，`score_all` 与
   `recall_memories` 的分数与排序逐字节相同。诊断只能观测，不能影响。
2. **数据真实**：诊断里的数字必须来自真实评分流程——命中 id 就是真被抬分的
   那几条，跨阈值数就是真从 `<0.3` 跨到 `>=0.3` 的那几条。这里用「自己重算一遍
   并逐条比对」来验证，而不是把期望值写死成 F04/F11 两个 id。
3. **不碰生产库**：全程在一份 `Connection.backup()` 出来的临时副本上跑。
   首尾比对生产库的 (size, mtime_ns) 来证明。

跑法：`python tests/concept_bridge_diag_test.py`
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
import _env_guard  # noqa: E402,F401  OPT-04a：数据库隔离闸门（必须在任何 app.* 之前）

ROOT = Path(__file__).resolve().parent.parent
PROD = ROOT / "data" / "agent.db"
sys.path.insert(0, str(ROOT))

RESULTS: list[tuple[bool, str, str]] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    RESULTS.append((bool(cond), name, detail))
    mark = "PASS" if cond else "FAIL"
    line = "  [%s] %s" % (mark, name)
    if detail and not cond:
        line += " — %s" % detail
    print(line)
    return bool(cond)


_TMPS: list[Path] = []


def mktmp() -> Path:
    d = Path(tempfile.mkdtemp(prefix="bridge-diag-"))
    _TMPS.append(d)
    return d


def fingerprint(p: Path) -> tuple[int, int]:
    try:
        st = p.stat()
        return st.st_size, st.st_mtime_ns
    except FileNotFoundError:
        return -1, -1


# ---------------------------------------------------------------- 夹具：临时库
def make_db() -> Path:
    """把生产库复制一份到临时目录，全程只用副本。"""
    d = mktmp()
    dst_path = d / "agent.db"
    src = sqlite3.connect(
        "file:%s?mode=ro&immutable=1" % PROD.as_posix(), uri=True)
    dst = sqlite3.connect(str(dst_path))
    src.backup(dst)
    dst.close()
    src.close()
    return dst_path


def main() -> int:
    prod_before = fingerprint(PROD)

    copy = make_db()
    os.environ["BIONIC_DB_PATH"] = str(copy)
    from app import db  # noqa: E402
    db.DB_PATH = copy
    db.init_db()

    from app import embeddings  # noqa: E402
    from app import memory_lexical as lex  # noqa: E402
    from app import memory_lifecycle as S  # noqa: E402

    embeddings.ensure_warmup()
    import time
    for _ in range(600):
        if embeddings.is_ready():
            break
        time.sleep(0.1)

    with db.connect() as conn:
        mems = [dict(r) for r in conn.execute(
            "SELECT * FROM memories WHERE status='active' ORDER BY id").fetchall()]

    Q = "插件装完没反应是为什么"
    # 期望值不写死成 F04/F11：用真实流程自己算一遍桥接前后的分。
    print("=" * 70)
    print("A 开关一致性：诊断不改变任何分数与排序")
    print("=" * 70)

    lex.CONCEPT_BRIDGE_DIAGNOSTICS = True
    d_on: dict = {}
    on = lex.score_all(Q, mems, d_on)
    lex.CONCEPT_BRIDGE_DIAGNOSTICS = False
    d_off: dict = {}
    off = lex.score_all(Q, mems, d_off)
    lex.CONCEPT_BRIDGE_DIAGNOSTICS = True

    check("开关开：诊断字典被写入", bool(d_on), str(d_on))
    check("开关关：诊断字典保持为空", d_off == {}, str(d_off))
    check("开关开/关：分数逐字节相同", on == off,
          "%d vs %d 项" % (len(on), len(off)))
    check("开关开/关：键集合相同", set(on) == set(off))

    d_none: dict = {}
    _ = lex.score_all(Q, mems)          # 完全不传 diagnostics
    check("不传 diagnostics：不报错且不产生字典", d_none == {})

    # 传 None 与不传等价（函数签名层面）
    check("显式传 None 与不传结果相同", lex.score_all(Q, mems, None) == on)

    # 真实检索链路
    S._touch_retrieved = lambda *a, **k: None
    r_on = S.recall_memories(Q, top_k=10, explain=True, record_access=False)
    lex.CONCEPT_BRIDGE_DIAGNOSTICS = False
    r_off = S.recall_memories(Q, top_k=10, explain=True, record_access=False)
    lex.CONCEPT_BRIDGE_DIAGNOSTICS = True
    check("recall_memories：开关开/关 id 序列相同",
          [r["id"] for r in r_on] == [r["id"] for r in r_off],
          "%s vs %s" % ([r["id"] for r in r_on], [r["id"] for r in r_off]))
    check("recall_memories：开关开/关 _score 序列相同",
          [r["_score"] for r in r_on] == [r["_score"] for r in r_off])
    check("explain 里开关开时带 _bridge",
          all("_bridge" in r for r in r_on), "缺 _bridge")
    check("explain 里开关关时没有 _bridge",
          all("_bridge" not in r for r in r_off), "仍有 _bridge")
    check("explain=False 时不带 _bridge",
          all("_bridge" not in r for r in
              S.recall_memories(Q, top_k=3, record_access=False)))

    print()
    print("=" * 70)
    print("B 指标定义与真实数据")
    print("=" * 70)
    b = d_on
    for key in ("bridge_hit_count", "bridge_hit_memory_ids",
                "seed_threshold_crossing_count", "bridge_alpha",
                "bridge_enabled"):
        check("诊断含 %s" % key, key in b, str(sorted(b)))
    check("bridge_alpha 等于模块常量", b.get("bridge_alpha") == lex.CONCEPT_BRIDGE_ALPHA,
          "%r vs %r" % (b.get("bridge_alpha"), lex.CONCEPT_BRIDGE_ALPHA))
    check("bridge_enabled 与模块开关一致", b.get("bridge_enabled") is True)
    check("bridge_hit_count 与 id 列表长度一致",
          b.get("bridge_hit_count") == len(b.get("bridge_hit_memory_ids") or []),
          "%r vs %r" % (b.get("bridge_hit_count"),
                        len(b.get("bridge_hit_memory_ids") or [])))
    ids = list(b.get("bridge_hit_memory_ids") or [])
    check("bridge_hit_memory_ids 是排好序的整数列表",
          ids == sorted(ids) and all(isinstance(i, int) for i in ids), str(ids))

    # 每个命中 id 都必须真的被抬分，抬的量必须正好是 α×(1-s)
    # s 从「关桥接后的分」拿到（同一批、同一流程，只差桥接这一步）
    lex.CONCEPT_BRIDGE_ENABLED = False
    base = lex.score_all(Q, mems)
    lex.CONCEPT_BRIDGE_ENABLED = True
    bumped = lex.score_all(Q, mems)
    exact = True
    detail = []
    for mid in ids:
        s = base.get(mid)
        got = bumped.get(mid)
        if s is None or got is None:
            exact = False
            detail.append("id=%s 缺分" % mid)
            continue
        want = round(s + lex.CONCEPT_BRIDGE_ALPHA * (1.0 - s), 6)
        if abs(got - want) > 1e-9:
            exact = False
            detail.append("id=%s got=%r want=%r" % (mid, got, want))
    check("每个命中 id 的抬分正好是 α×(1-s)", exact, " / ".join(detail))

    # 命中集合 = 真的「分数变高」的集合（不多不少）
    changed = {m for m in bumped if base.get(m) != bumped.get(m)}
    check("命中 id 集合 == 分数真的变高的集合", set(ids) == changed,
          "命中 %s / 变了 %s" % (sorted(ids), sorted(changed)))

    # 未命中的条目一分不动
    untouched = all(base.get(m) == bumped.get(m) for m in bumped if m not in ids)
    check("未命中条目分数一字不改", untouched)

    # 跨 seed 阈值统计：自己按定义重算
    thr = lex.BRIDGE_SEED_THRESHOLD
    expect_cross = sum(
        1 for mid in ids
        if base.get(mid, 0.0) < thr <= bumped.get(mid, 0.0))
    check("seed_threshold_crossing_count 与按定义重算一致",
          b.get("seed_threshold_crossing_count") == expect_cross,
          "%r vs %r" % (b.get("seed_threshold_crossing_count"), expect_cross))
    check("诊断回显的 seed_threshold 等于模块常量",
          b.get("seed_threshold") == thr, "%r vs %r" % (b.get("seed_threshold"), thr))

    print()
    print("=" * 70)
    print("C seed 阈值只有一处权威定义（S6-FINAL-FIX P2-3）")
    print("=" * 70)
    # 以前这里靠 inspect.getsource(recall_memories) 里找字符串
    # 'lex.get(d["id"], 0.0) >= 0.3' 来兜底——那要求源码一个字都不能改写法，
    # 改个空格/换个变量名就红，而真正的风险（两处常量各改各的）反而挡不住。
    # 现在改成三条互不依赖的运行时断言：
    #   1) 权威常量本身的值
    #   2) 兼容别名确实指向同一个对象（不是各自写了一遍 0.3）
    #   3) 真实行为——语料里确实有记忆被桥接抬过阈值（阈值真的在起作用）
    check("LEX_SEED_THRESHOLD == 0.3", lex.LEX_SEED_THRESHOLD == 0.3,
          repr(lex.LEX_SEED_THRESHOLD))
    check("BRIDGE_SEED_THRESHOLD 是 LEX_SEED_THRESHOLD 的别名（同一对象）",
          lex.BRIDGE_SEED_THRESHOLD is lex.LEX_SEED_THRESHOLD,
          "%r vs %r" % (lex.BRIDGE_SEED_THRESHOLD, lex.LEX_SEED_THRESHOLD))

    # 行为验证：关掉桥接算一遍原始词法分，与开启时的分逐条比，
    # 找出「原本低于阈值、桥接后跨过」的记忆。一条都没有就说明短语表/阈值
    # 与语料脱节，同样该红。
    lex.CONCEPT_BRIDGE_ENABLED = False
    try:
        raw = lex.score_all(Q, mems)
    finally:
        lex.CONCEPT_BRIDGE_ENABLED = True
    bridged = lex.score_all(Q, mems)
    crossed = [m for m in raw
               if raw[m] < lex.LEX_SEED_THRESHOLD <= bridged.get(m, raw[m])]
    check("语料里确有记忆被桥接抬过 seed 阈值（阈值真的在用）",
          len(crossed) >= 1, "crossed=%s" % sorted(crossed))

    print()
    print("=" * 70)
    print("C2 explain=False 时根本不传诊断（S6-FINAL-FIX P2-2）")
    print("=" * 70)
    # 以前 memory_lifecycle 无条件建 {} 并传下去：不回显，但照样算一遍。
    # 现在 explain=False 传 None。这里用 spy 直接看实参，而不是只看输出里有没有 _bridge。
    seen: list = []
    _orig_ls = S._lexical_scores

    def _spy_ls(query, rows, diagnostics=None):
        seen.append(diagnostics)
        return _orig_ls(query, rows, diagnostics)

    S._lexical_scores = _spy_ls
    try:
        r_off2 = S.recall_memories(Q, top_k=5, explain=False, record_access=False)
        r_on2 = S.recall_memories(Q, top_k=5, explain=True, record_access=False)
    finally:
        S._lexical_scores = _orig_ls
    check("explain=False：传给 _lexical_scores 的 diagnostics 是 None",
          len(seen) == 2 and seen[0] is None, "seen=%r" % (seen[:1],))
    check("explain=True：传给 _lexical_scores 的 diagnostics 是 dict",
          len(seen) == 2 and isinstance(seen[1], dict), "seen=%r" % (seen[1:2],))
    check("explain=False：结果里不含 _bridge",
          all("_bridge" not in r for r in r_off2))
    check("explain=True：结果里含 _bridge",
          all("_bridge" in r for r in r_on2))
    check("explain=False/True：id 与 _score 序列仍相同",
          [r["id"] for r in r_off2] == [r["id"] for r in r_on2]
          and [r["_score"] for r in r_off2] == [r["_score"] for r in r_on2])

    print()
    print("=" * 70)
    print("D 负向：不是概念桥意图时诊断为空")
    print("=" * 70)
    for q, why in [
        ("红烧肉怎么做", "无意图"),
        ("npm install 失败 装完报错", "意图=install_fail"),
        ("装完界面上找不到", "意图=wrong_location"),
        ("装完要重启吗", "意图=needs_restart"),
        ("插件没反应是为什么", "缺动作词"),
    ]:
        dd: dict = {}
        lex.score_all(q, mems, dd)
        check("「%s」（%s）不触发桥接" % (q, why),
              dd.get("bridge_hit_count") == 0,
              str(dd))

    print()
    print("=" * 70)
    print("E 生产库未被触碰")
    print("=" * 70)
    prod_after = fingerprint(PROD)
    check("生产库 (size, mtime_ns) 未变", prod_before == prod_after,
          "%s vs %s" % (prod_before, prod_after))
    check("生产库 -wal/-shm 均不存在",
          not (PROD.parent / "agent.db-wal").exists()
          and not (PROD.parent / "agent.db-shm").exists())

    print()
    print("=" * 70)
    passed = sum(1 for ok, _, _ in RESULTS if ok)
    total = len(RESULTS)
    print("结果：%d PASS / %d FAIL / 共 %d 项" % (passed, total - passed, total))
    if passed != total:
        print("失败项：")
        for ok, name, detail in RESULTS:
            if not ok:
                print("  - %s%s" % (name, ("  " + detail) if detail else ""))
    return 0 if passed == total else 1


if __name__ == "__main__":
    try:
        code = main()
    finally:
        for d in _TMPS:
            shutil.rmtree(d, ignore_errors=True)
    sys.exit(code)
