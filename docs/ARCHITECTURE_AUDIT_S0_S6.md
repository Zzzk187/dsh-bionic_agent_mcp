# BIONIC-AGENT-MCP 架构审计报告（S0–S6 完成后）

> 审计性质：**只读审计**。本报告只读取、分析和记录，未修改、创建、删除、移动任何项目源文件（唯一新建物是本报告与 `docs/ARCHITECTURE_FILE_INVENTORY.csv` 两份交付物，以及 `docs/` 目录本身）。
> 审计日期：2026-11-19
> 审计人：AI 架构审计

---

## 1. 审计范围和基线

### 1.1 基线

| 项目 | 值 |
| --- | --- |
| 项目路径 | `D:\Cache\Cache_Project\bionic-agent-mcp` |
| 分支 | `master` |
| HEAD | `862714c2ad149dd65060190d40a852effe2e7715` |
| 最新提交 | `feat: complete bionic memory system S0-S6`（2026-11-19 22:45:44 +0800，49 files changed, +14376/−197） |
| 阶段 | S0–S6 已完成并提交 |
| MCP 工具 | Core **36** + Governance **49** = **85** |
| Python | `D:\Software\Dev\Miniforge\python.exe`（3.13.13） |
| SQLite | 3.53.0 |
| tag | `v0.1.0`（2026-10-30 的旧 tag，指向 `2f36b183015e79246cb21dafedb1167b105f893d`，非本阶段产物） |

### 1.2 审计前的 Git 工作区

`git status --porcelain -uall` = **54 行，全部为 `??`（未跟踪）**，已暂存 0、未暂存 0。

| 分组 | 数量 | 路径 |
| --- | --- | --- |
| 数据残留 | 1 | `data/backup_F01_F03_before_sync.json` |
| 运行日志 | 2 | `logs/snapshot.log`、`logs/sync.log` |
| 个人调试脚本 | 51 | `scripts/dev/*.py`（52 个 .py + 1 个 .ps1，其中 2 个曾被跟踪） |

### 1.3 审计前的生产数据指纹

| 对象 | 指纹 |
| --- | --- |
| `data/agent.db` | sha256 `19D8CE986EA44F56E62908683CBE1E45EAF1A4ACD193AABDCB6ACE697D3EFA4E`，size **8581120**，mtime `2026-11-19T10:44:59.3862755Z` |
| `data/agent.db-wal` | **不存在** |
| `data/agent.db-shm` | **不存在** |
| `C:\Users\xing\.dsh\skill-mcp-manager\registry.json` | sha256 `C9591B7DE3EC8B7E836D95805BBE9F9C72ADBE7BA2F3DE5D01E7DC31F624CF67` |
| `C:\Users\xing\.dsh\dsh-memory\data\记忆.jsonl` | sha256 `F80E550D3BD64F1D2EF170F4FC10C7F9F85F663CA8068C1D8256C17BA3D63499`，76493 B |

### 1.4 审计方法与工具

- **AST 静态分析**：用 Python `ast` 遍历 71 个 Python 模块，提取 import 边、函数定义、`sqlite3.connect` 调用、硬编码路径、装饰器归属。脚本与中间产物在 `%TEMP%\aud-s06\`（`audit.py`、`audit.json`、`inventory.json`、`consumers.json`），**均在系统临时目录，未落入项目**。
- **非 SQL 手段**：目录体积用 `Get-ChildItem` 递归统计；文件行数用 Python `splitlines()`（与 `wc -l` 差 1，本报告统一用 `splitlines()` 口径）。
- **未执行**：未连接生产数据库执行任何审计 SQL；未运行任何测试（避免触碰生产库与 sidecar）；未执行任何 git 写操作。
- **实测而非推断**：关键结论均附实测证据（临时目录实验、源码行号抽查、AST 计数）。

### 1.5 审计发现的局限（如实声明）

1. 本次**未运行测试套件**。测试状态沿用 S6-FINAL-FIX 与 S6-COMMIT 的实测记录（33/33 通过）。
2. 本次**未连接生产库执行结构 SQL**。表结构结论来自源码 DDL 与 S6-FINAL 审计时对隔离副本的实测（`integrity_check=ok`、`foreign_key_check=0`、15 张表、行数已记录）。
3. 依赖图基于 **import 语句**，不解析 `getattr`/字符串拼接式动态导入；本次人工核查了 `mcp_server.py` 的延迟导入点，未发现动态模块加载。
4. 测试覆盖率统计基于**静态文本匹配**（测试文件里是否出现 `函数名(`），不是 `coverage.py` 插桩；对「通过别名/HTTP 间接调用」已人工复核并修正（见 §9.2）。

---

## 2. 完整目录树

```
bionic-agent-mcp/
├── .env.example                      118 B
├── .gitignore                        846 B
├── README.md                          45143 B
├── build_zip.py                       2823 B
├── deepseek-harness-mcp.example.yml   1561 B
├── nul                                79 B（0 字节占位，已 gitignore）
├── pyproject.toml                     1200 B
├── requirements.txt                   92 B
│
├── app/                               [38 files, 4,142,126 B]
│   ├── __init__.py                    0 B
│   ├── py.typed                       0 B
│   ├── admin_ui.html                  117036 B / 2287 行
│   ├── admin_ui.py                    65018 B / 1496 行
│   ├── mcp_server.py                  81684 B / 1738 行
│   ├── mcp_server_gov.py              1338 B / 35 行
│   ├── portable_restore.py            78743 B / 1653 行
│   ├── portable_backup.py             40132 B / 981 行
│   ├── db_backup.py                   21453 B / 549 行
│   ├── snapshots.py                   25994 B / 538 行
│   ├── db.py                          42425 B / 911 行
│   ├── knowledge.py                   59825 B / 1362 行
│   ├── memory_lifecycle.py            60416 B / 1178 行
│   ├── memory_lexical.py              35137 B / 695 行
│   ├── recall_profile.py              8967 B / 179 行
│   ├── activation_spreading.py        15566 B / 320 行
│   ├── feedback_loop.py               9947 B / 247 行
│   ├── expiry.py                      9848 B / 240 行
│   ├── protection.py                  7087 B / 164 行
│   ├── distillation.py                17574 B / 393 行
│   ├── task_resume.py                 11638 B / 282 行
│   ├── governance_rules.py            9271 B / 218 行
│   ├── governance_scoring.py          17117 B / 398 行
│   ├── governance_conflicts.py        10911 B / 252 行
│   ├── governance_service.py          54594 B / 1130 行
│   ├── governance_maintenance.py      24234 B / 549 行
│   ├── embeddings.py                  6504 B / 164 行
│   ├── star_map.py                    15743 B / 366 行
│   ├── static/
│   │   └── 3d-force-graph.min.js      1389954 B
│   ├── __pycache__/                   1530183 B / 48 files（忽略）
│   └── *.py.bak-*                     10 个备份，共约 374 KB（忽略）
│
├── tests/                             [34 files, 1,147,048 B] —— 全部已跟踪
├── scripts/                           [10 files, 398,083 B] —— 全部已跟踪
│   └── dev/                           [53 files, 208,126 B] —— 全部未跟踪
├── dsh-panel-client/                  [25 files, 174,726 B]
│   ├── cordis.patch.yml / package.json / semver-lite.mjs
│   ├── install.py / verify-install.mjs
│   ├── assets/panel-icon.svg
│   ├── lib/{client.js, index.js}
│   └── test/                          16 个 .mjs + semver-cases.json
│
├── data/                              327,639,251 B（运行时数据，gitignore 大部分）
│   ├── agent.db                       8,581,120 B ← 生产库
│   ├── agent.db.bak-*                 3 个历史备份，约 1.6 MB
│   ├── backups/                       4 个物理备份，9,175,040 B
│   ├── memory_packages/               4 个便携包，11,446,565 B（**未被 gitignore**）
│   ├── snapshots/                     6 个逻辑快照，44,439,482 B
│   ├── model_cache/                   252,139,482 B / 12 files
│   └── *.log                          3 个运行日志
│
├── logs/                              2 files（**未被 gitignore**）
├── docs/                              ← 本次审计新建（报告 + CSV）
├── dist/                              bionic-agent-mcp.zip 680,748 B
├── build/                             2,238,156 B / 29 files（pip 构建残留，已 gitignore 但未清理）
└── bionic_agent_mcp.egg-info/         1,464 B / 6 files（同上）
```

**规模汇总**：源码 + 测试 + 脚本 + 面板 = **149 个文件 / 38,402 行 / 1,749,535 B**。

---

## 3. 目录职责分析

### 3.1 目录职责表

| 目录 | 实际职责 | 职责是否单一 | 主要问题 |
| --- | --- | --- | --- |
| `app/` | 全部业务代码 + 前端单页 + 静态资源 + 85 个 MCP 工具 + 75 个 HTTP 路由 | **否** | 5 类职责混在一起（见 §3.2） |
| `app/static/` | 唯一静态资源（星图 JS，1.39 MB） | 是 | 合理 |
| `tests/` | 33 个测试套件 + 统一入口 + 入口自测 | 是 | 合理，分层见 §9 |
| `scripts/` | 运维（sync/snapshot/backup/mirror/recall）+ 导入（memory/knowledge）+ **评测（eval_×3）** | **否** | 三类职责混放（见 §3.3） |
| `scripts/dev/` | 53 个一次性调试脚本 | — | 全部未跟踪，**不属于交付物** |
| `dsh-panel-client/` | DSH 面板客户端插件（Node 侧，独立发布单元） | 是 | 合理，与 Python 侧解耦 |
| `data/` | 生产库 + 三类备份产物 + 模型缓存 + 运行日志 | **否** | 数据、备份、缓存、日志混在一个目录（见 §3.4） |
| `docs/` | 本次审计新建 | 是 | 此前不存在 |
| `dist/`、`build/`、`*.egg-info/` | 打包产物 | — | 后两者未清理 |

### 3.2 `app/` 内部职责混杂（最核心的目录问题）

`app/` 一个目录装了 5 类东西：

1. **MCP 接口层**：`mcp_server.py`（85 工具）、`mcp_server_gov.py`（转发）
2. **HTTP 接口层**：`admin_ui.py`（75 路由）、`admin_ui.html`（2287 行单页）
3. **业务算法层**：`memory_lexical.py`、`recall_profile.py`、`activation_spreading.py`、`governance_scoring.py`…
4. **数据访问层**：`db.py`、`knowledge.py`、`memory_lifecycle.py`（混了算法 + SQL）
5. **备份恢复子系统**：`portable_backup.py` + `portable_restore.py`（2634 行）+ `db_backup.py` + `snapshots.py`

证据：`app/mcp_server.py` 中 **26 个工具函数体内直接写 SQL**（66 处 `execute`），即第 1 类和第 4 类在同一函数内（详见 §6.4）。

### 3.3 `scripts/` 三类职责混放

| 类别 | 文件 | 是否应进发布包 |
| --- | --- | --- |
| 运维 | `sync_all.py`、`snapshot_db.py`、`physical_backup.py`、`check_mirror.py`、`recall.py` | 是 |
| 导入 | `import_dsh_memory.py`、`import_dsh_knowledge.py` | 是 |
| **评测** | `eval_lexical.py`、`eval_local.py`、`eval_recall.py` | **否**——靠 `build_zip.py:36 _excluded()` 的 `name.startswith("eval_")` 排除 |

**问题**：评测脚本与运维脚本靠**文件名前缀**区分发布与否，而不是靠目录。新增一个评测脚本若忘了用 `eval_` 前缀，就会被误打进发布包。

### 3.4 `data/` 四类数据混放

| 类别 | 内容 | gitignore | 备份覆盖 |
| --- | --- | --- | --- |
| 生产数据 | `agent.db` | ✅ | 逻辑快照 + 物理备份 |
| 备份产物 | `backups/`（物理）、`snapshots/`（逻辑）、`memory_packages/`（便携包） | ✅ / ✅ / **❌** | 部分 |
| 模型缓存 | `model_cache/`（252 MB） | ✅ | 不需要 |
| 运行日志 | `admin_ui.log`、`panel-*.log`（被忽略） | ✅ | 不需要 |
| **审计残留** | `backup_F01_F03_before_sync.json` | **❌** | — |

**问题**：`data/memory_packages/`（11.4 MB，4 个便携包）与 `data/backup_*.json` 未被 `.gitignore`，正是 54 个未跟踪文件中「数据残留 1 个」的来源。

### 3.5 其他目录问题

| 编号 | 问题 | 证据 | 级别 |
| --- | --- | --- | --- |
| A-1 | `build/`（2.24 MB / 29 文件）与 `bionic_agent_mcp.egg-info/`（6 文件）是 pip 构建残留，已在 `.gitignore` 但物理存在未清理 | `build/lib/app/*` 下有旧的 app 模块副本 | P3 |
| A-2 | `app/` 下 10 个 `.bak-*` 源码备份，共约 374 KB | 其中 `mcp_server.py.bak-20261102-review` 65041 B 与现役 81684 B 同量级，易被 grep/IDE 误认为现役代码 | P3 |
| A-3 | 根目录 `nul`（PowerShell 重定向误产物） | 已 gitignore | P3 |
| A-4 | 无 `docs/` 目录，全仓 markdown 只有 `README.md` 一个（45143 B / 634 行），承载了功能总览 + 快速开始 + 面板说明 + 项目结构 + 记忆模型 + 9 条设计决定 + 测试表 + 已知限制 | 无架构文档、无阶段交付文档入库（阶段报告都在 workspace 未入库） | P2 |
| A-5 | `app/static/3d-force-graph.min.js` 1.39 MB 单文件在 Python 包内 | `pyproject.toml:26` 已声明 `static/*`，`build_zip.py` 也收录 | P3（合理，但注意 wheel 会大 1.39 MB） |

---

## 4. 模块依赖关系

### 4.1 依赖热点（AST 精确统计，含 app/tests/scripts）

**fan-in（被引用最多）**：

| 模块 | 被引用次数 | 引用者示例 |
| --- | --- | --- |
| `app/db.py` | **49** | 几乎全部业务模块 + 30 个测试 + 2 个脚本 |
| `app/mcp_server.py` | 21 | admin_ui、mcp_server_gov、memory_lifecycle、测试、`scripts/dev/*` |
| `app/memory_lifecycle.py` | 21 | admin_ui、distillation、mcp_server、recall_profile、测试 |
| `app/embeddings.py` | 17 | admin_ui、distillation、governance_*、knowledge、mcp_server |
| `app/knowledge.py` | 14 | mcp_server、memory_lexical、portable_backup、portable_restore、check_mirror |
| `app/governance_rules.py` | 8 | activation_spreading、governance_maintenance/scoring/service、memory_lifecycle |
| `app/governance_scoring.py` | 7 | activation_spreading、governance_maintenance/service、memory_lifecycle、测试 |
| `app/memory_lexical.py` | 7 | mcp_server、memory_lifecycle、concept_bridge*_test、dev 脚本 |
| `app/portable_backup.py` | 7 | admin_ui、mcp_server、portable_restore、3 个测试 |
| `app/snapshots.py` | 7 | admin_ui、mcp_server、portable_backup、portable_restore、snapshot_db |

**fan-out（依赖最多，仅 app 内）**：

| 模块 | 依赖数 | 说明 |
| --- | --- | --- |
| `app/mcp_server.py` | **17** | MCP 层直接依赖 17 个业务模块 |
| `app/admin_ui.py` | **15** | HTTP 层直接依赖 15 个业务模块 |
| `app/memory_lifecycle.py` | **13** | 生命周期层依赖 13 个 |
| `app/governance_maintenance.py` | 6 | |
| `app/governance_service.py` | 6 | |

**结论**：`mcp_server.py`（17）与 `admin_ui.py`（15）都是「薄接口层」的理想形态应该是 fan-out 1~3（只依赖一个应用服务门面），实际是 15~17，**说明缺少应用服务层，接口层直接裸露调用业务模块**。

### 4.2 循环依赖（1 个 SCC，4 个模块）

Tarjan 算法找出唯一 SCC：`{app/distillation.py, app/mcp_server.py, app/memory_lifecycle.py, app/recall_profile.py}`。

**逐边判定（模块级 import vs 函数内延迟 import）**：

| 边 | 模块级 | 延迟 | 证据 |
| --- | --- | --- | --- |
| `distillation → memory_lifecycle` | ✅ | ❌ | `app/distillation.py:35 from app.memory_lifecycle import ALLOWED_RELATIONS` |
| `memory_lifecycle → recall_profile` | ✅ | ❌ | `app/memory_lifecycle.py:28` |
| `recall_profile → memory_lifecycle` | ❌ | ✅ | `app/recall_profile.py:133 from app.memory_lifecycle import RECALL_WEIGHTS as DEFAULT_WEIGHTS`（函数体内） |
| `memory_lifecycle → mcp_server` | ❌ | ✅ | `app/memory_lifecycle.py:809 from app import mcp_server as _mcp  # local import to avoid cycle at module load` |
| `mcp_server → memory_lifecycle` | ✅ | ✅ | `:19`、`:50` 模块级 + 其它延迟 |
| `mcp_server → distillation` | ✅ | ❌ | `:25` |

**判定**：**真实可加载路径没有死锁**——两种导入顺序实测都成功（`import memory_lexical → memory_lifecycle` 与反向均 OK）。这个 SCC 是「延迟导入」与「模块级导入」混在同一环里被静态分析器看见的结果。

**但它仍是结构信号**：
1. `recall_profile` 为了避开环，把「默认权重表」作为 `base=` 参数由调用方传入（`:130-131` docstring 自述这是为了避免 circular import）——**这是一处「为绕环而生的 API 妥协」**，虽然不是 bug，但把「默认值来自哪里」的知识推给了调用方。
2. `memory_lifecycle.py:809` 的 `from app import mcp_server as _mcp` 让**生命周期层反向依赖 MCP 接口层**，这是明确的反向依赖（见 §4.3）。
3. `distillation.py:35` 只为拿一个常量 `ALLOWED_RELATIONS` 就模块级 import 整个 `memory_lifecycle`。

### 4.3 不合理反向依赖（3 处）

| 编号 | 反向边 | 位置 | 为什么不合理 | 级别 |
| --- | --- | --- | --- | --- |
| B-1 | **生命周期层 → MCP 接口层** | `app/memory_lifecycle.py:809 from app import mcp_server as _mcp` | 抽象层级倒挂：领域逻辑依赖传输层。修改 MCP 层会波及领域层 | P2 |
| B-2 | **检索算法层 → 知识库层** | `app/memory_lexical.py:35 from app.knowledge import _SYNONYM_GROUPS, _segments` | 词法引擎（纯算法）依赖知识库模块的**两个私有函数**（`app/knowledge.py:525 _SYNONYM_GROUPS`、`:555 def _segments`）。`:27-28` docstring 自认「直接从 `app.knowledge` 借。那份是照抄…」 | P2 |
| B-3 | **备份模块 → 快照模块** | `app/portable_backup.py` / `app/portable_restore.py` → `snapshots.py`（fan-in 7 中含这两个） | 便携包与逻辑快照是同级备份方案，互相依赖使「三条备份路径互不重叠」的边界变模糊 | P3 |

### 4.4 重复实现（真重复 5 组 21 份 + 3 组同名不同义）

**必须区分「真重复」与「MCP 包装层同名转发」**——后者是设计必需，不是重复代码。

**真重复（建议收敛）**：

| 编号 | 函数 | 份数 | 位置 |
| --- | --- | --- | --- |
| B-4 | `_now()` | **8** | `app/distillation.py:48`、`app/expiry.py:54`、`app/governance_scoring.py:63`、`app/memory_lifecycle.py:119`、`app/portable_restore.py:78`、`app/snapshots.py:78`、`app/star_map.py:37`、`app/task_resume.py:43` |
| B-5 | `_parse_ts()` | **5** | `app/admin_ui.py:106`、`app/expiry.py:58`、`app/governance_maintenance.py:40`、`app/governance_service.py:57`、`app/task_resume.py:47` |
| B-6 | `_readonly_uri()` | **2** | `app/db_backup.py:139`、`app/portable_backup.py:205`（逐行等价，实测源码见 §5.3） |
| B-7 | `_sidecar_snapshot()` | 2 | `app/db.py:200`、`app/portable_backup.py:178` |
| B-8 | `_wal_has_content()` | 2 | `app/db.py:185`、`app/portable_backup.py:190` |

**同名但语义不同（不能合并，只能改名）：**

| 编号 | 函数 | 位置 | 差异 |
| --- | --- | --- | --- |
| B-9 | `_json()` | `app/governance_service.py:74` vs `app/memory_lifecycle.py:124` | **签名与行为都不同**：前者 `_json(value)` 只做 `json.dumps`（异常回 `"{}"`）；后者 `_json(x, default="{}")` 还会把 `None`/`""` 转成 default、非 dict/list 原样返回。**改名为 `dumps_json` / `coerce_json` 即可**，合并会改行为 |
| B-10 | `_event()` | `app/governance_service.py:91` vs `app/mcp_server.py:71` | **签名不同**：前者 `_event(conn, event_type, detail, task_id=None)` 复用调用方连接（在事务内）；后者 `_event(event_type, detail, task_id=None)` 自开连接。**不能合并**——合并会让服务层多开一个连接、破坏事务边界 |
| B-11 | `_run()` | `app/admin_ui.py:178` vs `app/mcp_server.py:1725` | **完全不同的功能**：前者是 HTTP 路由的「调后端 + 转 404/400」，后者是「确保 DB 初始化 + 起 MCP server」。纯同名巧合，**改名为 `call_backend` / `serve` 即可** |

**合法同名（不是重复代码，不得当作重复处理）**：

- **29 组 MCP 包装层同名转发**：`@mcp.tool()` 函数与底层服务函数同名，如 `apply_distillation`（`app/distillation.py:303` vs `app/mcp_server.py:978`）、`archive_memory_governance`（`:836` vs `:832`）、`find_stuck_tasks`（`:174` vs `:562`）。这是 MCP 协议要求「工具函数名 = 工具名」的正常结果。
- **`_run_cli` 3 份**（`app/admin_ui.py:1470`、`app/mcp_server.py:1730`、`app/mcp_server_gov.py:29`）：`pyproject.toml:29-31` 三个 console entry 各自需要，**不能合并**。

### 4.5 模块依赖图（文字描述）

```
                       ┌──────────────────────────────────────┐
                       │  接口层（fan-out 15~17）              │
                       │  mcp_server.py (85 tools)  ← mcp_server_gov.py
                       │  admin_ui.py (75 routes) + admin_ui.html
                       └────────────┬─────────────────────────┘
                                    │ 直达 15~17 个业务模块（无应用服务门面）
        ┌───────────────────────────┼───────────────────────────┐
        ▼                           ▼                           ▼
 ┌─────────────┐          ┌─────────────────┐        ┌──────────────────┐
 │ 检索/记忆     │          │ 治理             │        │ 备份恢复          │
 │ lifecycle 39 │          │ rules/scoring   │        │ portable_* 2634  │
 │ lexical 0SQL │          │ conflicts       │        │ db_backup        │
 │ recall_profile│         │ service 53 SQL  │        │ snapshots        │
 │ activation   │          │ maintenance     │        └────────┬─────────┘
 └──────┬───────┘          └────────┬────────┘                 │
        │                           │                          │
        └───────────┬───────────────┴──────────────────────────┘
                    ▼
         ┌──────────────────────┐
         │ 基础设施层            │
         │ db.py (fan-in 49)    │  ← 唯一迁移入口
         │ embeddings.py (17)   │
         │ knowledge.py (14)    │
         └──────────────────────┘
```

---

## 5. 数据库架构分析

### 5.1 连接入口与统一性

`sqlite3.connect` 全仓 **28 处**（含 2 处注释误命中），分类：

| 类别 | 处数 | 位置 | 是否规范 |
| --- | --- | --- | --- |
| `db.connect()` / `db.transaction()` / `db.read_transaction()` | 47 处（12 模块） | 业务模块统一入口 | ✅ |
| `app/db.py` 内部裸 connect | 6 | `:287`（探测）、`:427`/`:809`（`with closing`）、`:831`/`:852`/`:891`（迁移） | ✅ 基础设施自身 |
| `app/db_backup.py` | 3 | `:204`、`:331`/`:335`（src/dst） | ✅ 有 try/finally |
| `app/portable_backup.py` | 3 | `:272`、`:409`/`:410` | ⚠️ `:409` URI 未编码 |
| `app/portable_restore.py` | 12 | `:229`、`:396`、`:654`、`:795-796`、`:918-919`、`:1062`/`:1080`、`:1323`、`:1416-1417` | ⚠️ 8 处 URI 未编码 |
| `scripts/import_dsh_memory.py` | 1 | `:104` | ✅ |

**只读/读写区分**：只读路径统一用 `?mode=ro`（可选 `&immutable=1`）；写入路径统一 `db.transaction()`（`BEGIN IMMEDIATE`）。**分类清晰，无「以为只读实际可写」的连接**。

**资源释放**：所有裸 connect 均有 `try/finally` 关闭（抽查 `:228-236`、`:395-403`、`:653-661`、`:794-802`、`:917-924`、`:1322-1330`、`:1415-1423` 均见 `finally: src.close(); dst.close()`）。

### 5.2 迁移入口

**`app/db.py` 是唯一迁移入口**：`:434/:444/:455/:464` 四张基础表 → `:489 memory_links` → `:502 memory_embeddings` → `:536 _add_column_if_missing` → `:557 _migrate_phase5`（含 `:591/:605/:626` 三张治理表）→ `:678 _migrate_phase6`（`:699/:713`）→ `:736 _migrate_phase7`（`:756/:774/:789` 三张知识表）→ `:806 init_db_phase5`。

**唯一越权建表**（P2）：

```python
# app/snapshots.py:91
conn.execute("""CREATE TABLE IF NOT EXISTS db_snapshots (...)""")
```

`db_snapshots` 表在 `snapshots.py` 内自建，**不在 `app/db.py` 的 `REQUIRED_TABLES`（14 张）里**。后果：`app/db.py:226 probe_database_state()` 的完整性判定看不见这张表——若一个库里只有 `db_snapshots` 而缺其它表，探测会正确报不完整；但若 `db_snapshots` 表结构在别处被改，探测无法发现。

### 5.3 只读 URI 编码缺陷（P1，实测可复现）

**代码位置**：

- `app/portable_backup.py:409`（`_dump_db_logical` 内）：`src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)`
- `app/portable_restore.py` **8 处**：`:229`（`_db_fingerprint`）、`:396`（`inspect_restore_target`）、`:654`（`preview_restore`）、`:795`（`_create_protective_backup`）、`:918`（`_validate_snapshot_against_db`）、`:1323`（`verify_restored_state`）、`:1417`（`rollback_restore`）

**对照：已经正确编码的三处实现**（逐行等价，可直接复用）：

```python
# app/db_backup.py:139
def _readonly_uri(path: Path) -> str:
    p = str(Path(path).resolve()).replace("\\", "/")
    if not p.startswith("/"):
        p = "/" + p
    return "file:" + quote(p, safe="/:") + "?mode=ro"

# app/portable_backup.py:205
def _readonly_uri(db_path, *, immutable: bool = False) -> str:
    p = str(Path(db_path).resolve()).replace("\\", "/")
    if not p.startswith("/"):
        p = "/" + p
    uri = "file:" + quote(p, safe="/:") + "?mode=ro"
    if immutable:
        uri += "&immutable=1"
    return uri
```

**实测危害**（在系统临时目录造 6 个特殊字符库名逐一验证）：

| 库名 | 裸 URI（现状） | 编码后 |
| --- | --- | --- |
| `a b.db` | ✅ OK | ✅ OK |
| `中文.db` | ✅ OK | ✅ OK |
| `amp&1.db` | ✅ OK | ✅ OK |
| `plus+1.db` | ✅ OK | ✅ OK |
| `semi;1.db` | ✅ OK | ✅ OK |
| `at@1.db` | ✅ OK | ✅ OK |
| **`hash#1.db`** | ❌ `OperationalError: no such table: t`，**并在目录里凭空产生一个 0 字节的 `hash` 文件** | ✅ OK |
| **`pct%20.db`** | ❌ `unable to open database file` | ✅ OK |

**影响范围**：`#` 被当成 URI 片段分隔符截断、`%` 被当成百分号编码起始。备份包路径、恢复目标路径都可能来自用户输入（面板、MCP 参数），因此含 `#`/`%` 的路径会让**只读校验静默读到空库**（`no such table` 被上层 `except` 吞掉后可能被误判为「库为空/无数据」），并**在用户目录里留下垃圾文件**。

**推荐方案**：把 `app/portable_backup.py:205` 的 `_readonly_uri` 提为公共实现（如 `app/db.py`），11 处全部改调它。**不迁移数据库、不影响 MCP 接口**。

### 5.4 WAL 使用一致性

- 所有连接统一 `PRAGMA journal_mode=WAL` + `synchronous=NORMAL` + `foreign_keys=ON`。
- `db.connect()`/`transaction()` 与 `portable_restore.py` 的 `_connect_at`/`_transaction_at` **逐行重复**（见 §5.6）。
- **只读探测的 WAL 感知**（S6-FIX-03/04 成果）：`app/db.py:276-282` 的 `use_immutable = not _wal_has_content(target)` —— 有未合并 `-wal` 时退 `mode=ro`，否则 `immutable=1` 零副作用。`app/portable_backup.py:221 read_database_metadata` 同策略。**这套策略有反证实验支撑**（强制 immutable 时读到旧值 0，正确实现读到 55）。

### 5.5 生产库与测试库隔离

| 手段 | 覆盖 | 评价 |
| --- | --- | --- |
| `BIONIC_DB_PATH` 环境变量 | `app/db.py:10-13` | ✅ 主机制 |
| 测试内改 `db.DB_PATH` | 12 个测试（`semantic_test`、`smoke_test`、`embedding_test`、`feedback_loop_test`、`fusion_test`、`cross_device_test`、`import_dsh_memory_test`、`init_db_probe_test`、`portable_backup_test`、`portable_restore_mcp_test`、`portable_restore_test`、`snapshot_test`） | ⚠️ 3 个不恢复（见 §9.4） |
| `Connection.backup()` 副本 | 全部 DB 实验 | ✅ |
| 只读 URI 指纹 | `db_backup_test`、`admin_host_guard_test`、`portable_backup_metadata_test` | ✅ |

### 5.6 事务边界问题

| 编号 | 问题 | 证据 | 级别 |
| --- | --- | --- | --- |
| C-1 | `portable_restore.py` 的 `_connect_at`/`_transaction_at` 与 `db.connect()`/`transaction()` 逐行重复 | `:1061`、`:1079` vs `app/db.py:442`、`:469`；唯一差别是路径来源（全局 vs 参数） | P2 |
| C-2 | schema 检测逻辑 **5 处重复** | `app/db.py:296`、`:327`、`:545`；`app/portable_backup.py:421`、`:426`、`:452`；`app/portable_restore.py:927`、`:929`、`:1115`、`:1121`；`app/snapshots.py:85`、`:223`；`scripts/snapshot_db.py:102` | P2 |
| C-3 | `BEGIN IMMEDIATE` 只在 2 处（`app/db.py:518`、`app/portable_restore.py:1089`），其余写路径走 `db.transaction()` 正确继承 | — | ✅ 正常 |
| C-4 | 硬编码盘符路径 3 处，**全部无害** | `app/knowledge.py:49 DSH_MEMORY_ROOT = os.getenv("DSH_MEMORY_ROOT", r"C:\Users\xing\.dsh\dsh-memory")`（唯一真实默认值，且可被环境变量覆盖）；`app/portable_backup.py:38`、`:524` 是**纯注释** | ✅ |

### 5.7 dsh-memory 单向权威约束

- `dsh-memory` 是唯一权威源，MCP DB 是镜像 + 治理库。
- 单向写入**只**在 `scripts/import_dsh_memory.py`（UPDATE 只写内容列 6 列，绝不碰 `access_count`/`last_accessed_at`/`status`/`memory_status`）与 `scripts/sync_all.py`。
- **本次未发现任何反向写入路径**：`app/` 下无任何代码写 `C:\Users\xing\.dsh\dsh-memory\`（只有 `app/knowledge.py` 读它做知识索引、`scripts/import_*.py` 读它做导入）。✅

---

## 6. 85 个 MCP 工具盘点

### 6.1 归属与物理位置

| 项 | 值 |
| --- | --- |
| `@mcp.tool()`（Core） | **36** |
| `@mcp_gov.tool()`（Governance） | **49** |
| 总计 | **85** |
| 物理位置 | **全部在 `app/mcp_server.py`（1738 行）** |
| `app/mcp_server_gov.py` | 仅 35 行：`from app import mcp_server` + `_run(mcp_server.mcp_gov, "stdio")` |
| 同实例内重名 | 无 |
| core/gov 交集 | **空** |

> **重要澄清**：S6 审计期间曾因为「只 grep `mcp_server.py` 里的 `@mcp.tool()`」而误以为 85 个都在 core。实际是同一文件里定义了两个 `FastMCP` 实例的装饰器。`mcp_server_gov.py` 不做任何逻辑，只起第二个 stdio 进程。

### 6.2 命名规范

前缀分布：`get` 13 / `list` 7 / `find` 4 / `restore` 4 / `run` 3 / `create` 3 / `record` 3 / `save` 2 / `set` 2 / `search` 2 / `supersede` 2 / `archive` 2 / `recall` 2 / `review` 2 / `summarize` 2 / `apply` 2，其余各 1。

**一致性问题**：
1. **`recall_all` 在 Core、`recall_memories` 在 Governance**——同一个「检索」概念分属两个 MCP 实例，用户必须知道去哪个实例找。
2. `get_memory`（Core，单条）vs `get_memory_details`（Gov，完整视图）——语义相近、不在同一实例。
3. `search_memories`（Core，关键词）vs `semantic_search_memories`（Core，向量）vs `recall_memories`（Gov，融合）——三个检索入口，命名未体现语义层次。

### 6.3 docstring 与返回形态

| 项 | 值 |
| --- | --- |
| 有 docstring 的工具 | **85 / 85** ✅ |
| docstring < 40 字符 | **56**（最短：`list_tasks`、`get_task` 各 10 字符；`restore_memory` 11；`complete_task` 12；`get_memory_links`、`archive_memory` 各 13） |
| 返回形态 | dict 字面量 66、`_tool_result`/`_tool_error` 20、list 字面量 8、`result` 6、`out` 3、`_memory_out(row)` 3、其余零散 |

**问题**：返回形态 7 种并存 → 调用方（LLM）无法从 schema 得知「成功/失败的统一形状」。`_tool_result`/`_tool_error` 只被 20 个工具使用，其余 65 个自行拼 dict，**失败时可能返回 `{"error": ...}` 也可能返回 `{"ok": False, "message": ...}`**。

### 6.4 最大架构问题：26 个工具函数体内直接写 SQL（P1）

`app/mcp_server.py` 共 **67 处 `execute`**，其中 **66 处在工具函数体内**，只有 1 处（`_event` `:71-76`）在辅助函数里。

| 工具 | 行区间 | execute 次数 |
| --- | --- | --- |
| `supersede_memory` | L341-369 | **6** |
| `handoff_task` | L517-541 | **6** |
| `save_memory` | L100-167 | 4 |
| `link_memories` | L296-317 | 4 |
| `save_stage_snapshot` | L497-514 | 4 |
| `set_memory_expiry` | L170-195 | 3 |
| `update_memory` | L393-420 | 3 |
| `restore_memory` | L438-451 | 3 |
| `create_task` | L454-471 | 3 |
| `embedding_status` | L600-623 | 3 |
| `backfill_memory_types` | L1246-1292 | 3 |
| `search_memories` | L210-259 | 2 |
| `find_related_memories` | L262-292 | 2 |
| `get_memory_links` | L321-337 | 2 |
| `archive_memory` | L423-435 | 2 |
| `get_task` | L485-494 | 2 |
| `complete_task` | L574-584 | 2 |
| `list_events` | L587-597 | 2 |
| `index_memories` | L634-661 | 2 |
| `semantic_search_memories` | L664-724 | 2 |
| `get_memory` | L373-390 | 1 |
| `list_tasks` | L474-482 | 1 |
| `assess_memory_protection` | L929-943 | 1 |
| `summarize_protection` | L947-959 | 1 |
| `classify_memory` | L1394-1407 | 1 |
| `get_memory_lifecycle` | L1453-1469 | 1 |

**为什么是 P1 而不是 P2**：这 26 个工具**绕过了所有业务层的校验、审计与事件记录**。举例：
- `save_memory` 直接 INSERT，不经 `governance_service` 的事件记录；
- `supersede_memory` 6 处 execute 在一个工具里完成「建新版 + 标旧版 + 建关系」，事务边界由 `db.transaction()` 提供，但**逻辑分散在工具里**，`governance_service.supersede_memory_from_conflict` 有另一套实现；
- `archive_memory`（Core）与 `archive_memory_governance`（Gov）是两条不同的归档路径，前者直写 SQL、后者经服务层校验。

**不能因为「85 个太多」就建议合并**（任务书明确要求）。这里的建议是**分层**，不是合并：
- Core 里 26 个直写 SQL 的工具 → 下沉到 `app/memory/*` 的 store 层，工具函数只做参数校验 + 调用；
- **工具名、参数、返回结构保持不变**（MCP 契约不变）；
- 分期实施：先做 6 个 execute 的 `supersede_memory`/`handoff_task` 验证模式，再批量迁移。

### 6.5 测试覆盖（静态统计 + 人工复核）

| 类别 | 数量 | 说明 |
| --- | --- | --- |
| 被测试真实调用（`name(` 出现） | **39** | 最多：`save_memory` 10 个测试、`recall_memories` 9 个 |
| MCP 包装层未被直接调用 | **46** | 其中 **24 个底层服务函数有覆盖**（如 `score_memories`←`score_all`、`create_db_snapshot`←`create_snapshot`、`validate_memory_package`←`validate_package_archive`） |
| **完全无任何覆盖** | **22** | 见下表 |

**22 个零覆盖工具**（P2）：

| 域 | 工具 |
| --- | --- |
| 记忆 CRUD | `search_memories`、`find_related_memories`、`get_memory`、`update_memory`、`get_memory_links` |
| 任务 | `list_tasks`、`get_task`、`list_events` |
| 向量 | `index_memories`、`get_vector_consistency` |
| 治理 | `get_memory_governance_status`、`supersede_memory_from_conflict`、`get_governance_action`、`plan_memory_cleanup`、`get_governance_run` |
| 视图 | `get_memory_details`、`get_related_memories`、`list_memory_relations`、`get_memory_lifecycle` |
| 整理 | `find_duplicate_memories`、`run_memory_consolidation`、`get_memory_usage_stats` |

> 注意：`verify_mcp_split_test.py` 里对这 22 个名字的**出现只是清单核对**（`CORE_EXPECTED`/`GOV_EXPECTED` 元组），不是行为测试。本统计已排除该文件，避免虚假覆盖。

### 6.6 Core / Governance 职责边界

**边界清晰的部分**：只读诊断类（`score_memories`、`check_vector_consistency`、`summarize_protection`）、快照类（`create_db_snapshot`/`restore_db_snapshot`）、便携包恢复类（`preview/restore/delete_memory_package`）全部在 Gov，与「治理与危险操作需手动挂载」的设计一致。✅

**边界模糊的 4 组**（P2）：
1. **检索**：`recall_all`（Core，记忆+知识）vs `recall_memories`（Gov，仅记忆）
2. **归档**：`archive_memory`（Core，直写）vs `archive_memory_governance`（Gov，服务层校验 + 可回滚）
3. **恢复**：`restore_memory`（Core，直写）vs `restore_memory_governance`（Gov）
4. **快照**：`restore_db_snapshot`（Gov）与便携包 `restore_memory_package`（Gov）都能改库，但前者「恢复后不重建向量」、后者有状态机，语义不同却名字相近。

**不建议合并**（任务书要求说明调用场景）：`archive_memory` 是 Core 的快捷路径（LLM 日常归档用），`archive_memory_governance` 带服务层校验与审计、可 `rollback_governance_action`。两者的**爆炸半径不同**，合并会让 Core 工具也具备回滚能力（增加 Core 的复杂度与风险面）。建议保留两者，但在 docstring 里写明「快捷 vs 受控」。

---

## 7. 备份恢复架构分析

### 7.1 三条备份路径的职责边界（清晰 ✅）

| 路径 | 模块 | 产物 | 触发 | 恢复机制 |
| --- | --- | --- | --- | --- |
| **逻辑快照** | `app/snapshots.py` | `data/snapshots/snap-*.json` | 每日 05:00（`scripts/snapshot_db.py`）+ 手动 | `restore_snapshot` + `PRAGMA defer_foreign_keys=ON` + 提交前 `foreign_key_check` 必须 0 |
| **物理备份** | `app/db_backup.py` | `data/backups/agent_phys_*.db` | 每日 05:00（同一脚本）+ CLI | `verify_backup`（integrity + FK）→ 文件替换 |
| **便携包** | `app/portable_backup.py` + `app/portable_restore.py` | `data/memory_packages/*.zip` | 手动（MCP/面板） | preview → confirm（一次性 token，TTL 300s）→ execute（异常自动回滚） |

**互不重叠的实证**：三种产物格式完全不同（JSON / 二进制 .db / ZIP），写入目录不同，恢复入口不同。物理备份用 `Connection.backup()` 而非复制 WAL；逻辑快照是「逻辑 dump 非文件拷贝」；便携包是「源码 + 可选 DB dump + checksums」。

### 7.2 六模块公开 API 清单

| 模块 | 公开函数（行号） |
| --- | --- |
| `app/portable_backup.py` | `calculate_sha256` :104、`read_database_metadata` :221、`build_manifest` :461、`export_portable` :504、`validate_package_archive` :688、`inspect_package` :897、`list_packages` :927、`delete_package` :945 |
| `app/portable_restore.py` | `get_restore_status` :162、`list_restore_history` :172、`compute_current_bindings` :346、`inspect_restore_target` :381、`preview_restore` :496、`confirm_restore` :732、`verify_restored_state` :1307、`rollback_restore` :1401、`execute_restore` :1488 |
| `app/db_backup.py` | `BackupError` :71、`backup_dir` :78、`is_backup_name` :87、`list_backups` :119、`sha256_file` :165、`verify_backup` :176、`create_physical_backup` :252、`prune_backups` :462 |
| `app/snapshots.py` | `create_snapshot` :109、`list_snapshots` :185、`restore_snapshot` :403、`check_vector_consistency_after_restore` :486、`prune_snapshots` :511 |
| `scripts/physical_backup.py` | `main` :49（CLI：`--keep`/`--dry-run`/`--list`/`--verify`/`--prune-only`） |
| `scripts/snapshot_db.py` | `main` :87（05:00 计划任务入口，同时做逻辑 + 物理） |

### 7.3 调用链

```
计划任务 DSH-库快照（05:00）
  └─ Invoke-DshTask.ps1 -Task Snapshot
      └─ python scripts/snapshot_db.py --keep 14
          ├─ db_backup.create_physical_backup(keep=7, dry_run)   ← :51 物理（先）
          │    ├─ sqlite3.connect(只读 URI) → Connection.backup()
          │    ├─ verify_backup()（integrity + FK，失败即删临时文件）
          │    └─ os.replace() 原子改名 → prune_backups(protect=[新备份])
          └─ snapshots.create_snapshot(...)                       ← :119 逻辑（后）
               └─ snapshots.prune_snapshots(keep=14)              ← :137
```

**物理排在逻辑之前的理由**（源码注释写明）：逻辑 dump 失败往往正是因为库有异常，那时更需要真镜像。且 `_run_physical` 把任何异常收敛成 `{"ok": False, "message": ...}`，**独立失败域**——物理失败不会让逻辑快照失败，反之亦然。

### 7.4 校验、manifest、路径约束（扎实 ✅）

**便携包 21 项校验器**（`validate_package_archive`）：`zip_open`、`unique_paths`、`case_conflicts`、`path_safety`、`no_sensitive`、`size_limits`、`compression_ratio`、`crc`、`manifest_present`、`manifest_parse`、`manifest_fields`、`format_supported`、`type_matches`、`checksums_present`、`checksums_parse`、`sha256_match`、`manifest_checksum`、`manifest_zip_set_match`、`checksums_cover`、`required_present`、`embedding_info`。

**路径约束**：`_safe_rel`（`:126`）逐段拒 `..`/`.`/空段；`_is_sensitive`（`:140`）配 `SENSITIVE_PARTS`（`:59`）/`SENSITIVE_DIRS`（`:63`）/`SENSITIVE_NAMES`（`:65`）；四个尺寸上限（`:52-55`）；`delete_package` 必须 `bionic-memory-` 前缀 + `target.relative_to(p.resolve())` 二次校验。**未发现路径穿越。**

**物理备份保留策略**：`is_backup_name`（`:87`）正则整串锚定 `^agent_phys_(\d{8})_(\d{6})(?:_(\d{2,3}))?\.db$`；`prune_backups` 四道边界（名字匹配 / 拒符号链接 / `resolve().parent != base_real` / protect 集合）。

### 7.5 原子性与回滚

| 机制 | 位置 | 说明 |
| --- | --- | --- |
| 先临时名再原子改名 | `db_backup.py` `.partial-<uuid>` → `os.replace()` | 名字刻意不匹配 `FILE_RE`，残留不会被当备份 |
| 恢复一次性令牌 | `portable_restore.py:270 _issue_token` / `:293 _verify_token` | 写 `_token_key`/`_token_bindings`/`_token_consumed=False`，防重放 + `expire_at` + 逐字段绑定比对 |
| 恢复前保护快照 | `portable_restore.py` `_create_protective_backup` | 失败即中止恢复 |
| 异常自动回滚 | `execute_restore` | `file_undo` 只在 execute 内存，**未持久化** → 面板 rollback 路由只能做状态查询 |
| 恢复后向量检查 | `snapshots.check_vector_consistency_after_restore` | **不自动重建向量**，只报告 |

### 7.6 备份恢复子系统的问题

| 编号 | 问题 | 证据 | 级别 |
| --- | --- | --- | --- |
| E-1 | 8 处未编码只读 URI | 见 §5.3 | **P1** |
| E-2 | `_NON_RESTORABLE_TABLES` 与 `_SKIP_TABLES` 是同一语义写两遍 | `app/portable_restore.py:1106 = {"db_snapshots","sqlite_sequence","sqlite_stat1"}` vs `app/snapshots.py:60 = {"sqlite_sequence","sqlite_stat1","db_snapshots"}` | P2 |
| E-3 | `portable_restore.py` 1653 行、12 处裸 connect、40 处 execute，是 `app/` 第二大文件 | 见 §2 | P2 |
| E-4 | `rollback_restore` 的 `file_undo` 未持久化 | 已在 S2/S4 报告中作为「设计事实」记录，非缺陷但需文档化 | P3 |
| E-5 | 三条路径的「恢复后是否重建向量」策略不一致 | 逻辑快照 `check_vector_consistency_after_restore` 只报告；便携包恢复内部重算指纹；物理备份无恢复流程 | P3 |

**没有过度复杂设计**：状态机 18 个状态常量（`:47-64`）看着多，但对应 preview→confirm→execute→verify→done/failed 的真实分支，每个都有 `_transition` 持久化到 `restore_logs/`。**不建议简化**。

---

## 8. 检索与记忆治理架构分析

### 8.1 分层现状

| 模块 | SQL 次数 | 职责 | 分层评价 |
| --- | --- | --- | --- |
| `app/memory_lexical.py` | **0** | 纯词法 + 概念桥（输入 `list[dict]`，输出 `dict[id→score]`） | ✅ **完美分离** |
| `app/recall_profile.py` | **0** | 纯权重表 + clamp | ✅ 完美分离 |
| `app/activation_spreading.py` | 5 | 图算法（读关系表 → 算激活值） | ⚠️ 轻微混层 |
| `app/feedback_loop.py` | 4 | 反馈回流评分 | ⚠️ 轻微混层 |
| `app/expiry.py` | ~3 | 过期降权 | ⚠️ 轻微混层 |
| `app/memory_lifecycle.py` | **39** | 取数 + 打分 + 融合 + 排序 + 记账 | ❌ **算法与数据访问未分离** |

**数据访问密度排行**：`mcp_server.py` 67 → `db.py` 55 → `governance_service.py` 53 → `portable_restore.py` 40 → `memory_lifecycle.py` 39 → `knowledge.py` 33 → `snapshots.py` 19。

### 8.2 检索链路过耦合（P1）

`recall_memories`（`app/memory_lifecycle.py:430-623`）一个函数做完 8 件事：
1. 读全部 active 记忆（`:462`）
2. 算语义分（`:464`）
3. 算词法分（`:467`，含概念桥 + 意图抑制）
4. 算 seed 集合（`:468-470`）
5. 算激活扩散（`:476-477`）
6. 一次性读全部 feedback（`:480-485`）
7. 逐行算 8 个 parts 并加权（`:486-524`）
8. 稳定排序 + 截断 + 记账（`:559-617`）

**影响**：任何一处改动都要重新验证 45/24/48/38 四套评测（S5 阶段每次改动都跑了 4 套）。**建议**：抽出 `MemoryStore`（取数）、`Scorer`（打分）、`Ranker`（排序），`recall_memories` 只做编排。**不改算法、不改参数、不改返回结构**。

### 8.3 概念桥维护成本（重点，但不改）

| 项 | 值/位置 |
| --- | --- |
| `CONCEPT_BRIDGE_ALPHA` | `0.20`（`app/memory_lexical.py:486`） |
| `CONCEPT_BRIDGE_ENABLED` | `True`（`:487`） |
| `CONCEPT_BRIDGE_DIAGNOSTICS` | `True`（`:494`） |
| `LEX_SEED_THRESHOLD` | `0.3`（`:506`，唯一权威） |
| `BRIDGE_SEED_THRESHOLD` | 别名 = `LEX_SEED_THRESHOLD`（`:509`） |
| 三张人工枚举短语表 | `_BRIDGE_QUERY_ACTION` `:512`、`_BRIDGE_QUERY_SYMPTOM` `:517`、`_BRIDGE_MEMORY_MECHANISM` `:524` |
| 公式 | `s' = s + α × (1-s)`，`s` 是**纯词法分** |
| 全库命中 | 只有 `F04`(id 19) 与 `F11`(id 54) 两条（**F11 是误命中**） |
| α=0.25 已实测退化 | 45 题 R@1 30/41 → 29/41 |

**维护成本的真实来源**（不是代码复杂度，而是三点）：
1. **短语表是人工枚举的中文词表**——新增「装了但没好使」这类口语说法不会自动命中。
2. **命中范围是「讲重启机制」整类**——不只 F04，任何正文提「必须重启 DSH 才生效」的记忆都会被抬（`F11` 就是这么中招的）。
3. **α 距离退化边界只有 0.05**（0.20 好用 / 0.25 退化），调参余量极小。

**本次不修改**（任务书明确禁止）。已在 README `### 9` 与 `#### 什么时候必须复评` 章节留下六条复评触发条件。**§8.6 给出建议的观测机制**。

### 8.4 重复阈值与常量漂移（P1，真实缺陷）

**`COEXISTING_CATEGORIES` 两份且取值不同**：

```python
# app/governance_conflicts.py:21  —— 6 项
COEXISTING_CATEGORIES = {"preference","preferences","profile","taste","habit","style"}

# app/governance_rules.py:84      —— 7 项（多 "interest"）
COEXISTING_CATEGORIES = {"preference","preferences","profile","taste","habit","style","interest"}
```

两处注释**完全相同**（"Categories where two distinct values are expected to coexist..."），但 `governance_rules.py` 多一个 `"interest"`。

**影响**：这个常量的语义是「这些 category 下两个不同的值是**可以共存**的，不算冲突」。两份不一致意味着：
- 走 `governance_rules` 的判断会把 `category="interest"` 的两条不同记忆判为「可共存」（不报冲突）；
- 走 `governance_conflicts` 的判断会把它判为「可能冲突」（报冲突）。

**同一个库、同一对记忆，从两个入口看结论不同。** 这是 P1（行为不一致，会影响 `detect_memory_conflicts` 与 `review_memory_conflict` 的结果）。

另有 `GOV_STATUS_ACTIVE` 等状态常量在 `app/governance_rules.py:120-124` 与 `app/governance_scoring.py:25-26` 各有一份（本次未逐值核对是否一致 —— **如实声明为未验证项**）。

### 8.5 记忆生命周期职责重叠

| 能力 | Core 工具 | Gov 工具 | 底层实现 | 是否重叠 |
| --- | --- | --- | --- | --- |
| 归档 | `archive_memory` | `archive_memory_governance` | Core 直写 SQL；Gov 走 `governance_service` + 可回滚 | **是**（两条路径） |
| 恢复 | `restore_memory` | `restore_memory_governance` | 同上 | **是** |
| 替代 | `supersede_memory` | `supersede_memory_from_conflict` | Core 6 处 SQL；Gov 建新版 + `derived_from` | **是** |
| 冲突检测 | — | `detect_memory_conflicts`/`find_duplicate_memories` | 都调 `governance_conflicts` | 否（同一实现） |
| 检索 | `search_memories`/`semantic_search_memories`/`recall_all` | `recall_memories` | 三套实现（词法/语义/融合） | 否（不同算法） |

**建议**：Core 的直写路径保留（快捷、低延迟），但**必须走同一套事件记录**——目前 `archive_memory` 不记录 `events`，而 `archive_memory_governance` 记录 `governance.archived`。这让「审计轨迹」不完整：通过 Core 归档的记忆在 `events` 表里查不到。

### 8.6 建议的观测机制（不改算法，只增强可观测性）

概念桥的长期维护靠「有数据可查」而不是「记得去测」。建议（P2，需独立批准）：

1. **把 `_bridge` 诊断块写进 `events` 表**（当前只在 `explain=True` 时返回给调用方，不落库）。这样能事后统计「哪些查询触发了桥接、命中哪些记忆」。
2. **加一个只读统计脚本**（如 `scripts/eval/` 下）：扫 `events` 里的桥接记录，输出「命中记忆 ID 分布 / 触发查询数 / 是否有新增记忆开始被命中」——正好对应 README 里六条复评触发条件。
3. **不建议**做自动调参（α 自动搜索）——S5-F 已经证明「指标口径稍变结论就翻」，自动调参会把噪声固化成参数。

---

## 9. 测试体系分析

### 9.1 清单与组织

- `tests/` **34 个 .py，全部已被 git 跟踪**（与磁盘一致，未跟踪 0）。
- 统一入口 `tests/run_tests.py`（8353 B / 186 行）：`discover()` = `sorted(TESTS.glob("*_test.py"))` → **33 项**（`run_tests.py` 自身不匹配 `*_test.py`）。
- `tests/run_tests_optimize_test.py` 是入口的回归测试（防 `PYTHONOPTIMIZE` 假通过）。

**命名一致性**：100% 符合 `*_test.py`。✅

### 9.2 模块与测试对应关系（人工复核后的结论）

粗粒度 grep 会得出「`app/governance_service`、`app/star_map` 零覆盖」的假结论。**逐条核实后**：

| 疑似零覆盖 | 实际覆盖路径 | 结论 |
| --- | --- | --- |
| `app/governance_service` | `tests/feedback_loop_test.py:109 ctx = mcp.gov.build_scoring_context(conn, mid)`（`gov` 是 import 别名） | ✅ 有覆盖 |
| `app/star_map` | `app/admin_ui.py:678 from app import star_map` → `:681 star_map.build_graph(`；`tests/views_test.py:53 TestClient(admin_ui.app)` 走 HTTP | ✅ 有覆盖 |

**→ 不存在真正零覆盖的 app 模块。** 这条结论必须写进报告，避免后续重构时误删。

覆盖密度（被多少个测试文件提及）：`db` 30、`mcp_server` 14、`memory_lifecycle` 10、`snapshots` 10、`knowledge` 8、`embeddings` 6、`portable_backup` 5、`admin_ui` 4、其余 1-3。

### 9.3 测试是否依赖生产数据（1 处，设计选择）

`tests/lexical_test.py:93`：

```python
_conn = sqlite3.connect(str(Path(__file__).resolve().parent.parent / "data" / "agent.db"))
```

`:95-96` 只读 `SELECT id, content, tags_json FROM memories WHERE status='active' ORDER BY id`，用于生成「标题自召回」动态用例（48 条）。

- **这是唯一读生产库数据的测试**，且仅 `SELECT`。
- **设计理由**：标题自召回必须跟真实库同步，写死 48 个标题会在库变化后失效。
- **风险**：`recall@1 47/48` 这个门槛依赖「生产库当前有 48 条合格记忆」。若库涨到 60 条，门槛仍是 90%，即需要 ≥54 命中——**测试严格度会随库规模隐式变化**。
- **建议（P3）**：门槛改为「与固定快照副本比对」或「允许按条数比例浮动」，需独立评估，**本次不改**。

### 9.4 测试环境污染

| 项 | 数量 | 详情 |
| --- | --- | --- |
| 改 `db.DB_PATH` 后**恢复** | 5 | `semantic_test`(2/1)、`smoke_test`(2/1)、`embedding_test`(2/1)、`feedback_loop_test`(4/2)、`init_db_probe_test` |
| 改 `db.DB_PATH` 后**不恢复** | 3 | `fusion_test`、`cross_device_test`、`import_dsh_memory_test` |

**为什么现在不出问题**：`tests/run_tests.py` **每项独立子进程**（其 docstring:17 明写「它们都会在进程内改 `db.DB_PATH` 指向临时库」）。单跑某个测试时进程退出即恢复；统跑时进程隔离。

**风险**：若将来有人在同一进程内顺序调多个测试（如做 `pytest` 迁移），这 3 个会污染后续测试。**建议（P2）**：加 `try/finally` 恢复。

### 9.5 分层情况

| 层级 | 文件 | 说明 |
| --- | --- | --- |
| **单元/算法** | `concept_bridge_test`(68)、`concept_bridge_diag_test`(40)、`lexical_test`(16)、`fusion_test`(32)、`semantic_test`(25)、`recall_profile_test`(62) | 纯算法 + 内存数据 |
| **子系统集成** | `db_backup_test`(104)、`snapshot_test`(57)、`portable_backup_test`(65)、`portable_restore_test`(64)、`portable_restore_mcp_test`(29)、`portable_backup_metadata_test`(90)、`init_db_probe_test`(94)、`admin_backup_restore_test`(104)、`admin_host_guard_test`(64)、`import_dsh_memory_test`(28)、`cross_device_test`(20)、`concurrency_test`(8) | 真实 SQLite 临时库 |
| **领域行为** | `protection_test`(28)、`expiry_test`(37)、`feedback_loop_test`(26)、`distillation_test`(39)、`task_resume_test`(42)、`timeline_test`(12)、`views_test`(18)、`activation_test`(16)、`conflict_triage_test`(33)、`review_release_test`(25)、`knowledge_test`(26) | 真实库副本 |
| **契约** | `verify_mcp_split_test`(6 项)、`run_tests_optimize_test`(12) | 只核清单/源码 |
| **烟测** | `smoke_test`(3 assert) | 最小可用性 |

**分层清晰。** 唯一分层瑕疵：`lexical_test` 既是算法测试又依赖生产库（见 §9.3）。

### 9.6 重复测试

未发现「同一行为在两个文件里重复断言」。审查到的近似项：
- `concept_bridge_test`（68 项，算法行为）与 `concept_bridge_diag_test`（40 项，诊断开关 + 指标准确性）——**职责不同，不算重复**。
- `admin_backup_restore_test`（104 项，面板路由）与 `portable_restore_test`（64 项，恢复引擎）——**层次不同**。
- `snapshot_test`（57 项）与 `db_backup_test`（104 项）——**两种备份方案，不同**。

---

## 10. 文档与打包分析

### 10.1 文档现状

| 文档 | 存在 | 内容 | 问题 |
| --- | --- | --- | --- |
| `README.md` | ✅ 45143 B / 634 行 | 功能总览、环境要求、快速开始、可视化管理面板、项目结构、记忆模型、**九个"不显然"的设计决定**、可移植/传播要点、测试、已知限制 | 单文件承载全部文档 |
| 架构文档 | ❌ | — | **本次审计补 `docs/ARCHITECTURE_AUDIT_S0_S6.md`** |
| 阶段交付文档（S0–S6 报告） | ❌ 未入库 | 全部在 `C:\Users\xing\Documents\deepseek-harness\default-workspace\` | 仓库里查不到设计决策历史 |
| MCP 配置示例 | ✅ `deepseek-harness-mcp.example.yml` 1561 B | 三个入口 + token 说明 | 已在 S6 期同步为 36/49/85 |
| 环境变量示例 | ✅ `.env.example` 118 B | — | ✅ |
| `docs/` 目录 | 本次新建 | — | 此前不存在 |

### 10.2 文档与代码不一致（4 处，实测）

| 编号 | 位置 | 文档写 | 实际 | 级别 |
| --- | --- | --- | --- | --- |
| H-1 | `README.md:584` | `snapshot_test` **31 项** | **57 项** | P3 |
| H-2 | `README.md:586` | `distillation_test` **37 项** | **39 项** | P3 |
| H-3 | `README.md` 测试表格 | 缺 `tests/run_tests_optimize_test.py` 一行（12 项） | 实际 33 个套件 | P3 |
| H-4 | 章节标题历史漂移 | 原写「五个"不显然"的设计决定」而实际 8 条 | S6-FIX-06 已修正为「九个」并补第 9 条 ✅ | 已修 |

> H-1/H-2 在 S6-PRECOMMIT-FIX 时已发现，按「不确定的历史数据不得顺手改」保留原状，本次审计**同样不修改**，只记录。

### 10.3 打包与安装

**`pyproject.toml`（34 行）**：

```toml
[tool.setuptools]
packages = ["app"]

[tool.setuptools.package-data]
app = ["py.typed", "admin_ui.html", "static/*"]

[project.scripts]
bionic-agent = "app.mcp_server:_run_cli"
bionic-agent-governance = "app.mcp_server_gov:_run_cli"
bionic-agent-admin = "app.admin_ui:_run_cli"
```

| 项 | 状态 |
| --- | --- |
| `dependencies` 4 项（`mcp`、`fastembed`、`fastapi`、`uvicorn[standard]`） | ✅ 与 `requirements.txt` 完全一致，无漂移 |
| `static/*` 声明 | ✅ S6-FINAL-FIX 已补，wheel 实测含 `app/static/3d-force-graph.min.js`（1389954 B） |
| `py.typed` | ✅ 已创建 0 字节文件（PEP 561 标记） |
| 三个 console entry | ✅ 对应三个 `_run_cli` |

**`build_zip.py`（89 行）**：

| 项 | 值 |
| --- | --- |
| `INCLUDE_DIRS` | `["app", "dsh-panel-client", "scripts"]`（`:14`）——**不含 `tests/`** |
| `INCLUDE_FILES` | 6 个（`:15-21`） |
| `EXCLUDE_DIRS` | `{"__pycache__","backups",".git","node_modules","dev","dist"}`（`:23`） |
| `SCRIPTS_ALLOW` | 7 项，含 `physical_backup.py`（`:25-33`，S6-PRECOMMIT-FIX 已补） |
| `_excluded()` | 排除 `.bak`/`.tmp`/`.part`/`.swp`/`_` 开头/`eval_` 开头（`:36-49`） |
| 实测产物 | 65 文件 / 763680 B；`tests/`、`scripts/dev/`、`data/`、`logs/` 命中 **0** |

**问题**：

| 编号 | 问题 | 证据 | 级别 |
| --- | --- | --- | --- |
| H-5 | **两条分发路径内容不一致**：ZIP 含 `tests/`？不含；wheel 含 `tests/`？不含。但 ZIP 的 `SCRIPTS_ALLOW` 是白名单（7 个），wheel 用 `packages=["app"]` 所以 `scripts/` **根本不进 wheel** | `pyproject.toml:9 packages = ["app"]`；`build_zip.py:14` 含 `scripts` | P2 |
| H-6 | ZIP 不含 `tests/`，wheel 也不含 → **用户装完无法自检** | 同上 | P2 |
| H-7 | 评测脚本靠文件名前缀 `eval_` 排除发布 | `build_zip.py:36-49` | P2 |
| H-8 | `build/`（2.24 MB）与 `bionic_agent_mcp.egg-info/`（6 文件）残留 | 已 gitignore 但物理存在 | P3 |
| H-9 | `nul` 文件（0 字节）在根目录 | 已 gitignore | P3 |

### 10.4 安装依赖缺失

**未发现**：`dependencies` 4 项覆盖了所有 `import` 的第三方包（`mcp`、`fastembed`、`fastapi`、`uvicorn`）。其余 import 全是标准库（`sqlite3`、`hashlib`、`json`、`ast`、`urllib.parse`…）。✅

**配置重复**：`requirements.txt`（4 行）与 `pyproject.toml` 的 `dependencies` 是同一组约束的两次书写。**当前一致**，但下次升级依赖时容易漏改一处。

---

## 11. 安全与维护性分析

### 11.1 Admin API 权限与绑定

| 项 | 状态 |
| --- | --- |
| 路由总数 | **75**（`@app.get` 40 / `@app.post` 33 / `@app.put` 1 / `@app.delete` 1） |
| 鉴权 | **零**——全仓 `Depends`/`HTTPBasic`/`api_key`/`Authorization` 命中 **0** |
| 绑定地址守卫 | ✅ S6-FIX-01：`:1396 is_local_bind_host` / `:1429 check_bind_safety` / `:1484 allowed, message = check_bind_safety(args.host, args.allow_remote)`；非本机无 `--allow-remote` 时 `sys.exit(2)` |
| CORS | ✅ `:94-96 allow_origin_regex=r"^https?://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$"` |
| 静态文件 | `/static/{name}` 有后缀白名单（`.js/.css/.svg`）+ 拒 `/` `\` `..` |
| CSRF | ❌ 无 |

**完整路由分布**：`/api/memories*` 22 条、`/api/tasks*` 11、`/api/governance/*` 9、`/api/snapshots*` 6、`/api/backup/*` 12、`/api/protection*` 2、`/api/feedback*` 2、`/api/distillations*` 3、`/api/expiry/*` 3、`/api/task-resume/*` 2、`/api/recall/*` 2、`/api/conflicts/value`、`/api/stats`、`/api/ping`、`/`、`/static/{name}`。

**写操作全部裸奔**：`POST /api/memories/{id}/archive`、`POST /api/snapshots/{id}/restore`、`POST /api/backup/execute`。

**真实威胁模型**：默认只监听 `127.0.0.1`，同机其他进程可访问（本机用户级别的信任边界）。`--allow-remote` 后是**任何能连到该端口的人**都能改库。S6-FIX-01 的警告末行已明写「`--allow-remote` 只是你的授权声明，**不是认证**」。**本次不修改**（此为已知设计事实，已在 S6-FINAL 报告中列为 P1 并说明）。

### 11.2 文件路径安全

| 机制 | 位置 | 评价 |
| --- | --- | --- |
| `_safe_rel` 逐段拒 `..`/`.`/空段 | `app/portable_backup.py:126` | ✅ |
| `_is_sensitive` + `SENSITIVE_PARTS/DIRS/NAMES` | `:140`、`:59`/`:63`/`:65` | ✅ |
| 四个尺寸上限 + `CHECKSUM_RE` | `:52-55`、`:67` | ✅ |
| `delete_package` 前缀 + `relative_to` 二次校验 | `:945` 附近 | ✅ |
| `is_backup_name` 正则整串锚定 + `resolve().parent` 归属校验 | `app/db_backup.py:87`、`:462` | ✅ |
| 静态文件后缀白名单 | `app/admin_ui.py` | ✅ |

**未发现路径穿越。** 唯一相关缺陷是 §5.3 的 URI 编码（不是穿越，是 URI 解析错误）。

### 11.3 敏感信息

- 硬编码盘符路径 **3 处**，全部是 `DSH_MEMORY_ROOT` 默认值或注释（§5.6 C-4），**不含凭据**。
- 全仓扫描未发现 API key / token / 密码字面量；`git ls-files` 只有 `.env.example`（118 B，无真实值）。
- `app/db_backup.py` 里的「QQ号」告警经核实是日期字符串 `agent.db.before-cleanup-20261030-220741`。

### 11.4 异常处理与日志

| 项 | 实测 |
| --- | --- |
| 裸 `except:` | **0 处** ✅ |
| `except Exception` | **127 处**，最密：`portable_restore.py` 23、`portable_backup.py` 12、`governance_service.py` 11、`mcp_server.py` 9、`distillation.py` 8、`snapshots.py` 8、`admin_ui.py`/`db.py` 各 6 |
| `import logging` | **0 处**（全仓） |
| 日志实现 | `app/admin_ui.py` 3 处 `print`；10 个 scripts 全用 `print`（`physical_backup.py` 32、`snapshot_db.py` 24、`sync_all.py` 18、`recall.py` 11、`check_mirror.py` 10、`import_dsh_memory.py` 7、`import_dsh_knowledge.py` 6、`eval_*` 3-4） |

**问题**：`app/` 侧无结构化日志 → 面板错误只回 HTTP 500，MCP 工具错误回 `{"ok": false}`，**排查只能靠 `print` 到 stdout 被父进程吞掉**。CLI 脚本用 `print` 尚可（输出重定向到 `logs/*.log`）。

### 11.5 并发控制与超时

| 机制 | 值/位置 |
| --- | --- |
| `BUSY_TIMEOUT_SECONDS` | `30`（`app/db.py:19`，可用 `BIONIC_DB_BUSY_TIMEOUT` 覆盖） |
| `_retry_locked(fn, attempts=40, delay=0.05)` | `app/db.py:22-45`，只重试含 `"database is locked"` 的 `OperationalError`，退避 `delay*(1+i*0.25)` |
| 写事务 | `BEGIN IMMEDIATE`（`app/db.py:518`、`app/portable_restore.py:1089`） |
| 只读快照 | deferred `BEGIN`（`app/db.py:501-532 read_transaction()`） |
| 恢复锁 | `app/portable_restore.py` `_acquire_lock`（文件锁 + pid） |
| HTTP 超时 | `uvicorn` 默认 |
| 单文件锁 | `_retry_locked` 对 `PRAGMA journal_mode=WAL` 零等待（注释已说明） |

**并发测试覆盖**：`tests/concurrency_test.py`（8 项）+ `tests/init_db_probe_test.py` E 组（双线程 + 双进程 + 换库存隔离）。✅

### 11.6 资源释放

`with db.connect()`（自动关闭）在 12 个模块共 **47 处**；全部裸 `sqlite3.connect`（`db.py` 6 / `db_backup.py` 3 / `portable_backup.py` 3 / `portable_restore.py` 12）**均有 try/finally 关闭**（已抽查 7 处）。✅

### 11.7 配置管理

| 环境变量 | 默认值 | 位置 |
| --- | --- | --- |
| `BIONIC_DB_PATH` | `<project>/data/agent.db` | `app/db.py:10-13` |
| `BIONIC_DB_BUSY_TIMEOUT` | `30` | `app/db.py:19` |
| `DSH_MEMORY_ROOT` | `C:\Users\xing\.dsh\dsh-memory` | `app/knowledge.py:49` |
| `BIONIC_GOVERNANCE_*`（多个） | 见 `app/governance_rules.py` | `:190-199` 等 |

**评价**：环境变量覆盖机制统一（`_env_int`/`_env_float`/`_env_bool` 集中在 `governance_rules.py`），但**分散在多个模块**，没有统一的配置清单文档。**建议（P3）**：在 README 加一节「环境变量一览」。

---

## 12. 问题分级清单

> 共 **29 项**：P0 **0** / P1 **3** / P2 **12** / P3 **14**。
> 每项含 10 个必填字段。**本次不修复任何一项**，只给方案。

### 12.1 P1（核心功能可靠性 / 兼容性 / 架构边界重大风险）

#### P1-1 未编码只读 URI 导致静默读空库并留下垃圾文件

| 字段 | 内容 |
| --- | --- |
| **编号** | P1-1 |
| **级别** | P1 |
| **文件路径与代码位置** | `app/portable_backup.py:409`（`_dump_db_logical`）；`app/portable_restore.py:229`（`_db_fingerprint`）、`:396`（`inspect_restore_target`）、`:654`（`preview_restore`）、`:795`（`_create_protective_backup`）、`:918`（`_validate_snapshot_against_db`）、`:1323`（`verify_restored_state`）、`:1417`（`rollback_restore`） |
| **实际证据** | 共 8 处 `sqlite3.connect(f"file:{<路径>}?mode=ro", uri=True)`，路径未 `quote`。临时目录实测：`hash#1.db` 报 `OperationalError: no such table: t` 且**目录里凭空出现 0 字节 `hash` 文件**；`pct%20.db` 报 `unable to open database file`；正确编码 `"file:" + quote(p, safe="/:") + "?mode=ro"` 后 8 种特殊名全部通过。已存在正确实现可复用：`app/db_backup.py:139 _readonly_uri`、`app/portable_backup.py:205 _readonly_uri` |
| **影响范围** | 便携包导出 / 恢复预览 / 恢复前保护备份 / 恢复后校验 / 回滚。备份包路径与恢复目标路径都可来自用户输入（面板、MCP 参数）。含 `#`、`%` 的路径会让只读校验读到空库（`no such table` 可能被 `except` 吞掉后误判为「无数据」），并在用户目录留下垃圾文件 |
| **推荐方案** | 把 `app/portable_backup.py:205 _readonly_uri` 提为公共函数（放 `app/db.py`），8 处全部改调；`_immutable_uri` 同理。**纯替换，不改逻辑** |
| **是否需迁移数据库** | 否 |
| **是否影响现有 MCP 接口** | 否（内部实现变更，工具签名与返回不变） |
| **风险与回滚方案** | 风险极低（等价替换）。回滚 = git revert 单次提交。验证方式：现有 `portable_backup_metadata_test`（90 项）+ `portable_restore_test`（64 项）+ 新增 1 项「含 `#`/`%` 路径」用例 |
| **建议处理优先级** | **最高**（唯一有实测可复现危害的 P1） |

#### P1-2 26 个 MCP 工具函数体内直接写 SQL，接口层与数据访问层未分层

| 字段 | 内容 |
| --- | --- |
| **编号** | P1-2 |
| **级别** | P1 |
| **文件路径与代码位置** | `app/mcp_server.py`，共 66 处 `execute` 落在 26 个工具函数内。最重的：`supersede_memory` L341-369（**6**）、`handoff_task` L517-541（**6**）、`save_memory` L100-167（4）、`link_memories` L296-317（4）、`save_stage_snapshot` L497-514（4） |
| **实际证据** | 逐函数 execute 计数已验证（26 个，见 §6.4）。`archive_memory`（L423-435）直接 `UPDATE memories SET status='archived'` + `INSERT INTO events`，而 `archive_memory_governance`（L832-841）走 `gov.archive_memory_governance()` 服务层。两条路径的审计粒度、可回滚性不同 |
| **影响范围** | ①新增/修改 Core 工具时无复用点，容易复制粘贴；②这 26 个工具绕过了服务层的校验与审计（例如不经 `governance_service` 的 `governance_actions` 记录）；③`mcp_server.py` 已达 1738 行，无法按域拆分（拆开就要处理这些内嵌 SQL） |
| **推荐方案** | **分批下沉，不改接口**：先在 `app/memory/`（或现有 `memory_lifecycle.py`）建立 `MemoryStore`（CRUD）+ `TaskStore`；把 26 个工具的 SQL 搬进去，工具函数只做「参数校验 → 调 store → 组装返回」。**工具名/参数/返回结构一字不改**。分三期：①`supersede_memory`/`handoff_task`/`save_memory` 三个最重的验证模式；②剩余 23 个批量迁移；③才考虑把 `mcp_server.py` 按域拆文件 |
| **是否需迁移数据库** | 否 |
| **是否影响现有 MCP 接口** | **否**（这是本方案的核心约束：契约不变） |
| **风险与回滚方案** | 中（触及 26 个工具的写入路径）。回滚 = 每期独立 commit + 现成 33 项测试套件（当前 33/33 通过）+ 建议补 §12.3 P2-3 的 22 个零覆盖工具测试后再动手 |
| **建议处理优先级** | 高，但**必须在 P2-3（补测试）之后** |

#### P1-3 Admin API 75 个路由零鉴权

| 字段 | 内容 |
| --- | --- |
| **编号** | P1-3 |
| **级别** | P1 |
| **文件路径与代码位置** | `app/admin_ui.py` 全部 75 个路由（`@app.get` 40 / `@app.post` 33 / `@app.put` 1 / `@app.delete` 1） |
| **实际证据** | 全仓 `Depends`/`HTTPBasic`/`api_key`/`Authorization` 命中 **0**；`POST /api/memories/{id}/archive`、`POST /api/snapshots/{id}/restore`、`POST /api/backup/execute` 均无任何凭据校验。缓解措施：默认 `127.0.0.1` + CORS 白名单 + `--allow-remote` 显式授权（`app/admin_ui.py:1429 check_bind_safety`） |
| **影响范围** | 同机任意进程可改库；`--allow-remote` 后任何能连到端口的人可改库、恢复快照、执行备份 |
| **推荐方案** | ①加 `--token`（或 `BIONIC_ADMIN_TOKEN` 环境变量），中间件校验 `Authorization: Bearer`；②未设 token 时保持当前「仅本机 + 警告」行为，保证向后兼容；③`--allow-remote` 与 `--token` 绑定时**强制要求** token。**不引入新依赖**（用 `secrets.compare_digest`） |
| **是否需迁移数据库** | 否 |
| **是否影响现有 MCP 接口** | 否（只影响 HTTP 层）；但**面板前端需要带 token**，属兼容性变更，需同步改 `dsh-panel-client/lib/index.js:227` 的 spawn 参数 |
| **风险与回滚方案** | 中（前端要同步）。回滚 = 保留「不设 token 即旧行为」分支即可零影响回滚。验证：`tests/admin_host_guard_test`（64 项）+ `tests/admin_backup_restore_test`（104 项）+ 新增 token 用例 |
| **建议处理优先级** | 中高（S6-FINAL 已列，两次审计一致认为非阻断发布） |

### 12.2 P2（明显增加维护成本 / 耦合度 / 重复实现）

| 编号 | 问题 | 位置与证据 | 推荐方案 | 迁移DB | 影响MCP接口 | 风险/回滚 | 优先级 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| **P2-1** | `recall_memories` 一个函数做 8 件事（取数→语义→词法→seed→激活→feedback→加权→排序→记账），39 处 SQL | `app/memory_lifecycle.py:430-623` | 抽 `MemoryStore`/`Scorer`/`Ranker` 三层，`recall_memories` 只编排。**不改算法/参数/返回** | 否 | 否（返回结构不变） | 中：这是检索核心，改后须重跑 45/24/48/38/NL/PURE 六套。回滚=commit revert | 中 |
| **P2-2** | `mcp_server.py` 1738 行 + 17 个 fan-out，无应用服务层 | `app/mcp_server.py` | 与 P1-2 同一方案的第二/三期 | 否 | 否 | 中 | 中（须在 P1-2 之后） |
| **P2-3** | **22 个 MCP 工具零测试覆盖** | 见 §6.5 表格 | 为 22 个各加 1 个冒烟用例（调工具 → 断言返回形状），约 22×15 行 | 否 | 否 | 低 | **高（P1-2 的前置）** |
| **P2-4** | `COEXISTING_CATEGORIES` 两份定义取值不同（6 项 vs 7 项，缺 `interest`） | `app/governance_conflicts.py:21` vs `app/governance_rules.py:84` | 统一到 `governance_rules`，`conflicts` 的删掉、只在 `rules.get(...)` 缺失时抛错或导入常量 | 否 | 否 | 低 | 中 |
| **P2-5** | `_connect_at`/`_transaction_at` 与 `db.connect()`/`db.transaction()` 逐行重复 | `app/portable_restore.py:1061`、`:1079` vs `app/db.py:442`、`:469` | 把 `db.connect(path=None)`/`transaction(path=None)` 参数化，删掉复制 | 否 | 否 | 低 | 中 |
| **P2-6** | schema 检测逻辑 **5 处重复** | `app/db.py:296/327/545`、`app/portable_backup.py:421/426/452`、`app/portable_restore.py:927/929/1115/1121`、`app/snapshots.py:85/223`、`scripts/snapshot_db.py:102` | 收敛到 `app/db.py` 的 `table_names()/table_columns()/pk_columns()/table_exists()` | 否 | 否 | 低 | 中 |
| **P2-7** | `_NON_RESTORABLE_TABLES` 与 `_SKIP_TABLES` 同义两写 | `app/portable_restore.py:1106` vs `app/snapshots.py:60` | 提一个 `SKIP_TABLES` 常量共用 | 否 | 否 | 低 | 低 |
| **P2-8** | `db_snapshots` 表在 `snapshots.py:91` 自建，不在 `app/db.py` 的 `REQUIRED_TABLES`（14 张）里 | `app/snapshots.py:91` | 把 DDL 搬到 `app/db.py`，加进 `REQUIRED_TABLES`（→15 张），`probe_database_state` 才能发现它的缺失 | 否（`CREATE TABLE IF NOT EXISTS` 幂等） | 否 | 低：已存在的库不受影响 | 中 |
| **P2-9** | **生命周期层反向依赖 MCP 接口层** | `app/memory_lifecycle.py:809 from app import mcp_server as _mcp` | 把该处需要的功能提到服务层，删掉这条反向 import | 否 | 否 | 中（在 SCC 里） | 中 |
| **P2-10** | **检索算法层依赖知识库层的私有函数** | `app/memory_lexical.py:35 from app.knowledge import _SYNONYM_GROUPS, _segments` | 把 `_segments`/`_SYNONYM_GROUPS` 提到 `app/text.py`（纯文本工具），两侧都从那里取 | 否 | 否 | 中：**改词法分词会影响全部检索评测**，必须重跑六套 | 中（需独立批准） |
| **P2-11** | **两条分发路径内容不一致**：wheel 只含 `packages=["app"]`（无 `scripts/`），ZIP 含 `scripts/` 但无 `tests/` | `pyproject.toml:9` vs `build_zip.py:14` | 明确两条路径的定位：wheel = 可 pip 安装的库 + CLI；ZIP = 完整可自检的发布包（应含 `tests/`） | 否 | 否 | 低 | 中 |
| **P2-12** | `tests/lexical_test.py` 直连生产库，`recall@1 ≥ 90%` 门槛随库规模隐式变化 | `tests/lexical_test.py:93`（`sqlite3.connect(.../data/agent.db)`）、`:95-96` | 改为对固定快照副本测，或把门槛改为「相对基线 ±N 条」 | 否 | 否 | 低 | 中 |

### 12.3 P3（命名 / 文档 / 目录整洁）

| 编号 | 问题 | 位置 | 推荐方案 |
| --- | --- | --- | --- |
| P3-1 | `_now()` **8 份**重复 | `distillation.py:48`、`expiry.py:54`、`governance_scoring.py:63`、`memory_lifecycle.py:119`、`portable_restore.py:78`、`snapshots.py:78`、`star_map.py:37`、`task_resume.py:43` | 提到 `app/timeutil.py` |
| P3-2 | `_parse_ts()` **5 份**重复 | `admin_ui.py:106`、`expiry.py:58`、`governance_maintenance.py:40`、`governance_service.py:57`、`task_resume.py:47` | 同上 |
| P3-3 | `_json()`/`_event()`/`_run()` 各 2 份，**但都是同名不同义** | `governance_service.py:74/91`、`memory_lifecycle.py:124`、`mcp_server.py:71/1725`、`admin_ui.py:178` | **只改名不合并**：`_json`→`dumps_json`/`coerce_json`、`_event`→`log_event_with_conn`/`log_event`、`_run`→`call_backend`/`serve`。合并会改行为或破坏事务边界（见 §4.4 B-9/B-10/B-11） |
| P3-4 | `_sidecar_snapshot`/`_wal_has_content` 各 2 份 | `app/db.py:200/185`、`app/portable_backup.py:178/190` | 合并到 `app/db.py` |
| P3-5 | `README.md:584` `snapshot_test` 写 31 项实 57 项 | `README.md:584` | 改 57 |
| P3-6 | `README.md:586` `distillation_test` 写 37 项实 39 项 | `README.md:586` | 改 39 |
| P3-7 | README 测试表格缺 `run_tests_optimize_test.py` 一行 | README 测试表 | 补一行「12 项」 |
| P3-8 | 测试改 `db.DB_PATH` 后不恢复（3 个） | `fusion_test`、`cross_device_test`、`import_dsh_memory_test` | 加 `try/finally` 恢复 |
| P3-9 | `build/`（2.24 MB / 29 文件）与 `bionic_agent_mcp.egg-info/`（6 文件）残留 | 项目根 | 清理（已 gitignore，不影响仓库） |
| P3-10 | `app/` 下 10 个 `.bak-*` 源码备份（约 374 KB） | `app/*.py.bak-*` | 移到 `data/backups/` 或删除（已被 gitignore） |
| P3-11 | 根目录 `nul`（0 字节） | `/nul` | 删除 |
| P3-12 | 56 个工具 docstring < 40 字符（最短 10） | `app/mcp_server.py` | 补说明，尤其 `list_tasks`/`get_task`/`restore_memory`/`complete_task` |
| P3-13 | 工具返回形态 7 种并存 | `app/mcp_server.py` | 统一到 `_tool_result`/`_tool_error`（20 个已用，其余 65 个待迁） |
| P3-14 | 环境变量无统一清单文档 | 分散在 `db.py`/`knowledge.py`/`governance_rules.py` | README 加「环境变量一览」章节 |

---

## 13. 推荐目标目录结构

### 13.1 设计原则

1. **保留现有技术栈**：Python + SQLite + FastMCP + FastAPI，不引入 ORM/DI 框架。
2. **保留现有 MCP 工具接口**：85 个工具名、参数、返回结构**一字不改**。
3. **保留数据库结构与数据兼容性**：`data/agent.db` 不动、不迁移、路径不变。
4. **保留 S0–S6 全部功能**。
5. **不为整齐过度拆分**：`memory_lexical.py`（695 行，零 SQL）、`recall_profile.py`（179 行）等职责纯粹的文件**不拆**。
6. **边界清晰**：业务逻辑 / 数据访问 / MCP 接口 / 管理界面 / 脚本 / 测试各自成层。
7. **减少跨模块依赖**：接口层不直达业务模块，经应用服务层。

### 13.2 推荐目录树

```
bionic-agent-mcp/
├── app/
│   ├── __init__.py
│   ├── py.typed
│   │
│   ├── core/                      # 基础设施（零业务依赖）
│   │   ├── db.py                  # ← app/db.py（连接/事务/WAL/迁移/探测）
│   │   ├── dbutil.py              # 新增：_readonly_uri/_immutable_uri/table_names/table_columns
│   │   ├── embeddings.py          # ← app/embeddings.py
│   │   ├── timeutil.py            # 新增：_now/_parse_ts（收敛 8+5 份重复）
│   │   └── text.py                # 新增：_segments/_SYNONYM_GROUPS（从 knowledge 提出）
│   │
│   ├── memory/                    # 记忆领域（算法为主）
│   │   ├── store.py               # 新增：MemoryStore（从 mcp_server 的 26 个工具下沉）
│   │   ├── lifecycle.py           # ← app/memory_lifecycle.py（瘦身后只做编排）
│   │   ├── lexical.py             # ← app/memory_lexical.py（**不拆**，695 行职责纯粹）
│   │   ├── recall_profile.py      # ← app/recall_profile.py（**不拆**，179 行）
│   │   ├── activation.py          # ← app/activation_spreading.py
│   │   ├── feedback.py            # ← app/feedback_loop.py
│   │   ├── expiry.py              # ← app/expiry.py
│   │   ├── protection.py          # ← app/protection.py
│   │   ├── distillation.py        # ← app/distillation.py
│   │   └── tasks.py               # ← app/task_resume.py
│   │
│   ├── governance/
│   │   ├── rules.py               # ← app/governance_rules.py（COEXISTING_CATEGORIES 唯一来源）
│   │   ├── scoring.py             # ← app/governance_scoring.py
│   │   ├── conflicts.py           # ← app/governance_conflicts.py
│   │   ├── service.py             # ← app/governance_service.py
│   │   └── maintenance.py         # ← app/governance_maintenance.py
│   │
│   ├── knowledge/
│   │   ├── index.py               # ← app/knowledge.py（导入/检索/骨架）
│   │   └── segments.py            # ← 从 knowledge.py 提出的 _segments/_SYNONYM_GROUPS
│   │
│   ├── backup/                    # 备份恢复子系统（四个模块自成一域）
│   │   ├── physical.py            # ← app/db_backup.py
│   │   ├── logical.py             # ← app/snapshots.py
│   │   ├── portable_export.py     # ← app/portable_backup.py
│   │   └── portable_restore.py    # ← app/portable_restore.py
│   │
│   ├── mcp/                       # MCP 接口层（薄）
│   │   ├── __init__.py            # FastMCP 实例定义
│   │   ├── core_tools.py          # ← app/mcp_server.py 的 36 个 @mcp.tool()
│   │   ├── gov_tools.py           # ← app/mcp_server.py 的 49 个 @mcp_gov.tool()
│   │   └── gov_entry.py           # ← app/mcp_server_gov.py
│   │
│   └── api/                       # HTTP 接口层
│       ├── app.py                 # ← app/admin_ui.py 的 app 构造 + lifespan
│       ├── routes.py              # ← app/admin_ui.py 的 75 个路由
│       ├── star_map.py            # ← app/star_map.py
│       └── templates/
│           └── index.html         # ← app/admin_ui.html
│   └── static/
│       └── 3d-force-graph.min.js
│
├── scripts/
│   ├── ops/                       # 运维（进发布包）
│   │   ├── sync_all.py
│   │   ├── snapshot_db.py
│   │   ├── physical_backup.py
│   │   ├── check_mirror.py
│   │   └── recall.py
│   ├── imports/                   # 导入（进发布包）
│   │   ├── memory.py              # ← scripts/import_dsh_memory.py
│   │   └── knowledge.py           # ← scripts/import_dsh_knowledge.py
│   └── eval/                      # 评测（**不进发布包**，改由目录决定而非文件名前缀）
│       ├── lexical.py
│       ├── local.py
│       └── recall.py
│
├── tests/                         # ← 原样保留（34 个 .py 已全部跟踪）
├── dsh-panel-client/              # ← 原样保留（独立发布单元）
├── docs/                          # 架构/审计/阶段报告
├── data/                          # 运行时数据（生产库、备份、缓存）
├── build_zip.py → tools/build_zip.py
├── pyproject.toml / README.md / requirements.txt / .gitignore / .env.example
└── deepseek-harness-mcp.example.yml
```

### 13.3 各目录职责

| 目录 | 职责 | 依赖方向 |
| --- | --- | --- |
| `app/core/` | 数据库、嵌入、时间、文本工具。**不依赖任何业务模块** | 被所有层依赖 |
| `app/memory/` | 记忆领域：store（数据访问）+ 各算法模块 | 依赖 `core`、`knowledge`（读） |
| `app/governance/` | 治理：规则、评分、冲突、服务、维护 | 依赖 `core`、`memory` |
| `app/knowledge/` | 知识库索引与检索 | 依赖 `core` |
| `app/backup/` | 三条备份路径（物理/逻辑/便携） | 依赖 `core`、`memory`（读） |
| `app/mcp/` | MCP 协议层。**薄**：参数校验 → 调服务 → 组装返回 | 依赖 `memory`、`governance`、`knowledge`、`backup` |
| `app/api/` | HTTP 层。**薄**：同 MCP | 同上 |
| `scripts/ops/` | 运维 CLI | 依赖 `app` |
| `scripts/imports/` | 导入 CLI | 依赖 `app` |
| `scripts/eval/` | 评测 CLI（不发布） | 依赖 `app` |
| `tests/` | 33 个套件 + 统一入口 | 依赖 `app` |
| `dsh-panel-client/` | DSH 面板插件（Node 侧） | 独立 |
| `docs/` | 架构、审计、阶段报告 | — |

---

## 14. 文件迁移映射表

完整逐文件映射见 **`docs/ARCHITECTURE_FILE_INVENTORY.csv`**（149 行，含 path/type/bytes/lines/public_funcs/responsibility/references/target_path/move_advice/risk_level）。

**汇总（按 CSV 的 `move_advice` 列精确统计，合计 149 = 38 + 111）**：

| CSV `move_advice` | 个数 | 含义 |
| --- | --- | --- |
| `移动` | **34** | 换目录，文件不拆 |
| `移动+拆分` | **3** | `app/admin_ui.py`、`app/knowledge.py`、`app/memory_lifecycle.py` |
| `建议拆分（分三期）` | **1** | `app/mcp_server.py`（先拆 SQL 后拆文件，见 §6.5） |
| `保留` | 59 | 原地不动（含 `tests/`、`dsh-panel-client/`、`data/model_cache/` 等） |
| `不移动` | 52 | 明确不建议移动（`tests/` 34 个 + `scripts/dev/` 等） |

→ **需要变更位置的文件 38 个**（34 + 3 + 1）、**建议拆分 4 个**（`mcp_server.py` / `admin_ui.py` / `knowledge.py` / `memory_lifecycle.py`）、**保留原样 111 个**（59 + 52）。

**核心模块映射表**（`app/` 27 个 .py + 2 资源）：

| 现路径 | 目标路径 | 动作 | 风险 |
| --- | --- | --- | --- |
| `app/db.py` | `app/core/db.py` | 移动 | P1 |
| `app/embeddings.py` | `app/core/embeddings.py` | 移动 | P2 |
| `app/memory_lifecycle.py` | `app/memory/lifecycle.py` + `app/memory/store.py` | 移动+拆分 | P1 |
| `app/memory_lexical.py` | `app/memory/lexical.py` | 移动 | P2 |
| `app/recall_profile.py` | `app/memory/recall_profile.py` | 移动 | P3 |
| `app/activation_spreading.py` | `app/memory/activation.py` | 移动 | P3 |
| `app/feedback_loop.py` | `app/memory/feedback.py` | 移动 | P3 |
| `app/expiry.py` | `app/memory/expiry.py` | 移动 | P3 |
| `app/protection.py` | `app/memory/protection.py` | 移动 | P3 |
| `app/distillation.py` | `app/memory/distillation.py` | 移动 | P2 |
| `app/task_resume.py` | `app/memory/tasks.py` | 移动 | P3 |
| `app/governance_rules.py` | `app/governance/rules.py` | 移动 | P2 |
| `app/governance_scoring.py` | `app/governance/scoring.py` | 移动 | P2 |
| `app/governance_conflicts.py` | `app/governance/conflicts.py` | 移动 | P1 |
| `app/governance_service.py` | `app/governance/service.py` | 移动 | P2 |
| `app/governance_maintenance.py` | `app/governance/maintenance.py` | 移动 | P2 |
| `app/knowledge.py` | `app/knowledge/index.py` + `segments.py` | 移动+拆分 | P1 |
| `app/db_backup.py` | `app/backup/physical.py` | 移动 | P1 |
| `app/snapshots.py` | `app/backup/logical.py` | 移动 | P1 |
| `app/portable_backup.py` | `app/backup/portable_export.py` | 移动 | P1 |
| `app/portable_restore.py` | `app/backup/portable_restore.py` | 移动 | P1 |
| `app/mcp_server.py` | `app/mcp/core_tools.py` + `gov_tools.py` | **拆分** | P1 |
| `app/mcp_server_gov.py` | `app/mcp/gov_entry.py` | 移动 | P3 |
| `app/admin_ui.py` | `app/api/app.py` + `routes.py` | 移动+拆分 | P1 |
| `app/admin_ui.html` | `app/api/templates/index.html` | 移动 | P2 |
| `app/star_map.py` | `app/api/star_map.py` | 移动 | P3 |
| `app/static/3d-force-graph.min.js` | `app/static/`（不动） | 保留 | — |
| `app/py.typed` | `app/py.typed`（不动） | 保留 | — |

**包入口兼容（关键）**：`pyproject.toml` 的三个 console entry 必须同步改：
```toml
bionic-agent            = "app.mcp.core_tools:_run_cli"      # 或 app.mcp:__cli_core
bionic-agent-governance = "app.mcp.gov_entry:_run_cli"
bionic-agent-admin      = "app.api.app:_run_cli"
```
且 **DSH registry 的 `-m` 入口也要改**（`registry.json` 里是 `-m app.mcp_server` 与 `-m app.mcp_server_gov`）。**建议在 `app/mcp_server.py` 与 `app/mcp_server_gov.py` 留一层 3 行 shim（`from app.mcp.core_tools import *` 之类）**，让 `-m app.mcp_server` 继续可用 2~3 个版本，避免改 registry 造成 MCP 服务中断。

---

## 15. 推荐重构顺序

**原则：先补测试 → 再修缺陷 → 再收敛重复 → 再分层 → 最后才搬目录。** 每一步都要能单独回滚、单独验证。

| 期 | 内容 | 前置 | 验收 |
| --- | --- | --- | --- |
| **第 0 期** | 建立基线快照：记录当前 HEAD、33 项测试结果、六套评测指标（45/24/48/38/NL/PURE）、生产库指纹 | — | 一份 baseline 文件 |
| **第 1 期** | 修 **P1-1**（8 处未编码 URI） | 第 0 期 | `portable_backup_metadata_test` 90 + `portable_restore_test` 64 + 新增特殊路径用例；六套评测零变化 |
| **第 2 期** | 补 **P2-3**（22 个零覆盖工具各加 1 冒烟用例） | 第 0 期 | `run_tests.py` 从 33 项增至 33+22 项或新增 1 个 `mcp_tools_smoke_test.py` |
| **第 3 期** | 收敛重复：**P3-1**（`_now` 8 份 → `app/core/timeutil.py`）、**P3-2**（`_parse_ts` 5 份）、**P3-3**（`_json`/`_event`/`_run` 只改名，**不合并**）、**P2-5**（connect/transaction 参数化）、**P2-6**（schema 检测 5 处）、**P3-4**（sidecar/wal 各 2 份） | 第 2 期 | 全部 33 项 + 新增测试通过；六套评测零变化 |
| **第 4 期** | 修 **P2-4**（COEXISTING_CATEGORIES 统一）、**P2-8**（db_snapshots 进 REQUIRED_TABLES）、**P2-7**（SKIP_TABLES 合并） | 第 3 期 | `conflict_triage_test` 33 + `snapshot_test` 57 + `init_db_probe_test` 94 |
| **第 5 期** | **P1-2 第一期**：把 `supersede_memory`/`handoff_task`/`save_memory` 的 SQL 下沉到 `app/memory/store.py` | 第 2、3 期 | 这 3 个工具的返回逐字节不变（写断言锁）；全部测试通过 |
| **第 6 期** | **P1-2 第二期**：剩余 23 个工具 SQL 下沉 | 第 5 期 | 同上 |
| **第 7 期** | **P2-1**：`recall_memories` 8 合 1 拆成 store/scorer/ranker | 第 6 期 | **六套评测逐位一致**（这是硬门槛，一位都不能变） |
| **第 8 期** | **P1-3**：Admin API token 鉴权（保留不设即旧行为） | 独立 | `admin_host_guard_test` 64 + `admin_backup_restore_test` 104 |
| **第 9 期** | 目录迁移（按 §14 映射表），**保留 shim 兼容 `-m` 入口** | 第 1–8 期全部完成 | `git archive HEAD` + pip wheel + 33 项测试 + registry 仍能连上 |
| **第 10 期** | 清理：`build/`、`.egg-info/`、`nul`、`app/*.bak-*`、README 三处数字、`.gitignore` 补 `data/memory_packages/` | 独立 | `git status` 干净 |

**不建议的激进做法**：
- ❌ 一次性搬完所有目录再测（会同时引入几十个 import 错误，无法定位）
- ❌ 先拆 `mcp_server.py` 再下沉 SQL（拆完 26 处内嵌 SQL 会被分散到多个工具文件，更难统一）
- ❌ 为「目录整齐」拆 `memory_lexical.py`（695 行、零 SQL、职责单一，拆分只会增加 import 成本）

---

## 16. 兼容性与回滚策略

### 16.1 必须保持兼容的入口与接口

| 类别 | 具体项 | 兼容策略 |
| --- | --- | --- |
| **MCP 工具** | 85 个工具名、参数 schema、返回结构 | **一字不改**。任何重构都不许改签名 |
| **MCP 进程入口** | `-m app.mcp_server`（core）、`-m app.mcp_server_gov`（gov） | **保留 shim 至少 2 个版本**，否则改 `registry.json` 会造成服务中断 |
| **console entry** | `bionic-agent` / `bionic-agent-governance` / `bionic-agent-admin` | 同步改 `pyproject.toml`，旧名保留别名 |
| **数据库** | `data/agent.db` 路径、15 张表、全部列 | **不迁移**。`PRAGMA user_version` 保持 0（不引入版本号） |
| **环境变量** | `BIONIC_DB_PATH`、`BIONIC_DB_BUSY_TIMEOUT`、`DSH_MEMORY_ROOT`、`BIONIC_GOVERNANCE_*` | 全部保留，默认值不变 |
| **备份包格式** | `FORMAT_VERSION=1`、manifest 键、21 项校验 | 不改。历史 4 个包必须仍能被 `validate_package_archive` 通过 |
| **恢复状态机** | 18 个状态常量、一次性 token TTL 300s、`restore_logs/` 目录 | 不改 |
| **面板** | 75 个路由路径、`app/admin_ui.html` 的 `withBase('/static/...')` 约定 | 路径不改；若加 token，走可选参数 |
| **计划任务** | `Invoke-DshTask.ps1 -Task Snapshot` → `scripts/snapshot_db.py --keep 14` | 若 `scripts/` 分目录，**必须保留 `scripts/snapshot_db.py` 的 shim**，否则 05:00 备份会断 |
| **dsh-panel-client** | `lib/index.js:227 spawn(python, ["-m","app.admin_ui","--port", port])` | 若改入口模块，需同步改这里 |

### 16.2 回滚策略

| 层级 | 手段 |
| --- | --- |
| 代码 | 每期独立 commit，`git revert <sha>` 单期回滚 |
| 数据 | 重构**不触碰**数据库，无需数据回滚 |
| 配置 | `pyproject.toml`/`registry.json` 的改动逐项记录，可单独还原 |
| 备份 | 重构前跑一次 `scripts/physical_backup.py --keep 7` 留一份物理备份（**本次审计未执行**） |
| 验证 | 每期必跑：`python tests/run_tests.py`（33 项）+ `python -X utf8 -m compileall -q app scripts tests` + 六套评测 |

### 16.3 Git 工作区保护

本次审计**未执行任何 git 写操作**。审计后 `git status --porcelain -uall` = **56 行全为 `??`**（原来的 54 个 + 本次新建的 `docs/ARCHITECTURE_AUDIT_S0_S6.md`、`docs/ARCHITECTURE_FILE_INVENTORY.csv`）。**已暂存 0、未暂存 0，无任何 M/A/D 状态。**

---

## 17. 暂不建议修改的部分

| 模块/机制 | 行数 | 为什么不建议动 |
| --- | --- | --- |
| `app/memory_lexical.py` | 695 | **零 SQL、职责单一**（纯词法 + 概念桥）。拆开只会增加 import 成本。唯一要改的是 P2-10（`_segments` 上提），但那会影响全部检索评测，需独立批准 |
| `app/recall_profile.py` | 179 | 纯权重表 + clamp，顶层零 app 依赖（S6-FINAL-FIX 已把 `memory_lifecycle` 依赖改成延迟）。无可拆点 |
| `app/db.py` 的 `probe_database_state` + `init_db` 四分支 | 约 250 | S6-FIX-03 刚验收：`mode=ro&immutable=1` 双模式策略有反证实验支撑。**不要为了整齐改成单模式** |
| `app/db_backup.py` 的 `create_physical_backup` | 约 180 | S6-FIX-02 刚验收：临时名 `.partial-<uuid>` + 验证后才改名 + 清理独立 try/except + 失败不删历史。**顺序是刻意的，不要重排** |
| `app/portable_restore.py` 的状态机 | 约 300 | 18 个状态常量 + 一次性 token + 异常自动回滚。看着复杂，但每个分支都有对应测试（64 项）。**不建议简化** |
| `app/snapshots.py` 的 `restore_snapshot` | 约 120 | S0 修过的 P0：`reversed(live)` 的父子表顺序 + `defer_foreign_keys` + 提交前 `foreign_key_check`。**不要改删除顺序** |
| 概念桥的 α/短语表/seed 阈值 | — | S5-G2/05-A/06 三轮验收：α=0.20 好用、0.25 退化（45 题 R@1 30/41→29/41）；短语表是人工枚举的中文词表，改动必须重跑六套评测 |
| `_SYNONYM_GROUPS`（26 组） | — | 来自 `app/knowledge.py:525`，被词法引擎借用。改任何一组都会影响 45/24/48/38 四套评测 |
| `RECALL_WEIGHTS`（`semantic 0.27 / keyword 0.60 / …`） | — | S5-E 精确调优的最终值，理由写在源码注释（`importance` 已内含 F/C，融合层再加一遍是双计） |
| `tests/` 全部 34 个文件 | — | 本次审计**不修改任何测试、不改任何阈值、不改 ground truth** |
| `dsh-panel-client/` | 25 文件 | Node 侧独立发布单元，与 Python 侧解耦。本次不涉及 |
| `data/model_cache/` | 252 MB | fastembed 缓存，删了要重下 40+ MB。**不要当垃圾清理** |

---

## 18. 最终审计结论

### 18.1 总体判断

**架构健康度：中上。** 这是一个**功能完整、数据安全机制扎实、算法层分离良好**的项目，但存在**接口层过厚（26 工具直写 SQL）、目录职责混杂（`app/` 装 5 类东西）、重复实现较多（`_now` 8 份）**三项结构性债务。

**数据安全方面接近满分**：
- 三条备份路径职责互不重叠，且都有验证（逻辑快照 FK 门、物理备份 integrity+FK、便携包 21 项校验）
- 全部裸 `sqlite3.connect` 都有 try/finally 关闭
- 零裸 `except:`
- 未发现路径穿越、未发现凭据泄漏、未发现 dsh-memory 反向写入
- 只读探测的 WAL 感知策略（`immutable` vs `mode=ro`）有反证实验支撑

**算法质量方面优秀**：
- `memory_lexical.py` 零 SQL、`recall_profile.py` 零 SQL —— 真正的「算法与数据分离」
- 概念桥有完整诊断（40 项测试）、有边界案例（α=0.25 退化已实测）、有复评触发条件（README `:522`）
- 六套评测（45/24/48/38/NL/PURE）全部有基线记录

### 18.2 最值得优先处理的三件事

1. **P1-1（未编码只读 URI）** —— 唯一有实测可复现危害的缺陷（含 `#` 的路径读空库 + 留垃圾文件），修复成本最低（8 处替换 + 复用已有正确实现）。
2. **P2-3（补 22 个零覆盖工具测试）** —— 这是 P1-2（SQL 下沉）的前置条件。没有测试就重构 26 个写入路径，风险不可控。
3. **P1-2（26 工具 SQL 下沉）** —— 最大的架构边界问题，但必须分三期、在测试补齐之后做。

### 18.3 明确不建议做的事

- ❌ **不要为了目录整齐而大搬**：149 个文件里只有 **38** 个需要变更位置（34 个移动 + 3 个移动+拆分 + 1 个拆分），**111** 个保留原地。`app/memory_lexical.py` 等职责纯粹的模块不该拆。
- ❌ **不要一次性重构**：`app/` 的 import 边密度极高（`db.py` fan-in 49），一次性搬目录会产生几十个连锁错误。
- ❌ **不要合并 85 个 MCP 工具**：Core/Gov 分离是「治理工具按需挂载、降低上下文成本」的设计，合并会破坏这个边界。
- ❌ **不要调概念桥参数**：α 距退化边界只有 0.05，任何调整都必须重跑六套评测并独立批准。
- ❌ **不要清理 `data/model_cache/`**：252 MB 看着大，但删了要重新下载模型。
- ❌ **不要改测试阈值或 ground truth** 来让重构「通过」。

### 18.4 与 S6-FINAL 结论的关系

S6-FINAL 的结论是「**有条件通过，P0=0**」，前提是三条 P1 已修复（host 守卫 ✅、init_db 只读探测 ✅、物理备份 ✅）。本次架构审计**独立复核确认这三项已实质解决**，并新发现 1 条 P1（未编码 URI），与 S6-FINAL 的「面板零鉴权」合计 **P1 = 3**。

**P0 = 0 不变。可继续发布，但建议在第 1 期先修 P1-1。**

---

## 附录 A：审计方法学与可复现性

### A.1 中间产物（均在系统临时目录，未落入项目）

| 文件 | 内容 |
| --- | --- |
| `%TEMP%\aud-s06\audit.py` | AST 扫描脚本：import 边、函数定义、`sqlite3.connect`、硬编码路径、装饰器归属 |
| `%TEMP%\aud-s06\audit.json` | 上述脚本输出：71 模块、85 工具、1 SCC、37 组重名、28 处 connect、3 处硬编码路径 |
| `%TEMP%\aud-s06\inventory.json` | 149 个文件的 kind/rel/bytes/lines/funcs/public/classes |
| `%TEMP%\aud-s06\consumers.json` | 每个 app 模块被谁引用（精确 AST import） |
| `%TEMP%\aud-s06\q3.py` | 逐边判定「模块级 vs 延迟 import」 |
| `%TEMP%\aud-s06\toolcov3.py` | 46 个未直接调用工具的底层覆盖核查 |
| `%TEMP%\aud-s06\uriown.py` | 未编码 URI 所属函数定位 |
| `%TEMP%\aud-s06\coex2.py` | 共存类别常量漂移的只读验证（临时库，未碰生产） |
| `%TEMP%\aud-s06\mkcsv.py` | 生成 `docs/ARCHITECTURE_FILE_INVENTORY.csv` |

### A.2 口径说明

- **行数**：一律用 Python `splitlines()`，与 `wc -l` 差 1（本报告「634 行」对应 `wc -l` 的 635）。历史报告中出现的 ±1 差异即此口径差，不是错误。
- **测试覆盖率**：静态文本匹配（测试文件里是否出现 `函数名(`），不是插桩。已排除 `verify_mcp_split_test.py` 的清单核对，并人工复核了别名/HTTP 间接调用。
- **依赖图**：基于 `import` 语句，不解析动态导入。已人工核查 `mcp_server.py` 的延迟导入点。
- **85 工具**：按装饰器 `@mcp.tool()` / `@mcp_gov.tool()` 计数，与 `_tool_manager._tools` 运行时核对一致（S6-FINAL 已实测 36+49=85、交集为空）。

### A.3 本次审计**未做**的事（明确声明）

| 未做 | 原因 |
| --- | --- |
| 运行测试套件 | 避免触碰生产库与 sidecar；沿用 S6-COMMIT 的 33/33 记录 |
| 连接生产库执行结构 SQL | 任务书明确禁止；表结构结论来自源码 DDL + S6-FINAL 对隔离副本的历史实测 |
| 修改任何源文件 | 任务书明确禁止（唯一新建物是本报告与 CSV） |
| git add/commit/push/reset/checkout/clean | 任务书明确禁止 |
| 删除/覆盖 54 个未跟踪文件 | 任务书明确禁止 |
| 实施任何重构 | 任务书明确禁止，等审阅 |

---

## 附录 B：本次审计的产出清单

| 文件 | 说明 |
| --- | --- |
| `docs/ARCHITECTURE_AUDIT_S0_S6.md` | 本报告（18 节 + 2 附录） |
| `docs/ARCHITECTURE_FILE_INVENTORY.csv` | 149 行文件清单，10 列（path/type/bytes/lines/public_funcs/responsibility/references/target_path/move_advice/risk_level） |

**审计前后对比**：

| 项 | 审计前 | 审计后 | 是否变化 |
| --- | --- | --- | --- |
| HEAD | `862714c2ad149dd65060190d40a852effe2e7715` | 同 | ✅ 无变化 |
| `git status -uall` | 54 行（全 `??`） | **56 行**（全 `??`，多 `docs/ARCHITECTURE_AUDIT_S0_S6.md` 与 `docs/ARCHITECTURE_FILE_INVENTORY.csv`） | ⚠️ 仅新增本次两份交付物 |
| 已暂存 / 未暂存 | 0 / 0 | 0 / 0 | ✅ 无变化 |
| `data/agent.db` sha256 | `19D8CE98…EFA4E` | 同 | ✅ 无变化 |
| `data/agent.db` size | 8581120 | 同 | ✅ 无变化 |
| `data/agent.db` mtime | `2026-11-19T10:44:59.3862755Z` | 同 | ✅ 无变化 |
| `-wal` / `-shm` | 均不存在 | 均不存在 | ✅ 无变化 |
| `registry.json` sha256 | `C9591B7D…4CF67` | 同 | ✅ 无变化 |
| `记忆.jsonl` sha256 | `F80E550D…63499` | 同 | ✅ 无变化 |
| 源文件（`app/`、`tests/`、`scripts/`） | — | — | ✅ 零改动 |

---

**报告结束。**

> 本次审计严格遵守「只读」约束：除新建 `docs/` 目录及其内两份交付物外，未创建、修改、删除、移动任何项目文件；未修改生产数据库与 dsh-memory；未执行任何 git 写操作。所有结论均附代码位置或实测证据；未能完全验证的项目已在正文与附录 A.3 明确标注。
