"""S6-FIX-01 管理面板远程绑定安全守卫测试。

覆盖三件事：
1. `is_local_bind_host()` / `check_bind_safety()` 这两个纯函数的判定；
2. `_run_cli()` 在拒绝远程绑定时**真的**不启动服务、不初始化数据库、退出码非零；
3. 原有启动方式（无参数 / 只给 --port）仍然兼容。

原则：
- 不启动真实的 uvicorn 服务：用假 uvicorn 模块替换 `sys.modules["uvicorn"]`；
- 不碰生产数据库：mock 掉 `ui.core._ensure_db`，并对 `data/agent.db`
  做 sha256 + size 前后比对作为"零副作用"的独立证据；
- 不改任何现有测试的阈值。

运行：python -X utf8 tests/admin_host_guard_test.py
"""
from __future__ import annotations

import hashlib
import sys
import types
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
import _env_guard  # noqa: E402,F401  OPT-04a：数据库隔离闸门（必须在任何 app.* 之前）

from app import admin_ui as ui  # noqa: E402
from app import db as appdb  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"[PASS] {name}" + (f" — {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"[FAIL] {name}" + (f" — {detail}" if detail else ""))


def db_fingerprint():
    """生产库的只读指纹；库不存在时返回 None（只读打开都不需要）。"""
    path = Path(appdb.DB_PATH)
    if not path.is_file():
        return None
    st = path.stat()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return (digest, st.st_size, st.st_mtime_ns)


class FakeUvicorn(types.ModuleType):
    """A 替身：记录 uvicorn.run 收到的参数，不真的起服务。"""

    def __init__(self):
        super().__init__("uvicorn")
        self.calls = []

    def run(self, app, host=None, port=None, **kw):
        self.calls.append({"app": app, "host": host, "port": port, "kw": kw})
        return None


def run_cli(argv, ensure_db_mock, fake_uvicorn):
    """执行 _run_cli 并抓住 SystemExit，返回 (exit_code_or_None, stderr 文本)。"""
    import io
    import contextlib

    buf = io.StringIO()
    code = None
    with patch.object(sys, "argv", ["bionic-agent-admin", *argv]), \
            patch.dict(sys.modules, {"uvicorn": fake_uvicorn}), \
            patch.object(ui.core, "_ensure_db", ensure_db_mock), \
            contextlib.redirect_stderr(buf):
        try:
            ui._run_cli()
        except SystemExit as exc:
            code = exc.code
    return code, buf.getvalue()


# ---------------------------------------------------------------------------
# 第一组：纯函数判定
# ---------------------------------------------------------------------------
def group_pure():
    print("\n--- 第一组：is_local_bind_host / check_bind_safety ---")

    # 允许的（只可能接受本机连接）
    allow = ["127.0.0.1", "localhost", "LOCALHOST", " localhost ", "::1",
             "[::1]", "127.0.0.2", "127.1.2.3"]
    for host in allow:
        ok, msg = ui.check_bind_safety(host, allow_remote=False)
        check(f"允许本机绑定 {host!r}", ok is True and msg == "",
              f"allowed={ok}")

    # 拒绝的（通配 / 私网 / 公网 / 机器名 / 空）
    deny = ["0.0.0.0", "::", "192.168.1.10", "192.168.0.1", "10.0.0.5",
            "172.16.0.1", "8.8.8.8", "my-pc", "", "   "]
    for host in deny:
        ok, msg = ui.check_bind_safety(host, allow_remote=False)
        check(f"拒绝远程绑定 {host!r}", ok is False,
              f"allowed={ok}")
        check(f"拒绝时给出可操作提示 {host!r}",
              "拒绝启动" in msg and "--allow-remote" in msg)

    # 远程 + 显式授权：放行，但提示必须是警告而不是"没问题"
    ok, msg = ui.check_bind_safety("0.0.0.0", allow_remote=True)
    check("远程 + --allow-remote 放行", ok is True)
    check("授权后提示含安全警告",
          "安全警告" in msg and "远程网络" in msg)
    check("授权后提示说明无身份认证", "没有任何身份认证" in msg)
    check("授权后提示说明可删改恢复", "删除" in msg and "恢复" in msg)
    check("授权后提示要求可信网络", "可信网络" in msg)
    check("警告不冒充认证", "不是认证" in msg)

    # 本机 + 多余的 --allow-remote：允许，但说明它没用
    ok, msg = ui.check_bind_safety("127.0.0.1", allow_remote=True)
    check("本机 + --allow-remote 仍然放行", ok is True)
    check("本机 + --allow-remote 提示其无作用", "没有作用" in msg)

    # 类型健壮性：非字符串不该炸
    ok, msg = ui.check_bind_safety(None, allow_remote=False)
    check("host=None 被判为不安全（不抛异常）", ok is False)


# ---------------------------------------------------------------------------
# 第二组：_run_cli 的真实行为
# ---------------------------------------------------------------------------
def group_run_cli():
    print("\n--- 第二组：_run_cli 启动/拒绝行为 ---")

    fake = FakeUvicorn()

    # 2.1 默认启动：不允许任何人被挡
    before = db_fingerprint()
    ensure_calls = []
    code, err = run_cli([], lambda: ensure_calls.append(1), fake)
    check("无参数启动不退出", code is None)
    check("无参数默认 host=127.0.0.1",
          len(fake.calls) == 1 and fake.calls[-1]["host"] == "127.0.0.1",
          f"calls={fake.calls}")
    check("无参数默认 port=8899",
          len(fake.calls) == 1 and fake.calls[-1]["port"] == 8899)
    after = db_fingerprint()
    check("允许启动时数据库中指纹未变（本测试未触发 lifespan）",
          before == after, f"{before} -> {after}")

    # 2.2 只换端口：仍然允许
    fake2 = FakeUvicorn()
    code, err = run_cli(["--port", "9000"], lambda: None, fake2)
    check("只给 --port 仍允许启动", code is None)
    check("--port 透传给 uvicorn 且 host 仍为本机",
          len(fake2.calls) == 1
          and fake2.calls[-1]["port"] == 9000
          and fake2.calls[-1]["host"] == "127.0.0.1",
          f"calls={fake2.calls}")

    # 2.3 远程未授权：拒绝
    fake3 = FakeUvicorn()
    ensure3 = []
    code, err = run_cli(["--host", "0.0.0.0"], lambda: ensure3.append(1), fake3)
    check("0.0.0.0 未授权 → 退出码非零", code not in (None, 0), f"code={code}")
    check("0.0.0.0 未授权 → 退出码为 2", code == 2, f"code={code}")
    check("0.0.0.0 未授权 → 未调用 uvicorn.run", fake3.calls == [])
    check("0.0.0.0 未授权 → 未初始化数据库", ensure3 == [])
    check("0.0.0.0 未授权 → stderr 有拒绝说明", "拒绝启动" in err)

    # 2.4 局域网地址未授权：拒绝
    fake4 = FakeUvicorn()
    ensure4 = []
    code, err = run_cli(["--host", "192.168.1.10"], lambda: ensure4.append(1), fake4)
    check("192.168.1.10 未授权 → 退出码 2", code == 2, f"code={code}")
    check("192.168.1.10 未授权 → 未调用 uvicorn.run", fake4.calls == [])
    check("192.168.1.10 未授权 → 未初始化数据库", ensure4 == [])

    # 2.5 远程 + 授权：允许，带警告
    fake5 = FakeUvicorn()
    ensure5 = []
    code, err = run_cli(["--host", "0.0.0.0", "--allow-remote"],
                        lambda: ensure5.append(1), fake5)
    check("0.0.0.0 + --allow-remote → 允许启动", code is None)
    check("0.0.0.0 + --allow-remote → 调用了 uvicorn.run",
          len(fake5.calls) == 1 and fake5.calls[0]["host"] == "0.0.0.0")
    check("0.0.0.0 + --allow-remote → 打印安全警告",
          "安全警告" in err and "没有任何身份认证" in err)
    # 警告只是打印，不是拦截；lifespan 由 uvicorn 触发，这里替身不起服务，
    # 所以 _ensure_db 不应被 _run_cli 自己调用
    check("0.0.0.0 + --allow-remote → _run_cli 自身不碰数据库", ensure5 == [])

    # 2.6 机器名、空 host 也要被挡
    for host in ("my-pc", ""):
        f = FakeUvicorn()
        code, err = run_cli(["--host", host], lambda: None, f)
        check(f"{host!r} 未授权 → 退出码 2", code == 2, f"code={code}")
        check(f"{host!r} 未授权 → 未启动服务", f.calls == [])

    # 2.7 不传 host、只传 --allow-remote：本机 + 多余授权，仍允许
    f7 = FakeUvicorn()
    code, err = run_cli(["--allow-remote"], lambda: None, f7)
    check("仅 --allow-remote（host 仍默认本机）→ 允许",
          code is None and len(f7.calls) == 1
          and f7.calls[0]["host"] == "127.0.0.1")
    check("仅 --allow-remote → 提示其无作用", "没有作用" in err)


# ---------------------------------------------------------------------------
# 第三组：生产库零副作用（独立证据，不依赖 mock）
# ---------------------------------------------------------------------------
def group_no_side_effect():
    print("\n--- 第三组：拒绝时的生产库零副作用 ---")

    path = Path(appdb.DB_PATH)
    if not path.is_file():
        check("生产库存在（本机应存在）", False, f"missing: {path}")
        return

    # 只读指纹（sha256 + size + mtime_ns）
    before = db_fingerprint()
    # 附属文件也一并记录：被拒绝的启动不该产生/改动 -wal、-shm
    wal = Path(str(path) + "-wal")
    shm = Path(str(path) + "-shm")
    wal_before = (wal.stat().st_size, wal.stat().st_mtime_ns) if wal.is_file() else None
    shm_before = (shm.stat().st_size, shm.stat().st_mtime_ns) if shm.is_file() else None

    for host in ("0.0.0.0", "192.168.1.10", "my-pc"):
        f = FakeUvicorn()
        code, _ = run_cli(["--host", host], lambda: None, f)
        if code != 2 or f.calls:
            check(f"{host} 拒绝路径干净", False, f"code={code} calls={f.calls}")

    after = db_fingerprint()
    wal_after = (wal.stat().st_size, wal.stat().st_mtime_ns) if wal.is_file() else None
    shm_after = (shm.stat().st_size, shm.stat().st_mtime_ns) if shm.is_file() else None

    check("三次拒绝后生产库 sha256/size/mtime 全未变", before == after,
          f"{before} -> {after}")
    check("三次拒绝后 -wal 文件未变", wal_before == wal_after,
          f"{wal_before} -> {wal_after}")
    check("三次拒绝后 -shm 文件未变", shm_before == shm_after,
          f"{shm_before} -> {shm_after}")


def main():
    print("=== S6-FIX-01 管理面板远程绑定安全守卫测试 ===")
    group_pure()
    group_run_cli()
    group_no_side_effect()
    print(f"\n=== 结果：{PASS} PASS / {FAIL} FAIL ===")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
