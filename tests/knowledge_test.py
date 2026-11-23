"""知识库（knowledge/ 与 mine/）导入与检索的回归测试。

要盯住的不是"函数能跑"，而是几个会静默出错的地方：

1. **重导入必须幂等。** 导入如果每次都重写全部行，`imported_at` 会一直变，
   而向量表会被反复重建——表面上"一切正常"，实际每次跑都在浪费几分钟并
   抖动检索结果。
2. **删块必须连带删向量。** SQLite 的外键级联只作用一层：
   `knowledge_docs → knowledge_chunks` 删得掉，`knowledge_chunks →
   knowledge_embeddings` 删不掉。留下孤儿向量后，向量检索会拿已经不存在的
   chunk 算相似度，报的是"结果有点怪"而不是错误。
3. **检索质量是这次改动的全部目的。** 用 dsh-memory 自己的 38 条基准
   （`知识检索.ps1 -Eval` 用的同一批）钉住 recall@1/@3。这条测试失败就说明
   移植走样了，而不是"偶发不准"。
4. **`get_doc` 要能用文件名直接取。** 调用方拿到的是检索结果里的路径，
   再要求它换成 id 是纯摩擦。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
import _env_guard  # noqa: E402,F401  OPT-04a：数据库隔离闸门（必须在任何 app.* 之前）

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="bionic-knowledge-")
os.environ["BIONIC_DB_PATH"] = str(Path(_TMP) / "agent.db")
os.environ.setdefault("FASTEMBED_CACHE_PATH", str(ROOT / "data" / "model_cache"))

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


# 与 dsh-memory `知识检索.ps1 -Eval` 同一批用例（脚本 279-318 行），逐条照抄，
# 包括那些写了多个可接受答案的（同一个问题在几篇笔记里都有答案，硬压成唯一
# 答案会让测试去惩罚一个正确结果）。PS 侧实测 recall@1 38/38、recall@3 38/38。
CASES: list[tuple[str, tuple[str, ...]]] = [
    ("上下文坍缩", ("2026-10-20-长期Agent上下文与记忆工程.md",)),
    ("交接单 handoff", ("2026-10-20-学习第二批-交接与语音.md",)),
    ("切分 召回", ("2026-10-20-RAG切分与三层架构.md",)),
    ("PagedAttention", ("2026-10-20-推理引擎与vLLM.md",)),
    ("LoRA 参数", ("2026-10-20-参数高效微调新进展.md",)),
    ("DPO PPO 奖励模型", ("2026-10-20-私有化部署与训练算法.md",)),
    ("全参训练 成本", ("2026-10-20-私有化部署与训练算法.md",)),
    ("CosyVoice 方言", ("2026-10-20-语音克隆与微调实践.md", "2026-10-20-本地TTS与声音克隆.md")),
    ("IndexTTS 情绪", ("2026-10-20-学习第二批-交接与语音.md", "2026-10-20-本地TTS与声音克隆.md")),
    ("缓存 前缀 命中率", ("2026-10-20-提示词缓存原理与省钱.md", "2026-10-20-AI成本核算与单位经济学.md")),
    ("Rubric 置信区间", ("2026-10-20-评测与成本-Benchmark与计费.md",)),
    ("作弊 钩子 榜单", ("2026-10-20-Agent评测与技能包.md",)),
    ("注入 外部数据 红线", ("2026-10-20-多Agent编排与知识图谱.md", "2026-10-20-学习50条.md",
                            "2026-10-20-代刷B站Agent的设计与账号风控.md")),
    ("feature map 验证", ("2026-10-20-AI编码的信任与验证.md",)),
    ("Harness 成功率 60 90", ("2026-10-20-Agent工程与上下文实战.md",)),
    ("skill 审计 脚本", ("2026-10-20-Skill生态与Harness构建.md", "2026-10-20-AI编码的信任与验证.md")),
    ("视觉 RAG OCR", ("2026-10-20-RAG视觉与推理部署.md",)),
    ("知识图谱 融合 靶点", ("2026-10-20-私有化部署与训练算法.md",)),
    ("浏览器 自动化 token", ("2026-10-20-浏览器自动化与本地部署.md",)),
    ("代码地图 根指针", ("2026-10-20-大型代码库的AI工程实践.md",)),
    ("LangGraph 选型", ("2026-10-20-Agent框架选型地图.md",)),
    ("人格 BFI 漂移", ("2026-10-20-Agent评测与技能包.md",)),
    ("Obsidian Claudian", ("2026-10-20-AI知识库与Obsidian.md",)),
    ("本地模型 V100 显存", ("2026-10-20-浏览器自动化与本地部署.md",)),
    ("全参训练 87 万美元", ("2026-10-20-私有化部署与训练算法.md",)),
    ("GLM-130B 硬件 100-150 万", ("2026-10-20-私有化部署与训练算法.md",)),
    ("知识图谱 靶点 45 天", ("2026-10-20-私有化部署与训练算法.md",)),
    ("Harness 消融 裸跑 9 美元", ("2026-10-20-Skill生态与Harness构建.md",)),
    ("生成器 评估器 罚平庸", ("2026-10-20-Skill生态与Harness构建.md",)),
    ("Voicebox 国区补丁", ("2026-10-20-语音克隆与微调实践.md",)),
    ("DramaBox Gemma 12B 编码器", ("2026-10-20-语音克隆与微调实践.md",)),
    ("LAYRA 整页图 视觉 RAG", ("2026-10-20-RAG视觉与推理部署.md",)),
    ("AutoRound 逐层量化 600B", ("2026-10-20-RAG视觉与推理部署.md",)),
    ("ASI-Bench 撤方法", ("2026-10-20-Agent评测与技能包.md",)),
    ("SciΠvot 判断力 随机", ("2026-10-20-Agent评测与技能包.md",)),
    ("Cursor 46.9% token 瘦身", ("2026-10-20-Agent工程与上下文实战.md",)),
    ("ADK 编译视图", ("2026-10-20-Agent工程与上下文实战.md",)),
    ("ReAct 5-7 次调用", ("2026-10-20-Agent工程与上下文实战.md",)),
]


def main() -> int:
    from app import db, knowledge

    db.init_db()

    # ---- 1) 首次导入（不建向量：本测试验证的是文本与打分，向量另有覆盖）----
    r1 = knowledge.import_docs(embed=False)
    # 数字不写死：知识库会随笔记增长（106 → 109 → 110 都发生过）。
    # 写死 106 的后果是这个测试天天红，然后没人看它——比没有测试更糟。
    # 真正要守的是「盘上有的都导了」「每篇都有块」这两条。
    check("首次导入读到全部文档",
          r1["docs_seen"] == r1["docs_imported"] and r1["docs_seen"] > 0,
          f'seen={r1["docs_seen"]} imported={r1["docs_imported"]}')
    check("每篇文档至少产生一个块",
          r1["chunks_written"] >= r1["docs_imported"],
          f'chunks={r1["chunks_written"]} docs={r1["docs_imported"]}')
    check("导入无错误", not r1["errors"], str(r1["errors"])[:120])

    # ---- 2) 幂等：再跑一次不应重写任何东西 ----
    r2 = knowledge.import_docs(embed=False)
    check("重导入幂等（不改任何文档）",
          r2["docs_imported"] == 0 and r2["docs_unchanged"] == r1["docs_imported"],
          f'imported={r2["docs_imported"]} unchanged={r2["docs_unchanged"]}')
    check("重导入不重写分块", r2["chunks_written"] == 0,
          f'chunks_written={r2["chunks_written"]}')

    # ---- 3) 内容变化才会触发重导入（用 hash 模拟，不动用户的文件）----
    with db.transaction() as conn:
        conn.execute("UPDATE knowledge_docs SET content_hash='stale' WHERE id=1")
    drift = knowledge.drift()
    check("内容变了会被 drift 认出来",
          len(drift["needs_reimport"]) == 1,
          f'needs_reimport={drift["needs_reimport"]}')
    r3 = knowledge.import_docs(embed=False)
    check("只有那篇被重写", r3["docs_imported"] == 1,
          f'imported={r3["docs_imported"]}')

    # ---- 4) 删块必须连带删向量（SQLite 不会级联两层）----
    with db.connect() as conn:
        doc = conn.execute(
            "SELECT id, rel_path FROM knowledge_docs WHERE root='knowledge'"
            " ORDER BY id LIMIT 1").fetchone()
    first_id, first_path = doc["id"], doc["rel_path"]
    knowledge.import_docs(embed=False, limit=1)  # 确保这一篇有块
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO knowledge_embeddings(chunk_id, model_name, content_hash,"
            " dimension, vector_json) SELECT id, ?, 'x', 3, '[0,0,0]'"
            " FROM knowledge_chunks WHERE doc_id = ?", (knowledge.MODEL_NAME, first_id))
        conn.execute("UPDATE knowledge_docs SET content_hash='stale2' WHERE id=?",
                     (first_id,))
    with db.connect() as conn:
        before_vec = conn.execute(
            "SELECT COUNT(*) FROM knowledge_embeddings e JOIN knowledge_chunks c"
            " ON c.id = e.chunk_id WHERE c.doc_id = ?", (first_id,)).fetchone()[0]
    knowledge.import_docs(embed=False, limit=1)
    with db.connect() as conn:
        orphan = conn.execute(
            "SELECT COUNT(*) FROM knowledge_embeddings e LEFT JOIN knowledge_chunks c"
            " ON c.id = e.chunk_id WHERE c.id IS NULL").fetchone()[0]
        after_vec = conn.execute(
            "SELECT COUNT(*) FROM knowledge_embeddings e JOIN knowledge_chunks c"
            " ON c.id = e.chunk_id WHERE c.doc_id = ?", (first_id,)).fetchone()[0]
    check("重导入前该篇确有向量（前置条件成立）", before_vec > 0, f"vec={before_vec}")
    check("重导入后不残留孤儿向量（级联只管一层）", orphan == 0, f"orphan={orphan}")
    check("该篇的旧向量确实被清掉了", after_vec == 0, f"vec={after_vec}")

    # ---- 5) 检索质量：对齐 dsh-memory 的 38 条基准 ----
    hits1 = hits3 = 0
    misses = []
    for q, want in CASES:
        res = knowledge.search(q, limit=5, root="knowledge")
        names = []
        for x in res["results"]:
            name = os.path.basename(x["source"].replace("\\", "/"))
            if name not in names:
                names.append(name)
        ok1 = bool(names) and names[0] in want
        hits1 += ok1
        hits3 += any(n in want for n in names)
        if not ok1:
            misses.append(f"{q} -> {names[0] if names else '(空)'}")
    check("recall@1 全中（38/38）", hits1 == len(CASES),
          f"{hits1}/{len(CASES)}" + (f"  miss: {misses[:3]}" if misses else ""))
    check("recall@3 全中（38/38）", hits3 == len(CASES), f"{hits3}/{len(CASES)}")

    # ---- 6) 过滤与上限 ----
    r = knowledge.search("显存", limit=10, root="mine")
    check("root 过滤生效", all(x["root"] == "mine" for x in r["results"]),
          str({x["root"] for x in r["results"]}))
    r = knowledge.search("显存", limit=10, domain="语音")
    check("domain 过滤生效", all(x["domain"] == "语音" for x in r["results"]),
          str({x["domain"] for x in r["results"]}))
    r = knowledge.search("显存", limit=20)
    per = {}
    for x in r["results"]:
        per[x["source"]] = per.get(x["source"], 0) + 1
    check("同一篇最多 2 段（防单篇霸榜）", all(v <= knowledge.MAX_PER_DOC for v in per.values()),
          str(max(per.values()) if per else 0))

    # ---- 7) 空查询与不存在的文档 ----
    check("空查询返回 ok=False", knowledge.search("   ")["ok"] is False)
    check("取不存在的文档不抛异常",
          knowledge.get_doc(doc_id=999999)["ok"] is False)

    # ---- 8) get_doc：按 id 与按文件名都要能取 ----
    d_id = knowledge.get_doc(doc_id=first_id, max_chars=500)
    check("按 id 取文档成功", d_id["ok"] and d_id["doc"]["id"] == first_id)
    bare = os.path.basename(first_path.replace("\\", "/"))
    d_name = knowledge.get_doc(path=bare, max_chars=500)
    check("按文件名取文档成功（不用自己拼路径）",
          d_name["ok"] and d_name["doc"]["id"] == first_id,
          f"{bare} -> {d_name.get('ok')}")
    # 按路径查时 `doc_id` 参数还是 0，如果 chunks 查询继续用参数而不是
    # 查出来的行 id，就会返回空列表 —— 文档报着 chunk_count: 9、chunks: []。
    check("按文件名取时 chunks 不为空",
          len(d_name["chunks"]) > 0,
          f'{len(d_name["chunks"])} 段')
    check("按文件名取的 chunks 与按 id 取的一致",
          len(d_name["chunks"]) == len(d_id["chunks"]),
          f'{len(d_name["chunks"])} vs {len(d_id["chunks"])}')
    check("chunks 数量与文档声明的 chunk_count 一致",
          len(d_name["chunks"]) == d_name["doc"]["chunk_count"],
          f'{len(d_name["chunks"])} vs {d_name["doc"]["chunk_count"]}')
    check("截断是显式报告的",
          d_id["truncated"] is True and d_id["content_total_chars"] > 500,
          f'total={d_id["content_total_chars"]}')

    # ---- 9) 概况与实际数据一致 ----
    st = knowledge.status()
    check("status 的文档数与库一致", st["docs"] == r1["docs_imported"],
          f'docs={st["docs"]} imported={r1["docs_imported"]}')
    # 不写死 root 集合：2026-11-08 加入骨架（memory/script）后，写死它就天天红，
    # 而天天红的测试没人看。断言"该在的都在、没有意外的新 root、合计对得上"。
    known_roots = {"knowledge", "mine", "memory", "script"}
    check("status 分侧统计",
          {"knowledge", "mine"} <= set(st["by_root"])
          and set(st["by_root"]) <= known_roots
          and all(v["docs"] > 0 for v in st["by_root"].values())
          and sum(v["docs"] for v in st["by_root"].values()) == st["docs"],
          f'{st["by_root"]}')
    check("knowledge 域分布合计等于 knowledge 侧文档数",
          sum(st["knowledge_domains"].values())
          == st["by_root"]["knowledge"]["docs"],
          f'sum={sum(st["knowledge_domains"].values())}')

    failed = [n for n, ok, _ in RESULTS if not ok]
    print()
    if failed:
        print(f"FAIL={len(failed)}")
        return 1
    print(f"全部通过（{len(RESULTS)} 项）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
