# -*- coding: utf-8 -*-
"""S6-FINAL-FIX P2-4：统一测试入口不得因 PYTHONOPTIMIZE 而假通过。

问题
----
`tests/smoke_test.py`、`tests/embedding_test.py`、`tests/recall_profile_test.py`
内部用的是裸 `assert`。`python -O`（或环境变量 `PYTHONOPTIMIZE=1`）会把 `assert`
整条语句剥掉——断言既不执行也不报错，脚本打印「通过」并以 0 退出。
于是 `tests/run_tests.py` 会报「全部通过」，而实际上这三项什么都没验。

这是一个**静默失效**的口子：不报错、不警告、结果看起来完全正常。S6-FINAL
审计用 `PYTHONOPTIMIZE=1 python tests/run_tests.py --only smoke,embedding,recall_profile`
实测到了 3/3 全 PASS 的假象。

本文件验证四件事
----------------
1. 先证明「假通过」这个前提是真的——`-O` 下故意失败的 assert 会以 0 退出。
2. `run_tests._env()` 把 `PYTHONOPTIMIZE` 清成不开优化的值。
3. 用 runner 自己的 `_env()` 跑那个故意失败的脚本，必须得到非零退出码。
4. 端到端：外部设 `PYTHONOPTIMIZE=1` 时跑真正的 `run_tests.py`，
   三个裸 assert 测试的结果与不设时一致（都真跑断言）。

跑法：`python tests/run_tests.py --only run_tests_optimize`
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path
import _env_guard  # noqa: E402,F401  OPT-04a：数据库隔离闸门（必须在任何 app.* 之前）

ROOT = Path(__file__).resolve().parent.parent
RUNNER = ROOT / "tests" / "run_tests.py"
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
    d = Path(tempfile.mkdtemp(prefix="runner-opt-"))
    _TMPS.append(d)
    return d


# 故意失败的断言：断言生效 → 打印 FAIL 并 rc=1；断言被剥离 → 打印 PASS(假) 并 rc=0。
_BAD_ASSERT = '''# -*- coding: utf-8 -*-
import sys
try:
    assert 1 == 2, "故意失败"
except AssertionError:
    print("FAIL: 断言生效")
    sys.exit(1)
print("PASS(假): 断言被剥离")
sys.exit(0)
'''


def load_runner():
    """按路径导入 run_tests.py（它不是测试文件，没有 *_test 后缀）。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location("_rt_under_test", RUNNER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run(cmd, env=None, cwd=str(ROOT)):
    p = subprocess.run(
        cmd, cwd=cwd, env=env, timeout=300,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    return p.returncode, p.stdout.decode("utf-8", "replace")


def main() -> int:
    d = mktmp()
    bad = d / "bad_assert.py"
    bad.write_text(_BAD_ASSERT, encoding="utf-8")

    print("=" * 70)
    print("A 前提验证：-O / PYTHONOPTIMIZE 确实会让断言假通过")
    print("=" * 70)

    rc_plain, out_plain = run([sys.executable, "-X", "utf8", str(bad)])
    check("不优化时故意失败的 assert → rc=1",
          rc_plain == 1, "rc=%d out=%s" % (rc_plain, out_plain.strip()))

    env_o = dict(os.environ, PYTHONOPTIMIZE="1")
    rc_o, out_o = run([sys.executable, "-X", "utf8", str(bad)], env=env_o)
    check("PYTHONOPTIMIZE=1 时同一个脚本 → rc=0（假通过，这就是要防的）",
          rc_o == 0, "rc=%d out=%s" % (rc_o, out_o.strip()))

    # 记录一个反直觉的实测事实：-X optimize=0 挡不住环境变量。
    rc_x, out_x = run([sys.executable, "-X", "utf8", "-X", "optimize=0", str(bad)],
                      env=env_o)
    check("-X optimize=0 挡不住 PYTHONOPTIMIZE=1（所以只能清环境变量）",
          rc_x == 0, "rc=%d out=%s" % (rc_x, out_x.strip()))

    print()
    print("=" * 70)
    print("B runner 的 _env() 把 PYTHONOPTIMIZE 清成不开优化的值")
    print("=" * 70)

    rt = load_runner()
    old = os.environ.get("PYTHONOPTIMIZE")
    os.environ["PYTHONOPTIMIZE"] = "1"
    try:
        env = rt._env()
    finally:
        if old is None:
            os.environ.pop("PYTHONOPTIMIZE", None)
        else:
            os.environ["PYTHONOPTIMIZE"] = old

    check("_env() 里 PYTHONOPTIMIZE 不是 '1'/'2'",
          env.get("PYTHONOPTIMIZE") not in ("1", "2"),
          repr(env.get("PYTHONOPTIMIZE")))
    check("_env() 保留了 PYTHONIOENCODING=utf-8",
          env.get("PYTHONIOENCODING") == "utf-8", repr(env.get("PYTHONIOENCODING")))
    check("_env() 把项目根放进 PYTHONPATH",
          str(ROOT) in (env.get("PYTHONPATH") or ""), repr(env.get("PYTHONPATH")))

    # 用 runner 的环境跑那个故意失败的脚本：必须真跑断言
    rc_env, out_env = run([sys.executable, "-X", "utf8", str(bad)], env=env)
    check("用 _env() 跑故意失败的 assert → rc=1（断言生效）",
          rc_env == 1, "rc=%d out=%s" % (rc_env, out_env.strip()))

    print()
    print("=" * 70)
    print("C 端到端：外部设 PYTHONOPTIMIZE=1 时 runner 仍真跑断言")
    print("=" * 70)

    targets = ["--only", "smoke,embedding,recall_profile"]
    rc_no, out_no = run([sys.executable, "-X", "utf8", str(RUNNER)] + targets)
    check("不设 PYTHONOPTIMIZE 时三个裸 assert 测试通过（基线）",
          rc_no == 0, "rc=%d" % rc_no)

    rc_yes, out_yes = run([sys.executable, "-X", "utf8", str(RUNNER)] + targets,
                          env=dict(os.environ, PYTHONOPTIMIZE="1"))
    check("设了 PYTHONOPTIMIZE=1 后 runner 结果与基线一致",
          rc_yes == rc_no,
          "基线 rc=%d vs 设了 rc=%d" % (rc_no, rc_yes))
    check("设了 PYTHONOPTIMIZE=1 后 runner 仍然报 3/3 全跑过（不是靠跳过）",
          "3/3 通过" in out_yes, out_yes.strip().splitlines()[-2:])

    print()
    print("=" * 70)
    print("D runner 会显式记录清除了该变量（可审计）")
    print("=" * 70)
    src = RUNNER.read_text(encoding="utf-8")
    check("源码里显式写了 PYTHONOPTIMIZE 处理",
          "PYTHONOPTIMIZE" in src)
    check("源码注释记录了 -X optimize=0 挡不住环境变量这一实测事实",
          "挡不住" in src or "优先级" in src)

    print()
    print("=" * 70)
    total = len(RESULTS)
    passed = sum(1 for ok, _, _ in RESULTS if ok)
    failed = [n for ok, n, _ in RESULTS if not ok]
    print("结果：%d PASS / %d FAIL / 共 %d 项" % (passed, len(failed), total))
    if failed:
        for n in failed:
            print("  FAIL: %s" % n)
    print("=" * 70)

    import shutil
    for t in _TMPS:
        shutil.rmtree(t, ignore_errors=True)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
