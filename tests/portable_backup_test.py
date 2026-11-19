"""S1 Portable Memory Package 独立测试。

只用临时目录/临时 SQLite 库；不依赖生产库数据，不修改 dsh-memory 权威源。
覆盖 9 类基础验收 + 扩展安全场景（大小写路径、ZIP 炸弹、未知高版本、
缺 manifest/checksums、敏感文件排除、异常清理、DB 隔离、四模式矩阵）。
"""
from __future__ import annotations

import io
import json
import shutil
import os
import sys
import tempfile
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app import portable_backup as pb  # noqa: E402
from app import db as app_db  # noqa: E402

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


def mktmp(prefix="s1-test-"):
    p = Path(tempfile.mkdtemp(prefix=prefix))
    _TMPS.append(p)
    return p


def make_source(tmp, *, with_agents=True, chinese=False, extra=None):
    """造最小 dsh-memory 源根，返回 (src_root, agents_path)。"""
    src = tmp / "dsh-memory"
    for d in ("knowledge", "mine", "卷", "data", "docs", "bin", "lib", "var"):
        (src / d).mkdir(parents=True, exist_ok=True)
    name = "笔记_中文.md" if chinese else "note.md"
    (src / "knowledge" / name).write_text("# 标题\n\n" + ("正文内容测试。\n" * 50), encoding="utf-8")
    (src / "data" / "记忆.jsonl").write_text('{"id":"A01","status":"active","body":"测试"}\n', encoding="utf-8")
    (src / "约定.md").write_text("约定内容", encoding="utf-8")
    (src / "docs" / "体系.md").write_text("# 体系", encoding="utf-8")
    (src / "卷" / "环境.md").write_text("# 环境", encoding="utf-8")
    (src / "bin" / "查.ps1").write_text("# 查询脚本", encoding="utf-8")
    (src / "var" / "runtime.log").write_text("不应入包", encoding="utf-8")
    if extra:
        for rel, content in extra.items():
            p = src / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
    agents = src.parent / "AGENTS.md"
    if with_agents:
        agents.write_text("# AGENTS\n\n全局约定", encoding="utf-8")
    return src, agents


def export(mode, src, pkgdir, **kw):
    kw.setdefault("allow_external_dir", True)
    return pb.export_portable(mode, source_root=str(src), package_dir=str(pkgdir), **kw)


def zip_bytes(entries: dict[str, bytes]) -> bytes:
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w", zipfile.ZIP_DEFLATED) as z:
        for name, content in entries.items():
            z.writestr(name, content)
    return bio.getvalue()


def make_valid_pkg_bytes(mode="source_only"):
    """构造一个能过 validate 的最小合法包字节（自带一致的 manifest/checksums）。"""
    data = {"source/约定.md": "# 约定\n\n内容".encode("utf-8")}
    fm = []
    lines = []
    for p, c in data.items():
        h = pb.calculate_sha256(c)
        fm.append({"path": p, "size_bytes": len(c), "sha256": h,
                   "category": "wake", "required": True})
        lines.append(f"{h}  {p}")
    manifest = {
        "format_name": pb.FORMAT_NAME, "format_version": pb.FORMAT_VERSION,
        "project_name": pb.PROJECT_NAME, "project_version": "0.1.0",
        "schema_version": 0, "created_at": "2026-01-01T00:00:00+08:00",
        "backup_type": mode, "source_version": "test",
        "embedding": {"model_name": "test-model", "dimensions": 384},
        "checksum_algorithm": "sha256", "generator_version": "test",
        "file_count": len(fm), "total_size_bytes": sum(e["size_bytes"] for e in fm),
        "file_manifest": fm,
    }
    mb = json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode("utf-8")
    lines.append(f"{pb.calculate_sha256(mb)}  manifest.json")
    data["manifest.json"] = mb
    data["checksums.sha256"] = ("\n".join(sorted(lines)) + "\n").encode("utf-8")
    return data


def write_pkg(tmp, name, data):
    p = tmp / name
    p.write_bytes(zip_bytes(data))
    return p


# ---------------------------------------------------------------------------
def test_normal_export():
    print("[test_normal_export]")
    tmp = mktmp()
    src, _ = make_source(tmp)
    r = export("source_only", src, tmp / "pkg")
    check("正常导出 ok", r["ok"], str(r["errors"][:3]))
    check("返回结构字段齐全",
          all(k in r for k in ("package_id", "archive_name", "file_count", "manifest", "validation")))
    if r["ok"]:
        p = tmp / "pkg" / r["archive_name"]
        check("正式 ZIP 存在", p.is_file())
        with zipfile.ZipFile(p) as z:
            names = z.namelist()
            check("含 manifest", "manifest.json" in names)
            check("含 checksums", "checksums.sha256" in names)
            check("testzip=None", z.testzip() is None)
        v = pb.validate_package_archive(p, expected_type="source_only")
        check("自校验 ok", v["ok"], str(v["errors"][:3]))


def test_empty_source():
    print("[test_empty_source]")
    tmp = mktmp()
    empty = tmp / "empty"
    empty.mkdir()
    r = export("source_only", empty, tmp / "pkg")
    check("空源目录可解释失败", (not r["ok"]) and bool(r["errors"]), str(r["errors"][:2]))


def test_chinese_names():
    print("[test_chinese_names]")
    tmp = mktmp()
    src, _ = make_source(tmp, chinese=True, extra={"knowledge/深度笔记.md": "内容"})
    r = export("source_only", src, tmp / "pkg")
    check("中文文件名导出 ok", r["ok"], str(r["errors"][:3]))
    if r["ok"]:
        p = tmp / "pkg" / r["archive_name"]
        with zipfile.ZipFile(p) as z:
            names = z.namelist()
        check("中文路径入包", any("中文" in n or "深度" in n for n in names), str(names[:6]))
        check("中文名校验通过", pb.validate_package_archive(p, expected_type="source_only")["ok"])


def test_large_file_streaming():
    print("[test_large_file_streaming]")
    tmp = mktmp()
    src, _ = make_source(tmp)
    big = src / "knowledge" / "big.md"
    big.write_bytes(b"A" * (3 * 1024 * 1024))
    r = export("source_only", src, tmp / "pkg")
    check("大文件导出 ok", r["ok"], str(r["errors"][:3]))
    if r["ok"]:
        p = tmp / "pkg" / r["archive_name"]
        v = pb.validate_package_archive(p)
        check("大文件 hash 正确", v["ok"], str(v["errors"][:3]))


def test_zip_corrupt():
    print("[test_zip_corrupt]")
    tmp = mktmp()
    p = write_pkg(tmp, "corrupt.zip", make_valid_pkg_bytes())
    raw = bytearray(p.read_bytes())
    # 破坏中部字节
    mid = len(raw) // 2
    raw[mid:mid + 20] = b"\x00" * 20
    p.write_bytes(bytes(raw))
    v = pb.validate_package_archive(p)
    check("损坏 ZIP 校验失败", not v["ok"])
    check("有错误说明", bool(v["errors"]))


def test_hash_tamper():
    print("[test_hash_tamper]")
    tmp = mktmp()
    data = make_valid_pkg_bytes()
    m = json.loads(data["manifest.json"])
    m["file_manifest"][0]["sha256"] = "0" * 64
    mb = json.dumps(m, ensure_ascii=False, sort_keys=True).encode("utf-8")
    data["manifest.json"] = mb
    # 同步 checksums 里 manifest 条目，确保只暴露 hash 不匹配
    lines = [ln for ln in data["checksums.sha256"].decode().splitlines()
             if not ln.endswith("manifest.json")]
    lines.append(f"{pb.calculate_sha256(mb)}  manifest.json")
    data["checksums.sha256"] = ("\n".join(sorted(lines)) + "\n").encode()
    p = write_pkg(tmp, "tamper.zip", data)
    v = pb.validate_package_archive(p)
    check("hash 篡改校验失败", not v["ok"])
    check("sha256_match=False", v["checks"].get("sha256_match") is False)
    check("指出具体文件", any("source/约定.md" in e for e in v["errors"]), str(v["errors"][:3]))


def test_missing_required():
    print("[test_missing_required]")
    tmp = mktmp()
    data = make_valid_pkg_bytes()
    data.pop("source/约定.md")  # manifest 仍要求它
    p = write_pkg(tmp, "missing.zip", data)
    v = pb.validate_package_archive(p)
    check("缺必需文件校验失败", not v["ok"])
    check("required_present=False", v["checks"].get("required_present") is False)


def test_illegal_paths():
    print("[test_illegal_paths]")
    tmp = mktmp()
    p = write_pkg(tmp, "evilpath.zip", {"../evil.md": b"x", "manifest.json": b"{}", "checksums.sha256": b""})
    v = pb.validate_package_archive(p)
    check("路径穿越被拒", v["checks"].get("path_safety") is False)
    # 盘符与反斜杠
    p2 = write_pkg(tmp, "evilpath2.zip", {"C:/abs.md": b"x", "a\\b.md": b"y",
                                          "manifest.json": b"{}", "checksums.sha256": b""})
    v2 = pb.validate_package_archive(p2)
    check("盘符/反斜杠被拒", v2["checks"].get("path_safety") is False)


def test_duplicate_export():
    print("[test_duplicate_export]")
    tmp = mktmp()
    src, _ = make_source(tmp)
    pkg = tmp / "pkg"
    r1 = export("source_only", src, pkg)
    r2 = export("source_only", src, pkg)
    check("两次导出都成功", r1["ok"] and r2["ok"])
    check("包名唯一", r1["archive_name"] != r2["archive_name"],
          f"{r1['archive_name']} vs {r2['archive_name']}")
    check("两份包都在", (pkg / r1["archive_name"]).is_file() and (pkg / r2["archive_name"]).is_file())


def test_case_conflict():
    print("[test_case_conflict]")
    tmp = mktmp()
    p = write_pkg(tmp, "case.zip", {"A.md": b"1", "a.md": b"2",
                                     "manifest.json": b"{}", "checksums.sha256": b""})
    v = pb.validate_package_archive(p)
    check("大小写冲突被拒", v["checks"].get("case_conflicts") is False)


def test_zip_bomb_ratio():
    print("[test_zip_bomb_ratio]")
    tmp = mktmp()
    # 压缩比 > 200 且解压后体积 > 10MB 才算 ZIP 炸弹嫌疑
    p = write_pkg(tmp, "bomb.zip", {"data/big.txt": b"A" * (32 * 1024 * 1024),
                                     "manifest.json": b"{}", "checksums.sha256": b""})
    v = pb.validate_package_archive(p)
    check("高压缩比+超大体积被拒", v["checks"].get("compression_ratio") is False)


def test_unknown_high_version():
    print("[test_unknown_high_version]")
    tmp = mktmp()
    data = make_valid_pkg_bytes()
    m = json.loads(data["manifest.json"])
    m["format_version"] = 999
    mb = json.dumps(m, ensure_ascii=False, sort_keys=True).encode()
    data["manifest.json"] = mb
    lines = [ln for ln in data["checksums.sha256"].decode().splitlines() if not ln.endswith("manifest.json")]
    lines.append(f"{pb.calculate_sha256(mb)}  manifest.json")
    data["checksums.sha256"] = ("\n".join(sorted(lines)) + "\n").encode()
    p = write_pkg(tmp, "future.zip", data)
    v = pb.validate_package_archive(p)
    check("未知高版本被拒", not v["ok"])
    check("format_supported=False", v["checks"].get("format_supported") is False)


def test_missing_manifest():
    print("[test_missing_manifest]")
    tmp = mktmp()
    p = write_pkg(tmp, "nomani.zip", {"source/x.md": b"x", "checksums.sha256": b""})
    v = pb.validate_package_archive(p)
    check("缺 manifest 被拒", not v["ok"])
    check("manifest_present=False", v["checks"].get("manifest_present") is False)


def test_missing_checksums():
    print("[test_missing_checksums]")
    tmp = mktmp()
    data = make_valid_pkg_bytes()
    data.pop("checksums.sha256")
    p = write_pkg(tmp, "nocsum.zip", data)
    v = pb.validate_package_archive(p)
    check("缺 checksums 被拒", not v["ok"])
    check("checksums_present=False", v["checks"].get("checksums_present") is False)


def test_sensitive_excluded():
    print("[test_sensitive_excluded]")
    tmp = mktmp()
    src, agents = make_source(tmp, extra={
        ".env": "SECRET=123", "knowledge/console-token.txt": "token", "knowledge/normal.md": "ok",
    })
    r = export("source_only", src, tmp / "pkg")
    check("含敏感源仍导出成功", r["ok"], str(r["errors"][:3]))
    if r["ok"]:
        p = tmp / "pkg" / r["archive_name"]
        with zipfile.ZipFile(p) as z:
            names = z.namelist()
        check(".env 未入包", not any(n.endswith(".env") for n in names))
        check("console-token 未入包", not any("console-token" in n for n in names))
        check("正常文件入包", any("normal.md" in n for n in names))
    # 反向：构造含敏感文件的包必须被拒
    p2 = write_pkg(tmp, "sensitive.zip", {**make_valid_pkg_bytes(), "credentials/token.txt": b"x"})
    v = pb.validate_package_archive(p2)
    check("含敏感文件包被拒", v["checks"].get("no_sensitive") is False)


def test_temp_cleanup_on_failure():
    print("[test_temp_cleanup_on_failure]")
    tmp = mktmp()
    src, _ = make_source(tmp)
    pkg = tmp / "pkg"
    # monkeypatch validate 使其拒绝，触发 export 的失败清理路径
    orig = pb.validate_package_archive
    pb.validate_package_archive = lambda *a, **k: {"ok": False, "errors": ["模拟校验失败"]}
    try:
        r = export("source_only", src, pkg)
    finally:
        pb.validate_package_archive = orig
    check("校验失败时导出返回失败", not r["ok"])
    leftovers = [f.name for f in pkg.iterdir()] if pkg.is_dir() else []
    check("无残留临时/正式 ZIP", not any(n.startswith(".s1-pkg-") or n.endswith(".zip") for n in leftovers),
          str(leftovers))


def test_db_snapshot_isolated():
    print("[test_db_snapshot_isolated]")
    tmp = mktmp()
    src, _ = make_source(tmp)
    # 造临时隔离库（不碰生产库）
    isodb = tmp / "iso.db"
    old_db_path = app_db.DB_PATH
    try:
        app_db.DB_PATH = isodb
        app_db.init_db()
        with app_db.transaction() as c:
            c.execute("INSERT INTO memories(content,category,source,confidence,status,created_at,updated_at) "
                      "VALUES(?,?,?,?,?,?,?)",
                      ("隔离测试记忆", "测试", "test", 0.8, "active", "2026-01-01", "2026-01-01"))
    finally:
        app_db.DB_PATH = old_db_path
    before = pb.calculate_sha256(isodb)
    r = export("source_with_db", src, tmp / "pkg", db_path=str(isodb))
    check("DB 快照模式导出 ok", r["ok"], str(r["errors"][:3]))
    after = pb.calculate_sha256(isodb)
    check("导出前后隔离库字节不变（无写入）", before == after)
    if r["ok"]:
        p = tmp / "pkg" / r["archive_name"]
        with zipfile.ZipFile(p) as z:
            check("含逻辑快照", "database/logical_snapshot.json" in z.namelist())
            snap = json.loads(z.read("database/logical_snapshot.json"))
        check("快照覆盖多表", len(snap["tables"]) >= 10, str(snap.get("table_names", [])[:5]))
        check("快照含 memories 行", len(snap["tables"]["memories"]["rows"]) >= 1)


def test_mode_matrix():
    print("[test_mode_matrix]")
    tmp = mktmp()
    src, _ = make_source(tmp, extra={"mine/图片.dat": "not-md-not-image"})
    # 用临时隔离库，绝不依赖生产库数据
    isodb = tmp / "matrix.db"
    old = app_db.DB_PATH
    try:
        app_db.DB_PATH = isodb
        app_db.init_db()
    finally:
        app_db.DB_PATH = old
    expected = {
        "source_only": {"wake", "knowledge", "data_jsonl", "agents", "scripts", "docs", "volumes"},
        "source_with_db": {"wake", "knowledge", "data_jsonl", "agents", "scripts", "docs", "volumes", "db_snapshot"},
        "full": {"wake", "knowledge", "data_jsonl", "agents", "scripts", "docs", "volumes", "db_snapshot", "project_meta"},
        "knowledge_tasks": {"knowledge", "db_snapshot"},
    }
    for mode, want in expected.items():
        r = export(mode, src, tmp / ("pkg_" + mode), db_path=str(isodb))
        check(f"{mode} 导出 ok", r["ok"], str(r["errors"][:3]))
        if not r["ok"]:
            continue
        cats = {e["category"] for e in r["manifest"]["file_manifest"]}
        missing = want - cats
        check(f"{mode} 类别齐全", not missing, f"缺少 {missing}")
        if mode == "knowledge_tasks":
            check("kt 不含 AGENTS.md", "source/AGENTS.md" not in [e["path"] for e in r["manifest"]["file_manifest"]])
            # knowledge_tasks 按设计含 tasks 模式 DB 快照（任务阶段数据在 DB 里）
            if "db_snapshot" in cats:
                p = tmp / ("pkg_" + mode) / r["archive_name"]
                with zipfile.ZipFile(p) as z:
                    snap = json.loads(z.read("database/logical_snapshot.json"))
                check("kt DB 快照只含任务相关表", set(snap["tables"]).issubset({"tasks", "snapshots", "events"}),
                      str(sorted(snap["tables"])))
        if mode in ("source_only", "source_with_db", "full"):
            names = [e["path"] for e in r["manifest"]["file_manifest"]]
            check(f"{mode} 含 AGENTS.md", "source/AGENTS.md" in names)
            check(f"{mode} 排除 var/", not any("/var/" in n or n.startswith("source/var/") for n in names))


def list_inspect():
    print("[test_list_inspect]")
    tmp = mktmp()
    src, _ = make_source(tmp)
    r = export("source_only", src, tmp / "pkg")
    check("导出成功供 list", r["ok"])
    lp = pb.list_packages(tmp / "pkg", allow_external_dir=True)
    check("list_packages 找到包", lp["ok"] and lp["count"] >= 1, str(lp))
    if r["ok"]:
        insp = pb.inspect_package(tmp / "pkg" / r["archive_name"], expected_type="source_only")
        check("inspect ok", insp["ok"])
        check("inspect 含 manifest 摘要", insp["manifest"].get("backup_type") == "source_only")


def main():
    tests = [
        test_normal_export, test_empty_source, test_chinese_names, test_large_file_streaming,
        test_zip_corrupt, test_hash_tamper, test_missing_required, test_illegal_paths,
        test_duplicate_export, test_case_conflict, test_zip_bomb_ratio,
        test_unknown_high_version, test_missing_manifest, test_missing_checksums,
        test_sensitive_excluded, test_temp_cleanup_on_failure, test_db_snapshot_isolated,
        test_mode_matrix, list_inspect,
    ]
    for t in tests:
        try:
            t()
        except Exception as exc:
            global FAIL
            FAIL += 1
            print(f"  [FAIL] {t.__name__} 异常：{type(exc).__name__}: {exc}")
    print(f"\n=== {PASS}/{PASS + FAIL} 通过 ===")
    # 清理检查
    residue = [str(p) for p in _TMPS if p.exists()]
    for p in _TMPS:
        shutil.rmtree(p, ignore_errors=True)
    leftover = [str(p) for p in _TMPS if p.exists()]
    print(f"临时目录清理：{len(leftover) == 0}（本次创建 {len(_TMPS)} 个）")
    return 0 if FAIL == 0 and not leftover else 1


if __name__ == "__main__":
    sys.exit(main())
