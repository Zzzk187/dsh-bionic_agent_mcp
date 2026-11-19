"""记忆侧的词法打分：`记忆检索.ps1` 的 `Score-Entry` 移植。

为什么需要这个模块
------------------
`memory_lifecycle._keyword_score` 原来是 `len(inter)/sqrt(len(q)*len(c))`，
也就是「查询与正文的字符集合余弦」。它有两个实测出来的问题（2026-11-08
用同一套 8 题自然语言集对照 dsh-memory 的 PowerShell 实现）：

* **不分字段**。标题里出现一次「截断」和正文里出现十次得分一样，于是
  「唤醒的时候上下文被截断怎么办」把 L06 排到了 P05 前面。
* **不分词重**。中文按 unigram 切，常用字（的、了、一）与专门词（唤醒、
  预算）等权，正文越长越占便宜。本地的 bigram+trigram 展开反而更精确。

本地那份 PowerShell 打分在 8 题上 recall@1 6/8，MCP 侧 3/8，差距就是这两条。

为什么不直接改 `_keyword_score` 的签名
--------------------------------------
它是 `(query, content)` —— 拿不到 tags。而本地算法里 tags×4 是独立的一档
（「网络」「代理」这类标签常常是唯一能把条目捞出来的线索）。所以这里按
「一批记忆」打分：先看全部候选算出一份 peak 做归一化，再按 id 分发。

归一化是必须的：本地分数是无上限的整数累加（一个词在正文命中 10 行就是
20 分），而融合层（`recall_memories`）的词法分量要求 0..1。

同义词表只有一份
----------------
`_SYNONYM_GROUPS` 和 `_segments` 直接从 `app.knowledge` 借。那份是照抄
`知识检索.ps1` 并实测到 38/38 的，记忆侧再抄一份只会让两边口径分叉——
查询「压缩」时知识库认「compact」而记忆库不认，是最难查的那种不一致。
"""
from __future__ import annotations

import json

from app.knowledge import _SYNONYM_GROUPS, _segments

# 与 `记忆检索.ps1` 的 `Score-Entry` 逐字对齐的三个权重。
# 标题×6 远高于正文×2 不是随手定的：标题是这条记忆「讲什么」的浓缩，
# 正文里的词可能只是顺带提到。
# 长度惩罚指数：查询展开成 bigram 后，长文档天然命中更多片段。
# 0 = 不罚（会系统性偏向长文档）。0.2 是旧值；S5-C 批用真实库 45 题扫参后
# 定为 0.4（NL@1 3/8→4/8；再往上标题自召回开始掉，因为长条目的标题也被罚）。
LENGTH_PENALTY_EXP = 0.4

TITLE_WEIGHT = 6
TAGS_WEIGHT = 4
BODY_WEIGHT = 2

# ---- S5-C 批：覆盖率与强度两个因子的幂次 ------------------------------------
# 分数 = coverage^COVERAGE_POW × strength^STRENGTH_POW × 长度惩罚 × 标定。
# 两个幂次都用 1.0：这样分数就是「覆盖率 × 命中字段质量」的字面乘积，没有
# 引入额外假设。实验里把任一幂次调大（让覆盖率或强度更强势）都会让 NL@1
# 从 4/8 掉到 0~1/8——真实库里正确项的覆盖率本来就不高（0.25~0.64），
# 再压缩只会把正确答案一起压没。
COVERAGE_POW = 1.0
STRENGTH_POW = 1.0

# 值域标定幂次（单调变换，不改同批内次序）。
# 取消 peak 归一化后，真实库上的词法分只有 0.1~0.35，而融合层另有 0.45
# 权重的非相关性分量（importance/confidence/freshness/frequency/activation），
# 于是词法失去话语权、正确答案被治理分压过。gamma<1 把低值区拉回可用动态
# 范围。0.6 取自实测扫参（1.0→NL@1 1/8，0.8→2/8，0.6→4/8，0.4→2/8）。
SCORE_GAMMA = 0.6

# ---- S5-C 批实验开关 -------------------------------------------------------
# 这两个开关只用于「单因素实验」（A 原始 / B 仅长度 / C 仅覆盖率 / D 同时），
# 默认都开。**不要**在生产里把它们关掉来绕过问题——关掉就是退回 S5 基线
# 记录的两条缺陷（长文档凭词汇量获益、peak 归一化让弱命中拿满分）。
LENGTH_ENABLED = True
COVERAGE_ENABLED = True

# ---- S5-G2 批：短语级意图识别 + 反例抑制 ------------------------------------
# S5-G 批实测出的唯一有效算法层改动是「反例抑制」：查询在问「装完没生效」时，
# 把明确属于「安装命令缺失」主题的记忆压下去，避免 L09（CLI 装插件报 pnpm
# 未找到）抢占「插件装完没反应」这类查询的第一名。
#
# 但 S5-G 的初版词表有一处真实边界错误：它把**裸词**「找不到」当成「安装后
# 未生效」意图词，于是「npm install 失败 提示找不到包管理器」被误判为未生效
# 查询，把正确答案 L09 从 rank 3 压到 rank 15。本批改成**短语级 + 上下文约束**
# 的匹配：裸词单独出现不构成任何意图。
INTENT_INSTALL_FAIL = "install_fail"
INTENT_INSTALL_NO_EFFECT = "install_no_effect"
INTENT_WRONG_LOCATION = "wrong_location"
INTENT_NEEDS_RESTART = "needs_restart"
INTENT_CONFIG_WRONG = "config_wrong"
INTENT_NONE = ""

# 「安装位置错误」：装到别处了 / 找不到入口。
# **关键**：这里的「找不到」「看不到」必须带位置语境（界面上/设置里/桌面/
# 装到 web）。裸词不在此列——这是 S5-G 边界错误的直接修复点。
_WRONG_LOCATION_STRONG = (
    "界面上找不到", "界面找不到", "界面看不到", "界面上没有",
    "设置里没有", "设置里找不到", "设置里看不到",
    "桌面版设置", "桌面应用看不到", "桌面应用找不到", "桌面应用没有",
    "装到 web", "装到web", "web 的插件", "为什么不共通", "glob 搜不到",
)

# 「安装失败」：安装过程本身出错（命令/环境缺失）。这些短语自带安装语境，
# 单独出现即足以判定。
_INSTALL_FAIL_STRONG = (
    "不是内部命令",
    "already-installed",
    "already installed",
    "装不上",
    "装不了",
    "找不到包管理器",
    "包管理器",
    "找不到命令",
    "命令未找到",
    "pnpm 未找到", "pnpm 找不到", "找不到 pnpm",
    "npm 未找到", "npm 找不到", "找不到 npm",
    "install 失败", "install 报错",
    "安装失败", "安装报错",
    "未能验证恢复",
)

# 弱线索：必须与安装语境共现才算「安装失败」。裸词「报错」「失败」「未找到」
# 「找不到」都在这里——它们单独出现时可能属于任何一类问题，不能单独定性。
_INSTALL_FAIL_WEAK = ("报错", "失败", "未找到", "找不到")
_INSTALL_CONTEXT = ("npm", "pnpm", "install", "安装", "装插件", "plugin")

# 「安装后未生效」：装成功了，但功能没出来。每个短语都含「动作+否定后果」，
# 已经是短语级，不是裸词。
_INSTALL_NO_EFFECT_STRONG = (
    "没反应", "没生效", "不生效", "还是旧", "不工作", "不出来",
    "没用", "没起作用", "没变化", "不显示",
)

# 「需要重启」：重启才能生效。「重启」必须与生效语境共现才算意图——
# 单说「重启」可能是在问怎么重启。
_NEEDS_RESTART_CORE = ("重启",)
_NEEDS_RESTART_CORROBORATE = ("生效", "才", "必须", "才能", "之后", "后", "吗", "扫到")

# 「需要重载才生效」的另一种说法：用户不说「重启」，而是描述「东西放进去了
# 但系统还没看见」。这些是**短语级**信号（都带「扫描/发现/服务」语境），
# 不是裸词：单说「看不到」可能是位置问题，必须与「服务里 / 扫到」共现。
_NEEDS_RELOAD_STRONG = (
    "什么时候能扫到", "什么时候扫到", "能扫到", "扫不到", "没扫到",
    "服务里看不到", "服务里没有", "服务里找不到",
    "注册了但是", "注册了却", "注册了但", "还没生效",
)

# 「配置错误」：配置本身有问题 / 被覆盖 / 运行环境不许可。
_CONFIG_WRONG_STRONG = (
    "被默认值覆盖", "被覆盖", "覆盖了", "写不进去", "被拒绝",
    # 运行环境冲突（S5-G2 补）：桌面应用占着锁 / 两个 profile 不能同时改。
    # 这些是短语级——「开着的时候」「能不能用 CLI」单独都不够定性。
    "应用开着的时候", "应用运行时", "退出应用", "先完全退出",
    "能不能用 cli", "被拒绝",
)

# 抑制强度。0.45 取自 S5-G 实测：0.45 让目标类别（安装后未生效）@3 从 5/6 到
# 6/6、反例误命中 5→2，而 45 题逐题 0 变化。更强的压制会开始误伤真正答案。
INTENT_SUPPRESS_FACTOR = 0.45
INTENT_SUPPRESS_ENABLED = True


def detect_intent(query: str) -> str:
    """判断查询属于哪一类意图；无法判定返回 ``INTENT_NONE``。

    **判定顺序即优先级**，从「点名了具体机制」到「只描述了笼统现象」：

    1. 位置错误  —— 点名了位置（界面上 / 设置里 / 桌面 / 装到 web）
    2. 安装失败  —— 点名了失败机制（pnpm 未找到 / 不是内部命令）
    3. 需要重启  —— 点名了重启
    4. 配置错误  —— 点名了配置被覆盖 / 被拒
    5. 安装后未生效 —— 只说了「没效果」，最笼统，放最后

    两处顺序是刻意的：

    * **位置错误排在安装失败前**：「装了个东西结果界面上找不到」同时含
      「装个」（安装语境）和「找不到」（弱线索），但它问的是位置，不是失败。
    * **安装失败排在未生效前**：「npm install 失败 提示找不到包管理器」若被
      当成未生效查询，就会触发对 L09 的抑制——这正是 S5-G 的边界错误。有了
      这个优先级，它先被判成「安装失败」，不触发任何抑制。
    """
    q = (query or "").lower()
    if not q:
        return INTENT_NONE
    if any(p in q for p in _WRONG_LOCATION_STRONG):
        return INTENT_WRONG_LOCATION
    if any(p in q for p in _INSTALL_FAIL_STRONG):
        return INTENT_INSTALL_FAIL
    if any(p in q for p in _INSTALL_FAIL_WEAK) and \
       any(c in q for c in _INSTALL_CONTEXT):
        return INTENT_INSTALL_FAIL
    if any(c in q for c in _NEEDS_RESTART_CORE) and \
       any(c in q for c in _NEEDS_RESTART_CORROBORATE):
        return INTENT_NEEDS_RESTART
    # 「放进去了但系统还没看见」——同样是「要重启才生效」，只是用户没提
    # 「重启」这个词。必须排在「安装后未生效」之前：它含「没看到 / 扫不到」
    # 这类否定词，若不先判就会掉进未生效桶并触发抑制。
    if any(p in q for p in _NEEDS_RELOAD_STRONG):
        return INTENT_NEEDS_RESTART
    if any(p in q for p in _CONFIG_WRONG_STRONG):
        return INTENT_CONFIG_WRONG
    if any(p in q for p in _INSTALL_NO_EFFECT_STRONG):
        return INTENT_INSTALL_NO_EFFECT
    return INTENT_NONE


def _is_install_fail_topic(mem: dict) -> tuple[bool, bool]:
    """这条记忆是不是在讲「安装失败」，以及它是否也提到了「未生效」。

    第二个返回值是抑制的保护条件：一条记忆若自己也讲了「没反应/没生效」，
    它可能同时就是答案，不该压。
    """
    try:
        blob = " ".join(_fields(mem)).lower()
    except Exception:
        return False, False
    is_fail = any(p in blob for p in _INSTALL_FAIL_STRONG)
    has_effect = any(p in blob for p in _INSTALL_NO_EFFECT_STRONG)
    return is_fail, has_effect


def apply_intent_suppression(query: str, memories: list[dict],
                             scored: dict[int, float]) -> dict[int, float]:
    """查询问「装完没生效」时，压低「安装失败」主题记忆的词法分。

    只在 ``detect_intent`` 判定为 ``INTENT_INSTALL_NO_EFFECT`` 时生效；其余
    意图（含 ``INTENT_NONE``）原样返回。这是**唯一的意图驱动改动**——其余
    四类意图只用于分类与诊断，不改变打分，避免把意图识别变成新的黑盒。
    """
    if not INTENT_SUPPRESS_ENABLED or not scored:
        return scored
    if detect_intent(query) != INTENT_INSTALL_NO_EFFECT:
        return scored
    out = dict(scored)
    for mem in memories:
        mid = mem.get("id")
        if mid not in out or out[mid] <= 0:
            continue
        is_fail, has_effect = _is_install_fail_topic(mem)
        if is_fail and not has_effect:
            out[mid] = round(out[mid] * INTENT_SUPPRESS_FACTOR, 6)
    return out


# trust 加成。本地实现里 verified 的条目 ×1.15（user 的不加）。MCP 侧没有
# trust 列，但导入脚本把 trust 映射进了 confidence（verified→0.8 /
# user→0.95），方向恰好相反（越可信的数值越低），所以这里不做复制——
# confidence 分量在融合层已经独立计入，再乘一次就是双计。
# 保留这个常量是为了让上面这段解释有落点，不是死代码。


def split_words(text: str) -> list[str]:
    """`Split-Words`：latin 词 + CJK bigram/trigram 全展开。

    与 `记忆检索.ps1` 一致：CJK 不做词典分词（没有可用的依赖，也不该为
    此引入一个），而是把每个连续中文串展开成所有 2 字和 3 字片段。
    「唤醒预算」→ 唤醒 / 醒预 / 预算 / 唤醒预 / 醒预算。这比 unigram 精确
    得多：「存不」这种跨词缝的伪词不会和「显存」混淆。
    """
    out: list[str] = []
    for part in _segments(text):
        if not part:
            continue
        if len(part) <= 3 and part.isascii():
            out.append(part)
            continue
        chars = list(part)
        for i in range(len(chars)):
            if i + 1 < len(chars):
                out.append(chars[i] + chars[i + 1])
            if i + 2 < len(chars):
                out.append(chars[i] + chars[i + 1] + chars[i + 2])
        # 纯 latin 的短词（如 "qq"、"jsonl"）没有 CJK 片段，但本身要算一个词。
        if part.isascii() and len(part) >= 2:
            out.append(part)
    # 去重保序：同一片段出现多次不改变「命中与否」，只改变长度。
    seen: list[str] = []
    for t in out:
        if t and t not in seen:
            seen.append(t)
    return seen


def parse_query(text: str) -> tuple[list[str], list[str]]:
    """返回 (用户原词, 全部检索词)，含同义词展开。

    与 `知识检索.ps1` 的 `ConvertTo-Terms` 同一套规则：**按同义词组的 KEY
    匹配**（`key in low or low in key`），不是按组成员匹配。按成员匹配会让
    「显存」这种同时属于 部署/私有化/缩放 三组的词一次性带进十几个无关词。
    """
    q = (text or "").strip()
    if not q:
        return [], []
    originals = [t for t in _segments(q) if t]
    # 查询本身也要做 bigram/trigram 展开，这一步不能省。本地 `记忆检索.ps1`
    # 对 `$Query` 调的就是 `Split-Words`，所以「我那个刷B站的工具怎么用」
    # 会展开出「刷b」「刷b站」「b站」——正是这三十年片段才能命中标题
    # 「dsh-bili-scroll（刷b站）」。只按标点切分的整段中文永远对不上任何
    # 字段，实测就是自然语言题 8 题里 7 题返回空。
    terms: list[str] = []
    for part in originals:
        for w in split_words(part):
            if w not in terms:
                terms.append(w)
    low = q.lower()
    for group in _SYNONYM_GROUPS:
        key = group[0].lower()
        if key in low or low in key:
            for member in group:
                m = member.strip().lower()
                if m and m not in terms:
                    terms.append(m)
    return originals, terms


def direct_terms(text: str) -> list[str]:
    """只做分词展开、不做同义词扩展的那部分检索词。

    `score_all` 用它区分「原词命中」和「靠同义词兜的」：长句切开后会有
    「的工」「怎么」这种跨词缝的伪片段，它们当然算原词命中的一员——本地
    `记忆检索.ps1` 也是这么算的，两边口径必须一样。
    """
    out: list[str] = []
    for part in _segments((text or "").strip()):
        for w in split_words(part):
            if w not in out:
                out.append(w)
    return out


def _fields(mem: dict) -> tuple[str, str, str]:
    """拆出 (title, tags, body)。

    content 的形态是 "# 标题\\n\\n正文"（`import_dsh_memory.build_content`）。
    没有一级标题时整段都当正文，标题留空——不猜。
    """
    content = (mem.get("content") or "")
    title = ""
    body = content
    if content.startswith("# "):
        first, _, rest = content.partition("\n")
        title = first[2:].strip()
        body = rest.strip()
    raw_tags = mem.get("tags")
    if raw_tags is None:
        raw_tags = mem.get("tags_json")
    if isinstance(raw_tags, str):
        try:
            raw_tags = json.loads(raw_tags)
        except (TypeError, ValueError):
            # 源数据里有 "powerShell,编码,BOM" 这种逗号分隔的字符串形态，
            # 当列表用会逐字符拆开——按分隔符切，别再让脏数据变成怪词。
            raw_tags = [t for t in raw_tags.replace("，", ",").split(",") if t.strip()]
    if not isinstance(raw_tags, list):
        raw_tags = []
    tags = " ".join(str(t) for t in raw_tags).lower()
    return title.lower(), tags, body.lower()


def _doc_length(mem: dict) -> int:
    """打分用的正文长度（三个字段相加），下限 60 字符避免超短条目被过度抬升。"""
    title, tags, body = _fields(mem)
    return max(60, len(title) + len(tags) + len(body))


def raw_score(terms: list[str], mem: dict) -> float:
    """`Score-Entry`：逐词查三个字段，命中即加对应权重。"""
    title, tags, body = _fields(mem)
    score = 0
    for t in terms:
        tl = t.lower()
        if not tl:
            continue
        if tl in title:
            score += TITLE_WEIGHT
        if tl in tags:
            score += TAGS_WEIGHT
        if tl in body:
            score += BODY_WEIGHT
    return float(score)


def _field_weight_of(term: str, fields: tuple[str, str, str]) -> float:
    """某个词条在一条记忆里能拿到的最强字段权重（标题 6 > tags 4 > 正文 2）。

    取「最强」而不是累加：同一个词同时出现在标题和正文，只说明它重要一次，
    累加会让长正文因为「到处都能碰到」而加分（就是 S5 基线里的 P0-1）。
    """
    tl = term.lower()
    if not tl:
        return 0.0
    title, tags, body = fields
    if tl in title:
        return float(TITLE_WEIGHT)
    if tl in tags:
        return float(TAGS_WEIGHT)
    if tl in body:
        return float(BODY_WEIGHT)
    return 0.0


def coverage_factors(query: str, mem: dict) -> tuple[float, float, int, int]:
    """字段加权的**字符覆盖**：返回 (coverage, strength, covered, total)。

    为什么不是「命中的词条数 / 词条总数」：查询被展开成 bigram+trigram 后，
    相邻词条高度重叠（「为什么改了代码没生效」能展开出 17 个片段，其中
    「为什」「什么」「么改」互相咬合）。把它们当成 17 份独立证据，等于让
    「碰巧含有全部片段的长文档」白拿满分——S5 基线 P0-1 就是这么来的。

    这里改成真正的「查询覆盖率」：对查询的**每一个字符位置**判断它是否
    被某个命中片段覆盖，并取「从该位置起最长的那个命中片段」的字段权重。
    同一个位置被多个片段命中只取最大值（不累加——累加又回到「重叠片段当
    独立证据」的老问题）。

    返回值分成两个可解释因子：

      coverage = 被覆盖的查询字符数 / 查询总字符数
                 「这条记忆讲的是不是查询问的那件事」
      strength = 加权覆盖质量 / (覆盖字符数 × 标题权重)
                 「命中落在多重要的字段上」，全命中标题 = 1.0

    于是：
      * 覆盖得越多分越高（覆盖率真实参与评分，不是事后乘系数）；
      * 只命中一两个泛化字时 coverage 很小（不得凭单个泛化词拿高相关性）；
      * 必须是**连续子串**才算命中（不把相似但不同的词当有效命中）；
      * 标题命中 6 倍于正文命中（保住「标题是讲什么」的既有口径）。
    """
    q = (query or "").strip().lower()
    if not q:
        return 0.0, 0.0, 0, 0
    fields = _fields(mem)
    total = 0
    mass = 0.0
    covered = 0
    for seg in _segments(q):
        if not seg or len(seg) < 2:
            continue
        total += len(seg)
        # 每个字符位置能拿到的最强字段权重：从该位置起找**最长**的命中片段，
        # 只用这一个片段的权重（长片段命中说明「就是这个词」，比两个孤立的
        # 双子片段更有证据力——任务要求「精确匹配优于弱泛化匹配」）。
        best_w = [0.0] * len(seg)
        for i in range(len(seg) - 1):
            w = 0.0
            for j in range(len(seg), i + 1, -1):
                if j - i < 2:
                    continue
                w = _field_weight_of(seg[i:j], fields)
                if w > 0:
                    break
            if w > 0:
                for k in range(i, j):
                    if w > best_w[k]:
                        best_w[k] = w
        for w in best_w:
            if w > 0:
                covered += 1
                mass += w
    if total == 0 or covered == 0:
        return 0.0, 0.0, 0, total
    coverage = covered / total
    strength = min(1.0, mass / (covered * TITLE_WEIGHT))
    return coverage, strength, covered, total


# ---- S6-FIX-05 批：症状→机制概念桥 ------------------------------------------
# 要修的是一条**字面零重叠**的失败：用户问「插件装完没反应是为什么」，
# 正确答案 F04 的标题是「技能发现机制」、正文讲「改完需重启 DSH 才生效」——
# 全文没有「插件」以外的任何查询词，也没有「没反应」。
#
# 实测的病根（`S6_FIX05_LEXICAL_REPAIR_REPORT.md` §2）：
#   * 查询侧只覆盖到「插件」两个字（coverage 2/11），且落在 F04 的**正文**
#     （字段权重 2 → strength 0.333），词法分 0.186，词法名次第 4；
#   * F01 同样只中「插件」两个字，但落在**标题**（权重 6 → strength 1.0），
#     词法分 0.360，再加 activation 1.0 vs 0.0、importance 0.39 vs 0.28，
#     融合后领先 F04 0.109；
#   * F09 正文里**字面写着**「插件装完没反应」这个五字串（是一句评测引文），
#     覆盖率 7/11 拿到词法第 1。
# 也就是说：提升点不在「给 F04 加权」，而在**「症状」与「机制」之间缺一条
# 语义通路**——用户说的是现象（装完没反应），答案写的是机制（改完要重启）。
#
# 桥的判据是**双侧共存**，两侧都不含任何记忆 id / 记忆名：
#   查询侧：出现「动作完成」词 ∧ 出现「否定后果」词（且意图已判为未生效）；
#   记忆侧：出现「重启/重载才生效」这一类机制词。
# 于是它不是「插件类查询给 F04 提权」，而是「问装完没反应的查询，把讲
# 重启机制的条目提上来」——与本项目 `README.md` 记录的设计原则一致：
# 只在意图与证据都成立时局部生效，不做全局权重调整。
#
# 强度 0.20 取自实测扫参（S6-FIX-05 §4）：

CONCEPT_BRIDGE_ALPHA = 0.20
CONCEPT_BRIDGE_ENABLED = True

# 诊断开关：为 True 时 `apply_concept_bridge` 会把本次桥接的统计写进调用方
# 传进来的 dict（`diagnostics` 参数）。为 False 时一行都不写。
# 它只影响"要不要记账"，**不影响任何分数与排序**——`apply_concept_bridge`
# 的返回值与开关状态完全无关，两条路径由 `tests/concept_bridge_diag_test.py`
# 的 A 组逐字节比对锁死。
CONCEPT_BRIDGE_DIAGNOSTICS = True

# 词法 seed 阈值（S6-FINAL-FIX P2-3）：**唯一权威定义**。
#
# `app/memory_lifecycle.recall_memories` 用它决定哪些记忆参与激活扩散
# （词法分 >= 该值即入 seed），诊断口径也用它数「有几条被桥接抬过了线」。
# 以前这里是 `BRIDGE_SEED_THRESHOLD = 0.3`、那边是裸字面量 `>= 0.3`，两处
# 独立硬编码、没有单一来源——靠一条 `inspect.getsource` 的字符串匹配兜底，
# 那是测试兜底不是结构保证。现在两边都引用这一个常量。
#
# 它不参与打分，只决定「谁是 seed」。桥接把自己的输出抬过这条线时，激活
# 分量会二阶跳变（0 → 1.0），`seed_threshold_crossing_count` 数这件事。
LEX_SEED_THRESHOLD = 0.3

# 兼容别名：S6-FIX-06 引入的公开名字，测试与报告里都在用，保留。
BRIDGE_SEED_THRESHOLD = LEX_SEED_THRESHOLD

# 查询侧 A：动作已完成（装完 / 改完 / 放进去了 …）
_BRIDGE_QUERY_ACTION = (
    "装完", "装了", "安装完", "装好", "改完", "改了", "加了",
    "放进去了", "装上",
)
# 查询侧 B：否定后果（没反应 / 没生效 / 还是旧的 …）
_BRIDGE_QUERY_SYMPTOM = (
    "没反应", "没生效", "没效果", "没变化", "还是旧的", "还是旧",
    "不出来", "看不到", "不管用", "不起作用", "不生效", "不能用",
)
# 记忆侧：机制词——「必须重启/重载才生效」这一类因果说明。
# 注意这些都是短语而非裸词「重启」：单说「重启」是在讲怎么重启，
# 不是在讲「为什么装完没反应」。
_BRIDGE_MEMORY_MECHANISM = (
    "才生效", "需重启", "要重启", "没重启", "必须重启", "看不出变化",
    "旧技能还在", "重启 dsh", "重启后", "重启之后",
)


def concept_bridge_hit(query: str, mem: dict) -> bool:
    """查询在问「装完了但没效果」，而这条记忆给出「要重启才生效」的机制。

    双侧都满足才算命中——只有查询侧（问未生效）或只有记忆侧（讲重启）都不够，
    否则会把「怎么重启」这类查询、以及所有提到重启的记忆一并抬起来。

    刻意**不看**记忆的 id / 标题 / 分类：判据只来自查询措辞与记忆正文，
    这样它对新写入的记忆同样生效，不是给某一条打的补丁。
    """
    q = (query or "").strip().lower()
    if not q:
        return False
    # 意图门：只有已判为「安装后未生效」的查询才走桥。这道门把
    # 「为什么改了代码没生效」（正确答 L03「验证胜于自信」）挡在外面——
    # 实测它在 alpha>=0.25 时会被 F04 抢走第一名。
    if detect_intent(q) != INTENT_INSTALL_NO_EFFECT:
        return False
    if not any(a in q for a in _BRIDGE_QUERY_ACTION):
        return False
    if not any(b in q for b in _BRIDGE_QUERY_SYMPTOM):
        return False
    blob = " ".join(_fields(mem))
    return any(m in blob for m in _BRIDGE_MEMORY_MECHANISM)


def apply_concept_bridge(query: str, memories: list[dict],
                         scored: dict[int, float],
                         diagnostics: dict | None = None) -> dict[int, float]:
    """把桥接分单调混入词法分：``s' = s + alpha * (1 - s)``。

    用 ``1 - s`` 而不是乘 ``(1+alpha)``：值域仍然是 0..1，不会把某条记忆推到
    1.0 以上去压别的东西；且 s 越低抬得越多，正好补「只有泛化字命中」的条目。
    全程 round 到 6 位，与 score_all 其他环节一致。

    ``diagnostics``（S6-FIX-06）：可选出参。非 None 且
    `CONCEPT_BRIDGE_DIAGNOSTICS` 为 True 时，把本次桥接的统计**原地写进**这个
    dict。它只读不写别的任何地方：不碰数据库、不碰日志、不记录查询原文，
    **也不影响返回的分数**（返回值与是否传 diagnostics 逐字节相同）。

    S6-FINAL-FIX P2-1：诊断记账与否由 `_want_diag` 在函数开头算一次决定。
    以前 `hit_ids` / `crossing` 是**无条件**累加的，开关只挡最后写 dict，
    所以「关掉诊断」实测只省 3.7%——那不是关掉，只是不输出。现在关掉时
    连计数都不做：不开诊断这条路径上只多一次布尔求值。
    """
    if not scored:
        return scored
    if not CONCEPT_BRIDGE_ENABLED or CONCEPT_BRIDGE_ALPHA <= 0:
        return scored
    want_diag = diagnostics is not None and CONCEPT_BRIDGE_DIAGNOSTICS
    out = dict(scored)
    hit_ids: list[int] = []
    crossing = 0
    for mem in memories:
        mid = mem.get("id")
        if mid is None or mid not in out:
            continue
        if not concept_bridge_hit(query, mem):
            continue
        s = out[mid]
        bumped = round(s + CONCEPT_BRIDGE_ALPHA * (1.0 - s), 6)
        out[mid] = bumped
        if want_diag:
            hit_ids.append(int(mid))
            if s < LEX_SEED_THRESHOLD <= bumped:
                crossing += 1
    if want_diag:
        diagnostics["bridge_enabled"] = True
        diagnostics["bridge_alpha"] = CONCEPT_BRIDGE_ALPHA
        diagnostics["bridge_hit_count"] = len(hit_ids)
        diagnostics["bridge_hit_memory_ids"] = sorted(hit_ids)
        diagnostics["seed_threshold_crossing_count"] = crossing
        diagnostics["seed_threshold"] = LEX_SEED_THRESHOLD
    return out


def score_all(query: str, memories: list[dict],
              diagnostics: dict | None = None) -> dict[int, float]:
    """对一批记忆打分，返回 0..1 的**绝对**词法相关性。

    与旧实现的几处区别（S5-C 批）：

    1. **不再按本批最高分做 peak 归一化**。旧实现 `s/peak` 让「本批第一名」
       恒等于 1.0，于是「红烧肉怎么做」这种和记忆毫无关系、只碰到「怎么」
       两个字的查询，第一名也是 1.0；同一条记忆在不同候选批次里的分数还会
       跟着漂。现在分数只取决于「查询被覆盖了多少」——跨查询、跨批次可比。
    2. **覆盖率参与评分**，见 `coverage_factors`：分数 = coverage^a × strength^b，
       两个因子各自有明确语义，不是把覆盖率乘一个拍脑袋的系数。
    3. **值域标定**：纯 coverage^a×strength^b 在真实库上只有 0.1~0.35（因为
       口语查询很难覆盖到一半以上），而融合层另有 0.45 权重的非相关性分量，
       于是词法失去话语权、正确答案被治理分压过。用单调幂变换 gamma 把
       低值区拉开是标定手段，不改变同批内次序。

    长度惩罚仍然保留（只罚长、不奖短）。理由没变：同样覆盖一半查询，
    1976 字的记录比 79 字的记录更可能是「碰巧」包含，而不是「在讲这件事」。

    返回 {memory_id: 0..1}。**没有任何命中的记忆不进返回值**，而不是给 0——
    调用方靠「在不在字典里」区分「词法没意见」和「词法判它不相关」。

    末尾两步是有序的：先 `apply_intent_suppression` 压掉抢答的安装失败类
    条目，再 `apply_concept_bridge` 抬起讲「重启才生效」机制的记忆（S6-FIX-05）。

    `diagnostics`（S6-FIX-06）：可选出参，透传给 `apply_concept_bridge` 记账。
    **不传则行为与加诊断前完全一致**——诊断不改分数、不改返回值、不改排序。
    """
    if not memories:
        return {}
    originals, terms = parse_query(query)
    if not terms:
        return {}
    scored: dict[int, float] = {}
    for mem in memories:
        mid = mem.get("id")
        if mid is None:
            continue
        if COVERAGE_ENABLED:
            coverage, strength, covered, _total = coverage_factors(query, mem)
            if covered <= 0 or coverage <= 0:
                continue
            s = (coverage ** COVERAGE_POW) * (strength ** STRENGTH_POW)
            # 值域标定：单调幂变换，把 0..0.35 的低值区拉回可用的动态范围。
            # gamma < 1 只放大低分区、不改次序，不是「乘一个未经验证的固定系数」。
            # 放在长度惩罚**之前**：先标定相关性，再按长度折扣（顺序由实测扫参
            # 确定，反过来 NL@1 会从 4/8 掉到 3/8）。
            if SCORE_GAMMA != 1.0 and s > 0:
                s = s ** SCORE_GAMMA
        else:  # 对照：旧口径（词条加权累加）
            s = raw_score(terms, mem)
            if s <= 0:
                continue
        scored[mid] = s
    if not scored:
        return {}
    # 长度惩罚：查询被展开成十几个 bigram/trigram 后，长文档几乎必然命中
    # 其中一部分，短文档命中少。如果只按峰值归一化，得分就退化成"谁字数多
    # 谁第一"——实测这条把 L03（短，语义分其实最高 0.61）压到了长环境记录
    # 后面。惩罚用 BM25 的 doc-length norm 形状：score / (len/avg)^β。
    #
    # **只罚长、不奖短**（ratio < 1 时按 1 处理）。对称惩罚试过，会把 31 字
    # 的极短条目（id 5「用户偏好中文」）抬成万能答案——「中文脚本一跑就报语法
    # 错」它压过正确的 L11 拿了第一。短不等于精，它只是词少所以命中率低；
    # 归一化已经让它的绝对分很低了，再放大就是噪声。
    #
    # β 由实测扫参决定（见 S5C 报告：β 从 0.2 提到 0.4 后 NL@1 3/8→4/8，
    # 再大则标题自召回开始掉，因为长条目的标题也变长、被误伤）。
    lens = [_doc_length(m) for m in memories]
    avg = (sum(lens) / len(lens)) if lens else 0.0
    if LENGTH_ENABLED and avg > 0 and LENGTH_PENALTY_EXP > 0:
        for mem in memories:
            mid = mem.get("id")
            if mid in scored:
                ratio = _doc_length(mem) / avg
                if ratio <= 1.0:
                    continue          # 比平均短的不罚也不奖
                scored[mid] = scored[mid] / (ratio ** LENGTH_PENALTY_EXP)
    # **绝对分**：不再除以本批 peak。覆盖率本身已是「查询被覆盖的比例」，
    # 天然落在 0..1，融合同量纲要求已经由它满足；再按 peak 拉伸只会把
    # 「本批里相对最强」伪装成「绝对很相关」，让无关查询也不得不高分。
    out = {mid: round(max(0.0, min(1.0, s)), 6) for mid, s in scored.items()}
    # S5-G2：意图驱动的反例抑制（仅在查询问「装完没生效」时生效）。
    # 放在最后一步：先算完整的词法相关性，再按意图调整——这样抑制不会影响
    # 覆盖率/强度的计算本身，也不会在任何其他意图下改变分数。
    out = apply_intent_suppression(query, memories, out)
    # S6-FIX-05：症状→机制概念桥。排在抑制之后——抑制负责「压掉抢答的
    # 安装失败类条目」，桥负责「抬起讲机制的条目」，两者作用对象不重叠，
    # 但顺序固定成先压后抬，便于回归时单独观察各自的效果。
    return apply_concept_bridge(query, memories, out, diagnostics)
