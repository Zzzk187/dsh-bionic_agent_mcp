"""纯文本工具：中文分词 + 同义词表。没有任何业务依赖。

为什么单独一个模块
------------------
`app/knowledge.py` 与 `app/memory_lexical.py` 原本共用同一份实现：后者
`from app.knowledge import _SYNONYM_GROUPS, _segments` 借前者的**私有**名字。
结果是「记忆检索」这个业务模块反向依赖「知识库」这个业务模块的内部细节，
既不好测试也说不清谁是上游。

但两边**必须**用同一份表。`memory_lexical.py` 的原注释把理由写得很清楚：
记忆侧再抄一份会让口径分叉——查询「压缩」时知识库认「compact」而记忆库不认，
是最难查的那种不一致。

所以抽到这里：谁都不依赖谁，两边都依赖它。表与分词函数逐字搬到此处，
未做任何改写（`segments` 就是原来的 `_segments`）。
"""
from __future__ import annotations

import re

# Synonym groups, ported from dsh-memory's `知识检索.ps1` ($Synonyms).
# The table is not complete by design -- bigrams cover the gaps -- but it is
# what makes "压缩" also match "compact" / "摘要" / "剪枝", which bigrams never
# will. Keeping it in sync matters: a user asking the same question through MCP
# and through PowerShell should get the same answer.
SYNONYM_GROUPS: tuple[tuple[str, ...], ...] = (
    ("上下文坍缩", "上下文崩溃", "context collapse", "压缩崩坏", "上下文退化", "压缩吃掉"),
    ("上下文", "context", "窗口"),
    ("交接", "handoff", "交接单", "接力", "换会话"),
    ("压缩", "compact", "summarize", "摘要", "剪枝"),
    ("缓存", "cache", "命中率", "TTL", "前缀"),
    ("记忆", "memory", "回忆", "遗忘", "沉淀"),
    ("语音", "TTS", "唱歌", "声线", "克隆", "voice", "音频"),
    ("微调", "LoRA", "fine-tune", "P-Tuning", "训练"),
    ("评测", "基准", "benchmark", "rubric", "跑分", "评测集"),
    ("安全", "注入", "攻击", "防御", "红线", "越权"),
    ("切分", "分块", "chunk", "分段", "召回"),
    ("部署", "推理", "vLLM", "SGLang", "服务化", "显存"),
    ("知识图谱", "图谱", "GraphRAG", "Graph RAG", "本体"),
    ("编排", "orchestration", "多Agent", "多智能体", "workflow", "handoff"),
    ("提示词", "prompt", "系统提示", "指令"),
    ("缓存前缀", "前缀", "prefix", "缓存"),
    ("评测成本", "计费", "token 计费", "价格", "省钱", "缓存价"),
    ("代码审查", "code review", "review skill", "审查"),
    ("浏览器", "浏览器", "playwright", "agent-browser", "自动化"),
    ("Obsidian", "双链", "知识库", "Claudian", "插件"),
    ("私有化", "自建", "本地部署", "GLM", "显存门槛"),
    ("框架", "LangGraph", "CrewAI", "LlamaIndex", "Agent SDK", "选型"),
    ("缩放", "量化", "AutoRound", "W4A16", "显存"),
    ("人格", "BFI", "性格", "漂移"),
    ("技能包", "skill", "技能", "插件", "SKILL.md"),
    ("长任务", "task list", "功能清单", "handoff", "上下文重置"),
)


def segments(text: str) -> list[str]:
    """Split text into CJK runs / latin tokens.

    Kept because the synonym table is never complete: a query word may appear
    glued to other characters inside a longer run, and a pure bigram scan cannot
    tell "显存" (a unit) from "存不" (a seam).
    """
    out: list[str] = []
    for part in re.split(r"[\s，。、；：？！（）()\[\]【】<>/\\|·\-—\"'“”‘’]+", text.lower()):
        if not part:
            continue
        if re.fullmatch(r"[a-z0-9_.]+", part):
            if len(part) >= 2:
                out.append(part)
        else:
            out.append(part)
    return out


# 兼容别名：老代码（含注释里提到它的地方）用下划线名，语义完全一致。
_SYNONYM_GROUPS = SYNONYM_GROUPS
_segments = segments
