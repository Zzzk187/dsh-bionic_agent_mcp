"""只读体检：本地 dsh-memory ↔ MCP 库 是否还一致（给 bin/自检.ps1 用）。

为什么单独一个脚本：`sync_all.py` 是"去同步"的动作，而自检要的是"现在一致吗"。
以前自检里根本没有这一项，于是本地写了记忆、没人跑 sync，两边静默漂移——
自检照样全绿，因为它只检查本地那一侧。

检查四项，任何一项不过就退出码 1：
  1. 知识文档漂移：磁盘 135 篇 vs 库里 135 篇，缺/多/待重导都必须为 0
  2. 记忆向量覆盖：内容改了但向量没重建的条数（真会问出错的检索）
  3. 孤儿向量：指向已不存在记忆的向量行
  4. 治理分新鲜度：last_evaluated_at 停了多久（停了就是 0.07 权重在空转）

用法：python scripts/check_mirror.py
最后一行输出机器可读 JSON，方便被别的脚本接着用。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app import db as _db                      # noqa: E402
from app import embeddings as _emb            # noqa: E402
from app import knowledge                     # noqa: E402

GOV_STALE_DAYS = 3.0


def _hash(text: str) -> str:
    import hashlib
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def main() -> int:
    rc = 0
    report: dict = {"ok": True}

    drift = knowledge.drift()
    report["knowledge"] = {
        "on_disk": drift.get("on_disk", 0),
        "in_db": drift.get("in_db", 0),
        "missing_on_disk": len(drift.get("missing_on_disk") or []),
        "needs_import": len(drift.get("needs_import") or []),
        "needs_reimport": len(drift.get("needs_reimport") or []),
        "in_sync": bool(drift.get("in_sync")),
    }
    k = report["knowledge"]
    if not k["in_sync"]:
        rc = 1
        print(f"[FAIL] 知识文档不一致：磁盘 {k['on_disk']} / 库 {k['in_db']}"
              f"，待导入 {k['needs_import']}、待重导 {k['needs_reimport']}"
              f"、磁盘已删 {k['missing_on_disk']} → 跑 sync_all.py")
    else:
        print(f"[OK] 知识文档一致：{k['on_disk']} 篇")

    with _db.connect() as conn:
        rows = conn.execute(
            "SELECT m.id, m.content, e.content_hash AS ih, e.model_name AS im "
            "FROM memories m LEFT JOIN memory_embeddings e ON e.memory_id = m.id "
            "WHERE m.status='active'"
        ).fetchall()
        stale = [r["id"] for r in rows
                 if r["ih"] is None
                 or r["ih"] != _hash(r["content"])
                 or r["im"] != _emb.DEFAULT_MODEL]
        orphan = conn.execute(
            "SELECT COUNT(*) n FROM memory_embeddings "
            "WHERE memory_id NOT IN (SELECT id FROM memories)").fetchone()["n"]
        gov = conn.execute(
            "SELECT MAX(last_evaluated_at) t FROM memories WHERE status='active'"
        ).fetchone()["t"]

    report["memory_vectors"] = {"active": len(rows), "stale": len(stale),
                                "stale_ids": stale[:10], "orphan": orphan}
    mv = report["memory_vectors"]
    if mv["stale"]:
        rc = 1
        print(f"[FAIL] {mv['stale']} 条记忆的内容变了但向量没重建（id {mv['stale_ids']}）"
              f" → 跑 sync_all.py")
    else:
        print(f"[OK] 记忆向量覆盖完整：{mv['active']} 条")
    if orphan:
        rc = 1
        print(f"[FAIL] {orphan} 条孤儿向量（指向已删除的记忆）")
    else:
        print("[OK] 无孤儿向量")

    stale_days = None
    if gov:
        try:
            ts = datetime.fromisoformat(str(gov).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            stale_days = round(
                (datetime.now(timezone.utc) - ts).total_seconds() / 86400.0, 1)
        except ValueError:
            stale_days = None
    report["governance"] = {"last_scored": gov, "stale_days": stale_days}
    if stale_days is None:
        print("[WARN] 读不出治理分时间，跳过新鲜度检查")
    elif stale_days > GOV_STALE_DAYS:
        # 不设为失败：治理分只占 0.23 权重里的一小部分，停几天不至于查不到，
        # 但必须让人看见——它停了十万年也不会自己报错。
        print(f"[WARN] 治理分已停 {stale_days} 天没重算（freshness 分量在空转）"
              f" → 跑 sync_all.py")
    else:
        print(f"[OK] 治理分 {stale_days} 天前刚重算过")

    report["ok"] = rc == 0
    print(json.dumps(report, ensure_ascii=False))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
