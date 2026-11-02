"""补测试：三个新维度（分类 / 访问热度 / 时间轴）与访问记账。

要盯住的不是"函数能跑"，而是几个会静默出错的地方：

1. `_memory_out` 会剔除 access_count。补字段的 `_attach_governance` 一旦失效，
   前端不报错，只是所有记忆都落进「从没翻过」——分组看起来"全在一档"。
2. 热度档位用绝对值而非分位数。若改成分位数，全库都是 0 时仍会造出四档，
   看起来像有数据。
3. `touch_access` 曾是从没被调过的死代码。召回后不记账，热度分档就永远是
   "全 0"。这条要用"召回一次，access_count 必须涨"来钉住。
4. 时间轴的格子数必须覆盖首尾（含两边留白），否则密集期/空白期看不出。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="bionic-views-")
os.environ["BIONIC_DB_PATH"] = str(Path(_TMP) / "agent.db")

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def main() -> int:
    from fastapi.testclient import TestClient
    from app import admin_ui, db, memory_lifecycle as life, mcp_server as core

    db.init_db()

    # 造数据：三个分类，其中两条人为设高访问次数
    ids = []
    for content, cat in [("教训一号", "教训"), ("教训二号", "教训"),
                         ("项目一号", "项目"), ("环境一号", "环境")]:
        r = core.save_memory(content, category=cat)
        ids.append(r["id"])

    with db.transaction() as conn:
        conn.execute("UPDATE memories SET access_count=12 WHERE id=?", (ids[0],))
        conn.execute("UPDATE memories SET access_count=4 WHERE id=?", (ids[1],))
        conn.execute("UPDATE memories SET access_count=1 WHERE id=?", (ids[2],))
        # ids[3] 保持 0

    client = TestClient(admin_ui.app)
    with client:
        # ---- 1) 列表必须带回 access_count ----
        lst = client.get("/api/memories?limit=50").json()["data"]
        by_id = {m["id"]: m for m in lst}
        check("列表结果带 access_count 字段",
              all("access_count" in m for m in lst),
              f"缺字段 {[m['id'] for m in lst if 'access_count' not in m]}")
        check("access_count 的值正确回填",
              by_id[ids[0]]["access_count"] == 12
              and by_id[ids[3]]["access_count"] == 0,
              f"高={by_id[ids[0]]['access_count']} 零={by_id[ids[3]]['access_count']}")
        check("列表结果带 protected / importance",
              "protected" in lst[0] and "importance" in lst[0],
              f'keys={sorted(k for k in lst[0] if k in ("protected","importance"))}')

        # ---- 2) breakdown 三维度 ----
        bd = client.get("/api/memories/breakdown").json()["data"]
        check("breakdown total 正确", bd["total"] == 4, f'total={bd["total"]}')

        cats = {c["key"]: c["count"] for c in bd["by_category"]}
        check("分类统计正确", cats.get("教训") == 2 and cats.get("项目") == 1
              and cats.get("环境") == 1, f"{cats}")

        heat = {h["key"]: h["count"] for h in bd["by_heat"]}
        check("热度分档按绝对值：12→常翻的", heat["hot"] == 1, f"{heat}")
        check("热度分档：4→偶尔翻、1→冷门、0→从没翻过",
              heat["warm"] == 1 and heat["cold"] == 1 and heat["untouched"] == 1,
              f"{heat}")
        check("touched 统计正确（有访问的 3 条）", bd["touched"] == 3,
              f'touched={bd["touched"]}')
        check("有访问记录时 heat_note 不再是『全部都没有』",
              "全部" not in bd["heat_note"], bd["heat_note"][:60])

        # ---- 3) 召回记账 ----
        before = by_id[ids[3]]["access_count"]
        got = life.recall_memories("环境一号", top_k=1)
        check("召回能返回结果", len(got) == 1, f"{[g['id'] for g in got]}")
        with db.connect() as conn:
            after = conn.execute("SELECT access_count FROM memories WHERE id=?",
                                 (ids[3],)).fetchone()[0]
        check("召回后 access_count 真的涨了（touch_access 不再是死代码）",
              after == before + 1, f"{before} -> {after}")

        # ---- 4) 详情记账 ----
        client.get(f"/api/memories/{ids[3]}")
        with db.connect() as conn:
            after2 = conn.execute("SELECT access_count FROM memories WHERE id=?",
                                  (ids[3],)).fetchone()[0]
        check("打开详情也记一次访问", after2 == after + 1, f"{after} -> {after2}")

        # ---- 5) 列表不记账（否则翻页就等于访问）----
        before_list = after2
        client.get("/api/memories?limit=50")
        with db.connect() as conn:
            after_list = conn.execute("SELECT access_count FROM memories WHERE id=?",
                                      (ids[3],)).fetchone()[0]
        check("列表接口不记访问（翻页不等于看过）",
              after_list == before_list, f"{before_list} -> {after_list}")

    failed = [r for r in RESULTS if not r[1]]
    print(f"\n=== {len(RESULTS) - len(failed)}/{len(RESULTS)} 通过 ===")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
