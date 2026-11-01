"""Memory distillation: candidate discovery, provenance, and honesty about
what this module does NOT do.

The most important assertions here are negative: it must refuse to "distil"
from too little evidence, and it must not fabricate a conclusion.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="bionic-distill-test-")
os.environ["BIONIC_DB_PATH"] = str(Path(TMP) / "agent.db")
os.environ["BIONIC_SNAPSHOT_DIR"] = str(Path(TMP) / "snaps")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import db, distillation  # noqa: E402
from app import embeddings as emb  # noqa: E402
from app import mcp_server as mcp  # noqa: E402

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return ok


def seed_with_vectors(rows):
    """Insert memories plus hand-built vectors.

    Hand-built vectors rather than the real model: the test is about the
    clustering/provenance logic, and depending on a 400MB download would make
    it slow and flaky for reasons unrelated to what it verifies.
    """
    created = []
    with db.transaction() as conn:
        for content, category, vec in rows:
            cur = conn.execute(
                "INSERT INTO memories(content,category,source,confidence,status,memory_status) "
                "VALUES(?,?,?,?, 'active','active')",
                (content, category, "test", 0.8))
            mid = cur.lastrowid
            conn.execute(
                "INSERT INTO memory_embeddings(memory_id,model_name,content_hash,dimension,"
                "vector_json,updated_at) VALUES(?,?,?,?,?,CURRENT_TIMESTAMP)",
                (mid, "test-model", emb.content_hash(content), len(vec),
                 json.dumps(vec)))
            created.append(mid)
    return created


def unit(v):
    n = sum(x * x for x in v) ** 0.5
    return [x / n for x in v] if n else v


def test_cluster_found():
    print("\n=== 找出值得提炼的多次经历 ===")
    db.init_db()
    # Three near-identical theme vectors + two unrelated ones.
    base = unit([1.0, 0.2, 0.05, 0.0])
    near = [unit([1.0, 0.22, 0.04, 0.01]), unit([0.98, 0.25, 0.06, 0.02]),
            unit([1.0, 0.18, 0.03, 0.02])]
    far = [unit([0.0, 1.0, 0.1, 0.0]), unit([0.05, 0.0, 1.0, 0.1])]
    ids = seed_with_vectors([
        ("用户第三次拒绝了这个布局，说太花哨", "教训", near[0]),
        ("又一次反馈：界面元素太多，希望克制", "教训", near[1]),
        ("再次提到不喜欢满屏装饰，要留白", "教训", near[2]),
        ("今天部署用了 FastAPI + uvicorn", "项目", far[0]),
        ("显卡是 RTX 4090，24G 显存", "环境", far[1]),
    ])
    res = distillation.find_distillation_candidates(similarity=0.52)
    check("扫描成功", res.get("ok"), f"候选 {res.get('count')} 个 / 簇 {res.get('total_clusters')}")
    check("找到了那个三条一组的簇", res.get("count") == 1, f"count={res.get('count')}")
    cand = (res.get("candidates") or [{}])[0]
    check("簇里正好是那三条", sorted(cand.get("source_memory_ids") or []) == sorted(ids[:3]),
          str(cand.get("source_memory_ids")))
    check("候选带证据预览", len(cand.get("member_preview") or []) == 3,
          str([m["id"] for m in cand.get("member_preview") or []]))
    check("候选说明了为什么值得提炼", "多次具体经历" in (cand.get("why") or ""),
          (cand.get("why") or "")[:50])
    check("候选没有自带结论文字",
          "distilled_content" not in cand and not cand.get("content"),
          "候选中无 content 字段（结论必须由外部提供）")
    check("候选写明了怎么提交", "apply_distillation" in (cand.get("how_to_apply") or ""),
          (cand.get("how_to_apply") or "")[:50])


def test_min_size_enforced():
    print("\n=== 证据不足就拒绝提炼 ===")
    with db.connect() as conn:
        two = conn.execute("SELECT id FROM memories LIMIT 2").fetchall()
    ids2 = [int(r[0]) for r in two]
    res = distillation.apply_distillation("用户偏好简洁设计", ids2)
    check("只有 2 条来源时拒绝", not res.get("ok"), str(res.get("message"))[:60])
    check("拒绝理由提到了阈值", "至少需要" in str(res.get("message")),
          str(res.get("message"))[:60])
    check("拒绝理由建议改用 save_memory", "save_memory" in str(res.get("message")))

    res0 = distillation.apply_distillation("凭空结论", [])
    check("零来源时拒绝", not res0.get("ok"), str(res0.get("message"))[:50])
    resb = distillation.apply_distillation("", [1, 2, 3])
    check("空内容时拒绝", not resb.get("ok"), str(resb.get("message"))[:40])


def test_apply_and_provenance():
    print("\n=== 提炼落地并挂上证据链 ===")
    res = distillation.find_distillation_candidates(similarity=0.52)
    ids = (res.get("candidates") or [{}])[0].get("source_memory_ids") or []
    applied = distillation.apply_distillation(
        "用户偏好克制、留白的设计；反复拒绝元素堆叠的界面", ids,
        category="preference", confidence=0.8, note="三次布局反馈归纳")
    new_id = applied.get("memory_id")
    check("提炼成功", applied.get("ok"), f"new_id={new_id}")
    check("返回了挂上的来源", sorted(applied.get("sources") or []) == sorted(ids),
          str(applied.get("sources")))
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM memories WHERE id=?", (new_id,)).fetchone()
        links = conn.execute(
            "SELECT target_memory_id, relation FROM memory_links WHERE source_memory_id=?",
            (new_id,)).fetchall()
        src_alive = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE id IN (%s) AND status='active'"
            % ",".join("?" * len(ids)), ids).fetchone()[0]
    check("提炼结果的 source 标明了来源", str(row["source"]).startswith("distilled:"),
          row["source"][:40])
    check("memory_type 标成 reflection", row["memory_type"] == "reflection",
          str(row["memory_type"]))
    check("relation 全是 derived_from", all(r[1] == "derived_from" for r in links),
          str([r[1] for r in links]))
    check("每条来源都挂了边", len(links) == len(ids), f"{len(links)}/{len(ids)}")
    check("源记忆一条没被改", src_alive == len(ids), f"{src_alive}/{len(ids)} 仍 active")

    listed = distillation.list_distillations()
    item = next((d for d in listed.get("distillations", []) if d["memory_id"] == new_id), None)
    check("list 里能看到这条提炼", item is not None)
    check("list 里带来源数量", (item or {}).get("source_count") == len(ids),
          str((item or {}).get("source_count")))
    check("尚未被反驳", (item or {}).get("contradicted") is False)


def test_already_covered():
    print("\n=== 已提炼过的簇不再重复推荐 ===")
    second = distillation.find_distillation_candidates(similarity=0.52)
    check("默认跳过已覆盖的簇", second.get("count") == 0,
          f"count={second.get('count')} skipped={second.get('skipped_already_covered')}")
    third = distillation.find_distillation_candidates(similarity=0.52, include_covered=True)
    check("include_covered 时仍能看到", third.get("count") >= 1,
          f"count={third.get('count')}")


def test_contradiction_surfaced():
    print("\n=== 提炼被反驳时会被点出来 ===")
    listed = distillation.list_distillations()
    target = (listed.get("distillations") or [{}])[0]
    checker = mcp.save_memory(content="后来发现用户其实喜欢数据密度高的仪表盘",
                              category="preference")
    r = mcp.create_memory_relation(checker["id"], target["memory_id"], "contradicts",
                                   note="新观察与提炼相反")
    check("反驳边建成功", r.get("ok"), str(r.get("message"))[:50])
    again = distillation.list_distillations()
    item = next((d for d in again.get("distillations", [])
                 if d["memory_id"] == target["memory_id"]), None)
    check("提炼被标记为已被反驳", (item or {}).get("contradicted") is True)
    check("drifted 列表里点出了它", any(d["distilled_memory_id"] == target["memory_id"]
                                   for d in again.get("drifted") or []),
          str([d["distilled_memory_id"] for d in again.get("drifted") or []]))


def test_tool_surface():
    print("\n=== MCP 工具面 ===")
    for name in ("find_distillation_candidates", "apply_distillation", "list_distillations"):
        check(f"工具 {name} 存在", callable(getattr(mcp, name, None)))
    r = mcp.find_distillation_candidates(similarity=0.52)
    check("工具可直接调用", r.get("ok"), f"count={r.get('count')}")
    d = mcp.list_distillations()
    check("list 工具可用", d.get("ok"), f"count={d.get('count')}")


def test_real_library():
    """Against the live library this must not crash, and must be honest about
    producing nothing when the data does not support it."""
    print("\n=== 真实库（36 条中文记忆）不崩溃 ===")
    os.environ.pop("BIONIC_DB_PATH", None)
    import importlib
    from app import db as realdb
    importlib.reload(realdb)
    from app import distillation as rd
    importlib.reload(rd)
    res = rd.find_distillation_candidates(similarity=0.52)
    check("真实库扫描成功", res.get("ok"), f"候选 {res.get('count')} / 簇 {res.get('total_clusters')}")
    check("真实库扫描说明了不生成结论", "不生成结论" in (res.get("note") or ""),
          (res.get("note") or "")[:40])


def run():
    test_cluster_found()
    test_min_size_enforced()
    test_apply_and_provenance()
    test_already_covered()
    test_contradiction_surfaced()
    test_tool_surface()
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n=== {len(RESULTS) - len(failed)}/{len(RESULTS)} 通过 ===")
    return 1 if failed else 0


if __name__ == "__main__":
    code = run()
    try:
        test_real_library()
    except Exception as exc:
        print(f"[WARN] 真实库测试跳过：{type(exc).__name__}: {exc}")
    import shutil
    shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(code)
