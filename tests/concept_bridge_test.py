# -*- coding: utf-8 -*-
"""S6-FIX-05-A：概念桥规则的边界测试。

只测规则本身，不碰生产库、不改任何算法或参数（α 恒为 0.20）。

覆盖任务书要求八项：
  A 缺动作词 → 不触发
  B 缺症状词 → 不触发
  C 意图不匹配 → 不触发
  D 记忆没有重启机制 → 不触发
  E F04 / F11 等双侧条件成立的记忆 → 正常触发
  F 同一条记忆不会重复混入
  G 分数始终在 0..1
  H 桥接关闭时与原始算法完全一致

另附（审计中发现的额外证据）：
  I 触发字段范围：title / tags / body 参与，元数据不参与
  J 公式精确性：s' == s + 0.20*(1-s)，逐条比对
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import memory_lexical as lex  # noqa: E402

PASS = 0
FAIL = 0
FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        FAILED.append(name)
        print(f"  [FAIL] {name}" + (f" — {detail}" if detail else ""))


# ---------------------------------------------------------------- 测试夹具
# 真实记忆的等价物：F04 讲「改完需重启 DSH 才生效」，F11 讲「必须重启 DSH 才生效」。
# **重要**：两条都必须含「插件」二字——score_all 会先把「零字符命中」的记忆
# 直接筛掉（coverage 为 0 就 continue），桥接根本没有机会作用在它们身上。
# 真实 F04 正文里就有「插件 bundle」，所以这里也保留，与线上行为一致。
Q = "插件装完没反应是为什么"

MEM_F04 = {
    "id": 19,
    "content": "# 技能发现机制\n\n靠 `dsh-skill-mcp-manager`，**改完需重启 DSH**。"
               "也就是说：技能改了要重启 DSH 才生效，没重启之前看不出变化、旧技能还在。"
               "内置技能扫插件 bundle 内 `skills/<name>/SKILL.md`。",
    "tags_json": '["环境", "dsh-memory", "src:F04"]',
    "category": "环境",
    "memory_type": "semantic",
}
MEM_F11 = {
    "id": 54,
    "content": "# 本地骨架并入 MCP 知识库\n\n插件类骨架文件改了，必须重启 DSH 才生效。",
    "tags_json": '["记忆", "bionic-agent", "src:F11"]',
    "category": "环境",
    "memory_type": "semantic",
}
MEM_PLAIN = {
    "id": 900,
    # 含「插件」只为让 score_all 留下它（零字符命中会被直接筛掉），
    # 内容里刻意没有任何机制短语，所以桥接不该作用于它。
    "content": "# 普通条目\n\n插件列表和重启没有任何关系的内容。",
    "tags_json": '["测试"]',
    "category": "环境",
    "memory_type": "semantic",
}
ALL = [MEM_F04, MEM_F11, MEM_PLAIN]


def score_with(query: str, memories: list[dict]) -> dict:
    return lex.score_all(query, memories)


print("=" * 70)
print("A 缺动作词 → 不触发")
print("=" * 70)
# 「插件没反应是为什么」：有症状（没反应）、意图仍是 install_no_effect，但没有动作完成词
qA = "插件没反应是为什么"
check("意图仍判为 install_no_effect（确保是「动作词缺失」在起作用）",
      lex.detect_intent(qA) == lex.INTENT_INSTALL_NO_EFFECT, lex.detect_intent(qA))
check("缺动作词时 F04 不触发", lex.concept_bridge_hit(qA, MEM_F04) is False)
check("缺动作词时 F11 不触发", lex.concept_bridge_hit(qA, MEM_F11) is False)
# 反向验证：把动作词补回去就应触发
check("补回动作词「装完」后触发", lex.concept_bridge_hit("插件装完没反应是为什么", MEM_F04) is True)

print()
print("=" * 70)
print("B 缺症状词 → 不触发")
print("=" * 70)
for qB in ("插件装完是为什么", "插件装好了吗", "插件装完然后呢"):
    check(f"缺症状词「{qB}」F04 不触发", lex.concept_bridge_hit(qB, MEM_F04) is False,
          f"intent={lex.detect_intent(qB)}")
# 这条同时被意图门挡住（intent 为 none），但即使意图成立也应因缺症状词而拒绝：
check("症状词单独缺失时，即便动作词齐全也不触发",
      lex.concept_bridge_hit("插件装完", MEM_F04) is False)

print()
print("=" * 70)
print("C 意图不匹配 → 不触发")
print("=" * 70)
for qC, want in (
    ("装完界面上找不到", lex.INTENT_WRONG_LOCATION),
    ("装完要重启吗", lex.INTENT_NEEDS_RESTART),
    ("npm install 失败 装完报错", lex.INTENT_INSTALL_FAIL),
):
    got = lex.detect_intent(qC)
    check(f"「{qC}」意图={got or '(none)'}（期望 {want}）", got == want, got)
    check(f"「{qC}」意图不匹配时 F04 不触发", lex.concept_bridge_hit(qC, MEM_F04) is False)
check("完全无意图的查询不触发", lex.concept_bridge_hit("红烧肉怎么做", MEM_F04) is False)
check("裸词「没反应」不触发（无动作词）", lex.concept_bridge_hit("没反应", MEM_F04) is False)

print()
print("=" * 70)
print("D 记忆没有重启机制 → 不触发")
print("=" * 70)
check("纯无关记忆不触发", lex.concept_bridge_hit(Q, MEM_PLAIN) is False)
check("删光机制词后不触发",
      lex.concept_bridge_hit(Q, {**MEM_F04, "content": "# 技能发现机制\n\n讲别的事情",
                                 "tags_json": "[]"}) is False)
check("只留裸词「重启」不触发（不是机制短语）",
      lex.concept_bridge_hit(Q, {**MEM_F04, "content": "# 技能发现机制\n\n有问题就重启一下",
                                 "tags_json": "[]"}) is False)
check("「重启后」可触发（在机制词表内）",
      lex.concept_bridge_hit(Q, {**MEM_F04, "content": "# 技能发现机制\n\n重启后可以",
                                 "tags_json": "[]"}) is True)

print()
print("=" * 70)
print("E 双侧条件成立的记忆正常触发")
print("=" * 70)
check("F04 触发", lex.concept_bridge_hit(Q, MEM_F04) is True)
check("F11 触发", lex.concept_bridge_hit(Q, MEM_F11) is True)
s = score_with(Q, ALL)
check("F04 在结果里", 19 in s, str(sorted(s)))
check("F11 在结果里", 54 in s, str(sorted(s)))
check("F04 分数被抬起（> 未命中条目的可能值）", s[19] > 0)

print()
print("=" * 70)
print("F 同一条记忆不会重复混入")
print("=" * 70)
# 幂等性：对同一份输入连续调用，结果必须一致（不是每次再叠一层）
r1 = score_with(Q, ALL)
r2 = score_with(Q, ALL)
r3 = score_with(Q, ALL)
check("连续三次调用结果完全相同", r1 == r2 == r3,
      f"{r1.get(19)} / {r2.get(19)} / {r3.get(19)}")
# 直接验证 apply_concept_bridge 不就地修改入参、也不会二次叠加
import copy  # noqa: E402
src = {19: 0.3, 54: 0.2, 900: 0.1}
src_copy = copy.deepcopy(src)
once = lex.apply_concept_bridge(Q, ALL, src)
check("apply_concept_bridge 不修改入参字典", src == src_copy, f"{src}")
expected_19 = round(0.3 + lex.CONCEPT_BRIDGE_ALPHA * (1 - 0.3), 6)
check("F04 只混入一次（不是 alpha 累加两次）", once[19] == expected_19,
      f"{once[19]} vs {expected_19}")
# 把输出再喂回去，会再抬一次——说明是纯函数而非幂等变换；
# 正确性由「每次 score_all 都从原始分重算」保证，这里把该事实固化下来。
twice = lex.apply_concept_bridge(Q, ALL, once)
check("重复喂输出会再抬（证明必须在 score_all 内只调一次，代码确实是这么做的）",
      twice[19] > once[19])
import inspect  # noqa: E402
_src_text = inspect.getsource(lex.score_all)
# 只数「调用」——docstring 里也提到了函数名（不带括号），所以用带括号的形式定位
_n_calls = _src_text.count("apply_concept_bridge(")
check("score_all 里 apply_concept_bridge 只被调用一次",
      _n_calls == 1, str(_n_calls))
check("apply_concept_bridge 是 score_all 的最后一步",
      _src_text.rstrip().splitlines()[-1].strip().startswith(
          "return apply_concept_bridge("),
      _src_text.rstrip().splitlines()[-1].strip())

print()
print("=" * 70)
print("G 分数始终在 0..1")
print("=" * 70)
edge_cases = [
    {**MEM_F04, "id": 1},
]
# 覆盖各种 s 起点：0、极小、中间、接近 1、正好 1
for s0 in (0.0, 1e-9, 0.000001, 0.5, 0.999999, 1.0):
    got = lex.apply_concept_bridge(Q, ALL, {19: s0, 54: s0, 900: s0})
    check(f"s={s0:<9} → F04={got[19]:<9} 在 [0,1]",
          0.0 <= got[19] <= 1.0, str(got[19]))
    check(f"s={s0:<9} → F11={got[54]:<9} 在 [0,1]",
          0.0 <= got[54] <= 1.0, str(got[54]))
# 未命中的条目必须原样返回
check("未命中条目分数不变", lex.apply_concept_bridge(Q, ALL, {900: 0.42})[900] == 0.42)
# 整批扫一遍真实分数
full = score_with(Q, ALL)
check("整批分数全在 [0,1]", all(0.0 <= v <= 1.0 for v in full.values()), str(full))
check("α 仍是 0.20（本批不得改参数）", lex.CONCEPT_BRIDGE_ALPHA == 0.20,
      str(lex.CONCEPT_BRIDGE_ALPHA))

print()
print("=" * 70)
print("H 桥接关闭时与原始算法完全一致")
print("=" * 70)
lex.CONCEPT_BRIDGE_ENABLED = False
try:
    off = score_with(Q, ALL)
    off2 = score_with(Q, ALL)
    # 关闭时的恒等映射必须在「确实处于关闭状态」时断言，
    # 否则读到的已是恢复后的开关，测到的是别的东西。
    identity_ok = (lex.apply_concept_bridge(Q, ALL, {19: 0.3, 54: 0.2, 900: 0.1})
                   == {19: 0.3, 54: 0.2, 900: 0.1})
finally:
    lex.CONCEPT_BRIDGE_ENABLED = True
on = score_with(Q, ALL)
check("关闭后两次调用一致", off == off2)
check("关闭后 apply_concept_bridge 是恒等映射", identity_ok, str(off))
check("关闭时 F04 分数低于开启时", off[19] < on[19], f"{off.get(19)} vs {on.get(19)}")
check("关闭时未命中条目与开启时相同", off[900] == on[900],
      f"{off.get(900)} vs {on.get(900)}")
# 「关闭 == 原始算法」的等价性证明：把 α 置 0 应当与关开关得到完全相同的结果
_n_op = lex.CONCEPT_BRIDGE_ALPHA
try:
    lex.CONCEPT_BRIDGE_ALPHA = 0.0
    zero = score_with(Q, ALL)
finally:
    lex.CONCEPT_BRIDGE_ALPHA = _n_op
check("α 置 0 时与关闭开关结果一致（两个关闭途径等价）", zero == off,
      f"{zero.get(19)} vs {off.get(19)}")

print()
print("=" * 70)
print("I 触发字段范围：title / tags / body 参与，元数据不参与")
print("=" * 70)
base = {"id": 901, "content": "# 普通标题\n\n普通正文", "tags_json": "[]"}
check("① 无机制词 → 不触发", lex.concept_bridge_hit(Q, base) is False)
check("② 仅标题含机制词 → 触发",
      lex.concept_bridge_hit(Q, {**base, "content": "# 改完需重启 DSH 才生效\n\n普通正文"}) is True)
check("③ 仅正文含机制词 → 触发",
      lex.concept_bridge_hit(Q, {**base, "content": "# 普通标题\n\n必须重启才生效"}) is True)
check("④ 仅 tags 含机制词 → 触发",
      lex.concept_bridge_hit(Q, {**base, "tags_json": '["必须重启"]'}) is True)
check("⑤ 仅 category 含机制词 → 不触发",
      lex.concept_bridge_hit(Q, {**base, "category": "才生效"}) is False)
check("⑥ 仅 memory_type 含机制词 → 不触发",
      lex.concept_bridge_hit(Q, {**base, "memory_type": "需重启"}) is False)
check("⑦ 仅 importance_score 高 → 不触发",
      lex.concept_bridge_hit(Q, {**base, "importance_score": 100}) is False)
check("⑧ 仅 id 数字 → 不触发",
      lex.concept_bridge_hit(Q, {**base, "id": 1103}) is False)
check("⑨ tags 为逗号字符串形态也能正确解析",
      lex.concept_bridge_hit(Q, {**base, "tags_json": "必须重启,才生效"}) is True)

print()
print("=" * 70)
print("J 公式精确性：s' == s + 0.20*(1-s)")
print("=" * 70)
for s0 in (0.0, 0.123456, 0.185999, 0.348799, 0.5, 0.75, 0.999999):
    got = lex.apply_concept_bridge(Q, ALL, {19: s0})[19]
    want = round(s0 + 0.20 * (1.0 - s0), 6)
    check(f"s={s0:<10} → {got:<9} == {want:<9}", got == want, f"{got} vs {want}")
check("未触发条目的分数一字不改",
      lex.apply_concept_bridge(Q, [MEM_PLAIN], {900: 0.123456})[900] == 0.123456)

print()
print("=" * 70)
print(f"结果：{PASS} PASS / {FAIL} FAIL / 共 {PASS + FAIL} 项")
if FAILED:
    print("失败项：")
    for n in FAILED:
        print(f"  - {n}")
print("=" * 70)
sys.exit(0 if FAIL == 0 else 1)
