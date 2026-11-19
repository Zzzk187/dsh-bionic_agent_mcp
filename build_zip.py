"""Build the distributable ZIP (runnable files only).

Excludes the user's private live database (data/agent.db, data/backups/*) and
Python caches. A fresh deployment creates or upgrades its own database on first
launch: `app/db.py` runs the idempotent migration itself (no separate migrate
script is shipped).
"""
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "dist" / "bionic-agent-mcp.zip"

INCLUDE_DIRS = ["app", "dsh-panel-client", "scripts"]
INCLUDE_FILES = [
    "requirements.txt",
    "pyproject.toml",
    "README.md",
    ".env.example",
    "deepseek-harness-mcp.example.yml",
]
EXCLUDE_SUFFIXES = (".pyc", ".pyo", ".db", ".db-wal", ".db-shm")
EXCLUDE_DIRS = {"__pycache__", "backups", ".git", "node_modules", "dev", "dist"}
# [S0/P1-4] scripts/ 只收运维必需脚本；eval_*/一次性调试脚本不入发行包。
SCRIPTS_ALLOW = {
    "import_dsh_memory.py",
    "import_dsh_knowledge.py",
    "sync_all.py",
    "snapshot_db.py",
    "check_mirror.py",
    "recall.py",
    "physical_backup.py",
}


def _excluded(p: Path) -> bool:
    """[S0/P1-4] 复合后缀也要排除。

    旧逻辑只比 ``p.suffix in EXCLUDE_SUFFIXES``，而 ``db.py.bak-20261102-phase7``
    的 suffix 是 ``.bak-20261102-phase7``，10 个历史源码备份会被打进发行包。
    """
    if p.suffix in EXCLUDE_SUFFIXES:
        return True
    name = p.name.lower()
    if ".bak" in name or name.endswith((".tmp", ".part", ".swp")):
        return True
    if name.startswith("_") or name.startswith("eval_"):
        return True
    return False


def iter_files():
    seen = set()
    for d in INCLUDE_DIRS:
        base = ROOT / d
        if not base.exists():
            continue
        for p in base.rglob("*"):
            if any(part in EXCLUDE_DIRS for part in p.relative_to(ROOT).parts):
                continue
            if p.is_dir():
                continue
            if _excluded(p):
                continue
            # scripts/ 白名单：只发运维入口，不带评测/调试脚本
            if d == "scripts" and p.name not in SCRIPTS_ALLOW:
                continue
            seen.add(p)
    for f in INCLUDE_FILES:
        p = ROOT / f
        if p.exists():
            seen.add(p)
    return sorted(seen)


def main():
    files = iter_files()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as z:
        for p in files:
            arcname = p.relative_to(ROOT).as_posix()
            z.write(p, arcname)
            print("  +", arcname)
    size = OUT.stat().st_size
    print(f"\nWrote {OUT.name} ({size:,} bytes, {len(files)} files)")


if __name__ == "__main__":
    main()
