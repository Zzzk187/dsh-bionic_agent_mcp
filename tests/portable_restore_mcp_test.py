"""S3 Portable Restore MCP tools — 独立测试。

验证 7 个新工具是 S2/S1 的薄封装、工具数 36/49/85、安全边界
（令牌/预览执行分离/完整替换/回滚/无路径逃逸）。

【隔离策略】工具层一律用 unittest.mock 替换底层 S1/S2 函数，绝不触发
真实 export/preview/confirm/execute（那些会碰生产 data/agent.db 或真实
dsh-memory）。令牌真实性（过期/一次性/目标变化）由 S2 的 64/64 测试保证，
这里只在隔离临时库上抽查，并对工具层验证「不自行实现、不绕过、如实传播」。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
import _env_guard  # noqa: E402,F401  OPT-04a：数据库隔离闸门（必须在任何 app.* 之前）

from app import db as app_db            # noqa: E402
from app import portable_backup as pb   # noqa: E402
from app import portable_restore as pr  # noqa: E402
from app import mcp_server as srv       # noqa: E402

pb._read_source_version = lambda: "s3-test-version"

PASS = 0
FAIL = 0
_TMPS: list[Path] = []
VALID_PID = "bionic-memory-test"   # 格式合法但无需文件存在（工具层只做路径边界解析）

# registry 基线：在**导入这一刻**取，arch 组再比。
# 这里原来钉的是绝对时间上界 `0 < mt < 1795000000`（= 2026-11-18），本意是
# 「本次运行没动 registry」，写法却变成「registry 必须在某个日期之前」——
# 宿主注册新 MCP 服务器时（2026-11-21 加了 snowluma / web-search-host 等）
# 它必然失败，且失败原因与测试对象毫无关系。改成运行时取基线再比，
# 语义回到本意，且比原来更强：原来是「早于某个日期」，现在是「跑这一遍没变」。
REGISTRY_PATH = Path(r"C:\Users\xing\.dsh\skill-mcp-manager\registry.json")


def _registry_stamp() -> tuple[float, int, str]:
    """(mtime, size, sha256)；文件不存在时返回 (0, 0, "")。"""
    try:
        st = REGISTRY_PATH.stat()
    except OSError:
        return (0.0, 0, "")
    import hashlib
    try:
        digest = hashlib.sha256(REGISTRY_PATH.read_bytes()).hexdigest()
    except OSError:
        digest = ""
    return (st.st_mtime, st.st_size, digest)


REGISTRY_BASELINE = _registry_stamp()


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"  [FAIL] {name}" + (f" — {detail}" if detail else ""))


def mktmp(prefix="s3-mcp-"):
    p = Path(tempfile.mkdtemp(prefix=prefix))
    _TMPS.append(p)
    return p


def make_source(tmp):
    src = tmp / "dsh-memory"
    for d in ("knowledge", "mine", "卷", "data", "docs", "bin", "lib"):
        (src / d).mkdir(parents=True, exist_ok=True)
    (src / "knowledge" / "note.md").write_text("# 标题\n\n正文内容测试。\n" * 20, encoding="utf-8")
    (src / "data" / "记忆.jsonl").write_text('{"id":"A01","status":"active","body":"测试记忆"}\n', encoding="utf-8")
    (src / "约定.md").write_text("测试约定", encoding="utf-8")
    (src / "docs" / "体系.md").write_text("# 体系", encoding="utf-8")
    (src / "卷" / "环境.md").write_text("# 环境", encoding="utf-8")
    (src / "bin" / "查.ps1").write_text("# 查询", encoding="utf-8")
    (tmp / "AGENTS.md").write_text("# AGENTS\n\n全局约定", encoding="utf-8")
    return src


def make_db(tmp, name, *, memories=2):
    p = tmp / name
    os.environ["BIONIC_DB_PATH"] = str(p)
    app_db.DB_PATH = p
    app_db.init_db()
    ts = "2026-11-16 10:00:00"
    with app_db.transaction() as conn:
        for i in range(memories):
            conn.execute(
                "INSERT INTO memories(content,category,source,confidence,status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (f"记忆{i}", "测试", f"src:M{i}", 80, "active", ts, ts))
    return p


def make_pkg(tmp, tag, mode="full"):
    src = make_source(tmp)
    dbp = make_db(tmp, f"pkg-{tag}.db", memories=3)
    r = pb.export_portable(mode, source_root=str(src), db_path=str(dbp),
                           package_dir=str(tmp / f"pkg-{tag}"),
                           allow_external_dir=True, source_version_override="s3-test")
    if not r.get("ok"):
        raise RuntimeError(f"造包失败: {r.get('errors')}")
    return tmp / f"pkg-{tag}" / r["archive_name"]


# ---------------------------------------------------------------------------
# 1-6 注册与工具数
# ---------------------------------------------------------------------------
def group_registration():
    print("\n[注册与工具数]")
    core = asyncio.run(srv.mcp.list_tools())
    gov = asyncio.run(srv.mcp_gov.list_tools())
    cn = {t.name for t in core}
    gn = {t.name for t in gov}
    seven = {"export_memory_package", "list_memory_packages", "inspect_memory_package",
             "validate_memory_package", "preview_memory_restore",
             "restore_memory_package", "delete_memory_package"}
    check("1 七工具全部注册", seven <= (cn | gn), str(seven - (cn | gn)))
    core_new = {"export_memory_package", "list_memory_packages", "inspect_memory_package", "validate_memory_package"}
    gov_new = {"preview_memory_restore", "restore_memory_package", "delete_memory_package"}
    check("2 core 新增 4 个", core_new <= cn, str(core_new - cn))
    check("3 governance 新增 3 个", gov_new <= gn, str(gov_new - gn))
    check("4 总工具数=85", len(cn) + len(gn) == 85, f"core={len(cn)} gov={len(gn)} total={len(cn)+len(gn)}")
    check("5 原有 78 工具仍在（core36/gov49）", len(cn) == 36 and len(gn) == 49)
    check("6 工具名无重复", not (cn & gn), str(cn & gn))


# ---------------------------------------------------------------------------
# 7-9 参数校验（非法值在触碰 S2 前被拒）
# ---------------------------------------------------------------------------
def group_params():
    print("\n[参数校验]")
    check("7 非法导出模式拒绝", srv.export_memory_package("bogus")["ok"] is False)
    check("8 非法恢复模式拒绝", srv.preview_memory_restore(VALID_PID, restore_mode="hack")["ok"] is False)
    check("9 非法冲突策略拒绝", srv.preview_memory_restore(VALID_PID, conflict_policy="whatever")["ok"] is False)


# ---------------------------------------------------------------------------
# 10-21 安全边界（工具层用 mock；令牌真实性在隔离库抽查）
# ---------------------------------------------------------------------------
def group_safety():
    print("\n[安全边界]")

    # 10 预览工具只读转调，绝不触发执行
    with mock.patch.object(srv._prestore, "preview_restore",
                           return_value={"ok": True, "restore_id": "R10", "confirmation_token": "t10"}) as mp, \
         mock.patch.object(srv._prestore, "execute_restore") as me:
        r = srv.preview_memory_restore(VALID_PID, ttl_sec=60)
        check("10 预览只读转调且不触发执行", r["ok"] and mp.call_count == 1 and me.call_count == 0)
        check("10b 预览参数透传 ttl/模式", mp.call_args.kwargs.get("ttl_sec") == 60
              and mp.call_args.kwargs.get("restore_mode") == "merge")

    # 11 缺令牌拒绝（真实工具路径，提前 return，不碰 S2）
    r = srv.restore_memory_package("R11", "", VALID_PID)
    check("11 缺确认令牌拒绝", r["ok"] is False
          and any("confirmation_token" in e for e in r["errors"]), str(r["errors"]))

    # 12 无效令牌：工具如实传播 S2 失败，且绝不 execute
    with mock.patch.object(srv._prestore, "confirm_restore",
                           return_value={"ok": False, "state": "FAILED", "message": "无效令牌"}) as mc, \
         mock.patch.object(srv._prestore, "execute_restore") as me:
        r = srv.restore_memory_package("R12", "bad-token", VALID_PID)
        check("12 无效令牌被拒绝且不执行", r["ok"] is False and mc.call_count == 1 and me.call_count == 0)

    # 13/14/15 工具层不绕过令牌校验：confirm 失败即止步，不自行生成/复用令牌
    with mock.patch.object(srv._prestore, "confirm_restore",
                           return_value={"ok": False, "message": "令牌已过期"}) as mc, \
         mock.patch.object(srv._prestore, "execute_restore") as me:
        r = srv.restore_memory_package("R13", "expired-token", VALID_PID)
        check("13 过期令牌工具层止步", r["ok"] is False and me.call_count == 0)
    with mock.patch.object(srv._prestore, "confirm_restore",
                           return_value={"ok": False, "message": "令牌已使用"}) as mc, \
         mock.patch.object(srv._prestore, "execute_restore") as me:
        r = srv.restore_memory_package("R14", "used-token", VALID_PID)
        check("14 重复使用令牌工具层止步", r["ok"] is False and me.call_count == 0)
    with mock.patch.object(srv._prestore, "confirm_restore",
                           return_value={"ok": False, "message": "目标指纹变化"}) as mc, \
         mock.patch.object(srv._prestore, "execute_restore") as me:
        r = srv.restore_memory_package("R15", "stale-token", VALID_PID)
        check("15 目标变化后令牌工具层止步", r["ok"] is False and me.call_count == 0)
    # 工具绝不自行生成令牌：confirm 只接收传入 token，不调用 _issue_token
    #
    # OPT-03 3.1 把 85 个工具从 app/mcp_server.py 拆到 app/mcp/{core,gov}_tools.py，
    # 原来这里用 `srv.__file__` 截取「_pkg_path 到 _run」之间的源码取证——那段代码
    # 已经搬到 gov_tools.py，`srv.__file__` 现在是 10 行 shim，截取必然越界。
    # 断言的**语义没变**（工具层不得自行签发令牌），而且比原先更强：原先是只看
    # 那一段，现在扫两个工具实现文件的全文。
    _tool_srcs = [PROJECT_ROOT / "app" / "mcp" / f for f in ("core_tools.py", "gov_tools.py")]
    _hit = [p.name for p in _tool_srcs if "_issue_token" in p.read_text(encoding="utf-8")]
    check("15b 工具不含令牌签发逻辑",
          all(p.is_file() for p in _tool_srcs) and not _hit,
          f"扫 {[p.name for p in _tool_srcs]}，命中 {_hit}")

    # 16 source_only 全系统替换：工具原样传播 S2 blockers，不绕过
    with mock.patch.object(srv._prestore, "preview_restore",
                           return_value={"ok": False, "restore_id": "R16",
                                         "blockers": ["source_only 包不含完整数据库状态，禁止全系统完整替换"]}) as mp:
        r = srv.preview_memory_restore(VALID_PID, restore_mode="full_replace")
        check("16 source_only 全替换被阻断且 blocker 透传",
              r["ok"] is False and any("source_only" in str(b) for b in r.get("blockers", [])))

    # 17 blocker 存在时不能执行：confirm 失败即不 execute
    with mock.patch.object(srv._prestore, "confirm_restore",
                           return_value={"ok": False, "blockers": ["校验未通过"]}) as mc, \
         mock.patch.object(srv._prestore, "execute_restore") as me:
        r = srv.restore_memory_package("R17", "any", VALID_PID)
        check("17 blocker 存在时不可执行", r["ok"] is False and me.call_count == 0)

    # 18 S2 回滚状态如实返回（工具不吞状态）
    with mock.patch.object(srv._prestore, "execute_restore",
                           return_value={"ok": False, "state": "ROLLED_BACK", "message": "已回滚"}) as mx, \
         mock.patch.object(srv._prestore, "confirm_restore",
                           return_value={"ok": True, "state": "CONFIRMED"}):
        r = srv.restore_memory_package("R18", "tok", VALID_PID)
        check("18 回滚状态正确返回", r["status"] == "ROLLED_BACK" and r["ok"] is False, str(r.get("status")))

    # 19 ROLLBACK_FAILED 不被包装为成功（纯函数验证）
    fake = {"ok": False, "state": "ROLLBACK_FAILED", "message": "回滚失败，需人工介入"}
    wf = srv._tool_result("restore_memory_package", fake, restore_id="R-x")
    check("19 ROLLBACK_FAILED 不被包装为成功",
          wf["ok"] is False and wf["status"] == "ROLLBACK_FAILED" and "人工介入" in json.dumps(wf, ensure_ascii=False))

    # 20 异常返回结构化错误
    e = srv._tool_error("restore_memory_package", RuntimeError("boom"))
    check("20 异常返回结构化错误", e["ok"] is False and e["errors"] and e["operation"] == "restore_memory_package")

    # 21 不泄露令牌秘密与记忆正文（外层只含约定字段；内层 result 是 S2 原样，不额外暴露）
    sample = {"ok": True, "confirmation_token": "SECRET_TOKEN", "content": "私人记忆正文"}
    w = srv._tool_result("x", sample)
    check("21a 外层不单列 token 字段", "confirmation_token" not in w and "content" not in w)
    check("21b 工具错误不含令牌秘密", "SECRET_TOKEN" not in json.dumps(
        srv._tool_error("x", "bad"), ensure_ascii=False))


def group_isolated_tokens():
    """隔离临时库抽查 S2 令牌真实语义（过期/一次性/目标变化），不碰生产。"""
    print("\n[令牌真实性·隔离抽查]")
    tmp = mktmp()
    try:
        pkg = make_pkg(tmp, "tok")
        tsrc = tmp / "target"; tsrc.mkdir()
        tdb = make_db(tmp, "target.db")
        logdir = tmp / "logs"
        pv = pr.preview_restore(pkg, restore_mode="merge", db_path=tdb, source_root=tsrc,
                                 log_dir=logdir, allow_external=True)
        if not pv.get("ok"):
            check("隔离预览可用", False, str(pv.get("blockers", [])[:2]))
            return
        # 一次性：消费两次，第二次失败
        c1 = pr.confirm_restore(pv["restore_id"], pv["confirmation_token"], package_path=pkg,
                                restore_mode="merge", db_path=tdb, source_root=tsrc,
                                log_dir=logdir, allow_external=True)
        c2 = pr.confirm_restore(pv["restore_id"], pv["confirmation_token"], package_path=pkg,
                                restore_mode="merge", db_path=tdb, source_root=tsrc,
                                log_dir=logdir, allow_external=True)
        check("21c 令牌一次性（隔离）", c1.get("ok") is True and c2.get("ok") is False)
        # 目标指纹变化：换掉源目录内容后确认失败
        pv2 = pr.preview_restore(pkg, restore_mode="merge", db_path=tdb, source_root=tsrc,
                                  log_dir=logdir, allow_external=True)
        (tsrc / "新文件.md").write_text("变化", encoding="utf-8")
        c3 = pr.confirm_restore(pv2["restore_id"], pv2["confirmation_token"], package_path=pkg,
                                restore_mode="merge", db_path=tdb, source_root=tsrc,
                                log_dir=logdir, allow_external=True)
        check("21d 目标变化后令牌失效（隔离）", c3.get("ok") is False)
    except Exception as exc:  # noqa: BLE001
        check("隔离令牌抽查无异常", False, f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# 22-24 架构约束
# ---------------------------------------------------------------------------
def group_arch():
    print("\n[架构约束]")
    import app.mcp_server_gov as srv_gov
    # launcher 只转调 mcp_server._run(mcp_server.mcp_gov)，自身不 new FastMCP
    gov_src = Path(srv_gov.__file__).read_text(encoding="utf-8")
    check("22 未产生额外 MCP Server（仍两个实例）",
          hasattr(srv, "mcp") and hasattr(srv, "mcp_gov")
          and "FastMCP(" not in gov_src
          and "mcp_server._run(mcp_server.mcp_gov" in gov_src)
    # 23 原来钉死 `0 < mt < 1795000000`（2026-11-18 的绝对上界），宿主在
    # 2026-11-21 注册新 MCP 服务器后它必然失败——那是「registry 必须在某个
    # 日期之前」，不是「本次运行没改 registry」。改成跑这一遍前后比对：
    # mtime / size / sha256 三者全等才算没动过（只比 mtime 会被
    # 同秒内的改写骗过去；只比 size 会被等长改写骗过去）。
    current = _registry_stamp()
    existed = REGISTRY_BASELINE[0] > 0
    check("23 registry 未被本次修改（运行期间指纹不变）",
          current == REGISTRY_BASELINE,
          f"baseline={REGISTRY_BASELINE} current={current}")
    check("23b registry 可读（存在且非空）",
          (not existed) or (REGISTRY_BASELINE[1] > 0 and REGISTRY_BASELINE[2] != ""),
          f"exists={existed} size={REGISTRY_BASELINE[1]} sha={REGISTRY_BASELINE[2][:16]}")
    core_names = {t.name for t in asyncio.run(srv.mcp.list_tools())}
    gov_names = {t.name for t in asyncio.run(srv.mcp_gov.list_tools())}
    check("24 原有工具回归（标志性老工具在）",
          all(x in core_names for x in ("save_memory", "recall_all", "search_knowledge"))
          and all(x in gov_names for x in ("create_db_snapshot", "run_memory_maintenance", "recall_memories")))


def main():
    group_registration()
    group_params()
    group_safety()
    group_isolated_tokens()
    group_arch()
    print(f"\n=== S3 MCP 工具测试 {PASS}/{PASS + FAIL} 通过 ===")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
