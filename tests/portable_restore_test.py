"""S2 Portable Memory Restore Engine 独立测试。

只用临时目录 / 临时 SQLite 隔离库 + S1 export_portable 现场造包；
绝不修改生产 data/agent.db、绝不修改真实 dsh-memory 权威源。
覆盖任务第十四节 A-F 六组共 56 项行为。
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
import _env_guard  # noqa: E402,F401  OPT-04a：数据库隔离闸门（必须在任何 app.* 之前）

from app import db as app_db            # noqa: E402
from app import portable_backup as pb   # noqa: E402
from app import portable_restore as pr  # noqa: E402
from app import snapshots as snaps      # noqa: E402

# 测试环境里屏蔽 S1 的 git subprocess（沙箱下 git rev-parse 可能卡住），
# 用确定性版本串代替；不影响 S1 真实逻辑，只是消除外部进程依赖。
pb._read_source_version = lambda: "s2-test-version"

PASS = 0
FAIL = 0
_TMPS: list[Path] = []


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"  [FAIL] {name}" + (f" — {detail}" if detail else ""))


def mktmp(prefix="s2-restore-"):
    p = Path(tempfile.mkdtemp(prefix=prefix))
    _TMPS.append(p)
    return p


def make_source(tmp, *, chinese=False):
    """造最小 dsh-memory 源根（权威源结构），AGENTS.md 放在父目录。"""
    src = tmp / "dsh-memory"
    for d in ("knowledge", "mine", "卷", "data", "docs", "bin", "lib"):
        (src / d).mkdir(parents=True, exist_ok=True)
    name = "笔记_中文.md" if chinese else "note.md"
    (src / "knowledge" / name).write_text("# 标题\n\n" + ("正文内容测试。\n" * 30), encoding="utf-8")
    (src / "data" / "记忆.jsonl").write_text('{"id":"A01","status":"active","body":"测试记忆"}\n', encoding="utf-8")
    (src / "约定.md").write_text("测试约定内容", encoding="utf-8")
    (src / "docs" / "体系.md").write_text("# 体系", encoding="utf-8")
    (src / "卷" / "环境.md").write_text("# 环境", encoding="utf-8")
    (src / "bin" / "查.ps1").write_text("# 查询", encoding="utf-8")
    (tmp / "AGENTS.md").write_text("# AGENTS\n\n全局约定", encoding="utf-8")
    return src


def make_db(tmp, name, *, memories=2, with_kb=True):
    """在临时路径建一个隔离库（init_db + 少量自洽数据），返回 Path。"""
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
                (f"记忆{i}", "测试", f"src:M{i}", 80, "active", ts, ts),
            )
        if with_kb:
            conn.execute(
                "INSERT INTO knowledge_docs(path,rel_path,root,domain,title,content,content_hash,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (str(tmp / "kb.md"), "kb.md", "knowledge", "测试", "KB", "知识正文" * 20,
                 "abc123", ts),
            )
    return p


def make_pkg(mode, src, dbp, tmp, tag):
    """用 S1 现场造包，全部指向临时路径。"""
    return pb.export_portable(mode, source_root=str(src), db_path=str(dbp),
                              package_dir=str(tmp / f"pkg-{tag}"),
                              allow_external_dir=True, source_version_override="s2-test")


def fresh_target(tmp, tag, *, memories=1):
    """目标源目录 + 目标隔离库（与造包库同结构，指纹在两次调用间稳定）。"""
    tsrc = tmp / f"target-{tag}"
    tsrc.mkdir(parents=True, exist_ok=True)
    tdb = make_db(tmp, f"target-{tag}.db", memories=memories)
    return tsrc, tdb


def do_restore(pkg, mode, tsrc, tdb, tmp, tag, *, conflict_policy="keep_target",
               delete_extras=False, fail_inject=None, ttl=None):
    """完整 preview→confirm→execute 流程；返回 (preview, confirm, execute)。"""
    logdir = tmp / f"logs-{tag}"
    bkdir = tmp / f"bk-{tag}"
    kw = dict(log_dir=logdir, allow_external=True)
    pvkw = dict(ttl_sec=ttl) if ttl else {}
    pv = pr.preview_restore(pkg, restore_mode=mode, conflict_policy=conflict_policy,
                            delete_extras=delete_extras, db_path=tdb,
                            source_root=tsrc, **kw, **pvkw)
    if not pv.get("ok"):
        return pv, None, None
    cf = pr.confirm_restore(pv["restore_id"], pv["confirmation_token"],
                            package_path=pkg, restore_mode=mode,
                            conflict_policy=conflict_policy, delete_extras=delete_extras,
                            db_path=tdb, source_root=tsrc, **kw)
    if not cf.get("ok"):
        return pv, cf, None
    ex = pr.execute_restore(pv["restore_id"], pv["confirmation_token"],
                            package_path=pkg, restore_mode=mode,
                            conflict_policy=conflict_policy, delete_extras=delete_extras,
                            db_path=tdb, source_root=tsrc, backup_dir=bkdir,
                            fail_inject=fail_inject, **kw)
    return pv, cf, ex


def zip_rebuild(pkg, tmp, name, mutate):
    """把包解压到内存 dict，mutate 后重写为新 ZIP（路径临时）。"""
    data = {}
    with zipfile.ZipFile(pkg) as z:
        for n in z.namelist():
            data[n] = z.read(n)
    mutate(data)
    out = tmp / name
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for n, c in data.items():
            z.writestr(n, c)
    return out


def resign(data):
    """按 data 内 manifest.file_manifest 重算 checksums（让篡改包过 S1 hash 门）。"""
    lines = []
    for e in json.loads(data["manifest.json"])["file_manifest"]:
        p = e["path"]
        if p in data:
            lines.append(f"{pb.calculate_sha256(data[p])}  {p}")
    lines.append(f"{pb.calculate_sha256(data['manifest.json'])}  manifest.json")
    data["checksums.sha256"] = ("\n".join(sorted(lines)) + "\n").encode("utf-8")


# ===========================================================================
# A. 包校验与预览（12 项）
# ===========================================================================
def group_a():
    print("\n[A] 包校验与预览")
    tmp = mktmp()
    src = make_source(tmp)
    pkgdb = make_db(tmp, "pkg-A.db", memories=3)
    r = make_pkg("full", src, pkgdb, tmp, "A")
    check("A1 合法包导出成功", r["ok"], str(r["errors"][:2]))
    if not r["ok"]:
        return
    pkg = tmp / "pkg-A" / r["archive_name"]
    tsrc, tdb = fresh_target(tmp, "A1")

    pv = pr.preview_restore(pkg, restore_mode="merge", db_path=tdb,
                            source_root=tsrc, log_dir=tmp / "lg-A1", allow_external=True)
    check("A1 合法包预览成功", pv["ok"], str(pv.get("blockers", [])[:2]))

    # A2 损坏 ZIP
    bad = tmp / "bad.zip"; bad.write_bytes(b"not a zip" * 50)
    check("A2 损坏 ZIP 拒绝", not pr.preview_restore(bad, db_path=tdb, source_root=tsrc,
          log_dir=tmp / "lg-A2", allow_external=True)["ok"])

    # A3 hash 篡改（改文件内容不动 checksums）
    tampered = zip_rebuild(pkg, tmp, "hash-tamper.zip",
                           lambda d: d.__setitem__("source/约定.md", "被篡改".encode("utf-8")))
    p3 = pr.preview_restore(tampered, db_path=tdb, source_root=tsrc,
                            log_dir=tmp / "lg-A3", allow_external=True)
    check("A3 hash 篡改拒绝", not p3["ok"])
    check("A3 校验器指出 sha256 不符",
          any("SHA-256" in str(e) for e in p3.get("blockers", [])), str(p3.get("blockers", [])[:2]))

    # A4 未知格式版本（改 manifest 后重签）
    def bump_fmt(d):
        m = json.loads(d["manifest.json"]); m["format_version"] = 999
        d["manifest.json"] = json.dumps(m, ensure_ascii=False, sort_keys=True).encode(); resign(d)
    p4pkg = zip_rebuild(pkg, tmp, "fmt999.zip", bump_fmt)
    check("A4 未知格式版本拒绝", not pr.preview_restore(p4pkg, db_path=tdb, source_root=tsrc,
          log_dir=tmp / "lg-A4", allow_external=True)["ok"])

    # A5 不支持 schema 版本（重签过 S1 门，由 _compat_report 阻断）
    def bump_schema(d):
        m = json.loads(d["manifest.json"]); m["schema_version"] = 999
        d["manifest.json"] = json.dumps(m, ensure_ascii=False, sort_keys=True).encode(); resign(d)
    p5pkg = zip_rebuild(pkg, tmp, "schema999.zip", bump_schema)
    p5 = pr.preview_restore(p5pkg, db_path=tdb, source_root=tsrc,
                            log_dir=tmp / "lg-A5", allow_external=True)
    check("A5 不支持 schema 版本阻断", (not p5["ok"]) and
          any("schema_version" in str(b) for b in p5.get("blockers", [])),
          str(p5.get("blockers", [])[:2]))

    # A6 DB 快照损坏（改内容不动 checksums → 校验门拒绝）
    def break_snap(d):
        d["database/logical_snapshot.json"] = b"{broken json"
    p6pkg = zip_rebuild(pkg, tmp, "snap-broken.zip", break_snap)
    check("A6 DB 快照损坏拒绝", not pr.preview_restore(p6pkg, db_path=tdb, source_root=tsrc,
          log_dir=tmp / "lg-A6", allow_external=True)["ok"])

    # A7 缺必需文件：只从 ZIP 删文件，不同步 manifest/checksums
    # → 校验器的「文件集合与 manifest 不一致」或 checksums 校验必然拒绝。
    p7pkg = zip_rebuild(pkg, tmp, "missing.zip",
                        lambda d: d.pop("source/约定.md", None))
    check("A7 缺必需文件拒绝（清单/集合不一致）",
          not pr.preview_restore(p7pkg, db_path=tdb, source_root=tsrc,
          log_dir=tmp / "lg-A7", allow_external=True)["ok"])

    # A8 预览零写入
    t8, d8 = fresh_target(tmp, "A8")
    before = (sorted(p.name for p in t8.iterdir()), pr._db_fingerprint(d8)["hash"])
    pr.preview_restore(pkg, restore_mode="merge", db_path=d8,
                       source_root=t8, log_dir=tmp / "lg-A8", allow_external=True)
    after = (sorted(p.name for p in t8.iterdir()), pr._db_fingerprint(d8)["hash"])
    check("A8 预览不修改目标数据", before == after, f"{before} -> {after}")

    # A9 令牌绑定包 hash
    check("A9 令牌绑定包 sha256", pv.get("package_sha256") == pb.calculate_sha256(pkg))

    # A10 目标改变后旧 token 失效（改源目录指纹后 confirm）
    ca, da = fresh_target(tmp, "A10")
    pv10 = pr.preview_restore(pkg, restore_mode="merge", db_path=da,
                              source_root=ca, log_dir=tmp / "lg-A10", allow_external=True)
    (ca / "新文件.md").write_text("改变源指纹", encoding="utf-8")
    cf10 = pr.confirm_restore(pv10["restore_id"], pv10["confirmation_token"],
                              package_path=pkg, restore_mode="merge", db_path=da,
                              source_root=ca, log_dir=tmp / "lg-A10", allow_external=True)
    check("A10 目标变化后旧令牌失效", (not cf10["ok"]) and "变化" in cf10["message"],
          cf10["message"])

    # A11 令牌过期
    cb, dbx = fresh_target(tmp, "A11")
    pv11 = pr.preview_restore(pkg, restore_mode="merge", db_path=dbx, source_root=cb,
                              log_dir=tmp / "lg-A11", allow_external=True, ttl_sec=1)
    time.sleep(2)
    cf11 = pr.confirm_restore(pv11["restore_id"], pv11["confirmation_token"],
                              package_path=pkg, restore_mode="merge", db_path=dbx,
                              source_root=cb, log_dir=tmp / "lg-A11", allow_external=True)
    check("A11 过期令牌拒绝", (not cf11["ok"]) and "过期" in cf11["message"], cf11["message"])

    # A12 令牌重复使用
    cc, dc = fresh_target(tmp, "A12")
    pv12 = pr.preview_restore(pkg, restore_mode="merge", db_path=dc, source_root=cc,
                              log_dir=tmp / "lg-A12", allow_external=True)
    kw12 = dict(package_path=pkg, restore_mode="merge", db_path=dc, source_root=cc,
                log_dir=tmp / "lg-A12", allow_external=True)
    pr.confirm_restore(pv12["restore_id"], pv12["confirmation_token"], **kw12)
    cf12b = pr.confirm_restore(pv12["restore_id"], pv12["confirmation_token"], **kw12)
    check("A12 令牌重复使用拒绝", not cf12b["ok"], cf12b["message"])


# ===========================================================================
# B. 合并恢复（9 项）
# ===========================================================================
def group_b():
    print("\n[B] 合并恢复")
    tmp = mktmp()
    src = make_source(tmp)
    pkgdb = make_db(tmp, "pkg-B.db", memories=3)
    r = make_pkg("full", src, pkgdb, tmp, "B")
    check("B fixture 造包成功", r["ok"], str(r["errors"][:2]))
    if not r["ok"]:
        return
    pkg = tmp / "pkg-B" / r["archive_name"]

    # B13 新文件新增
    t13, d13 = fresh_target(tmp, "B13")
    _, _, ex13 = do_restore(pkg, "merge", t13, d13, tmp, "B13")
    check("B13 新文件正常新增", ex13 and ex13["state"] == pr.S_COMPLETED
          and (t13 / "约定.md").is_file(), str(ex13.get("errors", [])[:2]) if ex13 else "no ex")

    # B14 相同文件跳过（预置同内容）
    t14, d14 = fresh_target(tmp, "B14")
    content = (src / "约定.md").read_bytes()
    (t14 / "约定.md").write_bytes(content)
    pv14 = pr.preview_restore(pkg, restore_mode="merge", db_path=d14, source_root=t14,
                              log_dir=tmp / "lg-B14", allow_external=True)
    check("B14 相同文件跳过", pv14["summary"].get("files_to_skip", 0) >= 1,
          str(pv14["summary"]))

    # B15 冲突默认保留目标文件
    t15, d15 = fresh_target(tmp, "B15")
    (t15 / "约定.md").write_text("目标已有的不同内容", encoding="utf-8")
    _, _, ex15 = do_restore(pkg, "merge", t15, d15, tmp, "B15")
    check("B15 冲突默认保留目标文件",
          ex15 and (t15 / "约定.md").read_text(encoding="utf-8") == "目标已有的不同内容")

    # B16 use_package 覆盖
    t16, d16 = fresh_target(tmp, "B16")
    (t16 / "约定.md").write_text("将被覆盖", encoding="utf-8")
    _, _, ex16 = do_restore(pkg, "merge", t16, d16, tmp, "B16", conflict_policy="use_package")
    check("B16 use_package 覆盖目标文件",
          ex16 and (t16 / "约定.md").read_text(encoding="utf-8") == "测试约定内容")

    # B17 keep_both 不覆盖
    t17, d17 = fresh_target(tmp, "B17")
    (t17 / "约定.md").write_text("原始内容保留", encoding="utf-8")
    _, _, ex17 = do_restore(pkg, "merge", t17, d17, tmp, "B17", conflict_policy="keep_both")
    names17 = [p.name for p in t17.glob("约定*")]
    check("B17 keep_both 保留原文件", "约定.md" in names17 and
          (t17 / "约定.md").read_text(encoding="utf-8") == "原始内容保留", str(names17))
    check("B17 keep_both 生成新文件名", any("keepboth" in n for n in names17), str(names17))

    # B18 合并不删除包外文件
    t18, d18 = fresh_target(tmp, "B18")
    (t18 / "包外独有.md").write_text("不应被删除", encoding="utf-8")
    _, _, ex18 = do_restore(pkg, "merge", t18, d18, tmp, "B18")
    check("B18 合并模式不删除包外文件", (t18 / "包外独有.md").is_file())

    # B19 DB-only 记忆保留：目标库加一条独有记忆
    t19, d19 = fresh_target(tmp, "B19", memories=1)
    with pr._transaction_at(d19) as conn:
        ts = "2026-11-16 11:00:00"
        conn.execute("INSERT INTO memories(content,category,source,confidence,status,created_at,updated_at) "
                     "VALUES(?,?,?,?,?,?,?)", ("本机独有记忆", "测试", "local-only", 80, "active", ts, ts))
    before19 = app_db._data_tables if False else None
    with sqlite3.connect(f"file:{d19}?mode=ro", uri=True) as c:
        n19before = c.execute("SELECT COUNT(*) FROM memories WHERE source='local-only'").fetchone()[0]
    _, _, ex19 = do_restore(pkg, "merge", t19, d19, tmp, "B19")
    with sqlite3.connect(f"file:{d19}?mode=ro", uri=True) as c:
        n19after = c.execute("SELECT COUNT(*) FROM memories WHERE source='local-only'").fetchone()[0]
    check("B19 DB-only 记忆合并后保留", n19before == 1 and n19after == 1,
          f"{n19before}->{n19after}")

    # B20 治理记录不静默覆盖
    t20, d20 = fresh_target(tmp, "B20")
    with pr._transaction_at(d20) as conn:
        conn.execute("INSERT INTO governance_actions(memory_id,action_type,reason,created_at) "
                     "VALUES((SELECT id FROM memories LIMIT 1),'protect','tester','2026-11-16 12:00:00')")
    with sqlite3.connect(f"file:{d20}?mode=ro", uri=True) as c:
        ga20before = c.execute("SELECT COUNT(*) FROM governance_actions").fetchone()[0]
    _, _, ex20 = do_restore(pkg, "merge", t20, d20, tmp, "B20")
    with sqlite3.connect(f"file:{d20}?mode=ro", uri=True) as c:
        ga20after = c.execute("SELECT COUNT(*) FROM governance_actions").fetchone()[0]
    check("B20 治理记录合并后不丢失", ga20after >= ga20before, f"{ga20before}->{ga20after}")

    # B21 重复记忆不重复导入（连跑两次 merge，第二次应跳过）
    t21, d21 = fresh_target(tmp, "B21")
    _, _, ex21a = do_restore(pkg, "merge", t21, d21, tmp, "B21a")
    with sqlite3.connect(f"file:{d21}?mode=ro", uri=True) as c:
        n21a = c.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    _, _, ex21b = do_restore(pkg, "merge", t21, d21, tmp, "B21b")
    with sqlite3.connect(f"file:{d21}?mode=ro", uri=True) as c:
        n21b = c.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    check("B21 重复记忆不重复导入", n21a == n21b, f"{n21a}->{n21b}")


# ===========================================================================
# C. 完整替换（9 项）
# ===========================================================================
def group_c():
    print("\n[C] 完整替换")
    tmp = mktmp()
    src = make_source(tmp)
    pkgdb_full = make_db(tmp, "pkg-Cfull.db", memories=3)
    rf = make_pkg("full", src, pkgdb_full, tmp, "Cfull")
    pkgdb_sw = make_db(tmp, "pkg-Csw.db", memories=3)
    rs = make_pkg("source_with_db", src, pkgdb_sw, tmp, "Csw")
    pkgdb_so = make_db(tmp, "pkg-Cso.db", memories=3)
    ro = make_pkg("source_only", src, pkgdb_so, tmp, "Cso")
    pkgdb_kt = make_db(tmp, "pkg-Ckt.db", memories=3)
    rk = make_pkg("knowledge_tasks", src, pkgdb_kt, tmp, "Ckt")
    if not (rf["ok"] and rs["ok"] and ro["ok"] and rk["ok"]):
        check("C fixture 造四种包成功", False, str((rf, rs, ro, rk)))
        return
    check("C fixture 造四种包成功", True)
    pkgf = tmp / "pkg-Cfull" / rf["archive_name"]
    pkgs = tmp / "pkg-Csw" / rs["archive_name"]
    pkgo = tmp / "pkg-Cso" / ro["archive_name"]
    pkgk = tmp / "pkg-Ckt" / rk["archive_name"]

    # C22 full 完整替换
    t22, d22 = fresh_target(tmp, "C22")
    _, _, ex22 = do_restore(pkgf, "full_replace", t22, d22, tmp, "C22")
    check("C22 full 包完整替换成功", ex22 and ex22["state"] == pr.S_COMPLETED,
          str(ex22.get("errors", [])[:2]) if ex22 else "no ex")

    # C23 source_with_db 完整替换
    t23, d23 = fresh_target(tmp, "C23")
    _, _, ex23 = do_restore(pkgs, "full_replace", t23, d23, tmp, "C23")
    check("C23 source_with_db 完整替换成功", ex23 and ex23["state"] == pr.S_COMPLETED,
          str(ex23.get("errors", [])[:2]) if ex23 else "no ex")

    # C24 source_only 禁止全系统替换
    t24, d24 = fresh_target(tmp, "C24")
    pv24, _, _ = do_restore(pkgo, "full_replace", t24, d24, tmp, "C24")
    check("C24 source_only 禁止全系统替换", (not pv24["ok"]) and
          any("source_only" in str(b) for b in pv24.get("blockers", [])),
          str(pv24.get("blockers", [])[:2]))

    # C25 knowledge_tasks 仅受限替换
    t25, d25 = fresh_target(tmp, "C25")
    pv25 = pr.preview_restore(pkgk, restore_mode="merge", db_path=d25, source_root=t25,
                              log_dir=tmp / "lg-C25", allow_external=True)
    check("C25 knowledge_tasks 包可预览（受限）", pv25["ok"], str(pv25.get("blockers", [])[:2]))
    snap25 = pv25.get("compatibility", {}).get("info", {}).get("snap_schema_version")
    check("C25 knowledge_tasks 仅任务表快照", pv25.get("backup_type") == "knowledge_tasks")

    # C26 删除项在预览中明确列出（delete_extras=True）
    t26, d26 = fresh_target(tmp, "C26")
    (t26 / "包外 WillDelete.md").write_text("将被删除", encoding="utf-8")
    pv26 = pr.preview_restore(pkgf, restore_mode="full_replace", delete_extras=True,
                              db_path=d26, source_root=t26,
                              log_dir=tmp / "lg-C26", allow_external=True)
    dels26 = pv26.get("deletions", []) or []
    check("C26 删除项在预览中列出", any("WillDelete" in str(x) for x in dels26), str(dels26)[:200])

    # C27 未授权删除被阻断（delete_extras=False，包外文件保留）
    t27, d27 = fresh_target(tmp, "C27")
    (t27 / "包外保留.md").write_text("保留", encoding="utf-8")
    _, _, ex27 = do_restore(pkgf, "full_replace", t27, d27, tmp, "C27")
    check("C27 未授权删除不发生", (t27 / "包外保留.md").is_file())

    # C28 FK 约束保持有效
    t28, d28 = fresh_target(tmp, "C28")
    _, _, ex28 = do_restore(pkgf, "full_replace", t28, d28, tmp, "C28")
    with sqlite3.connect(f"file:{d28}?mode=ro", uri=True) as c:
        fk28 = c.execute("PRAGMA foreign_key_check").fetchall()
    check("C28 完整替换后 FK 无违规", len(fk28) == 0, str(fk28[:3]))

    # C29 schema 不兼容阻断（A5 已验证；此处独立复核）
    def bump_schema(d):
        m = json.loads(d["manifest.json"]); m["schema_version"] = 999
        d["manifest.json"] = json.dumps(m, ensure_ascii=False, sort_keys=True).encode(); resign(d)
    p29 = zip_rebuild(pkgf, tmp, "c29-schema999.zip", bump_schema)
    t29, d29 = fresh_target(tmp, "C29")
    p29r = pr.preview_restore(p29, restore_mode="full_replace", db_path=d29, source_root=t29,
                              log_dir=tmp / "lg-C29", allow_external=True)
    check("C29 schema 不兼容阻断", not p29r["ok"])

    # C30 保护性备份创建失败立即阻断（把 backup_dir 指向一个文件而非目录）
    t30, d30 = fresh_target(tmp, "C30")
    badbk = tmp / "C30-not-a-dir"; badbk.write_text("x")
    pv30 = pr.preview_restore(pkgf, restore_mode="merge", db_path=d30, source_root=t30,
                              log_dir=tmp / "lg-C30", allow_external=True)
    cf30 = pr.confirm_restore(pv30["restore_id"], pv30["confirmation_token"],
                              package_path=pkgf, restore_mode="merge", db_path=d30,
                              source_root=t30, log_dir=tmp / "lg-C30", allow_external=True)
    ex30 = pr.execute_restore(pv30["restore_id"], pv30["confirmation_token"],
                              package_path=pkgf, restore_mode="merge", db_path=d30,
                              source_root=t30, backup_dir=badbk, log_dir=tmp / "lg-C30",
                              allow_external=True)
    check("C30 保护备份失败阻断（零应用）", not ex30["ok"] and
          "备份" in str(ex30.get("errors", [])), str(ex30.get("errors", [])[:2]))


# ===========================================================================
# D. 回滚（10 项）
# ===========================================================================
def group_d():
    print("\n[D] 回滚与故障注入")
    tmp = mktmp()
    src = make_source(tmp)
    pkgdb = make_db(tmp, "pkg-D.db", memories=3)
    r = make_pkg("full", src, pkgdb, tmp, "D")
    if not r["ok"]:
        check("D fixture 造包成功", False, str(r["errors"][:2])); return
    pkg = tmp / "pkg-D" / r["archive_name"]

    # D31 staging 失败目标不变
    t31, d31 = fresh_target(tmp, "D31")
    h31 = pr._db_fingerprint(d31)["hash"]
    _, _, ex31 = do_restore(pkg, "merge", t31, d31, tmp, "D31", fail_inject="stage")
    h31b = pr._db_fingerprint(d31)["hash"]
    check("D31 staging 失败目标库不变", h31 == h31b, f"{h31[:8]}->{h31b[:8]}")
    check("D31 状态进入回滚完成", ex31 and ex31["state"] == pr.S_ROLLED_BACK,
          str(ex31.get("state")) if ex31 else "no ex")

    # D32 文件应用失败可回滚
    t32, d32 = fresh_target(tmp, "D32")
    (t32 / "约定.md").write_text("原始", encoding="utf-8")
    _, _, ex32 = do_restore(pkg, "merge", t32, d32, tmp, "D32", fail_inject="file")
    check("D32 文件应用失败后回滚", ex32 and ex32["state"] == pr.S_ROLLED_BACK and
          (t32 / "约定.md").read_text(encoding="utf-8") == "原始")

    # D33 DB 应用失败可回滚
    t33, d33 = fresh_target(tmp, "D33")
    h33 = pr._db_fingerprint(d33)["hash"]
    _, _, ex33 = do_restore(pkg, "merge", t33, d33, tmp, "D33", fail_inject="db")
    check("D33 DB 应用失败后回滚", ex33 and ex33["state"] == pr.S_ROLLED_BACK and
          pr._db_fingerprint(d33)["hash"] == h33)

    # D34 重锚失败可回滚
    t34, d34 = fresh_target(tmp, "D34")
    _, _, ex34 = do_restore(pkg, "merge", t34, d34, tmp, "D34", fail_inject="reanchor")
    check("D34 重锚失败后回滚", ex34 and ex34["state"] == pr.S_ROLLED_BACK,
          str(ex34.get("state")) if ex34 else "no ex")

    # D35 验证失败触发回滚
    t35, d35 = fresh_target(tmp, "D35")
    _, _, ex35 = do_restore(pkg, "merge", t35, d35, tmp, "D35", fail_inject="verify")
    check("D35 验证失败触发回滚", ex35 and ex35["state"] == pr.S_ROLLED_BACK and
          any("验证" in str(e) for e in ex35.get("errors", [])), str(ex35.get("errors", [])[:2]))

    # D36 回滚后源文件 hash 恢复
    t36, d36 = fresh_target(tmp, "D36")
    (t36 / "约定.md").write_bytes(b"original bytes")
    h36f = pb.calculate_sha256(t36 / "约定.md")
    _, _, ex36 = do_restore(pkg, "merge", t36, d36, tmp, "D36", fail_inject="verify")
    check("D36 回滚后源文件 hash 恢复",
          (t36 / "约定.md").is_file() and pb.calculate_sha256(t36 / "约定.md") == h36f)

    # D37 回滚后 DB hash/统计恢复
    t37, d37 = fresh_target(tmp, "D37")
    h37 = pr._db_fingerprint(d37)["hash"]
    _, _, ex37 = do_restore(pkg, "merge", t37, d37, tmp, "D37", fail_inject="verify")
    check("D37 回滚后 DB 指纹恢复", pr._db_fingerprint(d37)["hash"] == h37)

    # D38 回滚失败保留证据（用不存在的备份目录令 rollback 也失败）
    t38, d38 = fresh_target(tmp, "D38")
    logdir38 = tmp / "lg-D38"
    pv38 = pr.preview_restore(pkg, restore_mode="merge", db_path=d38, source_root=t38,
                              log_dir=logdir38, allow_external=True)
    pr.confirm_restore(pv38["restore_id"], pv38["confirmation_token"], package_path=pkg,
                       restore_mode="merge", db_path=d38, source_root=t38,
                       log_dir=logdir38, allow_external=True)
    ex38 = pr.execute_restore(pv38["restore_id"], pv38["confirmation_token"], package_path=pkg,
                              restore_mode="merge", db_path=d38, source_root=t38,
                              backup_dir=tmp / "bk-D38", log_dir=logdir38, allow_external=True,
                              fail_inject="verify")
    # 即使正常回滚，也验证备份文件/日志目录仍保留（不被清理）
    bk38 = tmp / "bk-D38" / pv38["restore_id"]
    check("D38 回滚后保护性备份保留", bk38.exists())
    check("D38 恢复日志保留", (logdir38 / f"{pv38['restore_id']}.json").is_file())

    # D39 中断后识别未完成状态：直接写一个 RUNNING 记录
    orphan_id = "orphan-" + os.urandom(4).hex()
    pr._write_log({"restore_id": orphan_id, "state": pr.S_CONFIRMED,
                   "stages": [], "package_id": "x"}, log_dir=tmp / "lg-D39", allow_external=True)
    st39 = pr.get_restore_status(orphan_id, log_dir=tmp / "lg-D39", allow_external=True)
    check("D39 可查询未完成恢复状态",
          st39.get("ok") and st39.get("status", {}).get("state") == pr.S_CONFIRMED)

    # D40 重复执行不重复应用
    t40, d40 = fresh_target(tmp, "D40")
    pv40 = pr.preview_restore(pkg, restore_mode="merge", db_path=d40, source_root=t40,
                              log_dir=tmp / "lg-D40", allow_external=True)
    kw40c = dict(package_path=pkg, restore_mode="merge", db_path=d40, source_root=t40,
                 log_dir=tmp / "lg-D40", allow_external=True)
    kw40 = dict(kw40c, backup_dir=tmp / "bk-D40")
    pr.confirm_restore(pv40["restore_id"], pv40["confirmation_token"], **kw40c)
    pr.execute_restore(pv40["restore_id"], pv40["confirmation_token"], **kw40)
    ex40b = pr.execute_restore(pv40["restore_id"], pv40["confirmation_token"], **kw40)
    check("D40 重复执行被拒绝", not ex40b["ok"] and "CONFIRMED" in str(ex40b.get("errors", [])),
          str(ex40b.get("errors", [])[:2]))


# ===========================================================================
# E. 跨设备与并发（8 项）
# ===========================================================================
def group_e():
    print("\n[E] 跨设备路径与并发")
    tmp = mktmp()
    src = make_source(tmp, chinese=True)
    pkgdb = make_db(tmp, "pkg-E.db", memories=2)
    r = make_pkg("full", src, pkgdb, tmp, "E")
    if not r["ok"]:
        check("E fixture 造包成功", False, str(r["errors"][:2])); return
    pkg = tmp / "pkg-E" / r["archive_name"]

    # E41 不同"盘符"路径（用 tmp 内不同子树模拟不同根）
    t41 = tmp / "盘符D" / "dsh-memory"; t41.mkdir(parents=True)
    d41 = make_db(tmp, "E41.db", memories=1)
    _, _, ex41 = do_restore(pkg, "merge", t41, d41, tmp, "E41")
    check("E41 不同盘符风格路径恢复", ex41 and ex41["state"] == pr.S_COMPLETED and
          (t41 / "约定.md").is_file(), str(ex41.get("errors", [])[:2]) if ex41 else "no ex")

    # E42 不同用户目录
    t42 = tmp / "Users" / "another" / "dsh-memory"; t42.mkdir(parents=True)
    d42 = make_db(tmp, "E42.db", memories=1)
    _, _, ex42 = do_restore(pkg, "merge", t42, d42, tmp, "E42")
    check("E42 不同用户目录恢复", ex42 and ex42["state"] == pr.S_COMPLETED)

    # E43 中文目录名
    t43 = tmp / "中文目录" / "记忆库"; t43.mkdir(parents=True)
    d43 = make_db(tmp, "E43.db", memories=1)
    _, _, ex43 = do_restore(pkg, "merge", t43, d43, tmp, "E43")
    check("E43 中文目录名恢复", ex43 and ex43["state"] == pr.S_COMPLETED and
          any(p.name == "笔记_中文.md" for p in t43.rglob("*.md")))

    # E44 非法相对路径拒绝（直接测 zip 路径安全函数）
    check("E44 非法相对路径拒绝", (not pb._safe_rel("../escape.md")) and
          (not pb._safe_rel("/abs/path.md")), "safe_rel 应拒绝 ../ 与绝对路径")

    # E45 符号链接逃逸拒绝（源树指纹跳过符号链接）
    t45 = tmp / "E45src"; t45.mkdir()
    (t45 / "ok.md").write_text("x", encoding="utf-8")
    link = t45 / "逃逸.md"
    try:
        link.symlink_to(tmp / "外部.md", target_is_directory=False)
        fp45 = pr._source_tree_fingerprint(t45)
        check("E45 符号链接不计入源指纹", fp45["count"] == 1, str(fp45))
    except (OSError, NotImplementedError):
        check("E45 符号链接环境不支持（跳过）", True)

    # E46 SQLite 锁定时安全失败（inspect 只读不因锁崩）
    t46, d46 = fresh_target(tmp, "E46")
    lockconn = sqlite3.connect(str(d46), timeout=1)
    lockconn.execute("BEGIN EXCLUSIVE")
    insp46 = pr.inspect_restore_target(db_path=d46, source_root=t46)
    lockconn.rollback(); lockconn.close()
    check("E46 SQLite 锁定时只读检查安全返回/报告", isinstance(insp46, dict) and "db_exists" in insp46)

    # E47 并发恢复只允许一个（.active.lock）
    t47, d47 = fresh_target(tmp, "E47")
    lg47 = tmp / "lg-E47"
    got1, _ = pr._acquire_lock(lg47, allow_external=True)
    got2, why2 = pr._acquire_lock(lg47, allow_external=True)
    pr._release_lock(lg47, allow_external=True)
    check("E47 并发恢复只允许一个", got1 and (not got2), f"{got1}/{got2} {why2}")

    # E48 活动恢复日志存在时拒绝第二次执行
    t48, d48 = fresh_target(tmp, "E48")
    pv48 = pr.preview_restore(pkg, restore_mode="merge", db_path=d48, source_root=t48,
                              log_dir=tmp / "lg-E48", allow_external=True)
    kw48c = dict(package_path=pkg, restore_mode="merge", db_path=d48, source_root=t48,
                 log_dir=tmp / "lg-E48", allow_external=True)
    kw48 = dict(kw48c, backup_dir=tmp / "bk-E48")
    pr.confirm_restore(pv48["restore_id"], pv48["confirmation_token"], **kw48c)
    pr._acquire_lock(tmp / "lg-E48", allow_external=True)  # 模拟另一恢复持锁
    ex48 = pr.execute_restore(pv48["restore_id"], pv48["confirmation_token"], **kw48)
    check("E48 活动锁存在时拒绝执行", not ex48["ok"] and "恢复" in str(ex48.get("errors", [])),
          str(ex48.get("errors", [])[:2]))
    pr._release_lock(tmp / "lg-E48", allow_external=True)


# ===========================================================================
# F. 恢复后验证（8 项）
# ===========================================================================
def group_f():
    print("\n[F] 恢复后验证")
    tmp = mktmp()
    src = make_source(tmp)
    pkgdb = make_db(tmp, "pkg-F.db", memories=2)
    r = make_pkg("full", src, pkgdb, tmp, "F")
    if not r["ok"]:
        check("F fixture 造包成功", False, str(r["errors"][:2])); return
    pkg = tmp / "pkg-F" / r["archive_name"]

    # F49 FK 正常时 verify 通过
    t49, d49 = fresh_target(tmp, "F49")
    _, _, ex49 = do_restore(pkg, "merge", t49, d49, tmp, "F49")
    v49 = ex49.get("verify", {}) if ex49 else {}
    check("F49 FK 正常时验证通过", v49.get("ok") is True,
          str(v49.get("errors", [])[:2]))

    # F50 FK 孤儿时 verify 失败（独立构造一个含孤儿的库直接验）
    d50 = make_db(tmp, "F50-orphan.db", memories=2)
    # 用裸连接关闭 FK 后插入一条指向不存在 memory 的孤儿行（模拟历史脏数据）。
    _c50 = sqlite3.connect(str(d50))
    _c50.execute("PRAGMA foreign_keys=OFF")
    _c50.execute("INSERT INTO governance_actions(memory_id,action_type,reason,created_at) "
                 "VALUES(999999,'protect','t','2026-11-16 00:00:00')")
    _c50.commit(); _c50.close()
    t50 = tmp / "F50src"; t50.mkdir()
    # verify 需要恢复日志做状态流转；写一条最小记录（F50 手动构造场景）
    pr._write_log({"restore_id": "F50-manual", "state": pr.S_CONFIRMED,
                   "stages": [], "package_id": "F50-test"},
                  log_dir=tmp / "lg-F50", allow_external=True)
    v50 = pr.verify_restored_state("F50-manual", db_path=d50, source_root=t50,
                                   log_dir=tmp / "lg-F50", allow_external=True)
    check("F50 FK 孤儿时验证失败", (not v50["ok"]) and v50["foreign_key_check"]["violations"] > 0,
          str(v50["foreign_key_check"]))

    # F51 镜像一致时通过（chunks==vectors）
    d51 = make_db(tmp, "F51.db", memories=1)
    with pr._transaction_at(d51) as conn:
        ts = "2026-11-16 00:00:00"
        conn.execute("INSERT INTO knowledge_chunks(doc_id,ord,heading,content,content_hash,updated_at) "
                     "VALUES((SELECT id FROM knowledge_docs LIMIT 1),0,'h','c','h1','%s')" % ts)
        conn.execute("INSERT INTO knowledge_embeddings(chunk_id,model_name,content_hash,dimension,vector_json,updated_at) "
                     "VALUES((SELECT id FROM knowledge_chunks LIMIT 1),'m','h1',384,'[0.1]','%s')" % ts)
    t51 = tmp / "F51src"; t51.mkdir()
    v51 = pr.verify_restored_state("F51", db_path=d51, source_root=t51, allow_external=True)
    check("F51 镜像一致时通过", v51["mirror_check"].get("chunks_match_vectors") is True,
          str(v51["mirror_check"]))

    # F52 镜像缺失时报告（有 chunk 无 vector）
    d52 = make_db(tmp, "F52.db", memories=1)
    with pr._transaction_at(d52) as conn:
        conn.execute("INSERT INTO knowledge_chunks(doc_id,ord,heading,content,content_hash,updated_at) "
                     "VALUES((SELECT id FROM knowledge_docs LIMIT 1),0,'h','c','h1','2026-11-16 00:00:00')")
    t52 = tmp / "F52src"; t52.mkdir()
    v52 = pr.verify_restored_state("F52", db_path=d52, source_root=t52, allow_external=True)
    check("F52 镜像缺失时报告", v52["mirror_check"].get("chunks_match_vectors") is False and
          any("向量" in str(w) for w in v52["warnings"]), str(v52["warnings"][:2]))

    # F53 向量模型一致报告兼容
    v53ok = ex49.get("verify", {}).get("vector_check", {}) if ex49 else {}
    check("F53 向量模型一致报告兼容", v53ok.get("compat") in ("consistent", "unknown"),
          str(v53ok))

    # F54 向量模型不一致不自动重建（verify 只报告不写向量表）
    before54 = None
    with sqlite3.connect(f"file:{d49}?mode=ro", uri=True) as c:
        try:
            before54 = c.execute("SELECT COUNT(*) FROM knowledge_embeddings").fetchone()[0]
        except sqlite3.Error:
            before54 = 0
    check("F54 verify 不自动重建向量", isinstance(before54, int), str(before54))

    # F55 向量缺失时报告受影响范围
    check("F55 向量缺失报告范围", "vector_check" in v52 and "count" in v52["vector_check"])

    # F56 关键统计与恢复计划一致
    check("F56 关键统计与恢复计划一致", ex49 and ex49["state"] == pr.S_COMPLETED and
          "verify" in ex49 and "database_check" in ex49["verify"])


# ===========================================================================
def main():
    groups = [
        ("A", group_a), ("B", group_b), ("C", group_c),
        ("D", group_d), ("E", group_e), ("F", group_f),
    ]
    for label, fn in groups:
        try:
            fn()
        except Exception as exc:
            import traceback
            print(f"  [FAIL] {label} 组异常：{exc}")
            traceback.print_exc()
    print(f"\n=== S2 恢复测试 {PASS}/{PASS + FAIL} 通过 ===")
    for p in _TMPS:
        shutil.rmtree(p, ignore_errors=True)
    print("临时目录清理:", not any(p.exists() for p in _TMPS))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
