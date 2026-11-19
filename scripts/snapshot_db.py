"""给 Bionic Agent 库拍一张整库快照（命令行入口，供计划任务调用）。

为什么要这个入口
----------------
库快照原本只能从 MCP 工具 `create_db_snapshot` 触发，而那是"人主动拍"的动作。
本地记忆有 `bin/备份.ps1` 每天 05:30 自动跑，库这边却没有任何自动保护——真出事
（批量归档、误跑治理、dedupe 过头）时手上可能只有一张几天前的快照。这个脚本让
两边能挂同一个调度。

[S6-FIX-02] 本脚本现在同时做两件事（同一个调度、同一个入口）：
  1. 逻辑快照 —— `data/snapshots/snap-*.json`，逐表 dump 数据行（原行为，未变）；
  2. 物理备份 —— `data/backups/agent_phys_*.db`，SQLite Backup API 出的整库镜像。
为什么要两件：逻辑 dump 能还原「表里的数据」，但 DB-only memories（id=4/5 这类
库内独有内容）、治理记分、冲突裁决这些只活在库文件里的东西，缺真镜像就只能
靠 dump 恰好完整。两个产物互不包含，各补一半。物理备份默认开启，用
`--no-physical` 关掉即回到旧行为。

用法：
    python scripts/snapshot_db.py               # 快照 + 物理备份，各保留默认份数
    python scripts/snapshot_db.py --keep 30     # 自定义快照保留份数
    python scripts/snapshot_db.py --phys-keep 14  # 自定义物理备份保留份数
    python scripts/snapshot_db.py --no-physical  # 只拍逻辑快照（旧行为）
    python scripts/snapshot_db.py --dry-run     # 只报会做什么，不落盘
    python scripts/snapshot_db.py --label xxx   # 自定义标签

退出码：0 = 两项都成功（且清理成功）；1 = 任一项失败（失败原因逐项打印，
不会因为一项失败就跳过另一项）。
输出最后一行是机器可读 JSON，供计划任务/上层脚本解析。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app import snapshots  # noqa: E402
from app import db_backup  # noqa: E402


def _run_physical(phys_keep: int, dry_run: bool) -> dict:
    """跑物理备份并打印结论。任何异常都收敛成 {"ok": False, ...}。

    独立成函数是为了让「物理备份炸了」不连累逻辑快照——两者失败域分开，
    上层按各自结果分别决定打印什么、最终退出码是多少。
    """
    try:
        res = db_backup.create_physical_backup(keep=phys_keep, dry_run=dry_run)
    except Exception as exc:                      # noqa: BLE001 - 守住调度不崩
        print(f"[FAIL] 物理备份异常：{exc}")
        return {"ok": False, "message": str(exc)}

    if dry_run:
        print(f"[dry-run] 物理备份将写入 {res.get('would_write')}")
        print(f"[dry-run] 现有 {res.get('existing', 0)} 份物理备份，"
              f"保留最近 {phys_keep} 份")
        for name in res.get("would_remove") or []:
            print(f"[dry-run] 将清理 {name}")
        return res

    if not res.get("ok"):
        print(f"[FAIL] 物理备份失败：{res.get('message')}")
        ver = res.get("verify")
        if ver:
            print(f"       校验：{ver.get('message')}")
        return res

    mb = round(res["bytes"] / 1024 / 1024, 2)
    print(f"[OK] 物理备份 {mb}MB · sha256 {res['sha256'][:12]}")
    print(f"     {res['path']}")
    print(f"[OK] 物理备份校验通过：integrity=ok、foreign_key 违规 0"
          f"（mode={res['verified']['mode']}）")
    if res.get("pruned"):
        print(f"[OK] 清理 {len(res['pruned'])} 份旧物理备份，保留 {res['kept']} 份")
    else:
        print(f"[OK] 无需清理，保留 {res['kept']} 份")
    for item in res.get("skipped") or []:
        print(f"     [跳过] {item['name']}（{item['reason']}）")
    if res.get("prune_error"):
        print(f"[WARN] 清理出错（备份本身有效）：{res['prune_error']}")
    return res


def main(
    keep: int = 14,
    label: str = "",
    dry_run: bool = False,
    physical: bool = True,
    phys_keep: int = db_backup.DEFAULT_KEEP,
) -> int:
    if dry_run:
        from app import db as _db
        # [S0/P2-1] dry-run 只读：库或元数据表不存在时按 0 张报告，
        # 绝不调用 _ensure_meta_table / 任何 DDL，也不凭空创建 agent.db。
        rows = []
        if Path(_db.DB_PATH).is_file():
            with __import__("app.db", fromlist=["db"]).connect() as conn:
                has = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='db_snapshots'"
                ).fetchone()
                if has:
                    rows = conn.execute(
                        "SELECT id,label,created_at,total_rows FROM db_snapshots ORDER BY id DESC"
                    ).fetchall()
        print(f"[dry-run] 现有 {len(rows)} 张快照，拍完保留最近 {keep} 张")
        for r in rows:
            print(f"  #{r[0]} {r[2]} {r[1]} rows={r[3]}")
        phys = _run_physical(phys_keep, dry_run=True) if physical else {"ok": True, "skipped": True}
        print(json.dumps({"ok": True, "dry_run": True, "existing": len(rows),
                          "keep": keep,
                          "physical": phys.get("ok", False),
                          "phys_keep": phys_keep if physical else None},
                         ensure_ascii=False))
        return 0

    res = snapshots.create_snapshot(
        label=label or "daily-auto",
        kind="auto",
        actor="scheduled-task",
        note=f"由 scripts/snapshot_db.py 自动拍摄，保留最近 {keep} 张",
    )
    logic_ok = bool(res.get("ok"))
    if not logic_ok:
        print(f"[FAIL] 拍摄失败：{res.get('message')}")
    else:
        print(f"[OK] 逻辑快照 #{res['snapshot_id']} "
              f"{round(res['bytes'] / 1024 / 1024, 2)}MB · {res['total_rows']} 行 · "
              f"sha256 {res['sha256'][:12]}")
        print(f"     {res['path']}")
        counts = res.get("table_counts") or {}
        top = sorted(counts.items(), key=lambda kv: -kv[1])[:6]
        print("     表：" + " / ".join(f"{k} {v}" for k, v in top))

        pr = snapshots.prune_snapshots(keep=keep)
        if pr.get("ok"):
            removed = pr.get("removed") or []
            msg = (f"清理 {len(removed)} 张旧快照（保留 {pr.get('kept')}）" if removed
                   else f"无需清理（保留 {pr.get('kept')}）")
            print(f"[OK] {msg}")
        else:
            print(f"[WARN] 清理失败：{pr.get('message')}")

    # [S6-FIX-02] 物理备份在逻辑快照之后、且**不受其成败影响**：
    # 逻辑 dump 失败往往正是因为库有异常，这种时候恰恰更想要一份真镜像。
    phys = _run_physical(phys_keep, dry_run=False) if physical else {"ok": True, "skipped": True}

    both_ok = logic_ok and bool(phys.get("ok"))
    if logic_ok:
        print(json.dumps({"ok": both_ok, "snapshot_id": res["snapshot_id"],
                          "bytes": res["bytes"], "total_rows": res["total_rows"],
                          "sha256": res["sha256"], "path": res["path"],
                          "pruned": (pr.get("removed") or []),
                          "kept": pr.get("kept"),
                          "physical_ok": phys.get("ok"),
                          "physical_path": phys.get("path"),
                          "physical_sha256": phys.get("sha256"),
                          "physical_pruned": phys.get("pruned") or [],
                          "physical_kept": phys.get("kept")},
                         ensure_ascii=False))
    else:
        print(json.dumps({"ok": False, "message": res.get("message"),
                          "physical_ok": phys.get("ok"),
                          "physical_path": phys.get("path")},
                         ensure_ascii=False))
    return 0 if both_ok else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Bionic Agent 库整库快照 + 物理备份")
    ap.add_argument("--keep", type=int, default=14, help="逻辑快照保留最近多少张（默认 14）")
    ap.add_argument("--label", default="", help="快照标签")
    ap.add_argument("--dry-run", action="store_true", help="只报告，不落盘")
    ap.add_argument("--no-physical", action="store_true",
                    help="跳过物理备份（回到 S6-FIX-02 之前的旧行为）")
    ap.add_argument("--phys-keep", type=int, default=db_backup.DEFAULT_KEEP,
                    help=f"物理备份保留最近多少份（默认 {db_backup.DEFAULT_KEEP}）")
    a = ap.parse_args()
    sys.exit(main(keep=a.keep, label=a.label, dry_run=a.dry_run,
                  physical=not a.no_physical, phys_keep=a.phys_keep))
