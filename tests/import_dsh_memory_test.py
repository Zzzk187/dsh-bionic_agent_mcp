"""[S0 第二批] import_dsh_memory.py 数据保真回归测试。

覆盖用户《S0 全量前置修复》第二批的 9 条验收要求：
  P1-2  本地同步不得重置 MCP 派生的 access_count / last_accessed_at
  P1-8  源 status 明确映射、未知状态上报不静默、archived 不复活
  库内独有记忆（无 src 锚点）不被普通同步删除

全部使用临时目录隔离数据库 + 临时源 jsonl；不连接、不写入生产库。
运行： D:/Software/Dev/Miniforge/python.exe -X utf8 tests/import_dsh_memory_test.py
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
import _env_guard  # noqa: E402,F401  OPT-04a：数据库隔离闸门（必须在任何 app.* 之前）

import app.db as db  # noqa: E402
from app.governance_scoring import score_frequency  # noqa: E402

MOD_PATH = PROJECT_ROOT / "scripts" / "import_dsh_memory.py"
_spec = importlib.util.spec_from_file_location("import_dsh_memory", str(MOD_PATH))
idm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(idm)

PASS = 0
FAIL = 0


def check(cond, label):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"[PASS] {label}")
    else:
        FAIL += 1
        print(f"[FAIL] {label}")


def _write_src(path, rows):
    with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def _env():
    tmp = tempfile.mkdtemp(prefix="bionic-idm-s0-")
    db_path = Path(tmp) / "agent.db"
    src_path = Path(tmp) / "记忆.jsonl"
    os.environ["BIONIC_DB_PATH"] = str(db_path)
    db.DB_PATH = db_path
    db.init_db()
    idm.DB = db_path
    idm.SRC = src_path
    return tmp, db_path, src_path


def _run_main(dry_run=False):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = idm.main(dry_run=dry_run)
    payload = None
    for ln in reversed(buf.getvalue().strip().splitlines()):
        if ln.startswith("{"):
            payload = json.loads(ln)
            break
    return rc, payload, buf.getvalue()


def _fetch(db_path, mid):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        return dict(conn.execute("SELECT * FROM memories WHERE id=?", (mid,)).fetchone())
    finally:
        conn.close()


def _count(db_path, where="1=1"):
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(f"SELECT COUNT(*) FROM memories WHERE {where}").fetchone()[0]
    finally:
        conn.close()


def test_access_count_and_status_preserved():
    """核心：连跑两次导入，access_count/last_accessed_at/status 全不被覆盖。"""
    tmp, db_path, src_path = _env()
    conn = sqlite3.connect(str(db_path))
    # 预置 A01：已存在（src 锚点）、带真实访问统计、已被归档
    conn.execute(
        """INSERT INTO memories(id,content,category,source,confidence,tags_json,
               status,memory_status,access_count,last_accessed_at)
           VALUES(100,'# 旧记忆\n\n正文A01','环境','dsh-memory',0.8,
               '["src:A01","dsh-memory"]','archived','archived',7,'2026-11-14 10:00:00')"""
    )
    # 预置独有记忆：无 src 锚点（治理派生/会话产生），active
    conn.execute(
        """INSERT INTO memories(id,content,category,source,confidence,tags_json,
               status,memory_status,access_count)
           VALUES(101,'库内独有偏好：冰美式','preference','dsh-session',0.9,
               '["中文","偏好"]','active','active',3)"""
    )
    conn.commit()
    conn.close()

    _write_src(src_path, [
        {"id": "A01", "kind": "环境", "title": "旧记忆", "body": "正文A01(改)",
         "status": "active", "trust": "verified", "tags": ["x"],
         "uses": 0, "last_used": "", "loc": "卷/环境.md"},
        {"id": "A02", "kind": "项目", "title": "新记忆", "body": "正文A02",
         "status": "active", "trust": "verified", "tags": [],
         "uses": 0, "last_used": "", "loc": ""},
    ])

    f_before, _ = score_frequency({"access_count": 7}, {}, {})

    rc1, p1, _ = _run_main(dry_run=False)
    rc2, p2, _ = _run_main(dry_run=False)  # 连跑两次，验证幂等且不重置
    check(rc1 == 0 and rc2 == 0, "两次导入退出码均为 0")
    check(p1["inserted"] == 1 and p2["inserted"] == 0,
          f"第一次新增 A02、第二次幂等不新增：{p1['inserted']}/{p2['inserted']}")

    a01 = _fetch(db_path, 100)
    check(a01["access_count"] == 7, f"[P1-2] access_count 保留 7（实际 {a01['access_count']}）")
    check(a01["last_accessed_at"] == "2026-11-14 10:00:00",
          f"[P1-2] last_accessed_at 保留（实际 {a01['last_accessed_at']!r}）")
    check(a01["status"] == "archived" and a01["memory_status"] == "archived",
          "[P1-8] 库内 archived 未被源 active 复活/覆盖")
    check("正文A01(改)" in a01["content"], "内容列仍正常同步更新（保真不代表不更新内容）")

    f_after, _ = score_frequency({"access_count": a01["access_count"]}, {}, {})
    check(f_after >= f_before and f_after > 0,
          f"[P1-2] frequency 评分不因同步下降（{f_before} → {f_after}）")

    a02 = None
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        r = conn.execute("SELECT * FROM memories WHERE tags_json LIKE '%\"src:A02\"%'").fetchone()
        if r:
            a02 = dict(r)
    finally:
        conn.close()
    check(a02 is not None, "[P1-8] 新记忆 A02 按显式映射落库")
    if a02:
        check(a02["status"] == "active" and a02["memory_status"] == "active",
              "[P1-8] 新记忆 status/memory_status 来自映射表 active→(active,active)")
        check(a02["access_count"] == 0, "新记忆访问统计初始化为 0（不是被改大的数掩盖）")

    own = _fetch(db_path, 101)
    check(own is not None and own["status"] == "active",
          "库内无 src 锚点的独有 active 记忆同步后仍存在")
    check(_count(db_path) >= 3, "同步不删除任何既有记忆（不重建 memories 表）")
    return tmp


def test_unknown_status_reported_not_swallowed():
    """未知源 status 必须记录上报、不新建、不静默映射 active。"""
    tmp, db_path, src_path = _env()
    _write_src(src_path, [
        {"id": "B01", "kind": "环境", "title": "正常", "body": "正文B01",
         "status": "active", "trust": "verified", "tags": [],
         "uses": 0, "last_used": "", "loc": ""},
        {"id": "B02", "kind": "环境", "title": "未知状态", "body": "正文B02",
         "status": "WEIRD_FUTURE", "trust": "verified", "tags": [],
         "uses": 0, "last_used": "", "loc": ""},
    ])
    rc, p, out = _run_main(dry_run=False)
    check(rc == 0, "含未知状态的导入仍正常退出")
    check(len(p["unknown_status"]) == 1 and p["unknown_status"][0]["src_id"] == "B02",
          f"[P1-8] 未知状态被汇总上报：{p['unknown_status']}")
    check("[WARN]" in out, "[P1-8] 未知状态打印 WARN，不静默")
    conn = sqlite3.connect(str(db_path))
    try:
        n = conn.execute("SELECT COUNT(*) FROM memories WHERE tags_json LIKE '%\"src:B02\"%'").fetchone()[0]
    finally:
        conn.close()
    check(n == 0, "[P1-8] 未知状态记忆不落库")
    check(_count(db_path) == 1, "[P1-8] 仅正常状态 B01 落库")
    # 纯函数映射表覆盖全部已知值
    check(idm.resolve_source_status("archived")["status"] == "archived",
          "映射表 archived→archived")
    check(idm.resolve_source_status("superseded")["memory_status"] == "superseded",
          "映射表 superseded→memory_status=superseded 但 status=active")
    check(idm.resolve_source_status("pending_review")["memory_status"] == "pending_review",
          "映射表 pending_review→memory_status=pending_review")
    check(idm.resolve_source_status("")["known"] is True,
          "缺 status 字段按 active 初始化且显式计数（不算未知被吞）")
    check("status_defaulted" in p, "status_defaulted 计数上报")
    return tmp


def test_archived_not_revived_via_source():
    """源标 archived 的新记忆落库后也是 archived，普通同步不复活。"""
    tmp, db_path, src_path = _env()
    _write_src(src_path, [
        {"id": "C01", "kind": "教训", "title": "已归档", "body": "正文C01",
         "status": "archived", "trust": "verified", "tags": [],
         "uses": 0, "last_used": "", "loc": ""},
    ])
    _run_main(dry_run=False)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        r = dict(conn.execute("SELECT * FROM memories WHERE tags_json LIKE '%\"src:C01\"%'").fetchone())
    finally:
        conn.close()
    check(r["status"] == "archived" and r["memory_status"] == "archived",
          "[P1-8] 源 archived 映射为 (archived, archived)")
    # 改为源 active 再同步：UPDATE 不碰状态列，仍 archived
    _write_src(src_path, [
        {"id": "C01", "kind": "教训", "title": "已归档", "body": "正文C01改",
         "status": "active", "trust": "verified", "tags": [],
         "uses": 0, "last_used": "", "loc": ""},
    ])
    _run_main(dry_run=False)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        r2 = dict(conn.execute("SELECT * FROM memories WHERE tags_json LIKE '%\"src:C01\"%'").fetchone())
    finally:
        conn.close()
    check(r2["status"] == "archived", "[P1-8] 后续同步不复活 archived")
    return tmp


def test_dry_run_no_writes():
    """dry_run 不写库、不建库、不执行 DDL。"""
    tmp, db_path, src_path = _env()
    _write_src(src_path, [
        {"id": "D01", "kind": "环境", "title": "dry", "body": "正文D01",
         "status": "active", "trust": "verified", "tags": [],
         "uses": 0, "last_used": "", "loc": ""},
    ])
    rc, p, _ = _run_main(dry_run=True)
    check(rc == 0 and p["dry_run"] is True, "dry_run 正常返回且标记 dry_run")
    check(_count(db_path) == 0, "[P1-1 关联] dry_run 不写入任何记忆")
    # 目标库不存在时直接拒绝，不凭空建库
    idm.DB = Path(tmp) / "nonexistent.db"
    rc2, _, _ = _run_main(dry_run=True)
    check(rc2 == 1, "目标库不存在时返回 1，不创建数据库")
    check(not (Path(tmp) / "nonexistent.db").exists(), "确实未创建任何数据库文件")
    return tmp


def main():
    tests = [
        test_access_count_and_status_preserved,
        test_unknown_status_reported_not_swallowed,
        test_archived_not_revived_via_source,
        test_dry_run_no_writes,
    ]
    tmps = []
    for t in tests:
        print(f"\n--- {t.__name__} ---")
        try:
            tmps.append(t())
        except Exception as exc:  # noqa: BLE001 - 测试脚本如实暴露异常
            global FAIL
            FAIL += 1
            import traceback
            traceback.print_exc()
            print(f"[FAIL] {t.__name__} 抛出异常：{exc}")
    print(f"\n=== {PASS}/{PASS + FAIL} 通过 ===")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
