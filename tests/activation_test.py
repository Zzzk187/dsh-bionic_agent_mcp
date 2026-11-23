"""激活扩散的种子来源测试。

这个模块一直是"能算但没人喂"：`seed_activation_from_row` 写得很完整，却
从没被 `compute_activation` 调用过——所以 `access_count` 只影响自己那一条的
frequency 项，永远不会让它的**邻居**更相关，关系图只能靠人工 `record_activation`
才能进得去。

要钉住的三件事：
1. 有访问痕迹的记忆必须是种子（否则这条通路又退化成死代码）。
2. usage 种子必须**弱于**显式激活与查询命中，否则"被翻过几次"会盖过
   "用户刚问的就是它"。
3. 关掉 usage 种子必须回到原行为（可回退、可对照）。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
import _env_guard  # noqa: E402,F401  OPT-04a：数据库隔离闸门（必须在任何 app.* 之前）

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="bionic-activation-")
os.environ["BIONIC_DB_PATH"] = str(Path(_TMP) / "agent.db")

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def main() -> int:
    from app import db, activation_spreading as sp
    from app import memory_lifecycle as life

    db.init_db()
    ids = []
    for content in ["记忆体系是五层结构", "记忆体系的分区规则", "记忆体系的写入流程"]:
        with db.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO memories(content,category,status,memory_status,"
                "importance_score,confidence_score,freshness_score,access_count)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (content, "项目", "active", "active", 50, 50, 50, 0))
            ids.append(cur.lastrowid)

    # 图：ids[0] --related_to--> ids[1] --related_to--> ids[2]
    with db.transaction() as conn:
        for a, b in [(ids[0], ids[1]), (ids[1], ids[2])]:
            conn.execute(
                "INSERT INTO memory_links(source_memory_id,target_memory_id,relation,note)"
                " VALUES(?,?,?,?)",
                (a, b, "related_to", "测试"))

    # ---- 1) 全零访问时没有任何种子 ----
    r0 = sp.compute_activation()
    check("没有访问痕迹时不产生种子", r0["seeds"] == 0, f'seeds={r0["seeds"]}')

    # ---- 2) 给 id[0] 访问痕迹后，它成为种子 ----
    with db.transaction() as conn:
        conn.execute("UPDATE memories SET access_count=10,"
                     " last_accessed_at=datetime('now') WHERE id=?", (ids[0],))
    r1 = sp.compute_activation()
    check("有访问痕迹的记忆成为种子", r1["seeds"] >= 1, f'seeds={r1["seeds"]}')
    act = r1["activation"]
    check("种子自身有激活值", act.get(ids[0], {}).get("activation", 0) > 0,
          str(act.get(ids[0], {}).get("activation")))
    check("一跳邻居拿到衰减后的激活",
          act.get(ids[1], {}).get("activation", 0) > 0,
          str(act.get(ids[1], {}).get("activation")))
    check("一跳 < 种子（确实在衰减）",
          act.get(ids[1], {}).get("activation", 0) < act.get(ids[0], {}).get("activation", 0),
          f'{act.get(ids[1], {}).get("activation")} < {act.get(ids[0], {}).get("activation")}')
    check("两跳邻居也拿到了（深度 2 生效）",
          act.get(ids[2], {}).get("activation", 0) > 0,
          str(act.get(ids[2], {}).get("activation")))

    # ---- 3) 关掉 usage 种子回到原行为 ----
    r_off = sp.compute_activation(include_usage_seeds=False)
    check("关掉 usage 种子后不再有种子", r_off["seeds"] == 0, f'seeds={r_off["seeds"]}')
    check("关掉后激活表为空", not r_off["activation"], str(len(r_off["activation"])))

    # ---- 4) usage 种子必须弱于显式激活与查询命中 ----
    usage_w = sp.seed_activation_from_row(
        {"access_count": 10, "last_accessed_at": None, "updated_at": None})
    effective = usage_w * sp.USAGE_SEED_WEIGHT
    check("usage 种子权重小于 1（弱于显式激活）", 0 < effective < 1.0,
          f'{usage_w} * {sp.USAGE_SEED_WEIGHT} = {effective}')
    r_explicit = sp.compute_activation(memory_ids=[ids[2]])
    check("查询命中仍是满强度 1.0",
          r_explicit["activation"][ids[2]]["activation"] == 1.0,
          str(r_explicit["activation"][ids[2]]["activation"]))
    check("查询命中不会被 usage 种子降低",
          r_explicit["activation"][ids[2]]["activation"] > effective,
          f'{r_explicit["activation"][ids[2]]["activation"]} > {effective}')

    # ---- 5) 显式激活事件也进种子（原有通路不能坏）----
    with db.transaction() as conn:
        conn.execute("UPDATE memories SET access_count=0, last_accessed_at=NULL")
    sp.record_activation(ids[1], reason="用户说这条又相关了")
    r_event = sp.compute_activation()
    check("显式激活事件能单独当种子", ids[1] in r_event["activation"],
          f'seeds={r_event["seeds"]}')
    check("显式激活是满强度",
          r_event["activation"][ids[1]]["activation"] == 1.0,
          str(r_event["activation"][ids[1]]["activation"]))

    # ---- 6) 查询一条都没命中时，usage 仍要能扩散 ----
    # 这一条是为了钉住一个具体回归：`recall_memories` 原来写
    # `spread = activation_for_seed_query(c, seeds) if seeds else {}`，
    # 没有种子就直接短路，于是 usage 播种在「模糊查询」——恰恰最需要它的场合
    # ——永远不生效，只有强命中时才走得到。代码看着在、实际是死路。
    with db.transaction() as conn:
        conn.execute("UPDATE memories SET access_count=0, last_accessed_at=NULL")
        # 上一步留下的显式激活事件也会当种子，会让"弱强度"断言看错对象，
        # 这里一并清掉，只留 usage 这一条通路。
        conn.execute("DELETE FROM events WHERE event_type='memory.activated'")
        conn.execute("UPDATE memories SET access_count=7,"
                     " last_accessed_at=datetime('now') WHERE id=?", (ids[0],))
    vague = life.recall_memories("完全不相干的一句话", top_k=5, explain=True)
    boosted = [r for r in vague
               if (r.get("_activation") or {}).get("amount", 0) > 0]
    check("查询零命中时 usage 种子仍会扩散", bool(boosted),
          f"{len(boosted)}/{len(vague)} 条拿到激活")
    check("零命中时拿到的激活是弱强度（不是满 1.0）",
          all((r.get("_activation") or {}).get("amount", 0) < 1.0 for r in boosted),
          str([round((r.get("_activation") or {}).get("amount", 0), 4) for r in boosted]))

    # ---- 7) 纯读：扩散不得改变任何记忆字段 ----
    with db.connect() as conn:
        before = [tuple(r) for r in conn.execute(
            "SELECT id, importance_score, status, memory_status, access_count"
            " FROM memories ORDER BY id").fetchall()]
    sp.compute_activation(memory_ids=[ids[0]])
    with db.connect() as conn:
        after = [tuple(r) for r in conn.execute(
            "SELECT id, importance_score, status, memory_status, access_count"
            " FROM memories ORDER BY id").fetchall()]
    check("扩散是纯读（记忆字段一个都没动）", before == after,
          "字段有变化" if before != after else f"{len(before)} 条记忆均未改变")

    failed = [n for n, ok, _ in RESULTS if not ok]
    print()
    if failed:
        print(f"FAIL={len(failed)}")
        return 1
    print(f"全部通过（{len(RESULTS)} 项）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
