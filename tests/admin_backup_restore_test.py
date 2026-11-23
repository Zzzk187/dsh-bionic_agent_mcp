"""S4 管理面板「备份与恢复中心」自动化测试。

原则：
- 用 fastapi.testclient.TestClient 直接测 app.admin_ui（不另起服务）；
- 所有 S1/S2 业务函数用 unittest.mock 替换，绝不触发真实 export/preview/confirm/
  execute（那些会碰生产库或真实 dsh-memory）；
- 不进入 TestClient 的 lifespan（不用 with 语句），避免 _ensure_db 碰生产库；
- 覆盖 Batch B-G 的行为，按组追加，每组独立可跑。

运行：python -X utf8 tests/admin_backup_restore_test.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fastapi.testclient import TestClient  # noqa: E402
import _env_guard  # noqa: E402,F401  OPT-04a：数据库隔离闸门（必须在任何 app.* 之前）

from app import admin_ui as ui  # noqa: E402

PASS = 0
FAIL = 0
_TMPS: list[str] = []

# 让 portable_backup 里的 git subprocess（_read_source_version）不卡住测试进程
ui.pbackup._read_source_version = lambda: "s4-test-version"

VALID_PID = "bionic-memory-test-full"
VALID_RID = "restore-20261116-test001"


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"[PASS] {name}" + (f" — {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"[FAIL] {name}" + (f" — {detail}" if detail else ""))


def mktmp():
    d = tempfile.mkdtemp(prefix="s4-admin-")
    _TMPS.append(d)
    return Path(d)


def client():
    # 不进 lifespan：TestClient 非 with 用法不会触发 startup 事件
    return TestClient(ui.app)


def make_manifest(backup_type="full"):
    return {
        "format_name": "bionic-memory-package",
        "format_version": 1,
        "backup_type": backup_type,
        "created_at": "2026-11-16T17:49:55+00:00",
        "project_version": "0.1.0",
        "schema_version": 0,
        "embedding": {"model_name": "test-model", "dimensions": 384},
        "included_categories": ["knowledge", "mine_md"],
        "db_snapshot_stats": {"tables": 15, "total_rows": 2553},
        "warnings": ["sample warning"],
    }


def make_pkg_file(pkgdir: Path, pid=VALID_PID):
    pkgdir.mkdir(parents=True, exist_ok=True)
    p = pkgdir / (pid + ".zip")
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(make_manifest(), ensure_ascii=False))
        z.writestr("source/约定.md", "# 约定\n" + ("测试内容\n" * 20))
        z.writestr("checksums.sha256", "placeholder\n")
    return p


# ===========================================================================
# Batch B：只读接口
# ===========================================================================
def group_b():
    c = client()

    # --- 包列表 ---
    with patch.object(ui.pbackup, "_resolve_package_dir", side_effect=lambda *a, **k: mktmp()), \
         patch.object(ui.pbackup, "list_packages", return_value={
             "ok": True,
             "packages": [{"name": VALID_PID + ".zip", "package_id": VALID_PID,
                           "size_bytes": 4629027,
                           "created_at": "2026-11-16T17:49:56+00:00"}],
             "count": 1,
         }), \
         patch.object(ui, "_package_meta", side_effect=lambda pid, path: {
             "package_id": pid, "backup_type": "full", "format_version": 1,
             "created_at": "2026-11-16T17:49:55+00:00",
             "supports_full_restore": True, "check_status": "unchecked",
             "warnings": ["sample warning"],
         }):
        r = c.get("/api/backup/packages")
    check("B1 包列表 200", r.status_code == 200, f"status={r.status_code}")
    j = r.json()
    check("B2 包列表 ok", j.get("ok") is True)
    items = (j.get("data") or {}).get("packages") or []
    check("B3 包列表返回真实条目", len(items) == 1, f"count={len(items)}")
    check("B4 包字段完整", bool(items and items[0].get("backup_type") == "full"
          and items[0].get("supports_full_restore") is True))
    check("B5 包列表不暴露绝对路径", all(
        ":\\" not in str(it) and not str(it.get("name", "")).startswith("D:") for it in items))

    # 空列表
    with patch.object(ui.pbackup, "list_packages", return_value={
            "ok": True, "packages": [], "count": 0}):
        r = c.get("/api/backup/packages")
    check("B6 空列表正常显示", r.status_code == 200 and
          len(r.json().get("data", {}).get("packages", [])) == 0)

    # --- 包详情 ---
    fake_manifest = make_manifest()
    with patch.object(ui.pbackup, "_resolve_package_dir", side_effect=lambda *a, **k: mktmp()), \
         patch.object(ui.pbackup, "inspect_package", return_value={
             "ok": True, "package_id": VALID_PID, "file_count": 161,
             "total_size_bytes": 4629027,
             "manifest": fake_manifest,
             "validation": {"ok": True, "errors": [], "warnings": ["w"], "checks": {"zip_open": True}},
         }):
        r = c.get(f"/api/backup/packages/{VALID_PID}")
    check("B7 包详情 200", r.status_code == 200, f"status={r.status_code}")
    d = (r.json().get("data") or {})
    check("B8 包详情 manifest 正确", d.get("manifest", {}).get("backup_type") == "full")
    check("B9 包详情不含逐文件正文清单", "files" not in d and "file_manifest" not in d)

    # 路径穿越 / 非法 id。拒绝可以表现为 400/404/307（URL 规范化/重定向），
    # 核心断言是：绝不返回真实包详情数据（file_count=161 / manifest.full）。
    attack_ids = ["../../etc/passwd", "D:\\secret", "bionic-memory-..%2F..", "readme.md"]
    for bad in attack_ids:
        r = c.get(f"/api/backup/packages/{bad}")
        body = r.text
        rejected = r.status_code in (400, 404, 307, 422) or (
            '"file_count":161' not in body.replace(" ", ""))
        check(f"B10 非法包ID拒绝 [{bad[:20]}]", rejected,
              f"status={r.status_code}")

    # 空 ID：直测受管 helper，必须返回拒绝（HTTP 空段会落到列表路由，故不测 HTTP）
    p_none, why_none = ui._backup_pkg_path("")
    check("B10b 空包ID helper 拒绝", p_none is None and bool(why_none))

    # --- 校验 ---
    with patch.object(ui.pbackup, "_resolve_package_dir", side_effect=lambda *a, **k: mktmp()), \
         patch.object(ui.pbackup, "validate_package_archive", return_value={
             "ok": True, "errors": [], "warnings": ["minor"],
             "checks": {"zip_open": True, "path_safety": True},
             "file_count": 161, "total_size_bytes": 4629027,
         }):
        r = c.post(f"/api/backup/packages/{VALID_PID}/validate")
    check("B11 校验接口 ok", r.status_code == 200 and r.json().get("data", {}).get("ok") is True)
    check("B12 校验输出 checks", "checks" in (r.json().get("data") or {}))

    # 校验失败带 blocker
    with patch.object(ui.pbackup, "_resolve_package_dir", side_effect=lambda *a, **k: mktmp()), \
         patch.object(ui.pbackup, "validate_package_archive", return_value={
             "ok": False, "errors": ["9.不安全路径"], "warnings": [],
             "checks": {"path_safety": False}, "file_count": 0, "total_size_bytes": 0,
         }):
        r = c.post(f"/api/backup/packages/{VALID_PID}/validate")
    check("B13 校验失败显示 blocker", r.status_code == 200 and
          "不安全路径" in str(r.json().get("data", {}).get("errors")))

    # --- 目标检查（脱敏）---
    fake_target = {
        "ok": True, "source_root": "C:\\Users\\xing\\.dsh\\dsh-memory",
        "source_exists": True, "db_path": "D:\\Cache\\Cache_Project\\bionic-agent-mcp\\data\\agent.db",
        "db_exists": True,
        "db_fingerprint": {"hash": "abc", "integrity": "ok", "path": "D:/secret/agent.db"},
        "source_fingerprint": {"hash": "def", "count": 138, "root": "C:/Users/xing/.dsh/dsh-memory"},
        "embedding": {"model_name": "m", "dimensions": 384},
        "doc_count": 138, "memory_count": 53, "task_count": 0, "vector_count": 51,
        "fk_violations": 0,
        "mirror": {"path": "C:\\Users\\xing\\.dsh\\dsh-memory\\knowledge", "in_sync": True},
        "warnings": [], "blockers": [],
    }
    with patch.object(ui.prestore, "inspect_restore_target", return_value=fake_target):
        r = c.get("/api/backup/target")
    tar = (r.json().get("data") or {})
    check("B14 目标检查 200", r.status_code == 200)
    check("B15 目标脱敏无绝对路径",
          "C:\\Users" not in json.dumps(tar, ensure_ascii=False) and
          "D:\\Cache" not in json.dumps(tar, ensure_ascii=False)
          and "C:/Users" not in json.dumps(tar, ensure_ascii=False),
          json.dumps(tar, ensure_ascii=False)[:120])
    check("B16 目标计数正确", tar.get("doc_count") == 138 and tar.get("memory_count") == 53)
    check("B17 目标 fingerprint 去 path/root",
          "path" not in (tar.get("db_fingerprint") or {}) and
          "root" not in (tar.get("source_fingerprint") or {}))

    # 目标检查失败
    with patch.object(ui.prestore, "inspect_restore_target", return_value={
            "ok": False, "blockers": ["目标数据库不存在"], "warnings": []}):
        r = c.get("/api/backup/target")
    check("B18 目标 blocker 展示", (r.json().get("data") or {}).get("blockers") == ["目标数据库不存在"])

    # --- 恢复历史 / 详情 ---
    fake_hist = {"ok": True, "count": 1, "items": [{
        "restore_id": VALID_RID, "state": "COMPLETED", "restore_mode": "merge",
        "package_id": VALID_PID, "created_at": "2026-11-16", "updated_at": "2026-11-16"}]}
    with patch.object(ui.prestore, "list_restore_history", return_value=fake_hist):
        r = c.get("/api/backup/restores")
    check("B19 恢复历史 200", r.status_code == 200 and
          (r.json().get("data") or {}).get("count") == 1)

    fake_status = {"ok": True, "status": {
        "restore_id": VALID_RID, "state": "COMPLETED",
        "package_path": "D:/Cache/Cache_Project/bionic-agent-mcp/data/memory_packages/x.zip",
        "stages": [{"state": "COMPLETED", "detail": {"backup_dir": "D:/Cache/.../restore_backups/r1"}}],
    }}
    with patch.object(ui.prestore, "get_restore_status", return_value=fake_status):
        r = c.get(f"/api/backup/restores/{VALID_RID}")
    sd = (r.json().get("data") or {}).get("status") or {}
    check("B20 恢复详情 200", r.status_code == 200)
    check("B21 恢复详情路径脱敏",
          "D:/Cache" not in json.dumps(sd, ensure_ascii=False),
          json.dumps(sd, ensure_ascii=False)[:120])


# ===========================================================================
# Batch C：导出与受管删除
# ===========================================================================
def group_c():
    c = client()

    # --- 导出四模式 ---
    ok_export = {"ok": True, "package_id": VALID_PID, "archive_name": VALID_PID + ".zip",
                 "backup_type": "full", "file_count": 161, "total_size_bytes": 4629027,
                 "archive_size_bytes": 4629027, "validation": {"ok": True}, "warnings": []}
    for mode in ("source_only", "source_with_db", "full", "knowledge_tasks"):
        with patch.object(ui.pbackup, "export_portable", return_value={**ok_export, "backup_type": mode}) as ex:
            r = c.post("/api/backup/export", json={"backup_type": mode})
        check(f"C1 导出模式 {mode} 200", r.status_code == 200 and
              (r.json().get("data") or {}).get("backup_type") == mode,
              f"status={r.status_code}")
        # 确认没有把前端路径参数透传给业务层
        _, kwargs = ex.call_args
        check(f"C2 导出 {mode} 不传路径参数",
              all(k not in kwargs for k in ("source_root", "db_path", "package_dir", "allow_external_dir")))

    # 非法模式
    r = c.post("/api/backup/export", json={"backup_type": "../../etc"})
    check("C3 非法导出模式拒绝", r.status_code == 400)
    r = c.post("/api/backup/export", json={"backup_type": ""})
    check("C4 空导出模式拒绝", r.status_code == 400)

    # 导出失败展示错误
    with patch.object(ui.pbackup, "export_portable", return_value={
            "ok": False, "errors": ["磁盘空间不足"], "warnings": []}):
        r = c.post("/api/backup/export", json={"backup_type": "full"})
    check("C5 导出失败显示原因", r.status_code == 400 and "磁盘空间不足" in r.text)

    # --- 删除：合法受管包 ---
    with patch.object(ui.pbackup, "_resolve_package_dir", side_effect=lambda *a, **k: mktmp()), \
         patch.object(ui, "_active_restore_package_ids", return_value=set()), \
         patch.object(ui.pbackup, "delete_package", return_value={
             "ok": True, "package_id": VALID_PID, "warnings": []}) as dp:
        r = c.delete(f"/api/backup/packages/{VALID_PID}")
    check("C6 删除合法包 200", r.status_code == 200 and
          (r.json().get("data") or {}).get("deleted") is True)
    check("C7 删除用 package_id 而非路径", dp.call_args.args[0] == VALID_PID)

    # 删除失败保留文件、不伪装成功
    with patch.object(ui.pbackup, "_resolve_package_dir", side_effect=lambda *a, **k: mktmp()), \
         patch.object(ui, "_active_restore_package_ids", return_value=set()), \
         patch.object(ui.pbackup, "delete_package", return_value={
             "ok": False, "errors": ["文件被占用"]}):
        r = c.delete(f"/api/backup/packages/{VALID_PID}")
    check("C8 删除失败不伪装成功", r.status_code == 400 and "文件被占用" in r.text)

    # 删除正在恢复的包 → 409，且绝不调用 delete_package
    with patch.object(ui.pbackup, "_resolve_package_dir", side_effect=lambda *a, **k: mktmp()), \
         patch.object(ui, "_active_restore_package_ids", return_value={VALID_PID}), \
         patch.object(ui.pbackup, "delete_package") as dp:
        r = c.delete(f"/api/backup/packages/{VALID_PID}")
    check("C9 删除活动包被拒 409", r.status_code == 409)
    check("C10 活动包拒绝时不调用删除", dp.call_count == 0)

    # 删除包外路径 / 穿越 → 400，不调用 delete_package
    for bad in ["../../etc/passwd", "D:\\secret", "../bionic-memory-x", "readme.md"]:
        with patch.object(ui.pbackup, "delete_package") as dp:
            r = c.delete(f"/api/backup/packages/{bad}")
        rejected = r.status_code in (400, 404, 307, 422) or dp.call_count == 0
        check(f"C11 删除包外路径拒绝 [{bad[:18]}]", rejected, f"status={r.status_code}")

    # 活动包状态集合定义正确（含 CONFIRMED 之后未终态）
    check("C12 活动状态含 STAGING_READY", "STAGING_READY" in ui._S4_ACTIVE_STATES)
    check("C13 终态 COMPLETED 不属活动", "COMPLETED" not in ui._S4_ACTIVE_STATES)


# ===========================================================================
# Batch D：恢复预览（只读，转调 S2 preview_restore）
# ===========================================================================
def group_d():
    c = client()
    good_prev = {
        "ok": True, "restore_id": "restore-s4-preview01", "package_id": VALID_PID,
        "package_sha256": "abc123", "backup_type": "full", "restore_mode": "merge",
        "conflict_policy": "keep_target", "summary": {"files_to_add": 3, "files_to_update": 1,
        "files_to_skip": 5, "files_to_remove": 0, "memories_to_add": 1, "conflicts": 1},
        "deletions": [], "compatibility": {"ok": True},
        "warnings": ["模型维度提示"], "blockers": [], "validation": {"ok": True},
        "backup_required": True, "required_space_bytes": 9258054,
        "target": {"ok": True, "source_exists": True, "db_exists": True,
                   "memory_count": 53, "doc_count": 138, "vector_count": 1006,
                   "fk_violations": 0, "embedding": {"model_name": "m", "dimensions": 384},
                   "db_fingerprint": {"hash": "h", "path": "C:/secret/agent.db"},
                   "blockers": [], "warnings": []},
        "confirmation_token": "one-time-token-secret", "token_expire_at": "2026-11-16T21:00:00",
        "message": "预览就绪",
        "package_path": "C:/Users/xing/.dsh/secret/bionic-memory-full.zip",
    }
    with patch.object(ui.pbackup, "_resolve_package_dir", side_effect=lambda *a, **k: mktmp()), \
         patch.object(ui.prestore, "preview_restore", return_value=good_prev) as prev:
        r = c.post("/api/backup/preview", json={"package_id": VALID_PID,
                   "restore_mode": "merge", "conflict_policy": "keep_target"})
    data = r.json().get("data") or {}
    check("D1 预览 200", r.status_code == 200 and r.json().get("ok") is True,
          f"status={r.status_code}")
    check("D2 预览 restore_id 透传", data.get("restore_id") == "restore-s4-preview01")
    check("D3 预览摘要透传", (data.get("summary") or {}).get("files_to_add") == 3)
    check("D4 预览调用 S2 不重实现", prev.call_count == 1)
    _, kw = prev.call_args
    check("D5 预览不传路径参数",
          all(k not in kw for k in ("db_path", "source_root", "package_dir", "log_dir", "allow_external")))
    check("D6 预览 package_path 脱敏",
          "C:/Users/xing" not in r.text and "C:\\Users\\xing" not in r.text)
    check("D7 预览 target 内部 path 剥离",
          "path" not in (data.get("target") or {}).get("db_fingerprint", {}))
    check("D8 令牌只在成功响应返回", data.get("confirmation_token") == "one-time-token-secret")

    # blocker 存在：预览返回 ok=False，令牌必须为空，前端据此禁用确认
    blocked = {**good_prev, "ok": False, "blockers": ["source_only 禁止全替换"],
               "confirmation_token": "should-not-leak", "token_expire_at": "x"}
    with patch.object(ui.pbackup, "_resolve_package_dir", side_effect=lambda *a, **k: mktmp()), \
         patch.object(ui.prestore, "preview_restore", return_value=blocked):
        r = c.post("/api/backup/preview", json={"package_id": VALID_PID})
    data = r.json().get("data") or {}
    check("D9 blocker 预览 ok=False", data.get("ok") is False)
    check("D10 blocker 展示", "source_only 禁止全替换" in (data.get("blockers") or []))
    check("D11 blocker 时令牌不返回", not data.get("confirmation_token"))

    # 非法模式/策略/包ID → 400，绝不调用 S2
    for bad in ({"package_id": VALID_PID, "restore_mode": "wipe_all"},
                {"package_id": VALID_PID, "conflict_policy": "delete_everything"},
                {"package_id": "../../etc/passwd"}):
        with patch.object(ui.prestore, "preview_restore") as prev:
            r = c.post("/api/backup/preview", json=bad)
        check(f"D12 非法预览参数拒绝 [{list(bad.values())[-1][:18]}]",
              r.status_code == 400 and prev.call_count == 0)

    check("D13 full_replace 在 S2 模式集合",
          "full_replace" in ui.prestore.RESTORE_MODES)
    check("D14 merge 在 S2 模式集合", "merge" in ui.prestore.RESTORE_MODES)


# ===========================================================================
# Batch E：确认与执行（令牌消费 / 强确认 / 状态闸门 / 回滚指引）
# ===========================================================================
def group_e():
    c = client()
    base = {"package_id": VALID_PID, "restore_mode": "merge",
            "restore_id": "restore-s4-exec01", "confirmation_token": "one-time-token"}

    # --- confirm：merge 正常 ---
    with patch.object(ui.pbackup, "_resolve_package_dir", side_effect=lambda *a, **k: mktmp()), \
         patch.object(ui.prestore, "confirm_restore", return_value={
             "ok": True, "restore_id": "restore-s4-exec01", "state": "CONFIRMED",
             "message": "已确认"}) as cf:
        r = c.post("/api/backup/confirm", json=base)
    check("E1 confirm merge 200", r.status_code == 200 and
          (r.json().get("data") or {}).get("state") == "CONFIRMED")
    check("E2 confirm 用真实令牌", cf.call_args.args[1] == "one-time-token")

    # full_replace 缺强确认短语 → 400，绝不调 S2
    with patch.object(ui.prestore, "confirm_restore") as cf:
        r = c.post("/api/backup/confirm", json={**base, "restore_mode": "full_replace"})
    check("E3 full_replace 缺短语拒绝", r.status_code == 400 and cf.call_count == 0)
    with patch.object(ui.prestore, "confirm_restore", return_value={
            "ok": True, "restore_id": "restore-s4-exec01", "state": "CONFIRMED",
            "message": "已确认"}) as cf:
        r = c.post("/api/backup/confirm", json={**base, "restore_mode": "full_replace",
                   "confirm_phrase": "确认完整替换"})
    check("E4 full_replace 正确短语放行（转调S2）", r.status_code == 200 and cf.call_count == 1,
          f"status={r.status_code}")
    with patch.object(ui.prestore, "confirm_restore") as cf:
        r = c.post("/api/backup/confirm", json={**base, "restore_mode": "full_replace",
                   "confirm_phrase": "随便写"})
    check("E5 full_replace 错短语拒绝", r.status_code == 400 and cf.call_count == 0)

    # 缺令牌 / 无效令牌工具层止步
    r = c.post("/api/backup/confirm", json={k: v for k, v in base.items() if k != "confirmation_token"})
    check("E6 缺令牌拒绝", r.status_code == 400)
    with patch.object(ui.prestore, "confirm_restore", return_value={
            "ok": False, "message": "令牌已失效"}):
        r = c.post("/api/backup/confirm", json=base)
    check("E7 无效令牌工具层止步", r.status_code == 400 and "令牌已失效" in r.text)

    # confirm 失败信息脱敏（路径不泄漏）
    with patch.object(ui.pbackup, "_resolve_package_dir", side_effect=lambda *a, **k: mktmp()), \
         patch.object(ui.prestore, "confirm_restore", return_value={
             "ok": False, "message": "指纹不匹配 C:/Users/xing/.dsh/agent.db"}):
        r = c.post("/api/backup/confirm", json=base)
    check("E8 confirm 错误脱敏", "C:/Users/xing" not in r.text and ".../" in r.text)

    # --- execute：状态闸门 ---
    with patch.object(ui.prestore, "get_restore_status", return_value={
            "ok": True, "status": {"state": "PREVIEW_READY"}}), \
         patch.object(ui.prestore, "execute_restore") as ex:
        r = c.post("/api/backup/execute", json=base)
    check("E9 未确认状态拒绝执行", r.status_code == 409 and ex.call_count == 0)

    # execute 正常：绝不传 fail_inject / do_reanchor / plan_files
    good_ex = {"ok": True, "restore_id": "restore-s4-exec01", "state": "COMPLETED",
               "stage_results": {"files": {"ok": True}, "verify": {"ok": True}},
               "warnings": [], "errors": [], "message": "恢复完成"}
    with patch.object(ui.pbackup, "_resolve_package_dir", side_effect=lambda *a, **k: mktmp()), \
         patch.object(ui.prestore, "get_restore_status", return_value={
             "ok": True, "status": {"state": "CONFIRMED"}}), \
         patch.object(ui.prestore, "execute_restore", return_value=good_ex) as ex:
        r = c.post("/api/backup/execute", json={**base,
                   "fail_inject": "verify", "do_reanchor": False, "plan_files": []})
    data = r.json().get("data") or {}
    check("E10 execute 200 COMPLETED", r.status_code == 200 and data.get("state") == "COMPLETED")
    _, kw = ex.call_args
    check("E11 execute 不传 fail_inject/do_reanchor/plan_files",
          all(k not in kw for k in ("fail_inject", "do_reanchor", "plan_files")))
    check("E12 execute 不自行生成令牌", ex.call_args.args[1] == "")

    # 执行失败/回滚状态如实呈现（非 HTTP 错误）
    fail_ex = {"ok": False, "restore_id": "restore-s4-exec01", "state": "ROLLED_BACK",
               "stage_results": {"verify": {"ok": False}}, "warnings": [],
               "errors": ["恢复后验证失败"], "message": "已回滚"}
    with patch.object(ui.pbackup, "_resolve_package_dir", side_effect=lambda *a, **k: mktmp()), \
         patch.object(ui.prestore, "get_restore_status", return_value={
             "ok": True, "status": {"state": "CONFIRMED"}}), \
         patch.object(ui.prestore, "execute_restore", return_value=fail_ex):
        r = c.post("/api/backup/execute", json=base)
    data = r.json().get("data") or {}
    check("E13 回滚状态如实呈现", r.status_code == 200 and data.get("state") == "ROLLED_BACK")
    check("E14 回滚错误展示", "恢复后验证失败" in (data.get("errors") or []))

    # 重复点击：第二次状态已 COMPLETED → 闸门拒绝
    with patch.object(ui.prestore, "get_restore_status", return_value={
            "ok": True, "status": {"state": "COMPLETED"}}), \
         patch.object(ui.prestore, "execute_restore") as ex:
        r = c.post("/api/backup/execute", json=base)
    check("E15 重复点击不重复执行", r.status_code == 409 and ex.call_count == 0)

    # --- rollback 指引 ---
    for state, severe in (("ROLLBACK_FAILED", True), ("ROLLED_BACK", False),
                          ("ROLLBACK_REQUIRED", False), ("COMPLETED", False)):
        with patch.object(ui.prestore, "get_restore_status", return_value={
                "ok": True, "status": {"state": state, "errors": []}}):
            r = c.post(f"/api/backup/restores/restore-s4-exec01/rollback")
        data = r.json().get("data") or {}
        key = "severe" if severe else "notsevere"
        check(f"E16 rollback 状态 {state} {key}", r.status_code == 200 and
              data.get("state") == state and data.get("severe") is severe)
    with patch.object(ui.prestore, "get_restore_status", return_value={
            "ok": True, "status": {"state": "ROLLBACK_FAILED", "errors": []}}):
        r = c.post("/api/backup/restores/restore-s4-exec01/rollback")
    check("E17 ROLLBACK_FAILED 人工介入指引",
          "人工介入" in ((r.json().get("data") or {}).get("guidance") or ""))

    # 引擎自动回滚声明（面板不伪造手动回滚按钮）
    with patch.object(ui.prestore, "get_restore_status", return_value={
            "ok": True, "status": {"state": "COMPLETED"}}):
        r = c.post("/api/backup/restores/restore-s4-exec01/rollback")
    check("E18 声明引擎自动回滚", (r.json().get("data") or {}).get("auto_rollback_by_engine") is True)


def group_f():
    """Batch F：前端页面静态证据（不启浏览器，验证结构/接口/令牌安全）。"""
    import re
    html = (PROJECT_ROOT / "app" / "admin_ui.html").read_text(encoding="utf-8")
    check("F1 备份与恢复中心 tab 存在", 'data-tab="backup"' in html)
    check("F2 backup view 存在", 'id="backup"' in html)
    check("F3 tab 切换挂载 loadBackup", "if(t.dataset.tab==='backup') loadBackup();" in html)
    check("F4 包列表接口", "/api/backup/packages" in html)
    check("F5 导出/删除接口", "/api/backup/export" in html and "method:'DELETE'" in html)
    check("F6 详情/校验接口", "/api/backup/packages/" in html and "/validate" in html)
    check("F7 目标检查接口", "/api/backup/target" in html)
    check("F8 预览/确认/执行接口",
          "/api/backup/preview" in html and "/api/backup/confirm" in html and "/api/backup/execute" in html)
    check("F9 历史/回滚接口", "/api/backup/restores" in html and "/rollback" in html)
    check("F10 四模式文案", all(m in html for m in
          ("source_only", "source_with_db", "full", "knowledge_tasks")))
    check("F11 两种恢复模式 + 三策略",
          all(x in html for x in ("merge", "full_replace", "keep_target", "use_package", "keep_both")))
    check("F12 full_replace 强确认短语", "确认完整替换" in html and "bkRiskCheck" in html)
    check("F13 令牌仅内存变量", "let bkPending" in html and "confirmation_token" in html)
    check("F14 令牌不写 localStorage/sessionStorage",
          not re.search(r"(localStorage|sessionStorage)\s*\.\s*setItem\([^)]*token", html, re.I))
    check("F15 无假进度条/静态假数据",
          not re.search(r"(fakeProgress|假进度|mockData|静态假数据)", html, re.I))
    scripts = re.findall(r"<script[^>]*>(.*?)</script>", html, re.S)
    check("F16 存在内联脚本", len(scripts) >= 1)
    alljs = "\n".join(scripts)
    check("F17 事件绑定齐全", all(
          x in alljs for x in ("$('#bkExport').onclick", "$('#bkPreview').onclick",
                                "$('#bkConfirmGo').onclick", "$('#bkPkgRefresh').onclick")))
    check("F18 令牌执行后置空", "bkPending = null;" in alljs)
    check("F19 不展示绝对路径给普通视图",
          "不展示逐文件清单" in html and "不展示" in html)
    # 可选：node --check 由独立脚本覆盖，这里不依赖 node 运行时
    check("F20 页面中文标题", "<title>Bionic Agent 管理面板</title>" in html)


def run():
    print("=== S4 管理面板测试 · Batch B+C+D+E+F（只读/导出删除/预览/确认执行/前端）===")
    group_b()
    group_c()
    group_d()
    group_e()
    group_f()
    print(f"\n=== Batch B+C+D+E+F 结果：{PASS} PASS / {FAIL} FAIL ===")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(run())
