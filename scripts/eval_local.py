"""用同一批自然语言题给本地 dsh-memory 的检索打分，与 scripts/eval_recall.py 口径一致。

为什么要单独写：本地检索是 PowerShell 的两段子进程（记忆检索.ps1 + 知识检索.ps1），
没法在 Python 里 import，只能 subprocess 调，且控制台是 GBK 输出，必须强制 UTF-8。
标准答案与 eval_recall.py 的 NL_CASES 完全相同，两边指标才可比。
用法：python -X utf8 scripts/eval_local.py
"""
import re, subprocess, time, statistics
import sys
from pathlib import Path

# 2026-11-08 模块化：脚本进 bin/、jsonl 进 data/。根目录只认一处来源。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.knowledge import DSH_MEMORY_ROOT  # noqa: E402

ROOT = DSH_MEMORY_ROOT
BIN = str(Path(ROOT) / "bin")

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


def run_ps(script, query):
    t = time.time()
    p = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
         str(Path(BIN) / script), "-Query", query],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    return p.stdout, int((time.time() - t) * 1000)


if __name__ == "__main__":
    ok1 = ok3 = 0
    times = []
    print(f"== 本地关键词检索 · 自然语言题（{len(NL_CASES)} 题）==")
    for q, want in NL_CASES:
        out, ms = run_ps("记忆检索.ps1", q)
        times.append(ms)
        ids = re.findall(r"\[([A-Z]\d{2,3})\]", out)[:3]
        hit1 = bool(ids) and ids[0] == want
        hit3 = want in ids
        ok1 += hit1
        ok3 += hit3
        mark = "ok  " if hit1 else ("r3  " if hit3 else "MISS")
        print(f"  [{mark}] {q:24s} want={want:4s} got={ids} {ms}ms")
    print(f"  recall@1 {ok1}/{len(NL_CASES)}  recall@3 {ok3}/{len(NL_CASES)}  "
          f"中位延迟 {int(statistics.median(times))}ms")
