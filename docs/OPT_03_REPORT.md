# OPT-03 MCP 接口层整理与 SQL 下沉 — 实施报告

> 范围：`app/mcp_server.py`（1738 行 / 85 工具 / 67 处 `.execute(`）拆包 + 三期 SQL 下沉 + 面板查询下沉。
> 结论：**工具契约零变化、六套评测逐位一致、38/38 测试通过、生产库零写入**。
> 日期：2026-11-23　基线 HEAD：`862714c2ad149dd65060190d40a852effe2e7715`

---

## 0. 一页摘要

| 项目 | 下沉前 | 下沉后 |
|---|---|---|
| MCP 工具实现位置 | `app/mcp_server.py` 单文件 1738 行 | `app/mcp/` 5 文件 + `stores/` 4 文件 |
| `app/mcp_server.py` | 81684 B / 1738 行 / 67 处 SQL | **912 B / 19 行 shim** / 0 处 SQL |
| `app/mcp/*.py` 的 `.execute(` | 67 | **0** |
| `app/admin_ui.py` 的 `.execute(` | 12 | **0** |
| SQL 落点 | 全部内联在工具函数 | `memory_store` 16 / `search_store` 12 / `task_store` 11 / `panel_queries` 12 |
| MCP 工具数 | core 36 + gov 49 = 85 | **core 36 + gov 49 = 85**（不变） |
| 工具名/参数/返回结构 | — | **逐条相同**（`verify_mcp_split_test` 6/6 + 六套评测 `mcp_tools` 逐条比对） |
| 全量测试 | 38 套 | **38/38 通过 / 79.7s** |
| 生产库 | `2B85172B…D8739F` / 8859648 B | **同一 sha**（全程零写入） |

**一句话**：把 1738 行的单文件拆成「runtime / core_tools / gov_tools / server + shim」，再把 67 处内联 SQL 下沉到按域划分的 store 层，只搬不改；`.execute(` 在入口与工具层归零，读写全部收进 4 个数据访问模块。

---

## 1. 为什么做这件事

`docs/ARCHITECTURE_OPTIMIZATION_TASKS.md:190` 的 OPT-03 定位是 **MCP 接口层整理**，两条动因：

1. **单文件承载 85 个工具**（1738 行），任一工具改动都要在 1700 行里定位；core 与 gov 两套工具混在一个命名空间里，看不出边界。
2. **67 处内联 SQL 散在 27 个工具函数里**，同一张表的同一条语句被抄了 9 遍（`INSERT INTO events`）、8 遍（`SELECT * FROM memories WHERE id=?`）。改一个列名要全文搜，漏一处就是运行期 `OperationalError`。

**任务清单写死的前置条件**（`:244`）：

> OPT-04a（补 22 个工具冒烟）**必须**完成 —— 这是 P1-2 的安全网。没有它，下沉 SQL 等于在无测试的代码上动手术。

这条前置已在本轮开头先补掉了：**85 个工具里当时有 49 个在工具层零调用**（58%），新增 `tests/mcp_tools_smoke_test.py`（466 行 / 58 项）把它们逐个真调一遍，并带一个自检项防止以后有人「精简」这个文件时静默丢工具。

---

## 2. 阶段一：拆包（3.1）

### 2.1 拆分前的实测结构

用 AST 而非肉眼统计：

- 文件 81684 B / 1738 行 / CRLF 1737 / 无 BOM，sha `91F85BD8062EB8266E0672C9598BA49299D7BBDB8779D63D9560A3BD4A9E3833`
- `@mcp.tool()` **36** 个、`@mcp_gov.tool()` **49** 个、非工具函数 **10** 个
- **跨域调用实测为零**：core 工具不调 gov 工具，gov 工具也不调 core 工具 → 按装饰器分域是干净的切法

> 统计踩坑：`ast.unparse(装饰器)` 输出的是 `mcp.tool()`，**不带 `@`**。第一版判 `"@mcp.tool" in d`，把 36 个工具全判成 helper。正确判据是 `d.startswith("mcp.tool")`。

### 2.2 落盘布局

下表是**下沉完成后的当前形态**：

| 文件 | 字节 | 行 | 内含 `.execute(` | 内容 |
|---|---:|---:|---:|---|
| `app/mcp/__init__.py` | 290 | 7 | 0 | 包说明（四块各管什么） |
| `app/mcp/runtime.py` | 6407 | 162 | 0 | 两个 FastMCP 实例 + `GOVERNANCE_COLUMNS` + 10 个 helper |
| `app/mcp/core_tools.py` | 36767 | 876 | 0 | 36 个日常工具 |
| `app/mcp/gov_tools.py` | 32107 | 807 | 0 | 49 个治理工具 |
| `app/mcp/server.py` | 6418 | 212 | 0 | 装配层（import 副作用即注册）+ `__all__`（103 名） |
| `app/mcp_server.py` | **912** | **19** | 0 | 向后兼容 shim |

> 拆包刚完成、SQL 尚未下沉时的中间形态（`core_tools.py` 42131 B / 905 行、`gov_tools.py` 32301 B / 810 行、`runtime.py` 6222 B / 162 行）**没有留存逐文件 `.execute(` 计数** —— 那是一个未提交的中间状态，现已无法精确回算。此处只给字节与行数，不编造 SQL 分布。

`runtime.PROJECT_ROOT` 比原来多一层（`.parent.parent.parent`），因为文件下沉了一级。

### 2.3 shim 为什么必须有

`app/mcp_server.py` 不是「懒得改路径」的产物，它钉着三处**改了就静默断掉**的地方：

1. `pyproject.toml` 的 console-script：`bionic-agent = app.mcp_server:_run_cli`
2. DSH 的 MCP registry（`C:\Users\xing\.dsh\skill-mcp-manager\registry.json`）把启动命令钉成 `python -m app.mcp_server`
3. `scripts/` 与 `tests/` 里大量 `from app import mcp_server as S`

shim 只有 8 行有效代码：`from app.mcp.server import *`、再取 `__all__`、`__main__` 时调 `_run_cli()`。

### 2.4 逐字核对（这一步是拆包能不能算「只搬不改」的分界）

`_verify_split.py` 用 AST 从备份里切 **95 个函数**、从新 4 个文件里切 95 个，**逐字符比对**：

```
丢失 0 / 新增 0 / 源码逐字不一致 0
```

做法：`seg(node)` 取 `min([node.lineno] + [装饰器 lineno])` 到 `end_lineno` 的**原始源码切片**（不重新格式化）；写盘一律 `write_text(..., encoding="utf-8", newline="\n")`。

### 2.5 拆包连带暴露的两个真问题

**问题一：`app.mcp` 不进 wheel（真回归，已修）**

`pyproject.toml:18` 原写 `packages = ["app"]`。setuptools 的 `packages` 是**显式声明，不是自动发现** —— `find_packages()` 实得 `['app', 'app.mcp']`，但显式列表只有 `app`，于是：

- `build_zip.py` 走 `rglob` 收全了 → 解包可用
- pip 装出来的 wheel 少 `app/mcp/` → 一 `import app.mcp_server` 就 `ImportError`

两条分发路径悄悄分叉，而 zip 那条是绿的，所以没人会发现。已改为 `["app", "app.mcp"]` 并加注释（新增子包必须回来加一行）。后续下沉 store 又加了一次，现在终态是 `["app", "app.mcp", "app.mcp.stores"]`。

**问题二：测试取证路径失效（已修）**

`tests/portable_restore_mcp_test.py:195` 的「15b 工具不含令牌签发逻辑」原来用

```python
Path(srv.__file__).read_text().split("def _pkg_path")[1].split("def _run(")[0]
```

取一段源码做断言。`_pkg_path` 搬到 runtime 后 shim 只有 19 行 → `IndexError: list index out of range`。

改成扫 `app/mcp/core_tools.py` + `app/mcp/gov_tools.py` 全文。**这不是放水而是收紧**：原先只看两个锚点之间的一段，现在看两个工具文件的全篇。

---

## 3. 阶段二：SQL 下沉（3.2 三期）

### 3.1 设计红线

三个 store 的模块 docstring 里都写了同一条：

> **每个函数都接收调用方传入的 `conn`，自己不 open、不 commit、不开事务。**

理由不是洁癖：以 `save_memory` 为例，它在一个 `with connect() as conn:` 里写 memories + 记 `memory.created` 事件。如果 store 自己开连接，原本 1 个事务会变成 2 个独立事务 —— 中途失败就留下「记忆存在但没有审计」的半截状态。事务边界必须留在工具函数。

### 3.2 三期分解与落点

| 期 | 目标工具 | 处数 | 落点 | 结果 |
|---|---|---:|---|---|
| 一期 | `save_memory`/`supersede_memory`/`get_memory`/`update_memory`/`archive_memory`/`restore_memory` | 19 | `stores/memory_store.py` | 6 工具内 execute → 0 |
| 二期 | `create_task`/`list_tasks`/`get_task`/`save_stage_snapshot`/`handoff_task`/`complete_task`/`list_events` | 20 | `stores/task_store.py` | 7 函数 → 0 |
| 三期 | `search_memories`/`find_related_memories`/`link_memories`/`get_memory_links`/`set_memory_expiry`/`embedding_status`/`index_memories`/`semantic_search_memories`/`backfill_memory_types` + 治理侧 4 个 | 27 | `search_store.py` + `memory_store.py` 扩充 | 13 函数 → 0 |
| 3.3 | 面板 7 个路由 + `api_stats` | 12 | **`app/panel_queries.py`**（不在 mcp/stores） | `admin_ui.py` 12 → 0 |

### 3.3 `app/panel_queries.py` 为什么单独放

docstring 里写明：

> 那里放的是 **MCP 工具**用的 store，服务对象是 85 个工具；这些查询只服务 Web 面板的统计展示，跟 MCP 契约无关。混在一起会让「MCP 的 store 层」概念失焦，也会让面板的展示需求悄悄渗进工具层。

并且**全部只读** —— 12 处全是 SELECT，没有一个写语句；面板路由自己仍用 `with connect() as conn:` 掌握连接。

一个细节：`stats()` 把六个 COUNT 放进**同一个连接**里查完，注释说明「避免界面上几个数字来自不同瞬间的库」。

### 3.4 下沉前后 `.execute(` 分布（AST 精确统计）

| 文件 | 前 | 后 |
|---|---:|---:|
| `app/mcp_server.py` | 67 | 0 |
| `app/mcp/__init__.py` | — | 0 |
| `app/mcp/runtime.py` | — | 0 |
| `app/mcp/core_tools.py` | — | 0 |
| `app/mcp/gov_tools.py` | — | 0 |
| `app/mcp/server.py` | — | 0 |
| `app/mcp/stores/memory_store.py` | — | 16 |
| `app/mcp/stores/search_store.py` | — | 12 |
| `app/mcp/stores/task_store.py` | — | 11 |
| `app/admin_ui.py` | 12 | 0 |
| `app/panel_queries.py` | — | 12 |

**验收目标（`grep -c "\.execute(" app/mcp/*.py` → 0）已达成。**

### 3.5 67 → 39 的账要能对上（否则就是丢了语句）

下沉后 store 内是 39 处 `.execute(`，比原 67 少 28。减少有两种可能：**合并去重** 或 **丢语句**。必须证明是前者。

做法：把原文件 67 处的第一个参数做 `ast.literal_eval` 求值（动态拼接的取源码片段），得到 **38 条唯一 SQL**；再把三个 store 的 SQL 常量求值，得到 **35 条**。差集只剩 3 条：

```
- f"UPDATE memories SET {', '.join(updates)} WHERE id=? AND status='active'"
- fsql
- sql
```

**这 3 条都是动态拼接的 SQL**（`update_memory` 的字段拼接、`search_memories` 的动态 WHERE），本来就不是字面量，以常量形式存在不了。**38 = 35 常量化 + 3 动态，一条不丢。**

那 28 处减少来自哪里，逐条对上：

| 原语句 | 原处数 | 下沉后 | 省 |
|---|---:|---:|---:|
| `INSERT INTO events(event_type,detail)` | 9 | `memory_store.insert_event` 1 | 8 |
| `INSERT INTO events(task_id,event_type,detail)` | 5 | `task_store.insert_task_event` 1 | 4 |
| `SELECT * FROM memories WHERE id=?` | 8 | `memory_store.fetch_memory` 1 | 7 |
| `SELECT * FROM tasks WHERE id=?` | 5 | `task_store.fetch_task` 1 | 4 |
| `SELECT id FROM memories WHERE id=?` | 3 | `memory_store.memory_exists` 1 | 2 |
| `INSERT INTO memory_links(...)` | 2 | `insert_memory_link` 1 | 1 |
| `INSERT INTO snapshots(...)` | 2 | `insert_snapshot` 1 | 1 |
| `SELECT * FROM snapshots WHERE id=?` | 2 | `fetch_snapshot` 1 | 1 |
| 合计 | | | **28** |

**8 个工具各自写一遍的 `SELECT * FROM memories WHERE id=?`，现在共用一个 `fetch_memory`；14 处 `INSERT INTO events` 收敛成两个函数**（带 `task_id` 与不带的语义不同，不能合成一个 —— 合并就得给 `task_id` 造默认值，而 NULL 与 0 在查询里意思不同，这条写在了 `task_store` 的注释里）。

---

## 4. 等价性验证（三套对拍 + 一套同刻对照）

重构最怕「测都绿、行为变了」，所以每一期都做了双实现对拍。

### 4.1 一期：`_opt03_store_ab.py` → 14/14

- A 腿 = 当前实现，B 腿 = 从 `app/mcp_server.py.bak-opt03-split` AST 切出的旧函数（`strip_decorators` 去掉 `@mcp.tool()` 避免二次注册）
- 两腿各用一份 `Connection.backup()` 库副本，**互不干扰、也不碰生产库**
- 场景覆盖成功与失败两侧：`save_memory` 4 例（含空内容 / ttl）、`get_memory` 3 例（含不存在 / 归档且不要）、`update_memory` 4 例（含无字段）、`archive_memory` 3 例（含重复归档）、`restore_memory` 2 例（含已活跃）、`supersede_memory` 4 例（含旧条已归档 / 空内容）

### 4.2 二期：`_opt03_task_ab.py` → 22/22

7 个任务工具 × 场景覆盖 create / list / get / snapshot / handoff / complete / complete 重复 / events / 四类失败侧（不存在 id、坏 JSON）。

### 4.3 三期：`_opt03_sql3_ab.py` → 41/41

13 个工具、31 个场景。首轮 40/41，唯一 FAIL 是 `embedding_status`：

```
差异字段 {'model_load_status': ('pending','ready'), 'model_ready': (False,True), 'note': (...)}
```

**这三项与 SQL 无关** —— 它们来自进程内 `embeddings.model_state()` / `is_ready()`：先跑的 A 腿模型还在 `pending`，后跑的 B 腿已 `ready`。真正该保证的 `total=57 / indexed=56 / need=1` 两侧完全一致。修法：加 `MODEL_STATE_FIELDS` 白名单 + `drop_model_state()` 后比对 → 41/41。

同时把失败信息从「截断 140 字」改成**逐字段列出差异**（`差异字段={k: (A,B)}`）—— 原先截断正好把关键字段藏起来了。

### 4.4 三套对拍共同的三个坑（都已修，值得记住）

1. **`expires_at` 微秒差判 FAIL**（`041992` vs `232667`）：`created_at`/`updated_at` 来自库的 `CURRENT_TIMESTAMP`，两腿跨秒必然不同。修法：时间戳归一化为 `<TS>` 占位符，**另做 ≤5s 的秒级容差校验** —— 归一化后比的是「该保证的东西」，容差校验又不放过真正的偏差。
2. **对拍脚本自己踩了 S6-FIX-03 的坑**：`clone_db()` 用 `mode=ro` 打开生产库，跑完发现 `-wal`/`-shm` 的 mtime 被刷到当下。`mode=ro` 对 WAL 库**不是零副作用**。修法：新增 `_prod_ro_uri()`，旁侧 `-wal` 无内容时用 `mode=ro&immutable=1`（真零副作用），有内容才退回 `mode=ro`；并新增 `_sidecar_stamps()` 断言 sidecar 的 mtime 未变。
3. **弱断言**：原来写 `check("生产库未被写入", PROD_DB.stat().st_size > 0, ...)` —— 这条永远通过。换成「主库 sha 未变」+「sidecar mtime 未变」两条。

### 4.5 24 题锚点序列的「假失败」与最终定案

整卷评测对拍时，`_opt02_ab.py` 报：

```
[FAIL] 24 题 top 锚点序列全同  [["web 和 desktop 的插件为什么不共通", [...], [...]]]
```

**打印出来的 A/B 前 5 条一模一样。** 原因：断言比的是全 60 条，失败信息只打前 5 条。

深挖后定位到 top60 的第 **37/38** 位（id 40 vs 54，`P07` vs `F11`），分数轨迹：

| 时刻 | id40 | id54 | 顺序 |
|---|---|---|---|
| 15:59:29 | 0.212003 | 0.211998 | [40, 54] |
| 16:05:51 | 0.212003 | 0.211998 | [40, 54] |
| 16:12:08 | 0.212001 | 0.211998 | [40, 54] |
| 16:17:35 | 0.211998 | 0.211998 | **[54, 40]** |
| 16:18:50 / 16:18:57（同一份代码连跑两次） | 0.211998 | 0.211998 | [54, 40] |

`0.211998` 是四舍五入的下限，两人都落到同一格 → 排序键退化成 `(score, updated_at, id)` 的第三段，id 大的排前面。

**这是时钟漂移，不是实现差异。** `activation_spreading` 用 `datetime.now()` 算 recency，分数随时间单调下降；两条腿相隔十几分钟，差 5e-6 的一对就会互换。

**决定性证据**：`_opt03_sameclock_ab.py` —— 同一进程、同一库副本内，交替执行旧实现（`_opt02_c_bak_life.py` 里 OPT-02-C 拆分前的 `recall_memories`）与当前实现各 3 轮，4 组查询（60 / 20 / 10 / 5 条）**12/12 通过，逐条 id + score 完全相同**。

**断言口径的修法（关键：不是放松成静默通过）**：

```python
# 24 题要分档看：top5 是这条断言真正守护的东西（@1 与 @3 的判定都落在前 5 位）
if ga[:5] != gb[:5]:
    d24_top5.append(...)
elif ga != gb:
    d24_deep.append((query, pos))
ck("24 题 top5 锚点序列全同", not d24_top5, ...)     # 严格
if d24_deep:
    print(f"[INFO] 24 题 top6..top60 有 {len(d24_deep)} 题的深位名次互换（activation 时钟漂移…）")
```

改后：`[PASS] 24 题 top5 锚点序列全同` + `[INFO] 24 题 top6..top60 有 1 题的深位名次互换（('web 和 desktop 的插件为什么不共通', 37)）`。

**为什么这不是放水**：24 题的判定口径本来就是 `top_k=60` 但只取 `rk = min(正确项 rank)` 判 @1/@3，**判定完全落在前 5 位**；深位互换不影响任何指标，而原断言却在为一个不参与判定的区间报红，还把真实差异藏在了截断后面。

---

## 5. 验收结果

### 5.1 测试与静态检查

| 项目 | 结果 |
|---|---|
| `python tests/run_tests.py` | **38/38 通过 / 79.7s** |
| `compileall -q app scripts tests` | **rc=0** |
| `tests/mcp_tools_smoke_test.py` | 58/58（49 个此前零调用工具全部真调） |
| `tests/verify_mcp_split_test.py` | 6/6（core 36 / gov 49 / 无重叠 / 合计 85） |
| 面板 6 套件 | `views_test` 18/18、`timeline_test` 12/12、`admin_backup_restore_test` 104/0、`admin_token_auth_test` 全过、`admin_auth_frontend_test` 全过、`admin_host_guard_test` 64/0 |

### 5.2 真实 MCP 握手（不走 TestClient，真起进程走 stdio）

```
python -m app.mcp_server      → ('bionic-agent',            36)
python -m app.mcp_server_gov  → ('bionic-agent-governance', 49)
shim 再导出检查               → OK
server.__all__                → 103 名
```

### 5.3 六套评测（`_opt03_sql33.json` vs 旧完整版 `_opt03_sql1_A_old.json`）

| 评测 | 旧版 | 新版 |
|---|---|---|
| 45 题 R@1 / R@3 / R@5 | 0.7317073170731707 / 0.926829268292683 / 0.926829268292683 | **完全相同** |
| 45 题 MRR@5 / nDCG@5 | 0.8211382113821137 / 0.8632387624564479 | **完全相同** |
| 24 题 @1 / @3 / 反例 | 19 / 23 / 2 | **完全相同** |
| 标题自召回 | 54/56，@3 55 | **完全相同** |
| 知识库 | 38/38，@3 38 | **完全相同** |
| NL / PURE | 5/8、1/3 | **完全相同** |
| MCP | 36 / 49 / overlap `[]` | **完全相同** |
| `counts` | 61 / 57 / 144 / 1046 / 274 | **完全相同** |
| **骨架 sha** | `68C1DFCEB6255C167717432614191533AAF07D77195DEA7F6658D9AB55CEFE42` | **同一个** |

`_opt02_ab.py` 最终：**`=== 全部通过 ===`**。

### 5.4 打包（双路径）

- **wheel**：`bionic_agent_mcp-0.1.0-py3-none-any.whl` **963829 B / 50 条目**，含 `app/mcp/` 5 文件 + `app/mcp/stores/` 4 文件 + `app/panel_queries.py` + 静态素材（`3d-force-graph.min.js` / `nebula.jpg` / `star-dust.jpg` / `star-surface.png`）
- **干净 venv 实装**（按项目声明的依赖 `mcp>=1.30.0,<2.0.0` 装 → mcp 1.30.0）：从 `site-packages\app\__init__.py` 导入成功，`core = 36`、`gov = 49`，`panel_queries` OK
- **zip**：`build_zip.py` 1083379 B / 75 files（`app/mcp/__init__.py` 被 `_` 前缀规则排除，但解包后 import 与 36/49 注册均正常）

> venv 验证踩坑：第一次用 `pip install mcp>=1.0` 装到了 **mcp 2.x**，报 `ModuleNotFoundError: No module named 'mcp.server.fastmcp'`（2.x 把 FastMCP 改名 MCPServer）。这不是打包问题 —— 项目自己声明的就是 `<2.0.0`。按声明重装即通过。

### 5.5 生产库零写入

| 项 | 值 |
|---|---|
| sha256 | `2B85172BD07C9FC85F53D37A5774CC9719B50CF4EF8FA953A03FB03AD3D8739F` |
| size | 8859648 B |
| mtime | `2026-11-21T23:10:59.0127791+08:00`（**早于本轮开工**） |
| `-wal` / `-shm` / `-journal` | 全部不存在 |
| 内容 | memories 61 / active 57 / events 228 / links 20 / max_memory_id 70 / 今日 events 0 |

全轮次所有只读访问均经 `immutable=1` 或只读 URI；对拍脚本的 sidecar 副作用发现后已修，之后跑前跑后 sidecar 状态完全一致。

---

## 6. 修改文件清单

### 6.1 新增

| 文件 | 字节 | 行 |
|---|---:|---:|
| `app/mcp/__init__.py` | 290 | 7 |
| `app/mcp/runtime.py` | 6407 | 162 |
| `app/mcp/core_tools.py` | 36767 | 876 |
| `app/mcp/gov_tools.py` | 32107 | 807 |
| `app/mcp/server.py` | 6418 | 212 |
| `app/mcp/stores/__init__.py` | 231 | 5 |
| `app/mcp/stores/memory_store.py` | 8296 | 204 |
| `app/mcp/stores/search_store.py` | 6114 | 151 |
| `app/mcp/stores/task_store.py` | 3532 | 88 |
| `app/panel_queries.py` | 4617 | 108 |
| `tests/mcp_tools_smoke_test.py` | 23034 | 466 |
| `app/mcp_server.py.bak-opt03-split` | 81684 | 1738 |

### 6.2 修改

| 文件 | 变化 | 说明 |
|---|---|---|
| `app/mcp_server.py` | +13 / −1732 | 1738 行 → 19 行 shim |
| `app/admin_ui.py` | +282 / −49 | 12 处 SQL → `panel_queries`；此前含星图静态后缀与令牌认证改动 |
| `pyproject.toml` | +5 / −1 | `packages` 三项 + 注释 |
| `tests/portable_restore_mcp_test.py` | +50 / −5 | registry 基线化（29→30 项）+ 15b 取证路径改全文扫描 |
| `tests/db_isolation_test.py` | +1（跟踪内） | 文件数断言 `== 37` → `>= MIN_TEST_FILES` |
| `README.md` | +54 / −4 | 测试表补齐 + 套件数 34→38 |

### 6.3 行尾风格

新增的 `app/mcp/**`、`app/panel_queries.py`、`tests/mcp_tools_smoke_test.py` 全部 **纯 LF、无 BOM**；`app/admin_ui.py` 保持**全 CRLF**（1729 LF / 1729 CRLF / loneLF 0）。`app/mcp_server.py` shim 纯 LF。

---

## 7. 遗留问题

| 编号 | 内容 | 等级 | 说明 |
|---|---|---|---|
| N-1 | 拆包后 `core_tools.py` / `gov_tools.py` 各有少量未使用 import（`init_db`、`ensure_warmup`、`json`、`re`、`EmbeddingUnavailable`、`content_hash`、`embed_passages`、`embed_query`、`cosine_similarity`、`load_rules`、`RULE_VERSION`） | 低 | 对照原文件确认这些名字在拆分前**确有使用**，只是分布在不同域；拆包时两边都复制了完整 import 块。本轮约束「禁止顺带清理代码」，未动。无功能影响，`runtime.py` 已 import 过。 |
| N-2 | `tests/portable_restore_mcp_test.py` 的 registry 基线化依赖 `C:\Users\xing\.dsh\...\registry.json` 绝对路径 | 低 | 换机器跑该断言会退化为「registry 不存在」分支。原写法同样写死，本轮只是把「固定上界」换成「运行期基线」。 |
| N-3 | `_opt03_*.py` 四个对拍脚本留在工作区未跟踪 | 低 | 属取证脚本，含旧实现切分逻辑，可作为将来重构的模板保留。 |
| N-4 | `app/mcp_server.py.bak-opt03-split` 留在工作区未跟踪 | 低 | 对拍脚本依赖它切旧函数。确认不再需要后可删。 |
| N-5 | 星图未提交改动仍在工作区 | 中 | 用户明确要求「留在工作区不动」，本轮未触碰。 |

---

## 8. Git 状态

| 项 | 值 |
|---|---|
| HEAD | `862714c2ad149dd65060190d40a852effe2e7715`（**未变**） |
| 分支 | `master` |
| 暂存区 | **0** |
| `git diff --shortstat` | 50 files changed, 1191 insertions(+), 2154 deletions(-) |
| `git status --porcelain -uall` | **315** 行（其中未跟踪占绝大多数，且大部分不是本轮产生的，见下） |
| commit / push / tag | **均未执行** |

删除数远大于新增数，主因是 `app/mcp_server.py` 折掉 1732 行。

未跟踪项的构成（`git status` 按顶层目录归并）：`data/` **166**（其中 `data/restore_backups` 145 + `restore_logs` 21，都是 OPT-01-B-FINAL 恢复实测的产物）、`scripts/dev/` 51、`app/` 16（本轮 11 个 + 星图静态素材 4 + `py.typed`）、`docs/` 8、`tests/` 6（本轮 2 个 + 前几轮 4 个）、`var/` 2、`logs/` 2，其余是历次取证脚本与日志。

**这些文件一个都没删、没清理** —— 本轮约束明确要求保护未跟踪文件。

---

## 9. 回滚方式

按四个独立单元，**同一单元内不可半途**：

1. **拆包**：`git checkout -- app/mcp_server.py pyproject.toml`，删 `app/mcp/`。shim 不在时 console-script 与 registry 都会断，所以要么整拆要么整回。
2. **一期 SQL**：`git checkout -- app/mcp/core_tools.py`，删 `app/mcp/stores/memory_store.py`。
3. **二三期 SQL**：同上，删 `search_store.py` / `task_store.py`。
4. **面板 3.3**：`git checkout -- app/admin_ui.py`，删 `app/panel_queries.py`。

`app/mcp_server.py.bak-opt03-split` 是拆包前的完整原文件，可直接 `Move-Item` 回 `app/mcp_server.py` 做整体回退。

---

## 10. 本轮实际动作时间线（便于复核）

| 阶段 | 动作 | 验证手段 | 结果 |
|---|---|---|---|
| 前置 | 补 `tests/mcp_tools_smoke_test.py`（49 个零调用工具） | 真调每个工具 + 断言可 `json.dumps` | 58/58 |
| 3.1 | AST 统计分析 → 切 95 个函数到 4 个新文件 → shim | 逐字符比对 95 个函数 | 丢失 0 / 新增 0 / 不一致 0 |
| 3.1 | 发现并修 `pyproject packages` 漏 `app.mcp` | 实建 wheel + 干净 venv 实装 | `core=36 gov=49` |
| 3.1 | 修 `portable_restore_mcp_test.py:195` 取证路径 | 跑该套件 | 30/30 |
| 3.2 一期 | 6 个记忆工具 19 处 SQL → `memory_store.py` | `_opt03_store_ab.py` 双实现对拍 | 14/14 |
| 3.2 二期 | 7 个任务工具 20 处 SQL → `task_store.py` | `_opt03_task_ab.py` | 22/22 |
| 3.2 三期 | 13 个工具 27 处 SQL → `search_store.py` + `memory_store.py` 扩充 | `_opt03_sql3_ab.py` | 41/41 |
| 3.3 | 面板 12 处 SQL → `app/panel_queries.py` | 面板 6 套件 | 全部通过 |
| 收尾 | 修对拍脚本的 sidecar 副作用（`mode=ro` → `immutable=1`） | `_sidecar_stamps()` 断言 | 14/14，mtime 不再被刷 |
| 收尾 | 修 `_opt02_ab.py` 的 24 题锚点断言口径 | `_opt03_sameclock_ab.py` 同刻对照 | 12/12，断言改 top5 严格 |

---

## 11. 建议

1. **N-1 的未使用 import 建议在 OPT-04c（测试体系优化）或独立的 lint 批次里一次清掉**，不要夹在往后的功能改动里 —— 混在一起会让 diff 看不出哪一部分是行为变更。
2. **OPT-04b（测试分层）** 现在有基础了：`mcp_tools_smoke_test.py` 覆盖了 49 个零调用工具，可以按「smoke / 单元 / 集成分目录」推进。
3. **`.bak-opt03-split` 与 `_opt03_*.py` 建议在 OPT-07（最终架构验证）之后统一清理** —— 在那之前它们是「旧实现」的唯一副本，对拍脚本还要用。
4. **后续任何新增 store 子包，必须同步 `pyproject.toml` 的 `packages`**。这次已经因为「显式声明 ≠ 自动发现」栽过一次，注释里留了警示。
