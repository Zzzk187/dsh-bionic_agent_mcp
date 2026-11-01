"""给时间分档补一个自检用例，防止以后改坏。

覆盖三条容易悄悄坏掉、但坏了不会报错的地方：
1. 分档是相对天数算的 —— "今天/昨天"会随时间推移而移动。如果谁把它改成固定
   日期区间，跨天后列表会整片错位，而单看代码看不出来。
2. 认不出的时间必须计入 unparsed，不能静默归进某一档（那会让"某天 0 条"
   变成谎话）。
3. /api/memories/timeline 必须能被路由到 —— 它和 /api/memories/{memory_id}
   是同一个前缀，声明顺序错了会被当成 id 解析成 int，返回 422 而不是 200。
   这个坑在 /api/tasks/stuck 上真实踩过。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="bionic-timeline-")
os.environ["BIONIC_DB_PATH"] = str(Path(_TMP) / "agent.db")

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def main() -> int:
    from fastapi.testclient import TestClient
    from app import admin_ui, db

    db.init_db()

    def iso(days_ago: float) -> str:
        return (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")

    # 造数据：今天 2 条、昨天 1 条、3 天前 1 条、40 天前 1 条、时间坏掉 1 条
    rows = [
        ("今天的记忆 A", iso(0.1)),
        ("今天的记忆 B", iso(0.2)),
        ("昨天的记忆", iso(1.2)),
        ("三天前的记忆", iso(3.2)),
        ("四十天前的记忆", iso(40.2)),
        # 时钟漂移/时区错会造出"未来"的时间戳。它必须归到今天，而不是从
        # 榜单上凭空消失 —— 消失比放错档更难被发现。
        ("未来时间的记忆", iso(-0.5)),
    ]
    with db.connect() as conn:
        for content, ts in rows:
            conn.execute(
                "INSERT INTO memories(content, category, status, created_at, updated_at) "
                "VALUES (?, 'general', 'active', ?, ?)",
                (content, ts, ts),
            )
        # 故意写一个解析不了的时间
        conn.execute(
            "INSERT INTO memories(content, category, status, created_at, updated_at) "
            "VALUES ('时间坏掉的记忆', 'general', 'active', 'not-a-date', 'not-a-date')"
        )

    client = TestClient(admin_ui.app)
    with client:
        resp = client.get("/api/memories/timeline?axis=created")
        check("timeline 路由能被命中（没被 /{memory_id} 抢先）",
              resp.status_code == 200, f"status={resp.status_code}")
        if resp.status_code != 200:
            return 1
        data = resp.json()["data"]

    buckets = {b["key"]: b for b in data["buckets"]}
    check("今天档 3 条（含未来时间的 1 条）", buckets["today"]["count"] == 3,
          f'今天={buckets["today"]["count"]}')
    check("昨天档 1 条", buckets["yesterday"]["count"] == 1,
          f'昨天={buckets["yesterday"]["count"]}')
    check("近 7 天档 1 条（3 天前那条）", buckets["week"]["count"] == 1,
          f'近7天={buckets["week"]["count"]}')
    check("近 90 天档 1 条（40 天前那条）", buckets["quarter"]["count"] == 1,
          f'近90天={buckets["quarter"]["count"]}')
    check("认不出的时间计入 unparsed 而不是混进某一档",
          data["unparsed"] == 1, f'unparsed={data["unparsed"]}')
    check("total 只统计能解析的", data["total"] == 6, f'total={data["total"]}')

    # by_day：今天 2 条、40 天前 1 条，共 4 个不同日期（今天/昨天/3天前/40天前）
    days = data["by_day"]
    check("by_day 按日期聚合", len(days) == 4, f"days={len(days)}")
    check("by_day 最新在前", days[0]["date"] > days[-1]["date"],
          f'{days[0]["date"]} > {days[-1]["date"]}')
    today_key = datetime.now().strftime("%Y-%m-%d")
    first = next((d for d in days if d["date"] == today_key), None)
    check("今天在 by_day 里是 3 条", first is not None and first["count"] == 3,
          f'{first["count"] if first else "缺"}')

    # 已知不可测（如实记下，不要为了反向验证好看去硬造用例）：
    # 相对档把上界写成严格小于；若被改成小于等于，只有在 days 恰好等于
    # 24 小时整数倍（精确 1.0 / 2.0 / 7.0 天）时行为才有差别。真实数据精确
    # 到秒，落点概率约等于零，构造这种用例只能证明测试会算数，不能证明
    # 代码在真实数据上安全。真正会出问题的是未来时间与解析失败，那两个
    # 已经有用例且反向验证抓得住。

    # 相对档会随时间移动 —— 断言"今天"的边界确实是 24 小时内
    check("今天档只装 24 小时内的（1.2 天前的没被算进今天）",
          buckets["today"]["count"] == 3 and buckets["yesterday"]["count"] == 1,
          f'今天={buckets["today"]["count"]} 昨天={buckets["yesterday"]["count"]}')

    # axis=updated 也必须能用
    with client:
        r2 = client.get("/api/memories/timeline?axis=updated")
    check("axis=updated 可用", r2.status_code == 200, f"status={r2.status_code}")

    failed = [r for r in RESULTS if not r[1]]
    print(f"\n=== {len(RESULTS) - len(failed)}/{len(RESULTS)} 通过 ===")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
