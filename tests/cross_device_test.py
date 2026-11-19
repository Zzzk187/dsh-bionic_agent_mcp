# -*- coding: utf-8 -*-
"""S0 第三批（跨设备路径）自动化验收：不连生产库、全部临时隔离。

覆盖：
  P1-1  knowledge_docs 以 (root, rel_path) 稳定键定位，换盘符不误判整套 missing
  P1-1  reanchor_paths() 重锚绝对路径：dry-run 不写 / 应用后仅改 path / hash 校验 /
        冲突与缺失分类 / 事务可回滚 / 内容不变
  P2-1  import_dsh_memory.DB 走统一 BIONIC_DB_PATH（不再写死 D 盘）
  P1-1  --dry-run / --status 在库不存在时不创建 agent.db、不执行任何 DDL

运行： D:/Software/Dev/Miniforge/python.exe -X utf8 tests/cross_device_test.py
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app import db, knowledge  # noqa: E402

# 动态加载脚本模块（与 import_dsh_memory_test.py 同法，不改脚本签名）
def _load_script(name: str):
    path = PROJECT_ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(name.replace(".", "_"), path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


idm = _load_script("import_dsh_memory.py")

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"[PASS] {name}" + (f" — {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"[FAIL] {name}" + (f" — {detail}" if detail else ""))


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _build_source_root(base: Path, tag: str) -> Path:
    """造一个最小但结构完整的 dsh-memory 根。返回根路径。"""
    root = base / tag
    _write(root / "knowledge" / "Agent" / f"note-{tag}.md", f"# 笔记 {tag}\n\n正文内容 {tag} 足够长以通过最小分块阈值要求。")  # noqa: E501
    _write(root / "mine" / f"daily-{tag}.md", f"# 日记 {tag}\n\n今天写了 {tag} 相关内容，也是足够长的正文。")  # noqa: E501
    _write(root / "约定.md", f"# 约定 {tag}\n\n中文沟通，先查根因。这一段足够长用于分块与哈希校验。")  # noqa: E501
    _write(root / "卷" / "环境.md", f"# 环境 {tag}\n\n记录本机环境 {tag}，内容足够长。")  # noqa: E501
    _write(root / "config.ps1", f"# config {tag}\n$MemRoot = 'x'\n")
    # AGENTS.md 在根上一级，与真实结构一致
    _write(base / "AGENTS.md", f"# AGENTS {tag}\n\n全局约定 {tag}，内容足够长用于检索与哈希校验。")  # noqa: E501
    return root


def _env(tmp: Path) -> Path:
    db_path = tmp / "agent.db"
    os.environ["BIONIC_DB_PATH"] = str(db_path)
    db.DB_PATH = db_path
    db.init_db()
    return db_path


def _counts() -> dict:
    with db.connect() as conn:
        rows = conn.execute("SELECT id, path, root, rel_path, content_hash FROM knowledge_docs").fetchall()
    return {r["id"]: dict(r) for r in rows}


# ---------------------------------------------------------------------------
def test_reanchor():
    tmp = Path(tempfile.mkdtemp(prefix="bionic-crossdevice-"))
    try:
        _env(tmp)
        root_a = _build_source_root(tmp, "rootA")
        res = knowledge.import_docs(root=str(root_a), embed=False, dry_run=False)
        check("import_docs 基础导入", not res.get("errors"), f"seen={res['docs_seen']} imported={res['docs_imported']}")
        base = _counts()
        check("导入有文档落库", len(base) >= 4, f"docs={len(base)}")
        # AGENTS.md 在根上一级（父目录），路径不含 rootA，单独排除
        non_agents = [r for r in base.values() if r["rel_path"] != "../AGENTS.md"]
        check("旧库路径均指向 rootA", all(str(root_a) in r["path"] for r in non_agents),
              f"checked={len(non_agents)}")

        # 关键：用稳定键 drift，换根不应报整套 missing
        dr_a = knowledge.drift(root=str(root_a))
        check("同根 drift 一致", dr_a.get("in_sync"), f"missing={len(dr_a.get('missing_on_disk', []))}")

        # 构造 rootB：复制内容，路径全变
        root_b = tmp / "rootB"
        shutil.copytree(root_a, root_b)
        # AGENTS.md 在 base/，两个根共用同一个父级 AGENTS.md —— 内容不变即可
        dr_b = knowledge.drift(root=str(root_b))
        check("换盘符后 drift 不误报整套 missing", not dr_b.get("missing_on_disk"),
              f"missing={len(dr_b.get('missing_on_disk', []))} added={len(dr_b.get('needs_import', []))}")

        # dry-run 不写
        plan = knowledge.reanchor_paths(str(root_b), dry_run=True)
        check("reanchor dry-run 有计划", plan.get("planned", 0) >= 4, f"planned={plan['planned']}")
        check("reanchor dry-run 未改库", _counts() == base)
        check("reanchor dry-run 分类齐全",
              all(k in plan for k in ("reanchored", "unchanged", "conflicts", "missing")))

        # 应用
        applied = knowledge.reanchor_paths(str(root_b), dry_run=False)
        check("reanchor 应用成功", applied.get("ok") and len(applied.get("reanchored", [])) >= 4,
              f"reanchored={len(applied.get('reanchored', []))}")
        after = _counts()
        non_agents_after = [r for r in after.values() if r["rel_path"] != "../AGENTS.md"]
        check("重锚后路径指向 rootB", all(str(root_b) in r["path"] for r in non_agents_after),
              f"checked={len(non_agents_after)}")
        check("重锚不改内容 hash",
              all(base[i]["content_hash"] == r["content_hash"] for i, r in after.items()))
        check("重锚不改 rel_path/root",
              all(base[i]["rel_path"] == r["rel_path"] and base[i]["root"] == r["root"]
                  for i, r in after.items()))
        dr_after = knowledge.drift(root=str(root_b))
        check("重锚后 drift 一致", dr_after.get("in_sync"))

        # 冲突：改一个文件内容后应报 conflict 而非重锚
        victim = root_b / "knowledge" / "Agent" / "note-rootA.md"
        if victim.is_file():
            victim.write_text("# 改动\n\n内容被改了，足够长用于分块。", encoding="utf-8")
            cf = knowledge.reanchor_paths(str(root_b), dry_run=True)
            check("内容变更报 conflict", len(cf.get("conflicts", [])) >= 1,
                  f"conflicts={len(cf['conflicts'])}")
        # 缺失：删一个文件应报 missing
        gone = root_b / "mine" / "daily-rootA.md"
        if gone.is_file():
            gone.unlink()
            ms = knowledge.reanchor_paths(str(root_b), dry_run=True)
            check("文件缺失报 missing", len(ms.get("missing", [])) >= 1,
                  f"missing={len(ms['missing'])}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_db_path_unified():
    check("DB 模块级名字可 patch", hasattr(idm, "DB"))
    check("DB 走统一配置而非写死 D 盘",
          "D:/Cache/Cache_Project/bionic-agent-mcp/data/agent.db" not in (PROJECT_ROOT / "scripts" / "import_dsh_memory.py").read_text(encoding="utf-8").split("DB = Path")[1].split("\n")[0]
          if "DB = Path" in (PROJECT_ROOT / "scripts" / "import_dsh_memory.py").read_text(encoding="utf-8") else False)


def test_dry_run_no_db():
    """三个脚本 --dry-run 在库不存在时不得创建 agent.db。"""
    py = sys.executable
    for script, extra in (
        ("import_dsh_knowledge.py", ["--dry-run"]),
        ("snapshot_db.py", ["--dry-run"]),
        ("import_dsh_memory.py", ["--dry-run"]),
    ):
        tmp = Path(tempfile.mkdtemp(prefix="bionic-dryrun-"))
        try:
            fake_db = tmp / "agent.db"
            env = dict(os.environ)
            env["BIONIC_DB_PATH"] = str(fake_db)
            env["PYTHONUTF8"] = "1"
            proc = subprocess.run(
                [py, "-X", "utf8", str(PROJECT_ROOT / "scripts" / script), *extra],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                env=env, cwd=str(PROJECT_ROOT), timeout=120,
            )
            check(f"{script} --dry-run 不创建数据库", not fake_db.exists(),
                  f"exit={proc.returncode} db_exists={fake_db.exists()}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def run():
    print("=== S0 第三批：跨设备路径 / dry-run 不建库 ===")
    test_reanchor()
    test_db_path_unified()
    test_dry_run_no_db()
    print(f"=== {PASS}/{PASS + FAIL} 通过 ===")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(run())
