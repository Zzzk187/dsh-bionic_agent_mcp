# -*- coding: utf-8 -*-
"""统一测试入口（S6-FIX-06）。

项目原本没有测试入口：每个 `tests/*_test.py` 都是可独立运行的脚本，靠人记住
清单手工跑。这份 runner 只做三件事——**发现、执行、汇总**，不引入任何测试
框架，也不改写任何测试。

用法::

    python tests/run_tests.py             # 跑全部
    python tests/run_tests.py --list      # 只列出会被跑到的文件
    python tests/run_tests.py --only lexical_test,concept_bridge_test
    python tests/run_tests.py --stop      # 第一个失败就停

设计取舍（都踩过）：

1. **每个测试跑在独立子进程里**。它们都会在进程内改 `db.DB_PATH` 指向临时库，
   同进程串跑会互相污染；子进程天然隔离，失败也不会带崩整个 runner。
2. **子进程必须脱离父进程的控制台进程组**（`CREATE_NEW_PROCESS_GROUP`）。
   默认继承时，父进程所在控制台收到的 Ctrl+C 会波及子进程：轻则让
   `portable_restore_test.py` 输出完 64 项却卡在退出阶段拿不到退出码，重则
   打断正在跑的测试。S6-FIX-03/FIX-04 把它记成「该测试进程不肯退出」，
   S6-FIX-06 定位到真因是控制台信号继承。**脱离进程组后它会自己正常退出**，
   所以不需要 runpy 在进程内跑之类的绕法。
3. **不能用 `CREATE_NO_WINDOW`**。实测它会让子进程里的 `os.kill(pid, 0)`
   （`app/portable_restore.py` 判断恢复锁是否还有主）抛 OSError，把活着的锁
   误判成僵尸锁，`portable_restore_test.py` 的 E47/E48 两项因此假失败。
   有控制台、但独立进程组 = 既不被 Ctrl+C 波及，也让 `os.kill` 语义正常。
4. **判定只看退出码**。所有既有测试的约定就是「全通过 exit 0、有失败 exit 1」，
   runner 不去解析输出文本——解析会把「测试自己打印了什么」变成契约。
5. 每项都有超时（默认 600s）。超时算失败并**明确标注 TIMEOUT**，不静默跳过。
6. **清掉 `PYTHONOPTIMIZE`**（S6-FINAL-FIX P2-4）。
   `smoke_test` / `embedding_test` / `recall_profile_test` 用的是裸 `assert`，
   `python -O` 会把它们整条剥掉，于是「通过」而实际什么都没验。实测
   `PYTHONOPTIMIZE=1` 时这 3 项全部假通过。**注意 `-X optimize=0` 挡不住环境
   变量**（实测仍假通过），只能真的把它清空。
7. **`--only` 拼错名字返回 rc=2**（S6-FINAL-FIX P3-1）。以前只 warn 然后照跑，
   拼错的人会以为测试真的过了。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"
PYTHON = sys.executable

DEFAULT_TIMEOUT = 600.0


def discover() -> list[Path]:
    """按文件名排序返回 tests/*_test.py，排除自身。"""
    return sorted(p for p in TESTS.glob("*_test.py") if p.is_file())


def _env() -> dict:
    """子进程环境。

    S6-FINAL-FIX P2-4：**必须清掉 `PYTHONOPTIMIZE`**。有 3 个测试
    （`smoke_test` / `embedding_test` / `recall_profile_test`）内部用的是裸
    `assert`，而 `python -O` 会把 `assert` 整条剥掉——于是断言既不执行也不
    报错，测试打印「通过」并以 0 退出。实测 `PYTHONOPTIMIZE=1` 时这 3 项
    全部假通过。

    **为什么是清空而不是加 `-X optimize=0`**：实测环境变量优先级高于命令行
    开关——`PYTHONOPTIMIZE=1` + `-X optimize=0` 仍然 `sys.flags.optimize == 1`、
    断言照样被剥离（rc=0 假通过）。所以命令行开关不能当保险，只能真清变量。

    清成空串而不是 `pop` 掉：两者实测等价（都得到 `sys.flags.optimize == 0`），
    但空串留痕——`os.environ` 里看得见我们动过它，`pop` 之后就分不清「本来
    没有」还是「我们删了」。取值 `''` 与 `'0'` 都不会开优化，`'1'`/`'2'` 会。
    """
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONOPTIMIZE"] = ""
    return env


def _child_flags() -> int:
    """子进程创建标志：脱离父进程的控制台进程组。

    Windows 上取 `CREATE_NEW_PROCESS_GROUP`（512）。**不要换成
    `CREATE_NO_WINDOW`**——那会让 `os.kill(pid, 0)` 失败并把活锁判成僵尸锁，
    让 `portable_restore_test` 的 E47/E48 假失败（见模块 docstring 第 3 条）。
    其它平台返回 0，python 的 subprocess 会忽略这个参数。
    """
    return getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


def run_one(path: Path, timeout: float) -> tuple[bool, float, str]:
    """跑一个测试，返回 (是否通过, 耗时秒, 备注)。"""
    started = time.time()
    # 只用 `-X utf8`：`-X optimize=0` **挡不住** `PYTHONOPTIMIZE=1`（实测环境
    # 变量优先级更高），真正生效的是 `_env()` 里把该变量清空（P2-4）。
    cmd = [PYTHON, "-X", "utf8", str(path)]
    note = ""

    try:
        proc = subprocess.run(
            cmd, cwd=str(ROOT), env=_env(), timeout=timeout,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=_child_flags(),
        )
        ok = proc.returncode == 0
        if not ok:
            tail = proc.stdout.decode("utf-8", "replace").strip().splitlines()[-6:]
            note = " | ".join(tail[-2:])
    except subprocess.TimeoutExpired:
        ok = False
        note = ("timeout" if not note else note + " timeout")
    return ok, time.time() - started, note


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="统一测试入口")
    ap.add_argument("--only", default="", help="逗号分隔的测试名（可省略 _test.py）")
    ap.add_argument("--list", action="store_true", help="只列出会跑的文件")
    ap.add_argument("--stop", action="store_true", help="第一个失败就停")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                    help=f"单项超时秒数（默认 {DEFAULT_TIMEOUT:.0f}）")
    args = ap.parse_args(argv)

    cases = discover()
    if args.only.strip():
        # 允许写 lexical / lexical_test / lexical_test.py 三种形式
        norm = set()
        for n in (x.strip() for x in args.only.split(",") if x.strip()):
            base = n[:-3] if n.endswith(".py") else n
            if not base.endswith("_test"):
                base += "_test"
            norm.add(base + ".py")
        cases = [c for c in cases if c.name in norm]
        missing = sorted(norm - {c.name for c in cases})
        if missing:
            # S6-FINAL-FIX P3-1：拼错名字必须失败。以前只 print [warn] 然后照常
            # 跑剩下的并可能「全部通过」——拼错的人会以为测试过了。
            print("[error] 未找到：%s" % ", ".join(missing), file=sys.stderr)
            print("可用：%s" % ", ".join(c.name for c in discover()), file=sys.stderr)
            return 2

    if args.list:
        for c in cases:
            print(c.name)
        print("共 %d 项" % len(cases))
        return 0

    if not cases:
        print("没有可运行的测试")
        return 1

    print("统一测试入口 · %d 项 · 超时 %.0fs/项" % (len(cases), args.timeout))
    print("-" * 72)
    results: list[tuple[str, bool, float, str]] = []
    t0 = time.time()
    for c in cases:
        ok, secs, note = run_one(c, args.timeout)
        results.append((c.name, ok, secs, note))
        mark = "PASS" if ok else "FAIL"
        extra = ("  (%s)" % note) if note else ""
        print("%-4s %-34s %6.1fs%s" % (mark, c.name, secs, extra))
        sys.stdout.flush()
        if not ok and args.stop:
            print("--stop：中止")
            break

    total = len(results)
    passed = sum(1 for _, ok, _, _ in results if ok)
    failed = [n for n, ok, _, _ in results if not ok]
    print("-" * 72)
    print("汇总：%d/%d 通过，耗时 %.1fs" % (passed, total, time.time() - t0))
    if failed:
        print("失败：%s" % ", ".join(failed))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
