# Bionic Agent MCP

**给 AI 助手一个本地、离线、能自我治理的长期记忆。**

数据全落在本地 SQLite（`data/agent.db`）——无云端调用、无 API Key、无数据库服务。
向量用本地 ONNX 推理，首次下载模型（约 100 MB）后永久离线。

```
85 个 MCP 工具 = 核心 36（日常） + 治理 49（按需）
                 同一份库、同一套后端、零重复逻辑
```

---

## 它能干什么

| 你想做的事 | 用的工具 |
|---|---|
| 记住偏好、事实、踩过的坑 | `save_memory` / `get_memory` / `update_memory` |
| 把它找回来 | `search_memories`（关键词）、`semantic_search_memories`（向量）、`find_related_memories`（关联） |
| 纠正旧结论但保留历史 | `supersede_memory`（旧条转 superseded，不物理删除） |
| 多阶段任务不丢上下文 | `create_task` → `handoff_task` → `resume_task` |
| 库变脏了要整理 | `detect_memory_conflicts` / `run_memory_maintenance` / `archive_memory_governance` |
| 误操作要能回滚 | `create_db_snapshot` / `restore_db_snapshot`（治理实例），每个治理动作也单独可回滚 |
| 换机器带走 | `export_memory_package`（核心）→ `preview_memory_restore` / `restore_memory_package`（治理） |

**一条铁律：全程零物理删除。** 归档、替代、过期都只是改状态，随时可回。

---

## 30 秒上手

### 1. 装依赖

```bash
cd bionic-agent-mcp
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt   # Windows
.venv/bin/python    -m pip install -r requirements.txt   # macOS / Linux
```

### 2. 自检（不连宿主也能验证）

```bash
.venv/Scripts/python tests/smoke_test.py
.venv/Scripts/python tests/verify_mcp_split_test.py
# 末行应打印 core=36 / governance=49 / 合计 85，6 项全 PASS
```

### 3. 挂到 MCP 宿主

写进宿主的 MCP 配置，**路径换成你自己的绝对路径**。DeepSeek Harness 的
`~/.dsh/skill-mcp-manager/registry.json` 长这样：

```yaml
- id: bionic-agent
  name: '@deepseek-ai/dsh-mcp-client'
  config:
    serverName: bionic-agent
    transport: stdio
    command: 'C:/YOUR/PATH/bionic-agent-mcp/.venv/Scripts/python.exe'
    args: ['-m', 'app.mcp_server']
    cwd: 'C:/YOUR/PATH/bionic-agent-mcp'

- id: bionic-agent-governance
  name: '@deepseek-ai/dsh-mcp-client'
  config:
    serverName: bionic-agent-governance
    transport: stdio
    command: 'C:/YOUR/PATH/bionic-agent-mcp/.venv/Scripts/python.exe'
    args: ['-m', 'app.mcp_server_gov']
    cwd: 'C:/YOUR/PATH/bionic-agent-mcp'
```

> ⚠️ **必须用 `-m app.mcp_server`**。写成 `args: ['.../app/mcp_server.py']` 会让
> `sys.path[0]` 变成 `app/`，`from app import ...` 直接失败。治理入口虽内置兜底，
> 但统一用 `-m` 最省心。其他宿主（Claude Desktop、Cursor 等）的 `mcpServers` 段同构。

重载宿主，核心实例立刻可用。

---

## 为什么要分两个实例

85 个工具塞进一个实例，光工具定义就吃掉大量上下文。所以拆成两个：

- **核心 `bionic-agent`（36）** — 日常读写、检索、任务、嵌入索引、备份包导出。固定挂载，token 成本可预期。
- **治理 `bionic-agent-governance`（49）** — 评分、冲突检测、保护分档、整理闭环、融合召回、**整库快照**、便携恢复。**需要时再挂**。

两者是**同一份库、同一套后端**，不存在数据分叉。

核心多出的 7 个（`set_memory_expiry`、`scan_expired_memories`、`run_expiry_policy`、
`get_resume_packet`、`find_stuck_tasks`、`fail_task`、`backfill_memory_types`）都是短参数
日常维护动作，放核心才不会让常见操作被迫多挂一个实例。另有 3 个知识库工具
（`search_knowledge`、`get_knowledge_doc`、`knowledge_status`）也在核心——查记忆和查知识
本来就是同一个动作，不该分两次挂载。

### 完整工具清单

<details>
<summary><b>核心实例 <code>bionic-agent</code>（36）</b></summary>

**记忆读写** `save_memory` `get_memory` `update_memory` `archive_memory` `restore_memory`
`supersede_memory`

**检索** `search_memories` `semantic_search_memories` `find_related_memories`
`recall_all`

**关系** `link_memories` `get_memory_links`

**嵌入** `embedding_status` `index_memories`

**有效期** `set_memory_expiry` `scan_expired_memories` `run_expiry_policy`
`backfill_memory_types`

**任务** `create_task` `list_tasks` `get_task` `save_stage_snapshot` `handoff_task`
`resume_task` `complete_task` `get_resume_packet` `find_stuck_tasks` `fail_task`
`list_events`

**知识库** `search_knowledge` `get_knowledge_doc` `knowledge_status`

**备份包** `export_memory_package` `list_memory_packages` `inspect_memory_package`
`validate_memory_package`

</details>

<details>
<summary><b>治理实例 <code>bionic-agent-governance</code>（49）</b></summary>

**评分与状态** `score_memories` `get_memory_governance_status` `get_memory_details`
`get_memory_lifecycle` `get_memory_usage_stats`

**冲突与裁决** `detect_memory_conflicts` `find_duplicate_memories` `list_memory_conflicts`
`review_memory_conflict` `supersede_memory_from_conflict`

**保护与审核** `assess_memory_protection` `summarize_protection` `protect_memory`
`set_review_status` `release_review_status`

**归档与回滚** `archive_memory_governance` `restore_memory_governance`
`get_governance_action` `rollback_governance_action`

**整理与巩固** `run_memory_maintenance` `plan_memory_cleanup` `review_governance_actions`
`apply_governance_actions` `get_governance_run` `consolidate_memory_candidates`
`run_memory_consolidation`

**融合召回** `recall_memories` `spread_activation` `record_memory_activation`

**反馈闭环** `record_memory_feedback` `summarize_memory_feedback` `record_recall`

**关系图** `create_memory_relation` `list_memory_relations` `get_related_memories`

**提炼** `find_distillation_candidates` `apply_distillation` `list_distillations`

**整库快照** `create_db_snapshot` `list_db_snapshots` `restore_db_snapshot`
`prune_db_snapshots`

**向量一致性** `get_vector_consistency` `check_vector_consistency` `warmup_embeddings`

**分类** `classify_memory`

**便携恢复** `preview_memory_restore` `restore_memory_package` `delete_memory_package`

</details>

---

## 管理面板（Web UI）

不想只走工具调用？自带一个自包含面板，直连同一份 `agent.db`，零 CDN、可离线：

```bash
.venv/Scripts/python -m app.admin_ui                 # http://127.0.0.1:8899
.venv/Scripts/python -m app.admin_ui --port 8900     # 只换端口，仍只监听本机
bionic-agent-admin                                   # 或用安装好的入口
```

覆盖记忆 / 任务 / 治理三大块：CRUD、语义检索、关系图、3D 记忆星图、治理评分与冲突检测、
备份与恢复中心、嵌入模型预热与状态。

面板是**独立进程**（FastAPI），不侵入宿主的 Electron/asar，所以宿主更新、热重载都不影响它。
它直接复用 `app.*` 的现成函数，**和 MCP 工具完全等价、实时同库**。

### 安全边界（先读这段）

- **默认无认证**，默认只绑 `127.0.0.1`。
- 绑非本机地址（`0.0.0.0`、`192.168.x.x`、机器名）会**拒绝启动、退出码 2**——
  那等于把「删记忆 / 恢复整库 / 导出备份」的接口交给同网段所有人。
- 确实要暴露到局域网，必须显式声明，且**绑定授权与令牌是两件独立的事**：

```bash
.venv/Scripts/python -m app.admin_ui --host 0.0.0.0 --allow-remote --token "你的令牌"
```

`--allow-remote` 只是「你知道后果」的授权声明、**不是认证机制**；反过来，配了令牌
也不能省掉它——远程绑定仍然要你显式声明。

### 访问令牌（可选）

| 方式 | 写法 | 优先级 |
|---|---|---|
| 命令行 | `--token <令牌>` | 高 |
| 环境变量 | `BIONIC_ADMIN_TOKEN=<令牌>` | 低 |
| 都不配 | —— | 认证关闭（历史行为） |

开启后首页与 `/static/*` 仍公开（否则页面本身打不开），**其余全部接口**（含 `/docs`、
`/openapi.json`）都要求 `Authorization: Bearer <令牌>`，否则 401。

浏览器用法：顶栏右侧多出「访问令牌」输入框，填进去即可。令牌**只存在页面内存变量里**
——不写 `localStorage`/`sessionStorage`、不进 URL、不进 Cookie、不写库、不打日志。
代价是刷新或重开要重填一次；这个代价值得付，落进 storage 后同源下任何脚本都能读走。

三个实测注意点：

- **令牌只能用 ASCII**。HTTP 头值本身只能是 ASCII，中文令牌浏览器根本发不出去，
  现场表现是「令牌明明对却全 401」——所以服务端启动时就拒掉（退出码 2），不让你去猜。
- 首尾空白自动裁掉（粘贴自带空格不算错）；控制字符拒绝。
- 走的是**明文 HTTP**。远程场景务必套 HTTPS 反代，或只用 SSH 隧道转发。

> 注意区分两层保护：`dsh-panel-client` 插件在**反代层**另有一道写保护（只允许从 DSH
> 自己的窗口发起写操作），它只对经插件挂载的路径生效、对直连 8899 无效。后端令牌认证
> 与之互补、不冲突。

---

## 装进 DeepSeek Harness 侧边栏（可选）

仓库带一个 Cordis 插件 `dsh-panel-client/`，只在 DSH 侧边栏加一个「记忆/任务」图标，
点开是 iframe 承载上面的面板——不改 asar、不碰宿主源码、宿主更新也不影响其他功能。

```bash
python dsh-panel-client/install.py        # 建符号链接 + 登记 bundles
# → 完全退出并重启 DSH（含托盘），侧边栏出现图标
```

**不用手动常驻服务**：插件启动时先探 8899，有就复用（绝不 kill 你自己起的那个）、
没有才 `spawn` 一个；DSH 退出只回收自己起的进程。子进程意外退出（OOM、依赖装坏、
被手滑 kill）时按 1s → 2s → 4s 退避重拉，最多 5 次——不做无限重试是有意的，
「python 里没装 fastapi」这类原因不会自己好，无限重拉只会刷满日志。

卸载：`python dsh-panel-client/install.py --uninstall`。

### 装完没图标？先跑自检，别靠猜

```bash
node dsh-panel-client/verify-install.mjs
```

它逐项验证宿主判定链（profile 目录、`dependencies`、`dsh.profile.bundles`、node_modules
可解析、patch 内容、`dsh.engines.dsh` 与真实运行时版本、8899 是否在监听），并用两份日志
把故障定位到具体一半：

| `data/panel-boot.log` | `data/panel-client.log` | 说明 |
|---|---|---|
| 有 | 有 | 通路全通 |
| 有 | 无 | Node 侧加载成功，卡在浏览器端 |
| 无 | — | 宿主根本没加载插件，多半是 `dsh.engines.dsh` 不满足被**静默跳过** |

> 两项显示 `[WAIT]` 是正常的——要等你**重启 DSH 并点开一次面板**之后才有证据。
> 刻意写成 `[WAIT]` 而不是猜测通过。更细的证据分级见脚本输出。

> **改了 `lib/client.js` 必须重启 DSH，刷新页面不够**——宿主按启动时算的文件 hash
> 判 bundle 是否要重读，进程还活着就一直发旧的那份。Node 侧的 `lib/index.js` 同理。

---

## 项目结构

```
bionic-agent-mcp/
├── app/
│   ├── mcp_server.py         # 19 行 shim（宿主 registry 钉死此路径，勿改）
│   ├── mcp_server_gov.py     # 治理实例入口
│   ├── mcp/
│   │   ├── runtime.py        # 两个 FastMCP 实例 + 公共 helper
│   │   ├── core_tools.py     # 36 个核心工具
│   │   ├── gov_tools.py      # 49 个治理工具
│   │   ├── server.py         # 组装与再导出
│   │   └── stores/           # 全部 SQL 在这里（memory / search / task）
│   ├── admin_ui.py           # 管理面板（FastAPI）
│   ├── admin_ui.html         # 面板前端（内联 CSS+JS，零外部依赖）
│   ├── panel_queries.py      # 面板专用只读查询
│   ├── db.py                 # SQLite 初始化 + 幂等迁移 + 事务
│   ├── memory_lifecycle.py   # 召回编排（候选 → 打分 → 排序 → 关联）
│   ├── memory_lexical.py     # 词法分 + 意图识别 + 概念桥
│   ├── embeddings.py         # 本地向量（预热 / 就绪探针 / 降级）
│   ├── knowledge.py          # 知识库导入与检索
│   ├── recall_profile.py     # 召回画像（按类型/问句选权重）
│   ├── text_tokens.py        # 同义词表与分词（记忆与知识共用）
│   ├── portable_backup.py    # 导出便携包
│   ├── portable_restore.py   # 恢复引擎（preview → confirm → execute）
│   ├── db_backup.py          # 物理备份
│   ├── snapshots.py          # 逻辑快照
│   └── governance_*.py       # 评分 / 冲突 / 维护 / 规则 / 服务
├── tests/                    # 38 个套件，真实 SQLite，零 mock
├── scripts/                  # 同步、导入、备份、评测
├── data/                     # agent.db（首次运行自动建库；勿手删）
└── dsh-panel-client/         # DSH 侧边栏插件
```

**分层原则**：工具层不写 SQL（全部走 `stores/`）。store 函数接收调用方传入的 `conn`、
自己不 commit、不开事务——事务边界永远由工具函数掌握。这样「记忆写入 + 审计事件」仍在
同一个事务里，不会留下「记忆存在但没有审计」的半截状态。

---

## 记忆模型

- `memories.status` 是唯一检索过滤字段（`active` / `archived`），**全程零物理删除**。
- `memory_status`（`active/archived/superseded/pending_review`）+ `protected` 是治理视图字段，
  不污染旧工具返回。
- 向量独立存 `memory_embeddings`；内容变更按哈希识别，自动补建。
- 冲突 / 关系 / 反馈 / 治理动作各有独立表 + `events` 审计，可回滚。

---

## 九个「不显然」的设计决定

看起来都能更简单，但简单版本会在真实使用里悄悄出错。写在这里，免得以后被「优化」回原样。

| # | 决定 | 一句话理由 |
|---|---|---|
| 1 | **曝光不计分** | `retrieved` 只记录、永远贡献 0 分。若它加分，被召回最多的持续胜出 → 自我固化，那是在优化「被返回」而不是「正确」。 |
| 2 | **分档只收紧、不放松** | `critical/high/normal/low` 不会把记忆「升级」成阈值本来会拒绝的自动动作。**能触发破坏性操作的安全轴不是安全轴。** |
| 3 | **快照是逻辑 dump** | 别进程持锁时拷 `agent.db` 会得到撕裂文件；事务内 dump 在 WAL 下视图一致。恢复是单事务、全成或全不成。 |
| 4 | **过期只降权、不删除** | 过期记忆通常仍然正确，只是不再当令。扣 15% 召回分并标 `_expired`，内容与可检索性都保留。 |
| 5 | **画像只改比重、不改成分** | 权重表的 key 是成分（semantic/freshness），记忆类型名是候选属性，混在一张表里会静默空转。 |
| 6 | **同骨架不同数值 ≠ 重复** | 文本几乎一致时，唯一的差异往往正是数字，而数字正是事实本身。判重复归档等于把真实端口号当垃圾。 |
| 7 | **提炼停在候选** | 规则能找「同主题多条经历」，但写不出抽象结论；硬写就是复制原句。来源少于 3 条直接拒绝。 |
| 8 | **并发靠原子操作** | 「先查后改」之间有窗口，6 进程并发曾崩 3 个。所有加列走 `_add_column_if_missing`，`transaction()` 用 `BEGIN IMMEDIATE`。 |
| 9 | **概念桥是放大器** | 它只抬「词法已有命中」的记忆，救不了字面完全不沾边的。α=0.25 实测就让 45 题 R@1 掉一题，**不要往上调**。 |

改动下面这些必须重跑固定题库复评：

- 反馈带方向少于 2 条不生效（单条是噪声）；单次调整上限 ±25%，且是**乘性**的。
- 召回权重 clamp 在 `[0.02, 0.50]`——任何成分都不许归零，也不许一个成分主导全部；
  归一化在 clamp 之后做。
- 类型乘子（`SHAPE_PREFERENCE`）实测取值落在 `[0.85, 1.30]`，未命中为 1.0（不惩罚）。
  它按**候选**生效（每条记忆自己的类型），权重表的 key 是**成分**，两个维度不能混。
- `MIN_WEIGHT` / `MAX_WEIGHT` 一旦改动，45 题指标必变，要重跑题库复评。
- `save_memory(ttl_days=...)` 对 `preference` / `fact` / `identity` **明确拒绝并说明原因**，
  不静默忽略——静默忽略会让调用方以为设了有效期，实际永远不过期。
- 概念桥的 seed 门是 `lex >= 0.3`；改它或改融合权重，桥接的二阶效应会跟着变。
- 复评题库：45 题（检索）/ 24 题（意图）/ 38 题（知识库），跑在隔离副本上。

---

## 测试

38 个套件，全部跑真实 SQLite（临时库）、零 mock。凡是「分数应该动」的断言，都盯具体
数值是否变化——只查计数增长的测试在坏代码上也会通过。

```bash
python tests/run_tests.py                    # 全部
python tests/run_tests.py --list             # 只看会跑哪些
python tests/run_tests.py --only lexical     # 只跑指定的几项
python tests/run_tests.py --stop             # 第一个失败就停
node dsh-panel-client/test/all.mjs           # 插件与面板（Node 侧）
```

运行器只做**发现 / 执行 / 汇总**三件事，不引入任何框架、也不改写任何测试：它按文件名
发现 `tests/*_test.py`，每项跑在独立子进程里（各测试都会在进程内改 `db.DB_PATH`，
同进程串跑会互相污染），判定只看退出码。单项也能直接 `python tests/xxx_test.py`。

### 测试库隔离

`tests/_env_guard.py` 一行 `import` 就把库指向 `var/test_db/` 下的沙箱副本，
**复制生产库而不是空库**（很多基准要真实语料才有意义）。它还会主动接管「调用方把
`BIONIC_DB_PATH` 指到生产库」这种伪隔离——否则环境变量非空、看着像配好了，实际
`db.DB_PATH` 就是生产库。跑完自动清理沙箱。

**新增套件时**：第一行必须是 `import _env_guard`，且在任何 `app.*` 导入之前。
`tests/db_isolation_test.py` 会静态核对这一条。

### 主要套件

| 套件 | 覆盖 |
|---|---|
| `smoke_test` / `embedding_test` | 建库迁移、记忆读写、向量持久化 |
| `verify_mcp_split_test` | 双实例工具注册（36 + 49 = 85，无重叠无遗漏） |
| `mcp_tools_smoke_test` | 49 个「零测试调用」的工具各真调一遍，卡住返回契约 |
| `lexical_test` / `semantic_test` / `fusion_test` | 词法回想、语义守门、融合权重约束 |
| `concept_bridge_test` / `concept_bridge_diag_test` | 概念桥边界与诊断一致性 |
| `knowledge_test` | 知识库导入幂等 + 38 条检索基准 full recall |
| `conflict_triage_test` / `protection_test` / `distillation_test` | 取值冲突、分档保护、提炼溯源 |
| `snapshot_test` / `db_backup_test` / `portable_*_test` | 逻辑快照、物理备份、便携包导出与恢复状态机 |
| `admin_*_test` | 面板路由、绑定守卫、令牌认证、前端认证链路 |
| `db_isolation_test` | 全部套件都带隔离闸门且早于 `app.*` 导入 |
| `readonly_uri_test` | 只读 URI 编码（`#` `%` `?` 空格 中文）不读错库不建垃圾文件 |

完整清单（含每套项数）见 `tests/` 目录内各文件末尾的汇总行。

---

## 可移植

- **路径零硬编码**：全部基于 `Path(__file__).resolve()`，换机器只改 MCP 配置里的绝对路径。
- **库随项目走**：首次启动自动建库 + 幂等迁移，空目录直接起面板也不会 `no such table`。
- **库位置可覆盖**：`BIONIC_DB_PATH=/path/to/agent.db`。跑第二实例、隔离测试、放到同步盘
  都用它，别去改 `app/db.py`。
- **迁移安全**：只幂等加列/建表，从不 DROP / 重建，重复运行无副作用。
- **打包**：`python build_zip.py` 生成可分发的 ZIP（自动排除 `data/agent.db`、`__pycache__`）。

**换机器**：源机备份 `data/agent.db` → 目标机克隆代码、建 venv、装依赖 → 拷库或让它自建
→ 挂 MCP 配置、重启宿主。

---

## 环境要求

- Python ≥ 3.10（开发用 3.13 验证通过）
- Windows / macOS / Linux（路径全部基于 `__file__` 解析）
- 依赖见 `requirements.txt`：`mcp` 1.30.x（**必须 <2.0**，2.x 把 FastMCP 改名成了
  MCPServer，导入会失败）、`fastembed` 0.7+、`fastapi` + `uvicorn`（仅面板需要）

换向量模型：`BIONIC_EMBEDDING_MODEL=...`（换后需重新 `index_memories`）。

---

## 已知限制

- 向量检索是「SQLite 存向量 + Python 余弦扫描活跃记忆」，适合中小规模；更大要换 ANN 索引。
- 治理是确定性规则、不用 LLM：零外部成本、可复现，但语义冲突需人工裁决
  （`review_memory_conflict`）。
- 管理面板是**单用户**工具，不做多租户隔离；远程暴露前请自行评估。
