"""用 dsh-memory 的官方自测集给 Bionic Agent 的记忆检索打分（recall@1/@3 + 延迟）。

存在意义：评估"哪套记忆系统好用"时不能只看手感，要拿同一套带标准答案的题去跑两边。
本脚本复用 查.ps1 -Eval 里那 12 条（标题 case），直接调 core 的语义检索，统计 Top-k。
用法：python -X utf8 scripts/eval_recall.py
"""
import sys, time, statistics
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app import mcp_server as S

# 12 条官方题：query -> 期望命中的「标题关键词」（记忆题给 src 编号，知识题给文件名主干）
MEM_CASES = [
    ("唤醒 预算 截断", "P05"),
    ("代理 断网 事故", "L10"),
    ("qq bridge 接进群", "P03"),
    ("用户 画像 喜好", "A02"),
    ("记忆 架构 jsonl", "P09"),
]
# 自然语言版（更贴近人真的会怎么问）
NL_CASES = [
    ("我那个刷B站的工具怎么用", "P01"),
    ("为什么改了代码没生效", "L03"),
    ("QQ群那个桥接老断线怎么办", "L12"),
    ("上次发布插件踩了什么坑", "F01"),
    ("唤醒的时候上下文被截断怎么办", "P05"),
    ("怎么让代理开的时候不把网络搞断", "L10"),
    ("中文脚本一跑就报语法错", "L11"),
    ("插件装完没反应是为什么", "L05"),
]


def run(cases, label):
    ok1 = ok3 = 0
    times = []
    print(f"\n== {label}（{len(cases)} 题）==")
    for q, want in cases:
        t = time.time()
        try:
            res = S.semantic_search_memories(q, limit=3, category="", min_score=-1)
        except TypeError:
            res = S.semantic_search_memories(q, 3, "", -1)
        ms = int((time.time() - t) * 1000)
        times.append(ms)
        # 结果可能是 list 或 dict（不同版本返回形状不同），统一取 id+tags
        items = res if isinstance(res, list) else (res.get("results") or res.get("memories") or [])
        got = []
        for it in items:
            tags = it.get("tags") or []
            anchor = next((x.replace("src:", "") for x in tags if str(x).startswith("src:")), None)
            got.append(anchor or (it.get("id")))
        hit1 = bool(got) and str(got[0]) == want
        hit3 = want in [str(g) for g in got]
        ok1 += hit1
        ok3 += hit3
        mark = "ok  " if hit1 else ("r3  " if hit3 else "MISS")
        print(f"  [{mark}] {q:24s} want={want:4s} got={got} {ms}ms")
    print(f"  recall@1 {ok1}/{len(cases)}  recall@3 {ok3}/{len(cases)}  "
          f"中位延迟 {int(statistics.median(times))}ms")
    return ok1, ok3, len(cases)


if __name__ == "__main__":
    a = run(MEM_CASES, "官方标题题")
    b = run(NL_CASES, "自然语言题")
    print(f"\n合计：@1 {(a[0]+b[0])}/{a[2]+b[2]}  @3 {(a[1]+b[1])}/{a[2]+b[2]}")
