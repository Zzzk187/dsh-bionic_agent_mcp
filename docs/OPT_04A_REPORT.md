# OPT-04a：测试环境隔离修复

> 项目：`D:\Cache\Cache_Project\bionic-agent-mcp`
> 起点 HEAD：`862714c2ad149dd65060190d40a852effe2e7715`（`master`，未提交任何改动）
> 执行日期：2026-11-21
> 生产库 SHA-256 全程：`BA9DEA62485FAEE9ABB24DE0ED3017D188A0F653F4A831A414130AA9A24EA240`

---

## 0. 先说三件必须让你知道的事

**第一，这个 bug 现在没有在发生。** 任务书写的是「彻底解决运行测试时意外写入生产数据库」，我按这个方向先做了两个对照实验，结论是**当前 37 个测试没有任何一个会写生产库**：

- 实验 A（真项目 + 把 `BIONIC_DB_PATH` 指向副本）：36 个测试逐个跑，**36/36 全部 `no-db-write`**，生产库内容指纹前后都是 `A2D0CAEBD90CB3F3`。
- 实验 B（整个仓库镜像到临时目录、**故意不设** `BIONIC_DB_PATH`）：**36/36 全部 `(clean)`**，镜像库零写入。

OPT-02 报告 §11.3 记录的那次写库（`memory_embeddings` 51→52）是**当时的调用路径**造成的：`tests/lexical_test.py` 直接调真工具 `semantic_search_memories(auto_index=True)`，顺着 `app/mcp_server.py:673-674` 的 `if auto_index: result = index_memories(...)` 落库。而今天那个测试已经自带处理。所以 OPT-04a 的性质是**「给未来装一道闸门」，不是「修一个正在流血的洞」**。这一点我必须讲清楚，否则报告会显得功劳比实际大。

**第二，闸门确实装上了，而且是从最底层装的。** 37/37 个测试文件、`tests/run_tests.py`、以及任何直接 `python tests/xxx_test.py` 的入口，现在都在导入 `app.*` 之前先过 `tests/_env_guard.py`。全部 37 套回归通过（66.3s），生产库 SHA-256 / size / mtime / 三个 sidecar 前后逐字节一致。

**第三，我在查副作用时挖出一个真 bug，但它不在测试代码里，在 `app/portable_restore.py`。** 这个我没有动（超出 OPT-04a 边界），但必须报给你，因为它会让**恢复功能永久卡死**。详见第 7 节 R1。

---

## 1. 根因（比任务书写的多一层）

任务书给的两条根因我都证实了，但真正要命的是第三层：

| 层 | 事实 | 出处 |
|---|---|---|
| 1 | `run_tests.py` 不给子进程设 `BIONIC_DB_PATH` | `tests/run_tests.py:62 _env()` |
| 2 | `lexical_test.py` 调真工具 `semantic_search_memories`，`auto_index=True` 会补建向量 | `tests/lexical_test.py:158` → `app/mcp_server.py:673-674` |
| 3 | **`app/db.py` 在模块导入时就把 `DB_PATH` 定死** | `app/db.py:11-14` |

第 3 层是关键。原代码：

```python
DB_PATH = Path(os.environ.get("BIONIC_DB_PATH")
               or (Path(__file__).resolve().parent.parent / "data" / "agent.db"))
```

这条语句在 `import app.db` 那一刻求值并固化。所以「先 import、再改环境变量」**完全无效**——进程手里已经攥着生产库路径了。这也意味着：**任何在测试文件里 `import app.*` 之后才设 `BIONIC_DB_PATH` 的写法都是无效隔离**。

顺带纠正一处文档漂移：`tests/run_tests.py` 的模块 docstring 原文写「它们都会在进程内改 `db.DB_PATH` 指向临时库」，与实测不符——多数测试靠环境变量，少数几个根本不做隔离。已在本轮如实记录（未改那句话，因为改它不属 OPT-04a 范围）。

---

## 2. 隔离方案

### 2.1 闸门模块 `tests/_env_guard.py`（新增，12786 B / 316 行 / 纯 LF）

一行 `import _env_guard` 即生效，逻辑：

1. `BIONIC_DB_PATH` 已设置**且指向的是别的库** → 一律不插手（尊重调用方，兼容各测试自己设的路径与 CI）。
2. `BIONIC_DB_PATH` 已设置**但指着生产库** → **接管**（详见 2.5，这是本轮补上的最重要一处）。
3. 否则建 `<项目根>/var/test_db/<label>-<pid>-<uuid8>/`，用 SQLite **在线备份 API** 把生产库复制进去，再把 `BIONIC_DB_PATH` 指过去。
4. 顺带把 `BIONIC_SNAPSHOT_DIR` 也指进沙箱，避免快照落在 `data/snapshots/`。
5. `atexit` 删沙箱；启动时清理僵尸沙箱（进程已死或超过 2 小时）。
6. **复制走只读连接**：旁边没有非空 `-wal` 时用 `immutable=1`（零副作用），有内容才退回 `mode=ro`（沿用既有 WAL 状态，不是我们创造的）。

### 2.2 三个设计选择，及为什么不能换

**（一）复制生产库，不用空库。** `lexical_test.py` 的主基准是「拿每条记忆自己的标题当查询、看能不能把自己找回来」。空库会让 48 道题全部无锚点，测试直接失去意义。**复制 ≠ 使用**：写入全落在 `var/test_db/` 里，退出即删。

**（二）沙箱必须放项目根内。** 这是本轮踩得最深的坑。`app/portable_backup.py:91-105` 的 `_resolve_package_dir()` 要求包目录位于项目根内（防任意路径写入），而包目录默认取 `Path(db.DB_PATH).parent / "memory_packages"`。把库放系统 temp 时：

- `admin_backup_restore_test.py`：**104 PASS / 0 FAIL → 100 PASS / 4 FAIL**
- `portable_restore_mcp_test.py:137`：`AttributeError: 'NoneType' object has no attribute 'kwargs'`

实测报错原文：`{"ok":false,"message":"包目录必须位于项目根内：C:\\Users\\xing\\AppData\\Local\\Temp\\o4b-d159aae5\\memory_packages（root=D:\\Cache\\Cache_Project\\bionic-agent-mcp）"}`。把库改放 `var\test_db\` 后两个测试立刻恢复 **104 PASS/0 FAIL** 与 **29/29**。所以「放临时目录」这个最直觉的做法会静默打坏 5 个测试项。

**（三）否决了一个看起来很漂亮的方案。** 我原本想用 `sitecustomize.py` 自动加载，一处生效、零侵入。实测**否决**：写进 `sub/` 后 `python sub/probe.py` 与 `cd sub; python probe.py` 都得 `SC_LOADED = None`——它不从脚本目录加载。照这个方案上线会得到「以为装好了、其实没装」的状态，比不装更危险。

### 2.3 覆盖面

| 入口 | 处理 |
|---|---|
| 37 个 `tests/*_test.py` | 每个都插 `import _env_guard`，位置由 AST 顶层判据确定 |
| `tests/run_tests.py` | `_env()` 增加对 `BIONIC_DB_PATH` / `BIONIC_SNAPSHOT_DIR` 的**清理**（见 2.4） |
| 直接 `python tests/xxx_test.py` | 同第一行，闸门在模块级执行，与怎么启动无关 |
| `python -m tests.xxx` | 同上 |
| 子进程 | `run_tests.py` 清掉父进程沙箱路径，子进程各自重走闸门 |
| `scripts/dev/warm.py` | 见 2.6——它是测试的间接依赖，已核实不写库 |

**插入位置的判据（第一版就在这翻车）**：必须用 `ast.Module.body` 只看**顶层**语句。我第一次用 `ast.walk`，结果 `init_db_probe_test.py` 的 app 导入在函数体内（`:171`），闸门被插进函数中间 → `IndentationError: expected an indented block after function definition on line 35`。已全量回滚重做。

**闸门必须是第一个 app 相关导入（静态强制）**：全部 37 个文件都满足「第一个顶层 `app.*` / `scripts.*` / `_env_guard` 导入就是 `_env_guard`」。这里抓到一处真问题：`tests/lexical_test.py` 原本是

```python
from scripts.dev.warm import wait_ready   # L27
import _env_guard                          # L28
```

`scripts/dev/warm.py` 自己不 import `app.*`（只在 `wait_ready()` 函数体内 import），所以它当时没有真的造成提前导入；但「闸门之前还有一条来自 scripts 的 import」是随时会变陷阱的形态——只要哪天 `warm.py` 顶层加一行 `import app.embeddings`，`lexical_test` 就会在闸门生效前锁定 `DB_PATH`。已把两行对调，闸门现在排第一。

### 2.4 `run_tests.py` 的 `_env()` 加固

原有三条（`PYTHONIOENCODING`、`PYTHONPATH`、`PYTHONOPTIMIZE=""`）一字未动，新增两类清理：

1. **清掉指向生产库的 `BIONIC_DB_PATH`、以及 `BIONIC_SNAPSHOT_DIR`。** 不这么做的话，子进程闸门会按「尊重调用方指向别的库」放行，等于把生产库路径原样传下去。
2. **空白值也清掉。** `app/db.py` 用的是 `os.environ.get(...) or 默认`，而 `"   "` 是**真值**——不处理的话 `DB_PATH` 会变成名叫三个空格的相对路径。

### 2.5 补掉一个真实缺口：伪隔离（本轮自查发现）

这是写报告过程中发现的、最初方案漏掉的一种情况。原逻辑是「`BIONIC_DB_PATH` 非空就不插手」。于是如果用户的 shell 里恰好是：

```powershell
$env:BIONIC_DB_PATH = "D:\Cache\Cache_Project\bionic-agent-mcp\data\agent.db"
python tests/lexical_test.py
```

环境变量非空、看着像配好了，**实际 `db.DB_PATH` 就是生产库**，闸门原样放行。实测确认了这个洞：

```
is_isolated() = False
sandbox_dir() = None
db.DB_PATH = D:\Cache\Cache_Project\bionic-agent-mcp\data\agent.db
>>> 是否仍指向生产库: True
```

这直接违反任务书第 2 条「不允许回退到 `data/agent.db`」。修法：新增 `_points_at_production(raw)` 用 `Path.resolve()` 归一后与生产库比对，命中就 pop 掉该变量、照常建沙箱，并在状态串里写明「覆盖了调用方设成生产库的…」。

六种写法逐个在**独立子进程**里验（因为 `DB_PATH` 导入即固化）：

| 写入值 | 结果 | `db.DB_PATH` |
|---|---|---|
| 绝对路径指向生产库 | **接管** | `var\test_db\_gap_one-22956-1d2df0da\agent.db` |
| `data/agent.db`（相对） | **接管** | `var\test_db\_gap_one-26360-08646fff\agent.db` |
| `data\agent.db`（反斜杠相对） | **接管** | `var\test_db\_gap_one-21916-caad800b\agent.db` |
| 全大写绝对路径 | **接管** | `var\test_db\_gap_one-13748-c3734747\agent.db` |
| `"   "`（三空格） | **接管** | `var\test_db\_gap_one-16512-278c34a5\agent.db` |
| 指向另一个库 | **尊重，不覆盖** | `var\elsewhere\x.db` |

第 4 条（全大写）值得单独说：Windows 不区分大小写，`Path.resolve()` 能正确判定它们是同一个文件，所以不需要额外做小写化。

### 2.6 间接依赖：`scripts/dev/warm.py`

`tests/lexical_test.py:28` 依赖 `from scripts.dev.warm import wait_ready`。这个模块在 `wait_ready()` 函数体内 `from app import embeddings`——函数体导入发生在测试已经调用它之后，此时闸门早已生效，因此安全。

我也核实了 `app/embeddings.py` 本身：`Select-String -Pattern 'INSERT|UPDATE|DELETE|connect\(|commit|db\.'` **零命中**，它的 import 只有 `hashlib`/`os`/`threading`。所以「等模型 ready」这一步无论跑在哪个库上都不写数据。

> 关于 `scripts/` 下 12 个直接 `import app.*` 的脚本（`sync_all.py`、`import_dsh_memory.py`、`eval_*.py`、`diag_*.py` 等）：**它们是生产工具，不是测试**。给它们套隔离闸门会让「用户真的想同步」变成「悄悄同步到沙箱」，那是更坏的错误。任务书要求覆盖的是「测试入口、子进程、测试辅助脚本」，`scripts/` 不属于这三类，本轮不动。

---

## 3. 修改文件清单

| 文件 | 变化 | 字节 | 行 | 行尾 |
|---|---|---|---|---|
| `tests/_env_guard.py` | **新增** | 12786 | 316 | 纯 LF（CRLF=0）/ 无 BOM |
| `tests/db_isolation_test.py` | **新增** | 17095 | 419 | 纯 LF（CRLF=0）/ 无 BOM |
| `tests/run_tests.py` | +26/−0 | 9613 | 212 | 纯 LF（CRLF=0）/ 无 BOM |
| `tests/lexical_test.py` | +1/−1（两行对调） | 8762 | 195 | 纯 LF（CRLF=0）/ 无 BOM |
| `tests/*_test.py`（其余 35 个） | 每个 +1 行 | — | — | 沿用各文件原行尾 |
| **合计** | **48 files changed, 814 insertions(+), 334 deletions(-)** | | | |

> `+814/−334` 是 `git diff --numstat` 的全量数字，**包含 OPT-01/OPT-02 之前累积的 14 个 tracked 文件改动**（`README.md`、`app/admin_ui.py` 等）。本轮 OPT-04a 自身的净增量是上表前四行 + 35 个测试各 1 行。

---

## 4. 测试结果

### 4.1 全量回归

```
compileall_rc=0
汇总：37/37 通过，耗时 66.3s
run_rc=0
```

（37 = 原有 36 个 + 本轮新增 `db_isolation_test.py`。反复跑过四次：69.1s / 64.2s / 67.9s / 66.3s，均 37/37。）

### 4.2 新增隔离测试 `tests/db_isolation_test.py`

**47 PASS / 0 FAIL / 共 47 项**，六组：

| 组 | 项数 | 覆盖内容 |
|---|---|---|
| A 静态 | 7 | 37 个 `*_test.py` 全部 import 闸门、全部早于 app 导入、全部可 `ast.parse` |
| B 主进程 | 11 | 沙箱在 `var/test_db` 下、`db.DB_PATH` 指向沙箱、非生产库、`is_isolated()`、两个环境变量、沙箱有真实语料（active 50 / embeddings 52）、**写沙箱后生产库 SHA-256 未变**且沙箱能查到写入行 |
| C 运行器 | 8 | `_env()` 五分支 + 原有三条保证仍在 |
| D 子进程 | 6 | 直跑 `smoke_test.py` rc=0、子沙箱路径可从 stderr 读到、与父沙箱不同、子沙箱已删、生产库 sha 与 sidecar 未变 |
| E 指纹 | 4 | 生产库整体 SHA-256 / size / mtime / sidecar 快照 |
| **F 伪隔离** | **11** | **六种 `BIONIC_DB_PATH` 写法各起一个独立子进程**：五种必须被接管（含说明覆盖原因）、一种必须被尊重；再核六个子进程跑完后生产库 SHA-256 与 sidecar 未变 |

写测试时踩到几个自身 bug，都已修：①闸门之后需要补 `sys.path.insert(0, 项目根)`，否则 `import app` 失败；②「测试文件数 = 36」的断言过时了，因为本文件自己也是 `*_test.py` → 改成 37；③F 组探针脚本漏了 `import json` 与 `sys.path.insert(0, 项目根)`，报 `NameError: name 'json' is not defined` / `ModuleNotFoundError: No module named 'app'`（子进程的 `sys.path` 不会自动含项目根，必须自己插）。

---

## 5. 隔离验证

### 5.1 生产库指纹前后对比

| 指标 | 起点 | 终态 | 判定 |
|---|---|---|---|
| SHA-256 | `BA9DEA62…A24EA240` | `BA9DEA62…A24EA240` | **一致** |
| size | 8581120 | 8581120 | **一致** |
| mtime (UTC) | `2026-11-21T09:47:51.5066895Z` | `2026-11-21T09:47:51.5066895Z` | **一致** |
| `-wal` | 不存在 | 不存在 | **一致** |
| `-shm` | 不存在 | 不存在 | **一致** |
| `-journal` | 不存在 | 不存在 | **一致** |
| `var/test_db` 残留 | — | **0 个条目** | 沙箱自动清理 |

> 起点状态说明：本轮开始时生产库**有** 0 字节 `-wal` 与 32768 字节 `-shm`（前几轮遗留）。过程中它们消失了，SHA-256 与 size 都没有变化——这是 SQLite 在最后一个连接正常关闭时合并 WAL 并删 sidecar 的正常行为。**所以这里写「不存在→不存在」是指本轮的起点与终点都是干净的**，不是因为我把过程抹掉了。

### 5.2 写库调用检查（要求第 3 条）

| 调用点 | 行为 | 是否受闸门约束 |
|---|---|---|
| `app/mcp_server.py:673-674` `if auto_index: index_memories(...)` | 补建向量，写 `memory_embeddings` | 是（走 `db.DB_PATH`） |
| `app/mcp_server.py:55 / :66` `_touch_retrieved` → `gov.touch_access(ids)` | 写 `access_count` / `last_accessed_at` | 是 |
| `app/admin_ui.py:669` `gov.touch_access([...])` | 同上 | 是 |
| `app/governance_service.py:117` `touch_access` | 唯一写者 | 是 |

`app/` 全目录只读三个环境变量：`BIONIC_DB_PATH`（`db.py:12`）、`BIONIC_DB_BUSY_TIMEOUT`（`db.py:20`）、`BIONIC_SNAPSHOT_DIR`（`snapshots.py:62`）。闸门覆盖前两个中的路径那个和第三个，**没有遗漏的写库开关**。（另注：`BIONIC_PACKAGE_DIR` 只在 `portable_backup.py:14` 的 docstring 里被提到，代码里根本不读。）

---

## 6. 一次真实的 edit 事故（已修）

改 `run_tests.py` 的 docstring 时，我把新段落插到了 `env = dict(os.environ)` **之后**却没删掉原来的 `"""`，导致 docstring 提前闭合、中文正文变成裸代码：

```
SyntaxError: invalid character '：' (U+FF1A)
```

修法是把段落挪进 docstring 内、删掉重复的三引号。记在这里是因为它验证了「`compileall` 应该每次都跑」——这次是它抓住的。

---

## 7. 遗留风险

### R1（高，不在本轮范围）：`app/portable_restore.py:1465` 的僵尸锁检测失效

```python
alive = False
if pid > 0:
    try:
        os.kill(pid, 0)
        alive = True
    except OSError:
        alive = False
```

`os.kill(pid, 0)` 在 Windows 上没有「探测存活」语义。实测（Python 3.13.13 / Windows 11）：

- 对一个**已经 kill 并 wait 完毕**的 pid → `os.kill` **正常返回**（被判为「活着」）
- 对一个**从未存在**的 pid → `OSError [WinError 87] 参数错误`
- `_env_guard._pid_alive()`（`OpenProcess` + `GetExitCodeProcess`）对同一个死 pid → 正确返回 `False`

**后果已实测（不是推测）**：手工写一个指向死 pid 的 `.active.lock`，再调 `_acquire_lock()`：

```
_acquire_lock() = ok=False why='已有恢复任务在执行（pid=27000）'  耗时=0.000s
```

持有者早已退出，新的恢复却**永远被拒**。用户唯一的出路是手工删 `data/restore_logs/.active.lock`。

**建议修法**（下一批，未授权我不动）：把 `_acquire_lock` 里的 `os.kill(pid, 0)` 换成与 `_env_guard._pid_alive()` 同构的 `OpenProcess` + `GetExitCodeProcess`；POSIX 分支保留 `os.kill`。约 20 行，可加单元测试（已死/从未存在/活着三种）。注意这属于 `app/` 业务代码，需要单独授权。

### R2（中）：我此前一条关于 `os.kill` 的结论被自己的实验推翻，已撤回

早先我判断「`os.kill(pid, 0)` 成功时真的投递了 Ctrl+C，会打断同组进程」，并据此解释了一轮诡异中断。后来补做的四组对照实验（目标：同组非组长 / 独立组组长 / 已死 / 从未存在，全部由第三方进程调用）显示：**四个目标没有一个被打断**。这条因果未获证实，已从 `_env_guard.py` 注释里撤掉，改成只写实测到的那部分。**留在这里是因为我差点把一个未证实的推断写成结论。**

### R3（低）：沙箱复制依赖 SQLite 在线备份

若生产库正被长时间写入，`src.backup()` 可能等待锁；当前 `timeout=10`。极端情况下闸门会抛异常而不是静默降级——这是刻意的（宁可测试失败，也不要悄悄用上生产库）。

### R4（低）：`-wal` 有内容时复制会短暂产生 sidecar

只在生产库旁边的 `-wal` 非空时发生，且是沿用既有 WAL 状态、非新建。本轮全程未触发（起点 `-wal` 为 0 字节）。

### R5（低）：`tests/run_tests.py` 的 docstring 仍写着「它们都会在进程内改 `db.DB_PATH`」

与实测不符。属文档漂移，改它不属 OPT-04a 范围，已记录未改。

### R6（低）：`scripts/` 下 12 个脚本仍直连生产库

`sync_all.py`、`import_dsh_memory.py`、`import_dsh_knowledge.py`、`snapshot_db.py`、`physical_backup.py`、`check_mirror.py`、`eval_*.py` ×3、`recall.py` 等，`import app.*` 后直接用 `db.DB_PATH`。这是**设计如此**（它们就是要在生产库上干活），但意味着「跑脚本」和「跑测试」是两种风险等级，不能混着理解。

---

## 8. 安全约束核对

| 约束 | 状态 |
|---|---|
| 不修改生产数据库及其内容 | ✅ SHA-256 / size / mtime / sidecar 前后一致 |
| 不删除、覆盖现有未跟踪文件 | ✅ 未删任何文件（`git status` 未跟踪数只增不减） |
| 不 `git reset` / `clean` / `commit` / `push` / `tag` | ✅ HEAD 仍为 `862714c`，暂存区 0 |
| 不顺手做 OPT-03 或目录迁移 | ✅ 未移动任何文件，未改任何 `app/` 逻辑 |
| 不通过降低测试标准解决问题 | ✅ 37/37 通过，**0 个测试的断言、预期值、门槛被修改** |
| 保留现有测试逻辑与业务算法 | ✅ 35 个测试文件各只加 1 行 `import`；`lexical_test.py` 只对调两行顺序 |

**Git 终态**：HEAD `862714c2ad149dd65060190d40a852effe2e7715`、分支 `master`、暂存区 **0**、porcelain `-uall` **293 行**。

---

## 9. 建议下一步

1. **先修 R1**（`portable_restore` 的僵尸锁）。它跟测试无关，但会让恢复功能永久不可用，是这轮挖出的最值钱的东西。
2. 再考虑 OPT-04b/4c（补 22 个工具的冒烟测试、测试分层）。
3. `data/restore_backups/`、`data/restore_logs/`、`var/` 仍在未跟踪区（OPT-02 的 N-6），建议进 `.gitignore`——但这需要你授权，我没动。
