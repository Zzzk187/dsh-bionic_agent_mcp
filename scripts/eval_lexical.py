"""对比评测：MCP 召回（旧字符余弦 vs 新字段词法）对同一套自然语言题。

存在意义：改打分手法的唯一正当理由是有数字，感觉不算。本脚本用
`脚本 eval_recall.py` 里那 8 条自然语言题（与本地 `记忆检索.ps1` 对照过
的那批），分别用 `semantic_search_memories`（纯语义，基线）和
`recall_memories`（融合，含新词法）打分。

用法：python -X utf8 scripts/eval_lexical.py
"""
import sys, time, statistics
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app import mcp_server as S
from app import memory_lifecycle as life

# 纯语义基线也会 touch access_count（semantic_search_memories 末尾的 _touch_retrieved），
# 而 access_count 反过来喂融合层的 frequency 分量。
# 评测里把它关掉，否则「先跑谁」会改变结果：同一脚本连跑两遍，第二遍
# 基线那条不再是干净的基线。
S._touch_retrieved = lambda *a, **k: None

# 等嵌入模型 ready 再开始。冷进程里 model_state 是 pending，
# _semantic_scores 会直接返回空 dict，语义分量恒为 0——
# 那样测出来的是「没有向量的系统」，不是线上行为。
from scripts.dev.warm import wait_ready
wait_ready()

MEM_CASES = [
    ("唤醒 预算 截断", "P05"),
    ("代理 断网 事故", "L10"),
    ("qq bridge 接进群", "P03"),
    ("用户 画像 喜好", "A02"),
    ("记忆 架构 jsonl", "P09"),
]
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
# 无共同词的纯语义题：本地系统在这里 0/3，MCP 语义 1/3
PURE_SEM_CASES = [
    ("我电脑被我自己搞断网过一次", "L10"),
    ("写脚本带中文字符就崩", "L11"),
    ("装了个东西结果界面上找不到", "L05"),
]


def _anchor(it):
    tags = it.get("tags") or []
    a = next((str(x).replace("src:", "") for x in tags if str(x).startswith("src:")), None)
    return a or it.get("id")


def run(fn, cases, label):
    ok1 = ok3 = 0
    times = []
    misses = []
    for q, want in cases:
        t = time.time()
        items = fn(q)
        ms = int((time.time() - t) * 1000)
        times.append(ms)
        got = [_anchor(it) for it in items][:3]
        strd = [str(g) for g in got]
        h1 = bool(got) and strd[0] == want
        h3 = want in strd
        ok1 += h1
        ok3 += h3
        if not h3:
            misses.append((q, want, got))
    print(f"  {label:22s} @1 {ok1}/{len(cases)}  @3 {ok3}/{len(cases)}  "
          f"中位 {int(statistics.median(times))}ms")
    for q, want, got in misses:
        print(f"      MISS {q:26s} want={want:4s} got={got}")
    return ok1, ok3, len(cases)


def sem(q):
    return S.semantic_search_memories(q, limit=3, category="", min_score=-1)


def fused(q):
    # record_access=False：评测不该往 access_count 里写，否则第二遍跑的就是
    # 被第一遍污染过的频次。
    return life.recall_memories(q, top_k=3, record_access=False)


if __name__ == "__main__":
    for name, cases in (("官方标题题", MEM_CASES), ("自然语言题", NL_CASES),
                        ("无共同词题", PURE_SEM_CASES)):
        print(f"\n== {name}（{len(cases)} 题）==")
        a = run(sem, cases, "纯语义(旧)")
        b = run(fused, cases, "融合(新词法)")
