"""A line-for-line transliteration of `知识检索.ps1`'s scoring, run standalone.

The Python port in app/knowledge.py drifted from the PowerShell in at least four
places, each of which changes the ranking, so instead of patching it again this
module mirrors the original statement by statement and is measured against the
same 38-case benchmark. Whatever this scores is the ceiling the port should hit.

Deliberate fidelity notes (all of these are things the port got wrong):

* sections are built from the raw file lines, and every section's text *starts
  with its own `## ` heading line* -- the PowerShell appends every line to the
  current section, including the heading;
* term frequency is counted as **matching lines**, capped at 6, not characters;
* the file-name bonus matches the **basename**, not the relative path;
* IDF df is counted over whole documents with `-like` semantics (case-insensitive
  substring), and the user's own words get `w * 2.5` capped at 10;
* scores are rounded to integers and ties break on **file name**, ascending;
* the index-map bonus (+2) is applied *before* the matched-ratio multiplier.
"""
import io
import math
import os
import re
import sys

ROOT = os.environ.get("DSH_MEMORY_ROOT", r"C:\Users\xing\.dsh\dsh-memory")

SYNONYMS = {
    "上下文坍缩": ["上下文坍缩", "上下文崩溃", "context collapse", "压缩崩坏", "上下文退化", "压缩吃掉"],
    "上下文": ["上下文", "context", "窗口"],
    "交接": ["交接", "handoff", "交接单", "接力", "换会话"],
    "压缩": ["压缩", "compact", "summarize", "摘要", "剪枝"],
    "记忆": ["记忆", "memory", "回忆", "遗忘", "沉淀"],
    "语音": ["语音", "TTS", "唱歌", "声线", "克隆", "voice", "音频"],
    "微调": ["微调", "LoRA", "fine-tune", "P-Tuning", "训练"],
    "评测": ["评测", "基准", "benchmark", "rubric", "跑分", "评测集"],
    "安全": ["安全", "注入", "攻击", "防御", "红线", "越权"],
    "切分": ["切分", "分块", "chunk", "分段", "召回"],
    "部署": ["部署", "推理", "vLLM", "SGLang", "服务化", "显存"],
    "知识图谱": ["知识图谱", "图谱", "GraphRAG", "Graph RAG", "本体"],
    "编排": ["编排", "orchestration", "多Agent", "多智能体", "workflow", "handoff"],
    "提示词": ["提示词", "prompt", "系统提示", "指令"],
    "缓存前缀": ["前缀", "prefix", "缓存"],
    "评测成本": ["计费", "token 计费", "价格", "省钱", "缓存价"],
    "代码审查": ["代码审查", "code review", "review skill", "审查"],
    "浏览器": ["浏览器", "playwright", "agent-browser", "自动化"],
    "Obsidian": ["Obsidian", "双链", "知识库", "Claudian", "插件"],
    "私有化": ["私有化", "自建", "本地部署", "GLM", "显存门槛"],
    "框架": ["框架", "LangGraph", "CrewAI", "LlamaIndex", "Agent SDK", "选型"],
    "缩放": ["缩放", "量化", "AutoRound", "W4A16", "显存"],
    "人格": ["人格", "BFI", "性格", "漂移"],
    "技能包": ["skill", "技能", "插件", "SKILL.md"],
    "长任务": ["长任务", "task list", "功能清单", "handoff", "上下文重置"],
}


def parse_query(text):
    """`ConvertTo-Terms`: split on delimiters, then add matching synonym groups."""
    terms = []
    for part in re.split(r"[\s,，、/|;；]+", text):
        p = part.strip()
        if p:
            terms.append(p)
    low = text.lower()
    for key, group in SYNONYMS.items():
        k = key.lower()
        if k in low or low in k:
            terms.extend(group)
    seen, out = set(), []
    for t in terms:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def originals(text):
    """`$orig`: only the user's own words, which later get the 2.5x boost."""
    return [p.strip() for p in re.split(r"[\s,，、/|;；]+", text) if p.strip()]


def load_docs(root=None):
    root = root or ROOT
    base = os.path.join(root, "knowledge")
    docs = []
    for dirpath, _dirnames, filenames in os.walk(base):
        for name in sorted(filenames):
            if not name.lower().endswith(".md"):
                continue
            if name in ("INDEX.md", "资产索引.md"):
                continue
            path = os.path.join(dirpath, name)
            lines = io.open(path, encoding="utf-8", errors="replace").read().split("\n")
            title = ""
            for ln in lines:
                m = re.match(r"^#\s+(.+)$", ln)
                if m:
                    title = m.group(1).strip()
                    break
            sections = []
            cur = None
            for i, line in enumerate(lines):
                is_head = re.match(r"^##\s+(.+)$", line)
                if is_head or i == 0:
                    if cur is not None:
                        sections.append(cur)
                    cur = {
                        "heading": is_head.group(1).strip() if is_head else "(前言)",
                        "start": i + 1,
                        "lines": [],
                    }
                if cur is not None:
                    cur["lines"].append(line)
            if cur is not None:
                sections.append(cur)
            docs.append({
                "file": name,
                "rel": os.path.relpath(path, base).replace("\\", "/"),
                "title": title,
                "full": "\n".join(lines).lower(),
                "sections": sections,
            })
    return docs


def index_map(root=None):
    root = root or ROOT
    idx = os.path.join(root, "knowledge", "INDEX.md")
    mapping = {}
    if not os.path.exists(idx):
        return mapping
    for line in io.open(idx, encoding="utf-8", errors="replace").read().split("\n"):
        m = re.match(r"^\|\s*\[[^\]]+\]\(([^)]+\.md)\)\s*\|\s*(.+?)\s*\|", line)
        if m:
            mapping[m.group(1).strip()] = m.group(2).strip().lower()
    return mapping


def _has(hay, needle):
    """PowerShell `-like "*$t*"`: case-insensitive substring on both sides."""
    return needle.lower() in hay.lower()


def score_docs(docs, terms, orig, imap):
    n_docs = len(docs)
    weights = {}
    low_orig = {o.lower() for o in orig}
    for t in terms:
        tl = t.lower()
        df = sum(1 for d in docs if tl in d["full"])
        w = min(4.0, 1.0 + math.log(n_docs / max(1, df)))
        if tl in low_orig:
            w = min(10.0, w * 2.5)
        weights[t] = w
    return weights


def search(text, top=5, root=None, docs=None, imap=None):
    docs = docs if docs is not None else load_docs(root)
    imap = imap if imap is not None else index_map(root)
    terms = parse_query(text)
    orig = originals(text)
    weights = score_docs(docs, terms, orig, imap)

    cjk = re.sub(r"[^\u4e00-\u9fff]", "", text)
    bigrams = list(dict.fromkeys(cjk[i:i + 2] for i in range(len(cjk) - 1)))
    qtext = text

    results = []
    for d in docs:
        idx_hit = False
        summary = imap.get(d["rel"]) or imap.get("./" + d["rel"])
        if summary:
            for t in terms:
                if _has(summary, t):
                    idx_hit = True
                    break
        for sec in d["sections"]:
            joined = "\n".join(sec["lines"]).lower()
            head = sec["heading"].lower()
            score = 0.0
            matched = 0
            rare = 0
            for t in terms:
                w = weights[t]
                hit = False
                if _has(d["file"], t):
                    score += 4 * w
                    hit = True
                if _has(d["title"], t):
                    score += 3 * w
                    hit = True
                if _has(head, t):
                    score += 3 * w
                    hit = True
                n = sum(1 for ln in sec["lines"] if _has(ln, t))
                if n:
                    score += min(n, 6) * w
                    hit = True
                if hit:
                    matched += 1
                    if w >= 6:
                        rare += 1
            if idx_hit:
                score += 2
            if matched and terms:
                score = round(score * (0.75 + 0.25 * (matched / len(terms))))
            if rare:
                score += 15 * rare
            if bigrams:
                bg = sum(1 for b in bigrams if b in joined)
                if bg:
                    score += min(6, bg) * 1.6
            if qtext.lower() in joined:
                score += 40
            if re.search(r"主题地图|索引|INDEX|重复与空白|资产索引|总图|速查", d["file"]):
                score = round(score * 0.55)
            if re.search(r"一句话|总结|速览|前言|结论|摘要", sec["heading"]):
                score = round(score * 0.7)
            if re.search(r"引用来源|来源表|视频清单|目录", sec["heading"]):
                score = round(score * 0.6)
            if score > 0:
                results.append({"score": score, "file": d["file"], "rel": d["rel"],
                                "heading": sec["heading"], "start": sec["start"]})

    results.sort(key=lambda r: (-r["score"], r["file"]))
    picked, per_file = [], {}
    for r in results:
        if len(picked) >= top:
            break
        c = per_file.get(r["file"], 0)
        if c >= 2:
            continue
        per_file[r["file"]] = c + 1
        picked.append(r)
    return picked


def load_cases():
    src = io.open(os.path.join(ROOT, "知识检索.ps1"), encoding="utf-8").read()
    cases = []
    for m in re.finditer(r"q = '([^']+)'.*?want = (?:'([^']+)'|@\(([^)]+)\))", src):
        q = m.group(1)
        wants = re.findall(r"'([^']+)'", m.group(3)) if m.group(3) else [m.group(2)]
        cases.append((q, wants))
    return cases


def evaluate(verbose=False):
    docs = load_docs()
    imap = index_map()
    cases = load_cases()
    v1 = v3 = 0
    miss = []
    for q, wants in cases:
        res = search(q, top=5, docs=docs, imap=imap)
        files = []
        for r in res:
            if r["file"] not in files:
                files.append(r["file"])
        if files and files[0] in wants:
            v1 += 1
        else:
            miss.append((q, files[0] if files else "-", wants[0]))
        if any(f in wants for f in files):
            v3 += 1
    n = len(cases)
    print(f"transliteration: recall@1 {v1}/{n} = {100*v1//n}%  recall@3 {v3}/{n} = {100*v3//n}%")
    if verbose:
        for q, got, want in miss:
            print("  MISS", q, "->", got, "| want", want)
    return v1, v3, n


if __name__ == "__main__":
    evaluate(verbose="-v" in sys.argv)
