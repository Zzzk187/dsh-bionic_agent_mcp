# -*- coding: utf-8 -*-
"""OPT-04a：测试数据库隔离的自动化验证。

覆盖三种入口（任务书要求「主进程 / 子进程 / 直接运行单项测试」）：

- **主进程**：本文件 import 闸门后，`db.DB_PATH` 必须指向沙箱而不是生产库。
- **子进程**：外部 `python tests/smoke_test.py` 直跑，其沙箱路径从闸门的
  stderr 里读出来，并核生产库指纹未变。
- **运行器**：`tests/run_tests.py` 的 `_env()` 必须把「指向生产库」的
  `BIONIC_DB_PATH` 清掉（否则子进程的闸门会按「尊重调用方」放行，直接开生产库）。

另外静态核查：36 个测试文件都 import 了闸门、且都在任何 `app.*` 之前。

不碰生产库：所有断言只对生产库做**文件级 SHA-256**，不开连接。
"""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import _env_guard  # noqa: E402,F401  OPT-04a：数据库隔离闸门（必须在任何 app.* 之前）

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # noqa: E402

from app import db  # noqa: E402

TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parent
TESTS = ROOT / "tests"
PRODUCTION_DB = ROOT / "data" / "agent.db"
RUNNER = TESTS / "run_tests.py"
GUARD = TESTS / "_env_guard.py"

RESULTS: list[tuple[bool, str, str]] = []


def check(name: str, cond, detail: str = "") -> bool:
    ok = bool(cond)
    RESULTS.append((ok, name, detail))
    line = "%s %s" % ("[PASS]" if ok else "[FAIL]", name)
    if detail and not ok:
        line += " — %s" % detail
    print(line)
    return ok


def sha256_of(path: Path) -> str:
    if not path.is_file():
        return ""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest().upper()


def sidecars(path: Path) -> dict[str, int | None]:
    out: dict[str, int | None] = {}
    for suf in ("-wal", "-shm", "-journal"):
        q = path.with_name(path.name + suf)
        out[suf] = q.stat().st_size if q.is_file() else None
    return out


def spawn(cmd: list[str], env: dict | None = None, timeout: float = 120.0):
    proc = subprocess.run(
        cmd, cwd=str(ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    return proc.returncode, proc.stdout.decode("utf-8", "replace")


# ---------------------------------------------------------------- 静态：闸门覆盖
def test_all_tests_guarded() -> None:
    print()
    print("=" * 70)
    print("A 静态：36 个测试文件都 import 了闸门，且都在任何 app.* 之前")
    print("=" * 70)

    files = sorted(TESTS.glob("*_test.py"))
    # 本文件自己也在 * _test.py 里，它同样带了闸门，所以一起算。
    #
    # ★ 这里断言的是"下限"不是精确值。原先写死 `== 37`，本意是防"有人删测试文件"，
    # 但精确值等于宣告"永远不许新增测试"——OPT-04a 收尾补 mcp_tools_smoke_test.py
    # 时就因此假失败。真正的检查是下面逐个文件核对闸门存在且位置正确，
    # 数量只用来兜住"被删"这一种情况，所以用 >= 下限即可。
    MIN_TEST_FILES = 37
    check("测试文件数 >= %d（原 37，只许多不许少）" % MIN_TEST_FILES,
          len(files) >= MIN_TEST_FILES, "实际 %d" % len(files))

    missing: list[str] = []
    late: list[str] = []
    unparsable: list[str] = []
    for p in files:
        text = p.read_text(encoding="utf-8")
        try:
            tree = ast.parse(text)
        except SyntaxError as exc:
            unparsable.append("%s: %s" % (p.name, exc))
            continue
        guard_line = None
        for node in tree.body:            # 只看模块顶层
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                         else [node.module or ""])
                if "_env_guard" in names:
                    guard_line = node.lineno
        if guard_line is None:
            missing.append(p.name)
            continue
        first_app = None
        for node in ast.walk(tree):       # app 导入可能在函数体内，要全树找
            ln = getattr(node, "lineno", None)
            if ln is None:
                continue
            is_app = ((isinstance(node, ast.Import)
                       and any(a.name == "app" or a.name.startswith("app.") for a in node.names))
                      or (isinstance(node, ast.ImportFrom) and node.module
                          and (node.module == "app" or node.module.startswith("app."))))
            if is_app:
                first_app = ln if first_app is None else min(first_app, ln)
        if first_app is not None and guard_line >= first_app:
            late.append("%s(闸门%d/app%d)" % (p.name, guard_line, first_app))

    check("全部文件可解析", not unparsable, "; ".join(unparsable))
    check("每个测试都 import 了 _env_guard", not missing, "缺: " + ", ".join(missing))
    check("闸门都早于第一个 app.* 导入", not late, "晚: " + ", ".join(late))

    # run_tests.py 自己不 import app.*，无需闸门，但必须存在
    check("tests/run_tests.py 存在", RUNNER.is_file())
    check("tests/_env_guard.py 存在", GUARD.is_file())


# ---------------------------------------------------------------- 主进程
def test_main_process() -> None:
    print()
    print("=" * 70)
    print("B 主进程：db.DB_PATH 已指向沙箱，不是生产库")
    print("=" * 70)

    box = _env_guard.sandbox_dir()
    check("闸门已启用（sandbox_dir 非 None）", box is not None, repr(box))
    check("沙箱位于 <项目根>/var/test_db/ 下",
          box is not None and str(box).startswith(str(ROOT / "var" / "test_db")),
          repr(box))
    check("沙箱目录存在", box is not None and box.is_dir(), repr(box))

    db_path = Path(db.DB_PATH).resolve()
    check("db.DB_PATH 指向沙箱内 agent.db",
          box is not None and db_path == (box / "agent.db").resolve(),
          str(db_path))
    check("db.DB_PATH 不是生产库",
          db_path != PRODUCTION_DB.resolve(), str(db_path))
    check("is_isolated() 为真", _env_guard.is_isolated())
    check("环境变量 BIONIC_DB_PATH 指向沙箱",
          Path(os.environ.get("BIONIC_DB_PATH", "")).resolve() == db_path,
          repr(os.environ.get("BIONIC_DB_PATH")))
    check("环境变量 BIONIC_SNAPSHOT_DIR 指向沙箱内 snapshots",
          os.environ.get("BIONIC_SNAPSHOT_DIR", "").startswith(str(box)),
          repr(os.environ.get("BIONIC_SNAPSHOT_DIR")))

    # 沙箱必须是生产库的副本（有真实语料），否则 lexical 类测试会失去意义
    with db.connect() as conn:
        n_active = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE status='active'").fetchone()[0]
        n_emb = conn.execute("SELECT COUNT(*) FROM memory_embeddings").fetchone()[0]
    check("沙箱里有活跃记忆（说明是生产库副本，不是空库）", n_active > 0, "%d 条" % n_active)
    check("沙箱里有向量", n_emb > 0, "%d 条" % n_emb)

    # 写沙箱不得影响生产库
    before = sha256_of(PRODUCTION_DB)
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO memories (content, category, source, confidence, status,"
            " created_at, updated_at, tags_json, memory_status)"
            " VALUES ('opt04a-isolation-probe', 'general', 'test', 0.5, 'active',"
            " '2026-01-01 00:00:00', '2026-01-01 00:00:00', '[]', 'active')")
        conn.commit()
    after = sha256_of(PRODUCTION_DB)
    check("写沙箱后生产库 SHA-256 未变", before == after, "%s -> %s" % (before, after))
    with db.connect() as conn:
        n2 = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE content='opt04a-isolation-probe'"
        ).fetchone()[0]
    check("写入确实落在沙箱里（沙箱能查到这条）", n2 == 1, "%d 条" % n2)


# ---------------------------------------------------------------- 运行器
def test_runner_env() -> None:
    print()
    print("=" * 70)
    print("C 运行器：_env() 把指向生产库的 BIONIC_DB_PATH 清掉")
    print("=" * 70)

    spec = importlib.util.spec_from_file_location("_rt_under_test_o4a", RUNNER)
    rt = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rt)

    prod_abs = str(PRODUCTION_DB)
    sandbox_like = str(ROOT / "var" / "test_db" / "x" / "agent.db")
    sentinel = str(ROOT / "data" / "snapshots")

    cases = [
        ("未设", None, None, None),
        ("指向生产库（绝对路径）", prod_abs, sentinel, None),
        ("指向生产库（相对路径）", "data/agent.db", sentinel, None),
        ("空白值", "   ", sentinel, None),
        ("指向别的库（应保留）", sandbox_like, None, sandbox_like),
    ]
    old = dict(os.environ)
    try:
        for name, dbp, snap, expect_db in cases:
            os.environ.pop("BIONIC_DB_PATH", None)
            os.environ.pop("BIONIC_SNAPSHOT_DIR", None)
            if dbp is not None:
                os.environ["BIONIC_DB_PATH"] = dbp
            if snap is not None:
                os.environ["BIONIC_SNAPSHOT_DIR"] = snap
            env = rt._env()
            got_db = env.get("BIONIC_DB_PATH")
            got_snap = env.get("BIONIC_SNAPSHOT_DIR")
            ok = got_db == expect_db
            if expect_db is None:
                ok = ok and got_snap is None
            check("_env() %s → %r" % (name, expect_db), ok,
                  "得到 db=%r snap=%r" % (got_db, got_snap))
    finally:
        os.environ.clear()
        os.environ.update(old)

    # 保留了原有两条保证
    env = rt._env()
    check("_env() 仍设 PYTHONIOENCODING=utf-8",
          env.get("PYTHONIOENCODING") == "utf-8", repr(env.get("PYTHONIOENCODING")))
    check("_env() 仍把项目根放进 PYTHONPATH",
          str(ROOT) in (env.get("PYTHONPATH") or ""), repr(env.get("PYTHONPATH")))
    check("_env() 仍不开 PYTHONOPTIMIZE",
          env.get("PYTHONOPTIMIZE") not in ("1", "2"), repr(env.get("PYTHONOPTIMIZE")))


# ---------------------------------------------------------------- 子进程
def test_subprocess_direct() -> None:
    print()
    print("=" * 70)
    print("D 子进程：直接跑单个测试文件，它自己建沙箱且不写生产库")
    print("=" * 70)

    before_sha = sha256_of(PRODUCTION_DB)
    before_side = sidecars(PRODUCTION_DB)

    # 清掉继承来的隔离变量，模拟「用户在自己的 shell 里直接跑一个测试文件」
    env = dict(os.environ)
    env.pop("BIONIC_DB_PATH", None)
    env.pop("BIONIC_SNAPSHOT_DIR", None)
    env["BIONIC_TEST_VERBOSE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONOPTIMIZE"] = ""

    rc, out = spawn([sys.executable, "-X", "utf8", str(TESTS / "smoke_test.py")], env=env)
    check("直接跑 smoke_test.py 返回 0", rc == 0, "rc=%d" % rc)

    box = ""
    for line in out.splitlines():
        if "[env_guard] 测试沙箱：" in line:
            box = line.split("：", 1)[1].split("（", 1)[0].strip()
    check("子进程打印了自己的沙箱路径", bool(box), out.strip()[:300])
    check("子进程沙箱位于 var/test_db 下",
          box.startswith(str(ROOT / "var" / "test_db")), box)
    check("子进程沙箱与父进程沙箱不是同一个",
          box and _env_guard.sandbox_dir() is not None
          and Path(box).name != _env_guard.sandbox_dir().name,
          "child=%s parent=%s" % (box, _env_guard.sandbox_dir()))
    check("子进程沙箱已在自己退出时删除（或不留下 agent.db 之外的垃圾）",
          not (Path(box) / "agent.db").is_file(), box)

    after_sha = sha256_of(PRODUCTION_DB)
    after_side = sidecars(PRODUCTION_DB)
    check("子进程跑完后生产库 SHA-256 未变", before_sha == after_sha,
          "%s -> %s" % (before_sha, after_sha))
    check("子进程跑完后生产库 sidecar 未变", before_side == after_side,
          "%r -> %r" % (before_side, after_side))


# ---------------------------------------------------------------- 生产库零写入
def test_production_untouched() -> None:
    print()
    print("=" * 70)
    print("E 生产库整体：SHA-256 / size / mtime / sidecar 全程未变")
    print("=" * 70)

    check("生产库存在", PRODUCTION_DB.is_file(), str(PRODUCTION_DB))
    sha = sha256_of(PRODUCTION_DB)
    st = PRODUCTION_DB.stat()
    print("     sha256 = %s" % sha)
    print("     size   = %d" % st.st_size)
    print("     mtime  = %d" % st.st_mtime_ns)
    print("     sidecar= %r" % sidecars(PRODUCTION_DB))

    # 本文件从 import 到现在一直在写沙箱，生产库不该有任何变化
    check("本文件的 _env_guard 沙箱不是生产库目录",
          _env_guard.sandbox_dir() is not None
          and _env_guard.sandbox_dir() != PRODUCTION_DB.parent,
          str(_env_guard.sandbox_dir()))
    check("生产库 SHA-256 与闸门记录的基线一致",
          sha == _env_guard.production_sha256(), "%s vs %s" % (sha, _env_guard.production_sha256()))


# ---------------------------------------------------------------- 伪隔离
GUARD_OVERRIDE_PROBE = r'''
import json, os, sys
from pathlib import Path
root = Path(sys.argv[1]); mode = sys.argv[2]
sys.path.insert(0, str(root))
sys.path.insert(0, str(root / "tests"))
prod = root / "data" / "agent.db"
values = {
    "abs":   str(prod),
    "rel":   "data/agent.db",
    "relbs": "data\\agent.db",
    "upper": str(prod).upper(),
    "blank": "   ",
    "other": str(root / "var" / "elsewhere" / "x.db"),
}
os.environ["BIONIC_DB_PATH"] = values[mode]
import _env_guard
import app.db as db
print("@@RESULT@@" + json.dumps({
    "mode": mode,
    "is_isolated": _env_guard.is_isolated(),
    "db_path": str(db.DB_PATH),
    "is_prod": Path(db.DB_PATH).resolve() == prod.resolve(),
    "status": _env_guard._status,
}, ensure_ascii=False))
'''


def test_guard_overrides_production_env() -> None:
    """调用方把 BIONIC_DB_PATH 设成**生产库**时，闸门必须接管。

    这是最危险的一种「以为隔离了」：环境变量非空、看着像配好了，实际
    `db.DB_PATH` 就是 `data/agent.db`。每个用例必须在**独立子进程**里跑，
    因为 `app/db.py` 的 `DB_PATH` 在导入那一刻就固化了。
    """
    print()
    print("=" * 70)
    print("F 伪隔离：BIONIC_DB_PATH 指着生产库时，闸门必须接管")
    print("=" * 70)

    before_sha = sha256_of(PRODUCTION_DB)
    before_side = sidecars(PRODUCTION_DB)

    base_env = dict(os.environ)
    base_env.pop("BIONIC_DB_PATH", None)
    base_env.pop("BIONIC_SNAPSHOT_DIR", None)
    base_env["PYTHONIOENCODING"] = "utf-8"
    base_env["PYTHONOPTIMIZE"] = ""

    must_takeover = ["abs", "rel", "relbs", "upper", "blank"]
    for mode in must_takeover + ["other"]:
        rc, out = spawn([sys.executable, "-X", "utf8", "-c", GUARD_OVERRIDE_PROBE,
                         str(ROOT), mode], env=base_env, timeout=180)
        payload = None
        for line in out.splitlines():
            if line.startswith("@@RESULT@@"):
                try:
                    payload = json.loads(line[len("@@RESULT@@"):])
                except Exception:
                    payload = None
        if payload is None:
            check("F/%s 子进程给出结果" % mode, False, "rc=%d out=%s" % (rc, out.strip()[:300]))
            continue

        if mode == "other":
            # 指向别的库：应当被尊重，不覆盖
            check("F/%s 指向别的库时闸门不插手" % mode,
                  payload["is_isolated"] and "elsewhere" in payload["db_path"]
                  and "沿用调用方" in payload["status"],
                  payload["status"])
        else:
            check("F/%s 闸门接管并改指沙箱" % mode,
                  (not payload["is_prod"]) and payload["is_isolated"]
                  and "var" in payload["db_path"] and "test_db" in payload["db_path"],
                  "db_path=%s is_prod=%s" % (payload["db_path"], payload["is_prod"]))
            if mode in ("abs", "rel", "relbs"):
                check("F/%s 状态里说明了覆盖原因" % mode,
                      "覆盖了调用方" in payload["status"], payload["status"])

    after_sha = sha256_of(PRODUCTION_DB)
    after_side = sidecars(PRODUCTION_DB)
    check("F/生产库 SHA-256 在六个子进程之后仍未变",
          before_sha == after_sha, "%s -> %s" % (before_sha, after_sha))
    check("F/生产库 sidecar 在六个子进程之后仍未变",
          before_side == after_side, "%r -> %r" % (before_side, after_side))


def main() -> int:
    test_all_tests_guarded()
    test_main_process()
    test_runner_env()
    test_subprocess_direct()
    test_production_untouched()
    test_guard_overrides_production_env()

    print()
    print("=" * 70)
    total = len(RESULTS)
    passed = sum(1 for ok, _n, _d in RESULTS if ok)
    failed = [(n, d) for ok, n, d in RESULTS if not ok]
    print("结果：%d PASS / %d FAIL / 共 %d 项" % (passed, len(failed), total))
    for n, d in failed:
        print("  FAIL: %s — %s" % (n, d))
    print("=" * 70)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
