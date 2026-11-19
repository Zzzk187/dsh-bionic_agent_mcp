"""给 agent.db 拍一张 SQLite **物理**备份（命令行入口）。

与 `scripts/snapshot_db.py` 的区别
---------------------------------
| | 产物 | 能还原什么 |
|---|---|---|
| `snapshot_db.py` | `data/snapshots/snap-*.json`（逻辑 dump） | 表里的**数据行** |
| 本脚本 | `data/backups/agent_phys_*.db`（SQLite 镜像） | 整个库文件，含 DB-only memories、治理记分、冲突裁决 |

两者互补，计划任务里两个都跑（`snapshot_db.py` 已内置调用本模块的物理备份，
所以定时任务本身不需要改）。

用法：
    python scripts/physical_backup.py                  # 拍一张，保留最近 7 份
    python scripts/physical_backup.py --keep 14        # 自定义保留份数
    python scripts/physical_backup.py --dry-run        # 只报会做什么，不落盘
    python scripts/physical_backup.py --list           # 只看现有备份
    python scripts/physical_backup.py --verify         # 校验现有备份，不新建
    python scripts/physical_backup.py --prune-only     # 只按保留策略清理

退出码：0 = 成功；1 = 失败。最后一行是机器可读 JSON，供计划任务解析。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app import db_backup, db  # noqa: E402


def _print_inventory() -> None:
    base = db_backup.backup_dir()
    items = db_backup.list_backups(base)
    print(f"[INFO] 库：{db.DB_PATH}")
    print(f"[INFO] 备份目录：{base}")
    if not items:
        print("[INFO] 还没有本工具生成的物理备份")
        return
    print(f"[INFO] 现有 {len(items)} 份（新→旧）：")
    for p in items:
        print(f"  {p.name}  {p.stat().st_size:>10} 字节  {p.stat().st_mtime_ns}")


def main(
    keep: int = db_backup.DEFAULT_KEEP,
    dry_run: bool = False,
    list_only: bool = False,
    verify_only: bool = False,
    prune_only: bool = False,
) -> int:
    if list_only:
        _print_inventory()
        print(json.dumps({"ok": True, "action": "list",
                          "count": len(db_backup.list_backups()),
                          "backup_dir": str(db_backup.backup_dir())},
                         ensure_ascii=False))
        return 0

    if verify_only:
        items = db_backup.list_backups()
        ok_n = bad_n = 0
        for p in items:
            v = db_backup.verify_backup(p)
            mark = "OK  " if v["ok"] else "FAIL"
            print(f"[{mark}] {p.name}  {v['size']} 字节  {v['message']}")
            ok_n += 1 if v["ok"] else 0
            bad_n += 0 if v["ok"] else 1
        print(f"[{'PASS' if bad_n == 0 else 'FAIL'}] 校验 {ok_n + bad_n} 份，"
              f"通过 {ok_n}，失败 {bad_n}")
        print(json.dumps({"ok": bad_n == 0, "action": "verify",
                          "total": ok_n + bad_n, "passed": ok_n, "failed": bad_n},
                         ensure_ascii=False))
        return 0 if bad_n == 0 else 1

    if prune_only:
        try:
            pr = db_backup.prune_backups(keep=keep, dry_run=dry_run)
        except db_backup.BackupError as exc:
            print(f"[FAIL] 清理失败：{exc}")
            print(json.dumps({"ok": False, "action": "prune", "message": str(exc)},
                             ensure_ascii=False))
            return 1
        for name in pr["removed"]:
            print(f"  {'[dry-run] 将删除' if dry_run else '已删除'} {name}")
        for item in pr["skipped"]:
            print(f"  [跳过] {item['name']}（{item['reason']}）")
        print(f"[OK] {pr['message']}")
        print(json.dumps({"ok": True, "action": "prune", **pr}, ensure_ascii=False))
        return 0

    res = db_backup.create_physical_backup(keep=keep, dry_run=dry_run)

    if dry_run:
        print(f"[dry-run] 源库 {res['source']}")
        print(f"[dry-run] 备份目录 {res['backup_dir']}")
        print(f"[dry-run] 将写入 {res['would_write']}")
        print(f"[dry-run] 现有 {res['existing']} 份，备份后保留最近 {res['keep']} 份")
        for name in res["would_remove"]:
            print(f"[dry-run] 将清理 {name}")
        print(json.dumps(res, ensure_ascii=False))
        return 0

    if not res.get("ok"):
        print(f"[FAIL] 物理备份失败：{res.get('message')}")
        ver = res.get("verify")
        if ver:
            print(f"       校验结果：{ver.get('message')}")
        print(json.dumps(res, ensure_ascii=False))
        return 1

    mb = round(res["bytes"] / 1024 / 1024, 2)
    print(f"[OK] 物理备份 {mb}MB · sha256 {res['sha256'][:12]}")
    print(f"     {res['path']}")
    print(f"[OK] 校验通过：integrity=ok、foreign_key 违规 0（mode={res['verified']['mode']}）")
    if res.get("pruned"):
        print(f"[OK] 清理 {len(res['pruned'])} 份旧备份，保留 {res['kept']} 份")
    else:
        print(f"[OK] 无需清理，保留 {res['kept']} 份")
    for item in res.get("skipped") or []:
        print(f"     [跳过] {item['name']}（{item['reason']}）")
    if res.get("prune_error"):
        print(f"[WARN] 清理出错（备份本身有效）：{res['prune_error']}")

    print(json.dumps({"ok": True, "path": res["path"], "bytes": res["bytes"],
                      "sha256": res["sha256"], "kept": res["kept"],
                      "pruned": res.get("pruned") or []}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Bionic Agent 数据库物理备份（SQLite Backup API）")
    ap.add_argument("--keep", type=int, default=db_backup.DEFAULT_KEEP,
                    help=f"保留最近多少份（默认 {db_backup.DEFAULT_KEEP}）")
    ap.add_argument("--dry-run", action="store_true", help="只报告，不落盘")
    ap.add_argument("--list", action="store_true", help="只列出现有备份")
    ap.add_argument("--verify", action="store_true", help="只校验现有备份")
    ap.add_argument("--prune-only", action="store_true", help="只按保留策略清理")
    a = ap.parse_args()
    if sum([a.list, a.verify, a.prune_only]) > 1:
        ap.error("--list / --verify / --prune-only 只能选一个")
    sys.exit(main(keep=a.keep, dry_run=a.dry_run, list_only=a.list,
                  verify_only=a.verify, prune_only=a.prune_only))
