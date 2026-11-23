# Bionic Agent MCP — 本地语义记忆 + 任务 + 治理

一个**零外部 API、可离线运行**的 MCP 服务器：给你（或任意 MCP 宿主里的 AI 助手）提供
**持久记忆**（关键词 + 本地多语言向量语义检索）、**多阶段任务交接**、以及一套
**自主记忆治理**（评分、冲突/重复检测、保护、归档恢复、审计、向量一致性）。

- 数据全部落在本地 SQLite（`data/agent.db`），无云端调用、无 API Key。
- 向量模型用 FastEmbed / ONNX 本地推理，首次使用自动下载一次后离线可用。
- 提供两个可分开挂载的 MCP 实例：**核心 36 工具**（日常）+ **治理 49 工具**（按需），同一个数据库、同一套后端，无重复逻辑。
  - 核心多出的 7 个（`set_memory_expiry` / `scan_expired_memories` / `run_expiry_policy` / `get_resume_packet` / `find_stuck_tasks` / `fail_task` / `backfill_memory_types`）都是日常维护动作、参数很短，放核心才不会让常见操作被迫多挂一个实例。
  - 另有 3 个知识库工具（`search_knowledge` / `get_knowledge_doc` / `knowledge_status`）同样放核心：`knowledge/` 与 `mine/` 的检索和记忆检索是同一个动作，不该分两次挂载。

---

## 功能总览

| 能力 | 工具（核心 / 治理） | 说明 |
|---|---|---|
| 记忆 CRUD | `save_memory` / `get_memory` / `update_memory` / `archive_memory` / `restore_memory` / `supersede_memory` | 软归档、纠正保留历史，默认禁止物理删除 |
| 检索 | `search_memories`（关键词）/ `find_related_memories`（关联）/ `semantic_search_memories`（向量语义）/ `get_memory_links` / `link_memories` | 关键词、关联、真实向量三种检索并存 |
| 任务 | `create_task` / `list_tasks` / `get_task` / `save_stage_snapshot` / `handoff_task` / `resume_task` / `complete_task` / `list_events` | 多阶段交接，自动生成初始 plan |
| 嵌入 | `embedding_status` / `index_memories` | 查看模型、建立/刷新向量索引 |
| 治理（治理实例） | 49 个：评分 `score_memories`、冲突 `detect_memory_conflicts`、保护 `protect_memory`、整理闭环 `run_memory_maintenance`、向量一致性 `get_vector_consistency`、融合召回 `recall_memories`、激活扩散 `spread_activation`、生命周期 `memory_lifecycle`、关系子图 `get_related_memories`、便携恢复 `preview_memory_restore` / `restore_memory_package` / `delete_memory_package` 等 | 见下方"治理实例"章节 |
| 反馈闭环 | `record_memory_feedback` / `record_recall` / `summarize_memory_feedback` | 用到/有用/被纠正会真实移动可信度；**曝光（retrieved）永不计分** |
| 整库快照 | `create_db_snapshot` / `list_db_snapshots` / `restore_db_snapshot` / `check_vector_consistency` / `prune_db_snapshots` | 单动作回滚之外的兜底；恢复前自动先给当前状态拍一张 |
| 分档保护 | `assess_memory_protection` / `summarize_protection` | critical/high/normal/low 由分数与类别推导，只收紧不放松 |
| 记忆提炼 | `find_distillation_candidates` / `apply_distillation` / `list_distillations` | 规则找候选 + 证据链；结论由人/模型撰写，`derived_from` 挂回来源 |
| 任务续跑 | `get_resume_packet` / `find_stuck_tasks` / `fail_task` | 续跑带真实快照上下文或明说带不了；不虚报进度；卡住只报告不自动重试 |
| 记忆有效期 | `set_memory_expiry` / `scan_expired_memories` / `run_expiry_policy`（`save_memory(ttl_days=...)`） | 只对临时类生效；**过期只降权不删除**；默认 dry_run、送审不直接归档 |
| 召回画像 | `backfill_memory_types`（`recall_memories` 自带） | 按记忆类型/问句形态选权重画像，结果回显画像名；只改比重不改成分 |
| 取值冲突 | （`detect_memory_conflicts` 内置 `value_conflict`） | 同骨架不同数值判为取值矛盾，建议两条都保留，不再当重复归档 |
| 记忆库浏览 | `/api/memories/timeline` / `/api/memories/breakdown`（面板） | 全库按天/分类/访问热度统计；访问记账走 `touch_access`（召回与详情各记一次，列表不记） |

**核心实例 `bionic-agent`：36 个工具。** 日常使用只挂它即可，固定工具清单 token 成本低。含 4 个备份包工具（`export_memory_package` / `list_memory_packages` / `inspect_memory_package` / `validate_memory_package`）。
**治理实例 `bionic-agent-governance`：49 个工具。** 需要记忆治理/整理/融合召回/便携恢复时再挂。

> 注：因 handoff_task 历史 bug 已修复——现在 handoff 会一并返回展开后的 `task.plan`（结构化对象），不再是 `plan_json` 字符串。

---

## 环境要求

- Python ≥ 3.10（开发用 3.13 验证通过）
- 操作系统：Windows / macOS / Linux 均可（路径全部基于 `__file__` 解析，无硬编码）
- 依赖：`mcp` (1.30.x) + `fastembed` (0.7.x)，见 `requirements.txt`

---

## 快速开始（三步走）

### 1. 装依赖（建议用虚拟环境）

```bash
cd bionic-agent-mcp
python -m venv .venv
# Windows
.venv\Scripts\python.exe -m pip install -r requirements.txt
# macOS / Linux
.venv/bin/python -m pip install -r requirements.txt
```

首次调用语义检索时，FastEmbed 会下载模型
`sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`（约 100MB，一次即可，之后离线）。
可在代码或环境变量里换模型：`BIONIC_EMBEDDING_MODEL=...`（换模型后需重新 `index_memories`）。

### 2. 跑通自检（不连 MCP 宿主也能验证）

```bash
.venv/Scripts/python -m compileall app
.venv/Scripts/python tests/smoke_test.py
.venv/Scripts/python tests/verify_mcp_split_test.py   # 应打印 core=36 / governance=49 / 合计 85，6 项全 PASS
```

### 3. 挂到 DeepSeek Harness（或任意 MCP 宿主）

把下面两段写进你的 MCP 配置（路径换成你自己的绝对路径）。**推荐用 `-m` 入口**
（最稳，自动处理 `sys.path`）：

```yaml
# DeepSeek Harness: ~/.dsh/skill-mcp-manager/registry.json 样式（节选）
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

> ⚠️ **不要**用 `args: ['.../app/mcp_server_gov.py']`（脚本式直接跑）来启动治理实例——
> 那样 `sys.path[0]` 会是 `app/` 而非项目根，`from app import ...` 会失败。
> 治理入口已内置 sys.path 兜底，但**统一用 `-m app.mcp_server_gov` 最省心**。
> 参考文件：`deepseek-harness-mcp.example.yml`。

重启宿主后，核心实例应可立即调用；治理实例挂载后即可用上 49 个治理工具。

---

## 可视化管理面板（Web UI）

不想只走 MCP 工具调用？仓库自带一个**自包含 Web 管理面板**，直连 `data/agent.db`，
零外部 CDN、可离线打开，覆盖记忆 / 任务 / 治理三大模块：记忆与任务 CRUD、语义检索、
关系图、治理评分 / 冲突检测 / 重复识别 / 巩固候选、嵌入模型预热与状态。

```bash
# 装好依赖后（含新建的 fastapi / uvicorn）
.venv/Scripts/python -m app.admin_ui                # 默认 http://127.0.0.1:8899
.venv/Scripts/python -m app.admin_ui --port 8900     # 只换端口，仍只监听本机
# 或走安装好的入口
bionic-agent-admin

# 开启令牌认证（除首页与 /static/* 外，全部接口要求 Authorization: Bearer <令牌>）
.venv/Scripts/python -m app.admin_ui --token "你的令牌"
BIONIC_ADMIN_TOKEN="你的令牌" .venv/Scripts/python -m app.admin_ui   # 效果相同
```

> **安全边界（请先读这段）**：面板**默认没有任何身份认证**，默认只绑定 `127.0.0.1`。
> 绑定非本机地址（`0.0.0.0` / `192.168.x.x` / 机器名）会被**拒绝启动**并退出码 2，
> 因为那等于把「删记忆 / 恢复整库 / 导出备份」的接口交给同网段所有人。
> 确实要暴露到局域网时必须显式授权，并自担风险：
>
> ```bash
> .venv/Scripts/python -m app.admin_ui --host 0.0.0.0 --allow-remote --token "你的令牌"
> ```
>
> 此时会打印安全警告。**绑定授权与令牌是两件独立的事，不能互相替代**：
> `--allow-remote` 只是「你知道后果」的授权声明、**不是认证机制**；反过来，
> 配了令牌也不能省掉 `--allow-remote`——远程绑定仍然要你显式声明。

### 访问令牌（可选）

只想「本机自己用」的话什么都不用配，行为和历史版本完全一致。若这个端口不是只有你能碰
（远程、别人共用一台机器、或你想给自己加道锁），就配一个令牌：

| 方式 | 写法 | 优先级 |
| --- | --- | --- |
| 命令行 | `--token <令牌>` | 高 |
| 环境变量 | `BIONIC_ADMIN_TOKEN=<令牌>` | 低 |
| 都不配 | —— | 认证关闭（历史行为） |

开启后：首页与 `/static/*` 仍然公开（否则页面本身打不开），**其余全部接口**（含
`/docs`、`/openapi.json`）都要求 `Authorization: Bearer <令牌>`，否则 401。

浏览器里怎么用：打开面板后顶栏右侧会多出「访问令牌」输入框，填同一个令牌即可。
令牌**只存在这个页面的内存变量里**——不写 `localStorage`/`sessionStorage`、不进 URL、
不进 Cookie、不写库、不打日志。代价是刷新或重开页面要重填一次；这个代价值得付，
落到 storage 里同源下任何脚本都能读走。

几条实测出来的注意点：

- **令牌只能用 ASCII**（字母数字与 `-_.~` 等）。HTTP 头值本身只能是 ASCII，
  中文令牌浏览器根本发不出去，现场表现是「令牌明明对、却全 401」——
  所以服务端会在启动时就把这种令牌拒掉（退出码 2），而不是让你去猜。
- 令牌首尾的空白会被自动裁掉（粘贴自带空格不算错）；控制字符会被拒绝。
- 令牌走的是**明文 HTTP**。远程场景请务必置于 HTTPS 反代之后，或只用 SSH 隧道转发。
- 更换令牌后刷新页面、在输入框里重填即可；清空输入框等于退回「不带令牌」。

> 注意区分两层保护：`dsh-panel-client` 插件在**反代层**另有一道写保护
> （只允许从 DSH 自己的窗口发起写操作），它只对经由插件挂载的路径 `/<mount>` 生效，
> 对直连 `8899` 无效。后端令牌认证与之互补、不冲突。

浏览器打开 `http://127.0.0.1:8899` 即可。面板直接用 `app.*` 的现成函数读写与 MCP 同一份
数据库，因此**和 MCP 工具完全等价、实时同步**——在面板里改的记忆，MCP 工具立刻能读到。

> 设计取舍：面板是**独立进程**（FastAPI），不侵入 DeepSeek Harness 的 Electron/asar，
> 因此宿主更新、热重载都不会影响它。

### 记忆星图（3D）

面板里「星图」这页把整个记忆库画成一个可旋转的三维天球：

- **方位由类别决定**：教训 / 项目 / 环境 / 授权各占一片天区，颜色对应固定。
- **簇内按语义再聚**：内容真正相近的会互相靠近，并被语义边连起来；标签共享只有最淡的边。
- **亮度 = 新鲜度**（`freshness` 为主 + 年龄折扣）。这只是「新旧直观感」，
  代码里明确注明了**不声称这是遗忘曲线**——没有半衰期、没有回弹，别把它当衰减模型读。
- **点节点**弹出信息卡（类别、重要度、新鲜度、被用次数、标签），可一键跳回记忆列表定位原文。
- 右上角图例显示各天区记忆数；阈值滑块（0.40–0.80）可调语义连边的松紧。

```bash
curl "http://127.0.0.1:8899/api/starmap?threshold=0.62"
# → {ok:true, data:{nodes:[...], edges:[...], meta:{node_count, edge_count, cluster_count, ...}}}
```

**渲染用的是 [3d-force-graph](https://github.com/vasturiano/3d-force-graph)**（MIT，v1.80.1），
three.js 已打包在它内部，**文件本地托管**在 `app/static/3d-force-graph.min.js`（1.39 MB），
不连 CDN —— 面板的前提是「拷走就能跑、离线可用」，DSH 里还是同源 iframe，外链会额外受网络与 CSP 影响。

> 第一版是我自己手写 Canvas 投影（`projectNodes` + 按深度排序），能跑但很丑：
> 固定比例缩放下旋转会让边缘节点跑出画布、发光做不出来、拖动惯性也没有。
> 换成现成库之后留下三个**实测踩过的坑**，都在 `test/starmap.mjs` 里钉成断言：
>
> 1. **坐标尺度是「丑」的根源**。后端返回 ±1.3 量级的坐标，最初乘 `SCALE=1400` →
>    天球跨度 3000 单位，节点只有个位数半径，相机被迫退到 5712，星星小到几乎看不见
>    （着色像素仅 141）。改成 `SCALE=260` 后着色像素 5067（**35.9 倍**）。
> 2. **`cameraPosition()` / `zoomToFit()` 是异步生效的**。设完立刻读 `graph2ScreenCoords()`
>    拿到的是上一次相机的投影 —— 实测 `zoomToFit(0,60)` 后立即测包围盒是 582×722，
>    等两帧后变成 194×279。所以自适应取景必须先 `await` 两帧 `requestAnimationFrame`。
> 3. **星团质心不在原点**。后端按类别定天区（"教训" y=+1601、"授权" y=-1330），
>    相机却永远对原点取景 → 必然裁边（曾出现 fillW 49% 却有 5 个节点出画）。
>    解法是先把坐标减去质心再摆位。
>
> 另外**不要把 `fx/fy/fz` 放开让力模拟自由布局**：试过，更糟（fillW 降到 20%），
> 因为 d3-force-3d 会把深度也铺开把天区揉散。钉死坐标才对。

#### 科幻化（深空星野 + 辉光节点 + 网格天球）

容器里叠了四层：`#starSky`（星云渐变）、`#starField`（平铺星点）、`#starMount`（3D 库专属）、
覆盖层标签。画布**保持透明**让底下两层露出来（实测透明像素 75%），节点是「实心球 + 加性混合辉光壳」，
外面套一个 wireframe 球壳当天球网格、缓慢自转。主题切浅色时深空层自动关掉。

这一轮踩了四个坑，**全都是「不报错、只是画不对」的类型**，都已钉成断言：

> 1. **`host.innerHTML = ''` 会把深空层一起删掉**。`#starSky`/`#starField`/`#starHint` 是 canvas 的
>    **同胞节点**，清父容器等于连它们一起清。首次进页面看不出（`STAR.graph` 还没建时不走那行），
>    一旦点「重新加载」或切主题重建，背景就**永久消失**（实测 `hasSky=false`、`transparentPct=100`）。
>    解法：单独建 `#starMount`，只清它。
> 2. **不能取「场景里第一个 mesh」当球**。场景里连线几何（74 个）比节点球（13 个）多得多，
>    遍历顺序又是连线在前 → 缓存的构造器是连线的，造出来的「星」是**几乎看不见的薄片**：
>    实测 lit 像素只有 43（画布 58 万），且**完全不报错**。
>    解法：只认 `parameters` 里同时有 `radius` 和 `widthSegments` 的几何。
> 3. **脏缓存会让后续所有修复失效**。`addStarGrid` 是同步调的，它先一步取构造器时异步节点
>    还没建好，于是把连线几何写进了模块级缓存；之后异步的 `applyNodeStyle` 命中脏缓存，
>    症状是「怎么改都没效果」。解法：非球一律**返回 null 且不写缓存**。
> 4. **自定义节点的每个子 mesh 都要补 `__data`**。库靠 `threeObj.__data` 从射线命中回溯
>    graph node；射线打中的是子 mesh，没标记就**静默失效**——不报错、不触发 `onNodeClick`，
>    只是点了没反应。实测补前 `clicked=null`，补后 `clicked=47`。

修完后实测：`lit 93179`（修前 46）、`THREE_CACHE.Geo='Os'`、`coreIsSphere=true`、
`gridRotating=true`（`rotDelta=0.05844`）、真实鼠标点击 `clicked=47` 且信息卡正确显示原文。

> 排查提示：`readPixels` 必须在**手动 `renderer.render()` 之后同帧**读，
> 否则因为 `preserveDrawingBuffer: false` 全是 0；另外合成 `PointerEvent` 派发到 canvas
> **不会**触发库的拾取，验证点选必须用真实鼠标事件。

> 布局上的一个实测教训：最初想用纯语义聚类定方位，结果 36 条真实记忆按 0.62 聚出
> **25 个簇**（几乎一点一簇，一盘散沙），降到 0.46 又塌成 **1 个 35 点巨簇**。
> 相似度中位数只有 0.319，这本来就是分散数据。所以改成 **类别定方位、语义只管簇内**——
> 类别是人给的、语义稳定、一眼能对上号。

### 可选：把面板嵌进 DeepSeek Harness（侧边栏）

仓库自带一个 Cordis client 插件 `dsh-panel-client/`，它只在 DSH 侧边栏加一个
「记忆/任务」图标，**点开后整块是 iframe 承载上面的 8899 面板**——不改 asar、不碰
宿主源码、下次 DSH 更新自动失效也不影响其他功能。

1. 把插件链接进 desktop profile 并登记到 bundles（**不需要**先手动跑 `bionic-agent-admin`，
   插件的宿主半会在 DSH 启动时自己拉起 8899 服务）：
   ```bash
   # 用管理员/有权限的终端（需改写 profile 的 package.json，会触发 DSH 重启）
   python dsh-panel-client/install.py
   ```
   脚本做的事：① 在 desktop profile 的 `node_modules/@local/bionic-agent-panel`
   建**符号链接**指向本仓库的 `dsh-panel-client/`；② 把
   `"@local/bionic-agent-panel"` 加进 `package.json` 的 `dependencies` 与 `dsh.profile.bundles`。
   插件自带的 `cordis.patch.yml` 会被宿主**自动层叠**，无需手工改 profile 的 patch。
2. **完全退出并重启 DeepSeek Harness**（含托盘）。重启后侧边栏出现「记忆/任务」图标，
   点开即是管理面板。

> **服务生命周期**：插件宿主半（`lib/index.js`）在 `apply()` 里先探测 8899 端口——
> 已有服务就直接复用（绝不 kill 用户自己起的那个），没有才 `spawn` 一个
> `python -m app.admin_ui --port 8899`，日志写到 `data/admin_ui.log`；DSH 退出时
> 只回收它自己起的进程。Python 解释器按 `.venv` → miniforge → anaconda → 系统 python
> 的顺序自动探测，也可在 `cordis.patch.yml` 的 `config.pythonPath` 里指定。
> 这种形态下不再需要手动常驻 `bionic-agent-admin`。若不想装插件，用纯浏览器开
> `http://127.0.0.1:8899` 同样可用，且零安装。要卸载：
> `python dsh-panel-client/install.py --uninstall`。
>
> **崩了会自己起来**：python 子进程意外退出（OOM、依赖装坏、被手滑 kill）时，
> 宿主半按 1s → 2s → 4s → … 退避重拉，最多 5 次，每次都写进 `data/panel-boot.log`。
> 不做无限重试是有意的——"python 里没装 fastapi"这类原因不会自己好，无限重拉只会
> 刷满日志。连续 5 次都起不来时，那行 warn 就是你要看的诊断结论，里面带日志路径。
>
> **复用的那个服务也会被看着**：复用别人起的实例时手上没有子进程句柄，挂不上 exit 监听，
> 所以额外挂了一个 5 秒间隔的健康探测——发现它没了就自己接管拉起。
> 这条是实测撞出来的：我把自己手动起的服务 kill 掉之后，面板一直白屏，
> 因为插件以为"有人在管"而其实没有人管。用例见 `test/adopt.mjs`。

#### 装完但侧边栏没图标？先跑自检，别靠猜

```bash
node dsh-panel-client/verify-install.mjs
```

它会逐项验证宿主的判定链（profile 目录、`dependencies`、`dsh.profile.bundles`、
node_modules 可解析、patch 内容、`dsh.engines.dsh` 与真实运行时版本、8899 是否在
监听），并且**用两份日志把故障定位到具体一半**：

| `data/panel-boot.log` | `data/panel-client.log` | 说明 |
| --- | --- | --- |
| 有 | 有 | 通路全通 |
| 有 | 无 | Node 侧加载成功，卡在浏览器端（client.js 求值失败 / 图标没挂上） |
| 无 | — | 宿主根本没加载插件，多半是 `dsh.engines.dsh` 不满足被**静默跳过** |

- `panel-boot.log`：宿主半 `apply()` 一被调用就落一行，证明 Node 侧加载了。
- `panel-client.log`：有两类报到，证据强度不同、**不要混为一谈**：
  - `source=client-boot`：`client.js` 一被宿主求值就在顶层发出，**不需要用户点开面板**。
    它证明"浏览器半真的加载了"，是最硬的自动证据（`sec-fetch-dest` 为 `empty`）。
  - `dest=iframe`：面板页面嵌进 iframe 后由页面自己发出，证明"面板真的被打开过"。
    这条要人点开图标才有，属于体验验证，不是安装正确性。
  脚本探测（UA 带 `curl/`）和直接打开 8899（`dest=document`）都不算数。
- 两份日志都会写进项目 `data/` 目录，已在 `.gitignore` 里。
- **报到要属于「当前这个服务实例」才算数**：`/api/ping` 每行带一个 `boot` 标识
  （`<pid>-<随机>`，每次启动都不同），`/api/stats` 也返回同一个值。自检拿当前实例的
  `boot` 去比对，上一批 DSH 留下的旧报到会被判为 `[WAIT]` 而不是 `[PASS]` —— 否则
  "我重启了但没点开面板"会显示成"浏览器端已经通了"（这个假信号踩过）。
- **跑完自检若这两项显示 `[PASS]`，但你还没重启过 DSH，那是假信号**——多半是有人
  手动起了服务或用脚本代理过。先删掉 `data/panel-boot.log`、`data/panel-client.log`
  再跑一次，两项应回到 `[WAIT]`。
- **改了 `lib/client.js` 必须重启 DSH 才生效，刷新页面不够**：宿主按
  `artifactRevision()`（文件内容 hash）判 bundle 是否要重读，但这份 revision 是
  进程启动时算好的；只要 DSH 还在跑，`/plugins/<id>/client.js` 就一直发启动时缓存的那份。
  Node 侧的 `lib/index.js` 同理。所以"我改了代码但行为没变"先确认重启过没有。

> 一个容易忽略的坑：面板服务在 8899，宿主页面在随机端口，**这是跨源**。如果
> 管理服务没给 CORS 头，跨源 `fetch` 会被浏览器拦掉——服务明明正常，探测也会
> 判成"没响应"。`app/admin_ui.py` 用 `CORSMiddleware` 限死本机来源
> （`127.0.0.1`/`localhost`/`[::1]` 任意端口）来解决，不放开 `*`。

#### 为什么面板走同源反代，而不是直连 8899

插件默认把面板挂到 **DSH 自己服务器的 `/bionic-panel` 前缀**上（`ctx.webServer`
反代到 8899），iframe 打开的是同源的 `/bionic-panel/`。这不是绕远路，是因为跨源
方案要同时赌三件事，任何一件不成立都表现为「白屏 / 空列表 / 网络错误」，而报错
只出现在用户浏览器的 console 里：

1. 浏览器允许 iframe 嵌入 8899（`X-Frame-Options` / `frame-ancestors`）；
2. 浏览器允许页面跨源 XHR 到 8899（CORS 头对不对）；
3. `client.js` 里硬编码的端口与 `cordis.patch.yml` 里的 `adminUrl` 一致
   （client 半拿不到 config，只能硬编码）。

同源反代把这三条都变成不变量，顺带解决了第 3 条。实现分两层：

- 宿主半 `lib/index.js`：`ctx.inject(["webServer"], …)` 注册 `{kind:"prefix",
  path:"/bionic-panel"}`，逐请求反代到 `adminUrl`；上游不可达时返回**可读的 502**
  （带 `message` 与排查提示），而不是挂住。`webServer` 不存在（非 Web 宿主）或
  注册冲突时只降级、不报错。
- 浏览器半 `lib/client.js`：iframe 先走 `/bionic-panel/?base=%2Fbionic-panel`
  （`base` 让面板页把自身的 `/api/...` 补上前缀）；同源探不通再退回跨源
  `http://127.0.0.1:8899`。**iframe 一律渲染**，探测失败只叠加一条说明条。

**写操作要过 DSH 的 Host/Origin fence。** 同源意味着任何能打到 DSH 端口的网页
都能对我们的写接口发请求（读响应被同源策略挡住，但 `POST`/`DELETE` 会被执行），
而记忆库的删除/归档不可逆。所以反代对**非 GET/HEAD/OPTIONS** 请求先调
`ctx.connection.requestRejection(req)`：被拒就返回 401/403 JSON，不转发上游。
只拦写方法是有意的——GET 没有跨站后果，把它也卡在 cookie 上反而会在 authority
变化的边缘情况把能用的面板判死；fence 自身抛错时也放行，安全组件故障不该让本地
面板变砖。

**端口被别人占着时会自动换端口，而不是复用。** 只探测「端口有没有人听」是不够的：
8899 是常见端口，可能被别的服务、上一次没退干净的进程占着。直接复用会把面板接到
别人的页面上——表现是打开后一片乱码，且不报任何错，最难查。所以宿主半在发现端口
有人听时，先 `GET /api/stats` 认一下「是不是我们自己」（要 `{"ok":true,"data":{...memories_total}}`
这个独有的形状）；形状不对就顺延到下一个空闲端口，并在日志里写明换了哪个。
反代的目标地址是逐请求现取的，所以换端口后路由跟着走，不用重挂。

> 副作用：直接用 `curl` 往 `/bionic-panel/api/...` 发写请求会被拦（没有 DSH 的
> cookie）。面板自己在 iframe 里跑是同源的，会自动带 cookie，不受影响。

想在面板页里改用别的地址，改 `cordis.patch.yml` 的 `adminUrl` 即可，前端不用动；
把 `mountPath` 设为 `''` 可关掉反代、退回纯跨源。

> 自检覆盖了这条链路，分两层：
>
> - 「同源反代挂载」（`test/proxy-mount.mjs`）用手写假 `webServer`，快速验证页面、
>   API、查询串透传、上游断开的 502、写方法 fence（401/403）、以及 dispose 后路由
>   被摘除。
> - 「真宿主 WebServer 反代」（`test/host-webserver.mjs`）把 DSH 安装目录里真的
>   `@deepseek-ai/dsh-host-webserver` 装进一个真 cordis `Context`，走完全真实的
>   `ctx.inject(["webServer"], ...)` 路径。这一层是必要的：假 `webServer` 复刻了
>   `register()` 的契约，但证明不了「服务就绪时才回调」这条路径通不通——它不通时
>   插件会安静地什么都不挂，而假 webServer 的用例永远是绿的。
>
> 这一层需要宿主源码（`@deepseek-ai/cordis` / `dsh-app-boot` / `dsh-host-webserver`，
> 它们只存在于 DSH 安装目录的 `app.asar` 里）。**不需要你手工准备**：
> `test/deps.mjs` 会按「`DEPS_DIR` 环境变量 → 已解包位置 → 现场从 `app.asar` 解出」
> 的顺序找，首次自动解包约 3 秒、落在 `C:/tmp/dsh_asar_extract/`（约 3MB）。
> 换机器、或者有人清了 `C:/tmp`，下一次跑自检会自动补回来。
> 只有连 `app.asar` 都找不到（没装 DSH）时才 SKIP 而不是 FAIL。
> 想自己控制位置：`DEPS_DIR=<@deepseek-ai 目录> node test/all.mjs`，或单独运行
> `node test/extract-deps.mjs --out <目录>`。

---

## 项目结构

```
bionic-agent-mcp/
├── app/
│   ├── mcp_server.py        # 双实例定义（mcp + mcp_gov）+ 36 核心工具
│   ├── mcp_server_gov.py    # 治理实例启动入口（python -m app.mcp_server_gov）
│   ├── admin_ui.py          # 可视化管理面板（python -m app.admin_ui，FastAPI）
│   ├── admin_ui.html        # 面板前端（内联 CSS+JS，零外部依赖）
│   ├── db.py                # SQLite 初始化 + 幂等迁移 + 事务
│   ├── embeddings.py        # 本地 FastEmbed 向量（预热/就绪探针/降级）
│   ├── governance_*.py      # 评分 / 冲突 / 维护 / 规则 / 服务 / 生命周期
│   ├── activation_spreading.py
│   └── memory_lifecycle.py
├── tests/                   # 离线单测 + MCP 工具注册验证
├── data/                    # agent.db（首次运行自动建库 + 迁移；勿手删）
├── requirements.txt
├── README.md
└── build_zip.py             # 生成可分发的 dist/bionic-agent-mcp.zip
```

---

## 记忆模型

- `memories` 表：`status` 是唯一检索过滤字段（`active` / `archived`），全程零物理删除。
- 治理视图字段 `memory_status`（`active/archived/superseded/pending_review`）与 `protected` 标志，不污染旧工具返回。
- 向量独立存 `memory_embeddings` 表；内容变更按哈希识别并自动重新索引。
- 冲突/关系/反馈/治理动作均有独立表 + 审计（`events`），可回滚。

---

## 九个"不显然"的设计决定

这几处看起来可以更简单，但简单版本会在真实使用里悄悄出错，所以写在这里，
免得以后被"优化"回原样。

### 1. 曝光不计分（`app/feedback_loop.py`）

`retrieved` 计数器永远贡献 0.0 分。如果它加分，被召回最多的记忆会持续胜出 →
被召回更多 → 自我固化。那是在优化"被返回"而不是"正确"。

分三层：`retrieved` 只记录（纯审计）、`used` 小正、`helpful` 最强正、`not_relevant` 弱负、
`corrected` 强负。带方向的反馈少于 2 条不生效（单条是噪声），单次调整上限 ±25%，
且是**乘性**的——高分不会被压到 0，低分不会被顶到 100。

### 2. 分档只收紧、绝不放松（`app/protection.py`）

`critical/high/normal/low` 由分数与类别推导，不靠人记得设 flag。关键约束：
分档不会把一条记忆"升级"成现有阈值本来会拒绝的自动动作。**能触发破坏性操作的安全轴
不是安全轴。** 调用方查 `blocks` 字典（机器可读）而不是比 tier 字符串，这样以后加档
不会静默放宽权限。

一个具体的判断顺序错误被测试抓到过：`temp` 类别 importance=75 时先命中高分带成了
`high`。修法是把"临时类别"判断移到分数带之前——**一次临时笔记被访问很多次仍然是
临时笔记**，让分数在这里胜出会重新制造分档本来要解决的问题。

### 3. 快照是逻辑 dump 不是文件拷贝（`app/snapshots.py`）

另一个进程持写锁时直接拷 `agent.db` 会得到撕裂文件；事务内 dump 行在 WAL 下视图一致。
payload 落盘、元数据进库（向量太大不塞 SQLite 行，库里存 sha256 便于发现损坏）。
恢复是**单事务、全成或全不成**——部分恢复比不恢复更糟。恢复前自动先给当前状态拍一张，
所以恢复本身可逆。恢复后不自动重建向量，如实报告 `vector_check`。

### 4. 过期只降权，不删除（`app/expiry.py`）

过期记忆通常**仍然正确**，只是不再当令。过滤掉会让它彻底查不到——而"三个月前
的端口号"这类信息恰恰在排查旧问题时最有用。所以过期只扣 15% 召回分并在结果里
标 `_expired`，内容与可检索性都保留。

另外两个刻意的取法：带 24h 宽限期（时钟/时区出错时不会一锅端）；`run_expiry_policy`
默认 dry_run，即使 `archive=True` 也只送 `pending_review` 不直接归档。

`save_memory(ttl_days=...)` 对 `preference`/`fact`/`identity` 这类**明确拒绝并说明原因**，
而不是静默忽略——静默忽略会让调用方以为自己设了有效期，实际永远不过期。

### 5. 召回画像只改比重，不改成分（`app/recall_profile.py`）

`RECALL_WEIGHTS` 原本是写死常量，而 `memory_type` 有 7 类却完全没用在召回上、
真实库里 38 条全是 NULL。现在按类型/问句形态选画像，但有三条硬约束：

- 任何权重不许归零，也不许超过 0.5（否则一句问法就能让某条记忆查不到）
- 类型乘子限在 `[0.5, 1.5]`，且未命中时为 1.0（不惩罚）
- 结果里回显画像名与权重，排序异常能追到「哪个画像」而不是猜

**两种维度不能混**：权重表的 key 是**成分**（semantic/freshness/frequency），
记忆类型名（episodic/procedural）是**候选属性**，只能作每条的软先验。
第一版把两者塞进同一张表，`bump.get("freshness")` 永远取不到值，形态画像与
默认表逐项相同——整个分支静默空转，而测试照样绿。

写入时自动归类是必要的配套：字段全是 NULL 时，再好的画像也是空转。

### 6. 同一件事的两种取值不是重复（`app/governance_conflicts.py`）

```
A: DSH 桌面版每次重启端口都会变，这次是 59647
B: DSH 桌面版每次重启端口都会变，这次是 51366
相似度 0.882 -> 旧逻辑判 duplicate -> 建议 archive_duplicate
```

文本几乎一致时，唯一的差异往往正是数字，而数字正是事实本身。判成重复并归档，
等于把一个真实存在过的端口号列为垃圾。新规则：双方都有数值且不等、同时抹掉数字
后的骨架相似度 ≥ 0.80 → `value_conflict`，建议两条都保留并各自标注生效时间。

刻意**不猜哪个值对**：规则只能发现两个数不一样，判断哪个是当前有效值需要时间和
上下文，那属于人。

### 7. 提炼停在候选，不生成结论（`app/distillation.py`）

规则能可靠地找出"同一主题下的多条经历"，但写不出抽象结论。任何声称能做的规则，
实质上就是复制某一条原句当新记忆——看起来像总结，实际是重复一行，比不做更糟。

所以：候选里**没有** `content` 字段；来源少于 3 条直接拒绝（并提示改用 `save_memory`，
那是断言新事实而非提炼）。

### 8. 并发靠原子操作，不靠"先查后改"（`app/db.py`）

6 个进程同时启动曾崩 3 个：`sqlite3.OperationalError: duplicate column name: supersedes_id`。
根因是「先 `PRAGMA table_info` 查、再 ALTER」两条语句之间的窗口。现在所有加列都走
`_add_column_if_missing()`，遇到 `duplicate column name` 视为"别人已加好"返回 False。

配套：`connect()` 统一 `busy_timeout`（默认 30s，`BIONIC_DB_BUSY_TIMEOUT` 可调）+ WAL +
`synchronous=NORMAL`；`transaction()` 用 `BEGIN IMMEDIATE`——deferred 事务先读后写会被
提升并报 `SQLITE_BUSY_SNAPSHOT`，而 busy_timeout **不重试**这个错误。

### 9. 概念桥是放大器，不是召回器（`app/memory_lexical.py`）

用户问「插件装完没反应是为什么」，正确答案 F04 讲的是「**改完需重启 DSH 才生效**」——
两者字面零重叠，所以词法分里它只命中泛化的「插件」两个字，排到第 8。
而 F01 的标题里就有「插件市场」，同样只命中两个字，却因为命中在标题（权重 6 vs 正文 2）
拿到满分强度，稳坐第 1。

修法不是给 F04 提权（那等于给一条记忆打补丁），而是补上**症状 → 机制**这一步跳跃：

```
查询侧：必须同时含「动作已完成」词（装完/改了/放进去了…）
       且含「否定后果」词（没反应/没生效/还是旧的…）
       且 detect_intent(query) == install_no_effect
记忆侧：正文/标签/标题里含「机制词」（才生效/需重启/必须重启/看不出变化…）
两侧同时成立 → s' = s + α × (1 - s)，α = CONCEPT_BRIDGE_ALPHA = 0.20
```

六条必须一起看的事实（改之前先读完，否则很容易"优化"坏）：

- **`s` 是纯词法分，不是融合分。** 混入发生在 `score_all` 内部、融合之前。所以
  `F04 0.254 → 0.367` 这个融合总分增量是**两段**叠出来的：keyword 段
  `(0.349-0.186)×0.556 = +0.090`，加上 activation 段 `1.0×0.022 = +0.022`。
- **activation 那段是二阶效应，不是桥接直接改的。** `recall_memories` 的 seed 门是
  `lex >= 0.3`（`app/memory_lifecycle.py`）；F04 桥接后从 0.186 抬到 0.349，**跨过 0.3**
  才从「非 seed」变成「seed」，激活分量于是 0 → 1.0。F11 同理。
  改 seed 阈值或融合权重时，这一段会跟着变，必须重新复评。
- **它是放大器，不是召回器。** `score_all` 只对「词法已有命中」的记忆打分
  （零命中的直接 `continue`），桥接没有机会把它们拉进候选。所以它永远救不了
  「一条字面完全不沾边的记忆」——那种情况要改的是记忆内容或同义词，不是 α。
- **当前记忆侧全库只有 2 条命中**：F04（`src:F04`）与 F11（本地骨架并入 MCP 知识库）。
  F11 是**误命中**——它讲的是 knowledge root，只是正文里有一句「必须重启 DSH 才生效」。
  这类"顺带提到机制"的记录天然会被误抬，是目前已知的主要噪声来源。
- **α = 0.25 实测会让 45 题 R@1 从 30/41 掉到 29/41、MRR 0.8211→0.8089**，
  因为 `为什么改了代码没生效` 会被 F04 抢走第一名（正确答是 L03「验证胜于自信」）。
  **不要把 α 往上调**——0.20 是扫参得到的边界，离退化点只有 0.05 余量。
- **只诊断、不改分**：`recall_memories(explain=True)` 的每条结果会带一个 `_bridge`
  块（`bridge_hit_count` / `bridge_hit_memory_ids` / `seed_threshold_crossing_count` /
  `bridge_alpha` / `bridge_enabled` / `seed_threshold`）。它由
  `CONCEPT_BRIDGE_DIAGNOSTICS` 开关控制，**只在 explain 时输出，不含查询原文，
  不影响任何分数与排序**。

#### 什么时候必须复评

以下任一发生，就要用固定题库（45 题 / 24 题 / 38 题）+ 隔离库重跑，并记录变更前后指标：

1. 记忆侧桥接命中数明显增长（比如从 2 条变成 5 条以上，或新写入的记忆开始大量触发）
2. `lex >= 0.3` 这个 seed 阈值被改动
3. `RECALL_WEIGHTS` / `recall_profile` 的融合权重被改动
4. `CONCEPT_BRIDGE_ALPHA` 或三个短语表（动作词/症状词/机制词）被改动
5. 45 题、24 题、38 题任一指标发生变化
6. 新增了与概念桥相关的意图分类或规则

**刻意不做**：不设"命中数超阈值就自动调 α"之类的机制。α 的取值只由离线扫参决定，
自动调参会让一个未经验证的数字悄悄改掉线上排序，出事时无法复现。

---

## 可移植 / 传播要点

- **路径零硬编码**：所有路径基于 `Path(__file__).resolve()`，换机器只改 MCP 配置里的绝对路径。
- **数据库随项目走**：`data/agent.db` 在首次启动时自动建库并执行幂等迁移；新机器拿到代码即可跑，无需手工建表。MCP 入口在每次工具调用前 `_ensure_db()`，管理面板在 FastAPI `lifespan` 里做同一件事——所以空目录直接起面板也不会出现 `no such table`。
- **库位置可覆盖**：`BIONIC_DB_PATH=/path/to/agent.db` 能换掉默认的 `data/agent.db`。跑第二实例、做测试、或想把库放到同步盘时用它，别去改 `app/db.py`（自检脚本 `dsh-panel-client/test/proxy-mount.mjs` 就是靠它把测试写入隔离到临时目录的）。
- **迁移安全**：`app/db.py` 的 `_migrate_phase5` 仅幂等加列/建表，从不 DROP/重建；重复运行无副作用。
- **打包**：`python build_zip.py` 生成可分发的 ZIP（自动排除 `data/agent.db`、`__pycache__` 等私有/临时文件）。

### 跨机迁移步骤

1. 在源机：`cp data/agent.db` 备份（或信任自动备份 `data/backups/`）。
2. 在目标机：克隆/解压代码 → 建 venv → `pip install -r requirements.txt`。
3. 把 `agent.db` 拷到目标机 `data/`，或直接启动让它自动建空库。
4. 挂载 MCP 配置（见上），重启宿主。

---

## 测试

### 插件与面板（Node，`node dsh-panel-client/test/all.mjs` 一把跑完）

| 套件 | 覆盖 |
|---|---|
| `test/client-smoke.mjs` | client 半：slot 注册、iframe 地址、布局不脱离文档流、探测假就绪、重试自愈 |
| `test/host-cordis.mjs` | 真 cordis 加载宿主半：config 透传、服务拉起与回收 |
| `test/layer-config.mjs` | profile 层叠：bundle 未被 skipped、patch config 进入配置树 |
| `test/proxy-mount.mjs` | 同源反代：路由、查询串透传、502 可读、写保护三种拒绝 |
| `test/port-conflict.mjs` | 8899 被别的程序占着时换端口，且不误杀别人 |
| `test/host-webserver.mjs` | 真宿主 WebServer 走 `ctx.inject(["webServer"])` 真实路径 |
| `test/self-heal.mjs` | 杀掉 python 子进程后自动重拉（boot 标识变化即证） |
| `test/theme.mjs` | 主题：三档选项、深浅两套变量齐全、样式不写死颜色、跟随 DSH 读令牌 |
| `test/starmap.mjs` | 星图：用现成库不手写投影、坐标尺度、异步取景、质心归零、主题只换色 + 后端布局实测 |
| `verify-install.mjs` | 安装态 20 项：宿主判定链 + 两份痕迹 + 活路由 |

> `verify-install.mjs` 里两项（`panel-boot.log` / `panel-client.log`）不打勾是正常的：
> 它们要等你**重启 DSH 并点开一次面板**之后才有证据。刻意写成 `[WAIT]` 而不是猜测通过。

### Python 侧（真实 SQLite，无 mock）

| 套件 | 覆盖 |
|---|---|
| `tests/smoke_test.py` | DB 迁移与记忆读写（离线，临时库） |
| `tests/embedding_test.py` | 向量持久化兼容 |
| `tests/verify_mcp_split_test.py` | 双实例工具注册验证（核心 36 + 治理 49 = 85） |
| `tests/concurrency_test.py` | 6 进程并发建库/写、跨进程持锁等待、busy_timeout 可配（8 项） |
| `tests/feedback_loop_test.py` | 反馈闭环：retrieved 不计分、分层权重、±25% 上限、端到端分数真会动（26 项） |
| `tests/snapshot_test.py` | 整库快照/恢复：sha256 校验、中途失败整库回滚、id 序列重置（31 项） |
| `tests/protection_test.py` | 分档保护：四档判定、只收紧不放松、接入归档与合并（28 项） |
| `tests/distillation_test.py` | 提炼：簇发现、链式串联防回归、证据不足拒绝、derived_from 溯源、被反驳可检出（37 项） |
| `tests/task_resume_test.py` | 任务续跑：无快照必须诚实说带不了、不虚报 plan 进度、retry_count 持久化（42 项） |
| `tests/expiry_test.py` | 有效期：临时类可设/长期类拒绝、过期降权不删除、24h 宽限、dry_run 不改数据（37 项） |
| `tests/recall_profile_test.py` | 召回画像：权重不归零不主导、画像确实改变算分、类型乘子有界、写入自动归类（62 项） |
| `tests/conflict_triage_test.py` | 取值冲突：同骨架不同数值不判重复、真重复仍抓得到、已裁决的对不再复活、MCP 端到端（33 项） |
| `tests/timeline_test.py` | 时间分档：路由不被 `/{id}` 抢先、未来时间归今天、认不出的计入 unparsed（12 项） |
| `tests/views_test.py` | 分类/热度/时间轴：access_count 必须回填、热度按绝对值分档、召回与详情记账而列表不记（18 项） |
| `tests/review_release_test.py` | 待审核保留的进与出：被藏的真检索不到、裁决后回到检索、`pending` 不解除、幂等、可回滚（25 项） |
| `tests/knowledge_test.py` | 知识库导入与检索：幂等重导入、删块连带删向量、按路径取文档时 chunks 不能空、38 条检索基准 recall@1/@3 全中（26 项） |
| `tests/activation_test.py` | 激活扩散的种子来源：访问痕迹能当种子、usage 弱于显式激活、零命中查询里也生效、扩散是纯读（16 项） |
| `tests/lexical_test.py` | 词法检索三段基准：标题自召回 ≥90%/≥95%、8 条口语题融合 @1 ≥5 且 @3 ≥7、3 条纯语义题 @3 ≥2（16 项） |
| `tests/concept_bridge_test.py` | 概念桥边界：缺动作词/缺症状词/意图不匹配/记忆无机制都不触发、双侧成立才抬、不重复混入、值域 0..1、关闭≡原始、公式 `s+α(1-s)` 精确（68 项） |
| `tests/concept_bridge_diag_test.py` | 概念桥诊断：开/关分数与排序逐字节相同、命中 id 集合与实际抬分集合相同、跨 seed 阈值数与按定义重算一致、负向查询不触发、生产库未触碰（40 项） |
| `tests/init_db_probe_test.py` | 初始化只读探测：已建库走只读路径且 sha256/size/mtime_ns 不变、新库正常、缺表/缺列/旧版本/半初始化能识别并安全迁移、损坏与锁冲突明确报错、并发幂等（94 项） |
| `tests/db_backup_test.py` | SQLite 物理备份：基本/WAL/失败安全/保留策略/边界，备份可独立打开、验证失败不删历史、干扰文件不受影响（104 项） |
| `tests/portable_backup_metadata_test.py` | 便携备份元数据读取隔离：指定 db_path 读取、多库互不干扰、全程不连生产库、库不存在不建空库、WAL 可见性、零附带 sidecar（90 项） |
| `tests/import_dsh_memory_test.py` | 本地→库同步的数据保真：不重置 MCP 派生的 `access_count`/`last_accessed_at`、源 status 显式映射、未知状态上报不静默、archived 不复活、库内独有记忆不被普通同步删（29 项） |
| `tests/cross_device_test.py` | 跨设备路径：`knowledge_docs` 以 `(root, rel_path)` 稳定键定位（换盘符不误判 missing）、`reanchor_paths()` 重锚绝对路径（dry-run 不写、应用后只改 path、hash 校验、冲突分类、事务可回滚）、`BIONIC_DB_PATH` 统一（19 项） |
| `tests/portable_backup_test.py` | 便携包导出（S1）：四模式矩阵、大小写路径、ZIP 炸弹、未知高版本、缺 manifest/checksums、敏感文件排除、异常清理、DB 隔离（56 项） |
| `tests/portable_restore_test.py` | 便携包恢复引擎（S2）：preview→confirm→execute 状态机、一次性令牌（过期/一次性/目标变化）、merge 与 full_replace、三种冲突策略、失败自动回滚（64 项） |
| `tests/portable_restore_mcp_test.py` | 恢复类 MCP 工具（S3）：7 个工具是 S1/S2 的薄封装、不自行实现不绕过、令牌真实性、无路径逃逸、工具数 36/49/85（29 项） |
| `tests/fusion_test.py` | 融合排序守门（S5-E）：权重表满足设计约束、frequency 只吃真实使用反馈绝不吃曝光、`_explain` 能复现 `_score`、`min_score` 默认关闭、空结果不伪装（32 项） |
| `tests/semantic_test.py` | 语义检索守门（S5-D）：原始余弦不标定且与 `cosine_similarity` 逐条一致、`content_hash` 不符的向量必须跳过、`model_name` 不符必须过滤、`pending_review` 不进检索（25 项） |
| `tests/admin_backup_restore_test.py` | 管理面板「备份与恢复中心」：8 类路由、一次性令牌只存内存、full_replace 强确认、状态机各分支（104 项） |
| `tests/admin_host_guard_test.py` | 面板绑定守卫：本机地址放行、远程必须显式 `--allow-remote` 否则 exit 2、拒绝路径零数据库触碰（64 项） |
| `tests/run_tests_optimize_test.py` | 测试入口抗优化：外部设 `PYTHONOPTIMIZE=1` 时断言不被剥离、`_env()` 清空该变量、三个裸 assert 套件不假通过（12 项） |
| `tests/readonly_uri_test.py` | SQLite 只读 URI 编码：普通/空格/中文/`#`/`%`/`?`/混合/多级目录八类路径正确读取、`mode=ro` 写被拒且文件指纹不变、不存在的库只报错不建文件、URI 形状（`%23`/`%25`/`%3F`/`%20`、单 `?`、前导斜杠）、旧写法反证、8 个调用点经业务函数验证（77 项） |
| `tests/admin_token_auth_test.py` | 面板可选令牌认证：CLI > 环境变量 > 关闭三档优先级、非 ASCII 令牌启动期拒绝、拒绝/放行矩阵、令牌不入 body/日志/URL、CORS 预检、远程绑定与令牌互相独立不可替代（113 项） |
| `tests/admin_auth_frontend_test.py` | 前端认证链路：401 身份头、公开路径不许扩张、前端只有一个网络出口、令牌只存内存不落 storage、备份恢复三阶段都带头、插件三消费方认 401（77 项） |
| `tests/db_isolation_test.py` | 测试库隔离：全部套件都带闸门且早于 app 导入、沙箱在项目根内且装真实语料、运行器 `_env()` 五分支、子进程直跑也隔离、伪隔离（把 `BIONIC_DB_PATH` 指向生产库/相对路径/全大写/空白）必须被接管、生产库 SHA-256 与 sidecar 全程不变（47 项） |
| `tests/mcp_tools_smoke_test.py` | MCP 工具层冒烟：49 个「零测试调用」工具各真调一遍，卡住返回契约（类型、关键字段、`_tool_result` 外壳、错误分支），并静态核对 49 个全覆盖不许精简掉（58 项） |

示例用法：

```bash
# Node 侧（面板/插件）一把跑完
node dsh-panel-client/test/all.mjs

# Python 侧：统一入口，跑全部 38 个套件（每项独立子进程 + 超时）
python tests/run_tests.py
python tests/run_tests.py --list                       # 只看会跑哪些
python tests/run_tests.py --only lexical,concept_bridge  # 只跑指定的几项
python tests/run_tests.py --stop                       # 第一个失败就停
```

`tests/run_tests.py` 只做**发现 / 执行 / 汇总**三件事，不引入任何测试框架，也不改写
任何测试：它按文件名发现 `tests/*_test.py`，每个跑在独立子进程里（各测试都会在进程内
改 `db.DB_PATH`，同进程串跑会互相污染），判定只看退出码。单独跑与统跑结果一致——
单项也能直接 `python tests/xxx_test.py`。

> 这些测试都跑真实 SQLite（临时库），不用 mock。凡是"分数应该动"的断言，都盯
> 具体数值是否变化——只查计数增长的测试在坏代码上也会通过。

> 仅保留上述可长期维护的核心回归测试。数据库迁移逻辑已内联进 `app/db.py`，
> 首次启动 `init_db()` 自动幂等执行，无需单独迁移脚本。

---

## 已知限制

- 默认向量检索是「SQLite 存向量 + Python 余弦扫描当前活跃记忆」，适合中小规模；超大规模可换 ANN 索引。
- 治理检测是确定性规则（不用 LLM），零外部成本，但语义冲突需人工裁决（`review_memory_conflict`）。
