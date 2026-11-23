# OPT-01-B-FINAL 实施与验收报告

**任务**：Admin UI 浏览器验收及面板插件兼容修复
**基线 HEAD**：`862714c2ad149dd65060190d40a852effe2e7715`（`master`）
**报告时间**：2026-11-20
**范围**：R1 插件兼容修复 · M1–M9 真实浏览器验收 · 前端认证安全边界 · 自动化测试 · 安全风险记录

> 本报告所有结论均来自本轮真实执行。测试项一律标注 `PASS` / `FAIL` / `BLOCKED` / `NOT TESTED`，
> 未做过的项不写成通过。所有"证明"附可复现命令或实测输出。

---

## 一、R1 问题调查结果

### 1.1 问题陈述

OPT-01-B 交付时遗留风险 R1：`dsh-panel-client` 探测管理服务的方式是
`GET /api/stats` 并检查响应体形状，探测请求**不带** `Authorization`。启用令牌认证后该请求返回
`401`，插件据此判定"端口上不是我们的服务"。

### 1.2 调查范围与结论

| 调查项 | 结论 |
|---|---|
| `dsh-panel-client` 是否属于本项目 | **属于**。位于 `dsh-panel-client/`，`git ls-files` 覆盖 25 个文件，`lib/index.js`、`lib/client.js`、`verify-install.mjs` 均为 tracked |
| 包身份 | `package.json`：`name=@local/bionic-agent-panel`，`version=0.1.0`，`private:true`，`main: lib/index.js`，`dsh.engines.dsh >= 0.2.1-rc.3` |
| 文件格式 | `lib/index.js` 28537 B / 713 行 / CRLF 712 / 无 BOM；`lib/client.js` 16725 B / 433 行 / 纯 LF / 无 BOM |
| 运行时 | Node **v26.13.0** |

**所以 R1 可以在本仓库内直接修复，不属于"修改其他项目"。**

### 1.3 三个消费方与故障链

**消费方 1：`dsh-panel-client/lib/index.js` 的 `identifyAdmin(port, timeoutMs = 1500)`（`:112`）**

```js
httpRequest({host:"127.0.0.1", port, path:"/api/stats", method:"GET",
             headers:{host, accept:"application/json"}, timeout})   // :120-128
if (res.statusCode !== 200) { finish({ours:false, reason:`HTTP ${res.statusCode}`}); return; }  // :134-137
const ours = body?.ok === true && body?.data !== undefined && "memories_total" in body.data;     // :145
```

调用点三处：

| 调用点 | 时机 | 误判后果 |
|---|---|---|
| `:387` `ensureHealthWatch` | 每 5 秒巡检 | `!who.ours` → 触发 `startOnce()` 接管 |
| `:417` `startOnceInner` | **启动前判端口占用** | **严重**：`isListening(port)` 为真且 `!who.ours` 时执行 `pickFreePort(port + 1)` 换端口另起实例 |
| `:445` 启动后 | 认形状 | 打 warn「有响应但不是管理服务」 |

**`identifyAdmin` 给 `:417` 喂了错信息，后果不是"界面上多一条错误提示"，而是同一台机器上跑出两个管理服务实例、同一份 `data/agent.db` 被两个进程打开。** 这是本项被列为必须修的原因。

**消费方 2：`dsh-panel-client/lib/client.js`**

- `isOurApi`（`:202`）判 `resp.ok !== true → false`，只认 `body.ok === true`；
- 探测在 `:226`（同源 `${MOUNT_PATH}/api/stats`）与 `:244`（跨源 `${FALLBACK_ADMIN_URL}/api/stats`）；
- `status` 三态 `probing` / `ready` / `down`，`:260-263` 到 `PROBE_TIMEOUT_MS = 30000` 才转 `down`
  → **启用令牌后 iframe 白等 30 秒才报「管理服务没有响应」**，用户完全不知道要输令牌；
- `reportClientBoot`（`:54-60`）`fetch(MOUNT_PATH + "/api/ping?source=client-boot")` 同样 401，但已 `.catch(()=>{})`，不影响加载。

**消费方 3：`dsh-panel-client/verify-install.mjs`**

- `PORT_SCAN = [8899…8908]`（`:115`）、`probeAdmin(candidatePort, timeoutMs=2500)`（`:118`）；
- `:122` `if (!res.ok) return undefined;`、`:124` 形状判据；
- 失败文案 `:152-156`「8899–8908 都没探到：DSH 可能还没重启，或插件宿主半没生效」
  → **令牌模式下会误报"插件坏了"，把运维引向完全错误的方向**。

---

## 二、插件兼容方案

### 2.1 方案选择

| 候选方案 | 结论 | 理由 |
|---|---|---|
| A. 新增公开的 `GET /api/health` | **否决** | `tests/admin_token_auth_test.py:211-212` 断言白名单**恰好**是 `["/", "/static/{name}"]`。新增公开路由会打破既有测试预期（任务书明令禁止改测试预期），且等于为了兼容去扩大未认证面 |
| B. 给插件加配置项，把令牌写进配置文件 | **否决** | 令牌落盘到 `cordis.patch.yml` 等于把凭据写进明文配置，与"不硬编码令牌"原则冲突 |
| C. 探测请求携带令牌 | **否决** | 插件读不到令牌的来源会变得不确定；且"探测"本身应当是无需凭据即可完成的行为判断 |
| D. **在 401 响应上携带服务身份标记** | **采用** | 401 本来就是返回给未认证调用方的响应，只是**把该响应说清楚自己是谁**，公开面一条不增 |

### 2.2 方案细节

服务端在 401 响应上输出两个头：

```
WWW-Authenticate: Bearer realm="bionic-admin"      # 本来就返回，标识认证域
X-Bionic-Service: bionic-agent-admin               # 新增，标识服务身份
```

（`SERVICE_ID = "bionic-agent-admin"`，模块级常量，注释说明**故意不跟 `BOOT_ID` 走**——后者每次重启都变，做不了身份判断。）

插件侧只认这两个标记、**不认"任何 401"**：别的服务也可能用 Bearer 认证，见 401 就认领等于把别人的服务当成自己。

### 2.3 顺带修掉的设计缺口（200 也要带标记）

自动化测试首轮 `76/77`，唯一失败项 `200 也带 X-Bionic-Service — None` 暴露了一个真实设计缺口：
身份标记只在 401 上，认证通过后调用方要判"这是不是我"就得走**两条完全不同的逻辑**——401 看头、200 看 body 形状。
而 body 形状是随版本变的东西（多加/改名一个字段就误判），**响应头才是契约**。

已改为认证通过分支同样注入：

```python
response = await call_next(request)
response.headers["X-Bionic-Service"] = SERVICE_ID
return response
```

---

## 三、实际修改文件

| 文件 | 基线 | 现在 | 变更 | 说明 |
|---|---|---|---|---|
| `app/admin_ui.py` | 65018 B | **78959 B** | +250/−6 | `SERVICE_ID` 常量；401 与 200 均注入身份头；`SERVICE_ID` 注释说明与 `BOOT_ID` 的区别 |
| `app/admin_ui.html` | 120824 B | **121781 B** | +102/−7 | 顶栏令牌输入框与按钮；`setToken`/`authHeaders`/`showTokenUi`/`hideTokenUi`/`applyTokenFromInput`/`refreshAll`；`api()` 与 `sendPing()` 带凭据；**修掉 M3 的 reload 死循环** |
| `README.md` | — | **48002 B** | +43/−4 | 启动示例补 `--token`/`BIONIC_ADMIN_TOKEN`；安全边界改写；新增「访问令牌（可选）」小节 |
| `dsh-panel-client/lib/index.js` | 27003 B | **28537 B** | +25/−0 | `identifyAdmin` 401 分支识别服务身份；两处调用点补 `needsToken` 提示 |
| `dsh-panel-client/lib/client.js` | 15338 B | **16725 B** | +26/−6 | `isOurApi` 401 分支；`status` 增加 `auth` 态；zh/en 文案 |
| `dsh-panel-client/verify-install.mjs` | — | **18383 B** | +23/−1 | `probeAdmin` 401 分支；检查项区分"需要令牌"与"正常" |
| `tests/admin_auth_frontend_test.py` | — | **14908 B** / 317 行 | 新增 | 8 组 77 项 |
| `app/portable_backup.py` | — | 40129 B | +1/−1 | **上一批 OPT-01 遗留**（只读 URI），非本批 |
| `app/portable_restore.py` | — | 78750 B | +7/−7 | **上一批 OPT-01 遗留**（只读 URI），非本批 |

**格式核对（逐字节实测，防止全量重写把行尾搞乱）**

| 文件 | 字节 | 行数 | CRLF | 裸 LF | BOM |
|---|---:|---:|---:|---:|---|
| `app/admin_ui.py` | 78959 | 1741 | 1740 | 0 | 无 |
| `app/admin_ui.html` | 121781 | 2383 | 2382 | 0 | 无 |
| `README.md` | 48002 | 675 | 674 | 0 | 无 |
| `dsh-panel-client/lib/index.js` | 28537 | 713 | 712 | 0 | 无 |
| `dsh-panel-client/lib/client.js` | 16725 | 433 | 0 | 432 | 无 |
| `dsh-panel-client/verify-install.mjs` | 18383 | 352 | 351 | 0 | 无 |
| `tests/admin_auth_frontend_test.py` | 14908 | 317 | 0 | 316 | 无 |
| `app/portable_backup.py` | 40129 | 982 | 0 | 981 | 无 |
| `app/portable_restore.py` | 78750 | 1654 | 0 | 1653 | 无 |

每个文件保持其原有行尾风格，未发生整体转换。

---

## 四、M1–M9 测试结果

浏览器：**Microsoft Edge 157.0.0.0**（Chromium 内核），`Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36 Edg/157.0.0.0`，lang `zh-CN`，dpr 1，窗口 2560×1440。
所有用例均为**真实浏览器 + 真实鼠标点击坐标**，不是 curl 模拟。

| 编号 | 场景 | 结果 | 关键实测证据 |
|---|---|---|---|
| M1 | 无令牌：页面加载、tab 切换、读、写 | **PASS** | 首页 200；`#tokenInput`/`#tokenBtn` 存在且 `display:none`（无多余提示）；`#stats` = 「记忆 3 活跃 3 归档 0 任务 0 关系 0 事件 0」；点 `button[data-tab="governance"]` 正常切换；`POST /api/memories` 200 且 3→4（createdId=4） |
| M2 | 启用令牌：页面能加载、不白屏 | **PASS** | `GET /` 200（105995 B）；`/api/stats` 无令牌 401 + `WWW-Authenticate: Bearer realm="bionic-admin"`；顶栏出现令牌框与「填令牌」按钮；正文显示「需要访问令牌」；error/unhandledrejection/console.error 钩子**零捕获** |
| M3 | 输入正确令牌后解锁 | **PASS（修复后）** | 首轮 **FAIL**，见 §7.1；修复后真实点击 (1107,25)：`stats` 有值、`cards: 3`、`inpVisible: false`、toast「令牌有效，已解锁（仅本页有效，刷新后需重填）」 |
| M4 | 输入错误令牌 | **PASS** | 仍 401、`cards: 0`、输入框保持显示、toast「这个面板需要访问令牌」；`bodyHasToken: false`、`htmlHasToken: false`；无 `Traceback`/`sqlite3.`/`site-packages` 等内部信息 |
| M5 | 写操作：未认证拒绝、认证后成功 | **PASS** | 未认证 `POST /api/memories` → 401 `{"ok":false,"message":"需要访问令牌（Authorization: Bearer <token>）"}`；带令牌 3→4；`refreshAll` 后 UI `cards: 4` |
| M6 | 备份导出 | **PASS** | 未认证 `POST /api/backup/export` → 401；带令牌 → `ok:true`、`package_id: bionic-memory-source_only-20261120T232404-576b3d0d`、`file_count: 159`、`archive_size_bytes ≈ 1547 KB` |
| M7 | 恢复预览 → 确认 → 执行（走真实 UI 按钮） | **PASS** | `#bkRestorePkg` 选包 → `#bkPreview` → 预览含「source_only · eba3617c8a38…」「merge / keep_target」「冲突数 0」「保护性备份 必须（执行前自动创建）」「预计空间 4.2 MB」→ `#bkConfirmBg` 风险提示 → `#bkConfirmGo` → 「执行状态：COMPLETED」+ toast「恢复完成」；恢复后隔离库 integrity ok、FK 违规 0、memories 3 条内容未变；未认证三阶段 preview/confirm/execute **全部 401** |
| M8 | 刷新页面后需重新认证 | **PASS** | 解锁后 `cards: 3`/`inpVisible: false`；`page.reload()` 后 `cards: 0`、`inpVisible: true`、`_memToken.length === 0`、`localStorage`/`sessionStorage` 均无令牌、URL 无令牌 |
| M9 | iframe 嵌入（宿主反代场景）+ 帧内刷新 | **PASS** | 见 §4.1 细表 |

**九项全部 PASS（M3 为修复后 PASS）。** 无 `BLOCKED`、无 `NOT TESTED`。

### 4.1 M9 细分：iframe 内「认证 → 解锁 → 刷新 → 重新认证」

任务书 M9 要求覆盖"退出、刷新、重新认证是否正常"。分开实测两段：

**第一段（跨源 iframe）**：父页面用 `file://` 承载 `<iframe src="http://127.0.0.1:8904/">`。
`contentDocument` 为 `null`（`file://` 与 `http://` 跨源），**这是浏览器的同源策略在生效，不是本服务的问题**。
页面本身在 iframe 里正常渲染、令牌框正常出现、token 正常提交——只是父脚本读不到子文档。
DSH 的真实场景不跨源（反代把面板挂在同源的 `/bionic-panel` 下），所以这一段只作记录。

**第二段（同源 iframe，等价 DSH 反代场景）**：在服务自己的页面上注入 `src="/"` 的 iframe，逐步取真实 DOM：

| 步骤 | 实测结果 |
|---|---|
| iframe 加载完成 | `sameOrigin: true`、`title: "Bionic Agent 管理面板"`、`#tokenInput` **可见**、正文尾部「这个面板需要访问令牌」 |
| 帧内真实鼠标点「填令牌」 | `cards: 3`、`stats` = 「记忆 3 活跃 3 归档 0 任务 0 关系 0 事件 0」、`inpVisible: false`、`_memToken.length = 19`（与 `accept-token-ABC123` 长度一致） |
| 帧内 `location.reload()` | `cards: 0`、`inpVisible: true`、`_memToken.length = 0`、`localStorage`/`sessionStorage` 均不含令牌、正文回到「需要访问令牌」 |
| 父页面错误钩子 | `errs: []` —— **刷新前后零未捕获错误** |

**结论**：iframe 场景下 CSP/跨域/认证均正常，认证 → 解锁 → 刷新 → 回到待认证 的闭环行为与顶层窗口完全一致，
令牌不跨帧、不跨刷新残留。**这一条补齐了任务书 M9 里"刷新、重新认证"的要求。**

### 4.2 备份包目录约束（实测发现的运维约束，非缺陷）

M6/M7 的隔离库必须放在**项目根之内**，否则报：

```
包目录必须位于项目根内：…（root=D:\Cache\Cache_Project\bionic-agent-mcp）
```

这是 `_resolve_package_dir` 的既有设计（受管包目录不得逃出项目根）。验收因此改用
`var/m_accept/m3_bk/agent.db`。

### 4.3 M6 包内容有效性复验与「浏览器下载」口径说明

**解包实测（证明导出的不是空壳）**：

```
包: bionic-memory-source_only-20261120T232404-576b3d0d.zip
  大小: 1584545 B
  testzip: 全部 CRC 校验通过
  条目数: 161
  manifest.json → format_version=1, backup_type=source_only
  顶层键: backup_type, checksum_algorithm, created_at, db_snapshot_stats, embedding,
          excluded_categories, file_count, file_manifest, format_name, format_version,
          generator_version, included_categories, project_name, project_version,
          schema_version, source_stats, source_version, total_size_bytes, warnings
  样本 source/AGENTS.md: 10301 B      sha256=34d52d97bf7d30c9…
  样本 source/bin/备份.ps1: 2967 B    sha256=6bb1144942ecb6fd…
```

即：认证开启后导出的包**结构完整、CRC 全通过、文件可按名读出且各自带哈希**，
不是"只回了 `ok:true` 而内容其实是空的"。

**关于任务书 M6 里「文件下载正常 / 浏览器下载没有被认证机制破坏」这一条的口径（必须说清，不冒充 PASS）**：

本面板**从来没有实现过浏览器下载**。备份导出是**服务端落盘 + 页面显示结果**，
用户看到的是 `package_id` 与体积，不是浏览器的下载对话框。因此：

- "浏览器下载没有被认证破坏"——**架构上不存在可破坏点**，因为没有下载链路。此项标 **`NOT APPLICABLE`**；
- 真要改成"点了就下载到本地"，需要引入 `<a download>` 或 `createObjectURL` 之类，
  而**浏览器原生下载不吃 `Authorization` 头**——那会引入新的、需要单独设计的认证承载点（例如一次性下载令牌）。
  **本轮不做**：属功能扩展，非本批范围，且不应为了凑一条测试而临时开洞。

### 4.4 前端网络原语的实证清单（"没有绕过统一封装的调用"）

任务3 要求确认所有受保护请求都能携带认证，特别是"下载或恢复接口是否直接用了原生 fetch/XHR"。
对 `app/admin_ui.html`（121781 B / 2383 行）逐模式计数：

| 原语 | 出现次数 | 结论 |
|---|---:|---|
| `fetch(` | **2** | 均在 `api()` 与 `sendPing()` 内部，**两者都套 `authHeaders()`** |
| `await api(` | 57 | 全部业务请求（含备份 preview/confirm/execute）都经统一封装 |
| `XMLHttpRequest` | **0** | 无 |
| `WebSocket` / `EventSource` / `sendBeacon` | **0** | 无 |
| `axios` / 其他第三方请求库 | **0** | 无 |
| `window.open` | **0** | 无 |
| `location.href =`（跳转式） | **0** | 无 |
| `createObjectURL` | **0** | 无 |
| `<a download>` | **0** | 无 |

**这就是 M6"下载未受影响"能给出 `NOT APPLICABLE` 的结构性依据**：不是"我们测了下载没坏"，
而是**这份前端根本没有独立于 `api()` 的第二条网络路径**。将来若有人加原生下载，上面这张表立刻会变，
自动化组 3 会当场失败——那正是它存在的意义。

---

## 五、浏览器类型及版本

| 项 | 值 |
|---|---|
| 浏览器 | Microsoft Edge |
| 版本 | **157.0.0.0**（Chromium 内核，User-Agent 内 Chrome/157.0.0.0） |
| UA | `Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36 Edg/157.0.0.0` |
| vendor / lang | `Google Inc.` / `zh-CN` |
| devicePixelRatio | 1 |
| 屏幕 | 2560×1440 |
| 自动化方式 | CDP（`Page.addScriptToEvaluateOnNewDocument` 注入错误钩子 + `Input.dispatchMouseEvent` 真实坐标点击） |

**限制说明**：仅在上述单一浏览器版本上做了真实验收。Firefox / Safari / 移动端浏览器 **NOT TESTED**——
本面板为本地管理界面，目标环境是桌面 Chromium 系，其余浏览器不在本轮范围。此项如实标注，不写成通过。

---

## 六、自动化测试结果

### 6.1 新增 `tests/admin_auth_frontend_test.py`

**77 项 / 8 组 / exit=0**。首轮 76/77，失败项即 §2.3 的设计缺口，修复后 77/77。

| 组 | 内容 | 项数 |
|---|---|---|
| 组 1 | 401 身份标记（后端真发 `SERVICE_ID`、错误令牌也带标记、401 body 不含令牌） | 10 |
| 组 2 | **白名单不许扩张**：公开路径**恰好** `["/", "/static/{name}"]`，且不存在 `/api/health` | 4 |
| 组 3 | 前端网络原语计数：`fetch(` 恰好 2 处且都含 `authHeaders(`；`XMLHttpRequest`/`WebSocket`/`EventSource`/`sendBeacon`/`axios`/`window.open`/`location.href=`/`createObjectURL`/`<a download>` **全 0** | 12 |
| 组 4 | 令牌存储与刷新：`let _memToken = ''` 存在、storage 无令牌键、**无 `location.reload`**、有 `hideTokenUi`/`refreshAll` | 8 |
| 组 5 | 备份恢复三阶段（preview/confirm/execute）都经 `api()` | 5 |
| 组 6 | 插件三个消费方都认 401 与 `bionic-agent-admin`，且无拼错的身份值 | 17 |
| 组 7 | 开关前后各链路：关闭时非 401；开启后 GET 6 条 + POST 5 条全 401；带令牌非 401 | 17 |
| 组 8 | 数据保护：生产库指纹未变、无 sidecar、使用独立临时库且不在项目 `data/` 下 | 4 |
| **合计** | | **77** |

组 8 实测输出：

```
[PASS] 生产库 data/agent.db 指纹未变 — (8581120, 1795085099386275500) vs (8581120, 1795085099386275500)
[PASS] 未产生生产库 sidecar
[PASS] 使用独立临时库 — C:\Users\xing\AppData\Local\Temp\bionic-auth-front-hwh9x53f\agent.db
[PASS] 临时库不在项目 data/ 下
```

### 6.2 全量回归

```
python tests/run_tests.py
汇总：36/36 通过，耗时 79.8s
exit code: 0
```

原 35 套 + 本批新增 `admin_auth_frontend_test.py` = 36 套。最慢三项：`portable_restore_test` 15.2s、
`run_tests_optimize_test` 7.7s、`semantic_test` 3.7s。

```
python -m compileall -q app scripts tests
rc = 0
```

**未修改任何旧测试的预期值，未降低任何门槛，未删除任何测试。**

---

## 七、人工测试结果与过程中的真实缺陷

### 7.1 M3 首轮 FAIL —— 真 bug（已修）

**现象**：M2 页面正常显示令牌输入框，但输入正确令牌、点「填令牌」后，页面**永远停在「需要访问令牌」**，重试无数次都一样。

**根因**（读代码定位，非猜测）：

```js
function applyTokenFromInput() {
  _memToken = ...;
  setTimeout(() => location.reload(), 300);   // ← 问题在这
}
```

令牌按设计只存内存变量 `_memToken`（不写 storage），而 `location.reload()` 会把刚填的令牌一起扔掉
→ 刷新后仍是未认证状态 → 提示再输令牌 → 输完又 reload。**这是一个闭环死锁，任何令牌都进不去。**

**修复**：

- 新增 `function hideTokenUi()`（隐藏输入框并复位 `_authPrompted = false`）；
- `applyTokenFromInput` 改为 `await refreshAll()` **原地重拉**，成功后 `toast('令牌有效，已解锁（仅本页有效，刷新后需重填）','ok')`；空值分支提示后同样走 `refreshAll`；
- 新增 `async function refreshAll(){ await sendPing(); await loadStats(); await loadCategories(); await loadMemories(); await loadTimeline(); }`；
- 启动 IIFE 由逐条调用改为 `await refreshAll();`。

**修复后**：真实鼠标点击 (1107,25) → `stats` 有值、`cards: 3`、输入框隐藏、toast 正确。M3 → PASS。

**这个缺陷只有真实浏览器点得出来。** curl / TestClient 全程 200，任何自动化 API 测试都会给它绿灯——
它不在"接口通不通"这一层，而在"人能不能用"这一层。这正是本轮要求真实浏览器验收的理由。

### 7.2 其他人工观察

| 观察 | 结论 |
|---|---|
| 认证失败时页面**不白屏**、不抛未捕获异常 | 正确。错误钩子全程零捕获 |
| 错误令牌时输入框**保持显示**，用户可重试 | 正确 |
| toast 文案区分「需要令牌」与「令牌有效」 | 正确 |
| iframe 嵌入时认证与普通页面行为一致 | 正确 |
| 恢复执行前自动创建保护性备份（M7 实测产生 `data/restore_backups/rst-…`） | 符合设计 |

---

## 八、未完成项目

| 编号 | 项目 | 状态 | 说明 |
|---|---|---|---|
| U1 | Firefox / Safari / 移动端浏览器验收 | **NOT TESTED** | 目标环境为桌面 Chromium 系；本轮仅 Edge 157 |
| U2 | `verify-install.mjs` 在真实 DSH 宿主内的端到端重装验证 | **NOT TESTED** | 已改代码并通过语法与静态断言（组 6），但未在 DSH 里实际重装插件跑完整巡检 |
| U3 | 认证失败速率限制 / 账号锁定 | **NOT IMPLEMENTED** | 实测 20 次错误令牌全 401、8ms/次、无 429、无锁定。见 §10.1 |
| U4 | 令牌轮换与撤销 | **NOT IMPLEMENTED** | 令牌是启动时读入的静态值，改令牌需重启服务 |
| U5 | 多用户 / 分级权限 | **NOT IMPLEMENTED** | 单令牌，通过即全权（含删除、恢复执行） |
| U6 | HTTPS / TLS | **NOT IMPLEMENTED** | 服务本身只讲明文 HTTP。见 §9.1 |
| U7 | `data/restore_backups`、`data/restore_logs` 是否应进 `.gitignore` | **未处理** | 属既有 N-2 类潜在 P0，本轮不动 |

---

## 九、HTTP 安全风险

### 9.1 明文 HTTP 传输（无 TLS）

| 维度 | 内容 |
|---|---|
| 等级 | **中**（仅本机）/ **高**（一旦绑到远程） |
| 触发条件 | 通过 HTTP 访问面板，且网络路径可被嗅探（公共 Wi-Fi、被控交换机端口、ARP 欺骗） |
| 影响 | `Authorization: Bearer <token>` 在网络上明文字节级可见；令牌被截获即等于全权接管面板，含恢复执行与删除 |
| 实测 | 服务只监听明文 HTTP，进程内无 TLS 层 |
| 建议 | ①默认保持 `127.0.0.1`（现状）；②需要远程时用 HTTPS 反代或 SSH 隧道（`ssh -L 8899:127.0.0.1:8899 host`），**不要把明文 HTTP 直接暴露到网络**；③已写入 README「访问令牌（可选）」小节 |

### 9.2 iframe 嵌入 / 点击劫持

| 维度 | 内容 |
|---|---|
| 等级 | **低** |
| 现状 | 后端未设 `X-Frame-Options` / CSP `frame-ancestors`，面板可被任意本机页面 iframe 嵌入（M9 实测可嵌入并正常交互） |
| 影响 | 攻击者可在自己页面里嵌面板并诱导点击。但**仍需要令牌才能读到任何数据**；写操作另有 `dsh-panel-client` 反代层 `guardWrite` 保护（该保护对直连 8899 无效，仅覆盖反代路径） |
| 建议 | 若未来要更严，可加 `X-Frame-Options: SAMEORIGIN`；**但它会破坏 DSH 宿主 iframe 挂载**（宿主端口与本服务不同源），需先解决宿主白名单机制，**不宜在本批直接加** |

### 9.3 CORS 暴露面

| 维度 | 内容 |
|---|---|
| 等级 | **低（已最小化）** |
| 现状 | `allow_origin_regex` 仅放行 `127.0.0.1` / `localhost` / `[::1]`（含任意端口），`allow_credentials=False`，未放宽为 `*` |
| 收益 | 配合 Header 认证，浏览器不会为跨站请求自动附带自定义 `Authorization` 头 → 跨源伪造请求天然无凭据 |
| 建议 | 维持现状。任何为"方便"把 regex 放宽到 `*` 的改动都应视为安全回退 |

---

## 十、Token 安全风险

### 10.1 无速率限制 / 无锁定（暴力尝试）

| 维度 | 内容 |
|---|---|
| 等级 | **中** |
| 实测 | 连续 20 次错误令牌：**20×401、总耗时 0.16s、平均 8ms/次、无 429、无延迟递增、无锁定** |
| 触发条件 | 攻击者能访问该端口 |
| 影响 | 理论上可高速穷举。实际风险取决于令牌强度：高熵令牌（如 `openssl rand -hex 32`）穷举不可行；**弱令牌（短口令、有意义的词）风险高** |
| 建议 | ①短期靠令牌强度：README 建议 ≥32 位随机 ASCII；②中期可加失败计数与指数退避（需专门设计，避免误伤用户正常重试）；③**不要在无速率限制的前提下用短口令** |

### 10.2 令牌不可热轮换

| 维度 | 内容 |
|---|---|
| 等级 | **低** |
| 触发条件 | 令牌疑似泄露 |
| 影响 | 只能重启服务换令牌，运行期间无法撤销 |
| 建议 | 泄露时立即重启并更换；后续批次可考虑热加载 |

### 10.3 单令牌全权（无权限分级）

| 维度 | 内容 |
|---|---|
| 等级 | **低**（个人本地）/ **中**（多人共用机器） |
| 触发条件 | 令牌被交给第三方 |
| 影响 | 对方可执行删除、恢复覆盖等破坏性操作，无分级授权 |
| 建议 | 当前个人本地场景可接受；多用户需引入角色体系，属架构变动，不在本批 |

### 10.4 CSRF

| 维度 | 内容 |
|---|---|
| 等级 | **实测无风险** |
| 依据 | 认证走 `Authorization` 头而非 Cookie。**浏览器不会自动为跨站请求附加自定义 `Authorization` 头**，跨站表单/fetch 无法携带凭据。CORS 亦限死本机来源且 `allow_credentials=False` |
| 结论 | 无需 CSRF token。这是选择 Header 方案（而非 Cookie/Session）换来的结构性收益 |

### 10.5 浏览器存储

| 维度 | 内容 |
|---|---|
| 等级 | **实测无风险** |
| 依据 | 令牌只存页面内存变量 `_memToken`，**不写 `localStorage` / `sessionStorage` / Cookie / URL**；M8 与自动化组 4 双重验证；刷新即失效 |
| 代价 | 刷新需重填（已在 UI toast、启动提示、README 三处说明） |
| 理由 | 一旦落到 storage，同源下任何脚本（含被注入的第三方脚本）都能读走 |

### 10.6 令牌进入日志 / 异常 / 诊断

| 维度 | 内容 |
|---|---|
| 等级 | **实测无风险** |
| 依据 | 401 响应体为固定文案不含令牌；`data/panel-client.log` 不含令牌；启动提示只打印**长度**不打印内容；自动化组 1 / 组 4 覆盖断言 |

### 10.7 令牌字符集与传输可行性

| 维度 | 内容 |
|---|---|
| 等级 | **已处理** |
| 背景 | HTTP 头值只能是 ASCII。中文令牌在客户端就发不出去（实测 `UnicodeEncodeError: 'ascii' codec can't encode ...`） |
| 现状 | 启动期 `token_problem()` 拒绝非 ASCII 与控制字符并给出具体字符与码位，**不静默降级成关闭认证**；首尾空白自动裁剪后接受 |
| 建议 | 使用 ASCII 随机串（字母数字与 `-_.~`）；长度 ≥32 |

### 10.8 公开面

| 维度 | 内容 |
|---|---|
| 等级 | **已最小化** |
| 现状 | 公开路径**恰好 2 条**：`/`（HTML 外壳，本身不含任何记忆数据）与 `/static/{name}`（后缀白名单 `.js/.css/.svg` + `..`/斜杠拦截） |
| 依据 | 自动化组 2 断言白名单恰好等于 `["/", "/static/{name}"]`，并显式断言**不存在** `/api/health` |
| 备注 | 为解决 R1 而新增公开路由的方案被否决，正是为了守住这 2 条 |

---

## 十一、生产数据库保护证明

**全程零写入。** 前后指纹完全一致：

| 检查项 | 基线 | 收尾 | 结论 |
|---|---|---|---|
| `data/agent.db` SHA256 | `19D8CE986EA44F56E62908683CBE1E45EAF1A4ACD193AABDCB6ACE697D3EFA4E` | **同左** | 未变 |
| 大小 | 8581120 B | **8581120 B** | 未变 |
| mtime | `1795085099386275500` | **1795085099386275500** | 未变 |
| `-wal` / `-shm` / `-journal` | 无 | **无** | 未产生 sidecar |
| MCP registry | `C9591B7DE3EC8B7E…` | 未变 | 未变 |
| `data/记忆.jsonl` | `F80E550D3BD64F1D…` / 76493 B / 47 行 | 未变 | 未变 |

**隔离方式**：所有测试与浏览器验收均通过 `BIONIC_DB_PATH` 指向 `tempfile.mkdtemp()`
或 `var/m_accept/` 下的独立库；`tests/admin_auth_frontend_test.py` 组 8 对生产库做
"指纹不变 + 无 sidecar + 临时库不在项目 `data/` 下"三重断言。

**未执行任何生产恢复。**

---

## 十二、Git 状态前后对比

| 项 | 基线（本批开始） | 收尾 | 变化 |
|---|---|---|---|
| HEAD | `862714c2ad149dd65060190d40a852effe2e7715` | **同左** | 未变 |
| 分支 | `master` | `master` | 未变 |
| 暂存区（staged） | 0 | **0** | 未变（**未 `git add`**） |
| tracked 已修改 | 5 | **8** | +3（`dsh-panel-client/lib/index.js`、`lib/client.js`、`verify-install.mjs`） |
| 未跟踪（untracked） | 60 | **228** | **+168：+167 为本轮验收产物，+1 为本报告自身** |

**tracked 变更明细（`git diff --numstat`）**

```
43   4   README.md
102  7   app/admin_ui.html
250  6   app/admin_ui.py
1    1   app/portable_backup.py          ← 上一批 OPT-01 遗留
7    7   app/portable_restore.py         ← 上一批 OPT-01 遗留
26   6   dsh-panel-client/lib/client.js
25   0   dsh-panel-client/lib/index.js
23   1   dsh-panel-client/verify-install.mjs
```

**未跟踪 60 → 228 的构成（如实说明）**

| 路径 | 条目数 | 性质 |
|---|---:|---|
| `data/restore_backups/rst-20261120T232454-b99a928f/` | 145 | M7 恢复前自动创建的保护性备份（145 个文件） |
| `data/restore_backups/` 其他 | 17 | 同上 |
| `data/restore_logs/` | 3 | `rst-…-c003e0fd.json`(52220B)、`rst-…-bd0f5057.json`(52220B)、`rst-…-b99a928f.json`(53890B) |
| `var/m_accept/` | 1 | M3–M9 验收隔离库 + M6 导出的备份包 |
| `docs/OPT_01_B_FINAL_REPORT.md` | 1 | **本报告自身**（本批唯一有意新增的文档） |
| 原有未跟踪（`scripts/dev/` 51、`docs/` 4、`logs/` 2、`data/backup_F01_F03_before_sync.json` 1、`tests/` 3） | 61 | 与本轮代码改动无关（`tests/` 3 为本批与前批新增的测试文件） |

**关键核实**：`git ls-files data/restore_backups data/restore_logs var` → **0 行**，即这些目录**从未被 git 跟踪**，
`data/restore_backups` 与 `data/restore_logs` 是系统运行时产物目录（恢复流程自动写入）。
**这些不是用户文件**，是本轮验收触发的产物。按"禁删用户文件、禁清理未跟踪文件"的约束，**本轮全部原样保留，一个都没删。**

**未执行**：`git add`、`git commit`、`git push`、`git tag`、`git reset --hard`、`git checkout .`、`git clean -fd`。

**另有既有事实需说明**：`git status -sb` 显示 `## master...origin/master [ahead 24]`——本地分支比远端领先 24 个提交，这是本批开始前就存在的状态（上一批 S6-COMMIT 明确未 push），**不是本轮造成的**。本轮未 push 任何内容。

---

## 十三、回滚方式

三级，按侵入程度递增：

### 第 1 级：只关认证（零代码回滚，10 秒）

不传 `--token`、不设 `BIONIC_ADMIN_TOKEN`，重启 Admin UI。
令牌认证是**可选功能，默认关闭**，关闭后行为与加入此功能前逐字一致（M1 实测证明，旧测试 `admin_host_guard_test.py` 64 项全过也证明）。

```powershell
python -m app.admin_ui --port 8899
```

### 第 2 级：回滚代码改动（精确到文件）

本批改动的 6 个文件全部可单独还原，互不依赖：

```powershell
git checkout -- app/admin_ui.py app/admin_ui.html README.md `
                dsh-panel-client/lib/index.js dsh-panel-client/lib/client.js `
                dsh-panel-client/verify-install.mjs
```

> 注：`app/portable_backup.py` 与 `app/portable_restore.py` 的改动属**上一批 OPT-01**（只读 URI 安全修复），
> **不应随本批一起回滚**——回滚它们会重新引入只读 URI 编码缺陷。

新增测试文件 `tests/admin_auth_frontend_test.py` 为独立新增，回滚时可保留（不影响运行）或删除。

### 第 3 级：回滚插件（含备份）

`dsh-panel-client/lib/index.js` 改动前已备份：

```
C:\Users\xing\Documents\deepseek-harness\default-workspace\_final_backup_index.js   (27003 B, 原文件)
```

回滚插件只需还原该文件并重启 DSH（插件在 Electron 进程内运行，**必须完全退出重启含托盘**）。
还原后插件回到"启用令牌时误判服务不可用"的旧行为——**认证本身仍然有效**，只是插件体验退化，数据安全不受影响。
`lib/client.js` 与 `verify-install.mjs` 的改动可用第 2 级的 `git checkout` 一并还原。

### 回滚后的验证

```powershell
python tests/run_tests.py        # 期望 35/35（本批新增测试单独移除后）
python -m compileall -q app scripts tests
```

---

## 十四、是否建议 OPT-01-B 最终验收

**建议通过最终验收。**

### 支持结论的实测依据

| 验收维度 | 状态 | 依据 |
|---|---|---|
| R1 插件兼容 | **已解决** | 服务端 401/200 双带身份头；三个消费方全部识别；组 6 静态断言通过 |
| M1–M9 真实浏览器 | **9/9 PASS** | 真实 Edge 157 + 真实鼠标坐标，含备份与恢复全流程 |
| 前端认证覆盖 | **完整** | `fetch(` 恰好 2 处且都带凭据；`XHR`/`WebSocket`/`EventSource`/`sendBeacon`/`axios`/`window.open`/`location.href=`/`createObjectURL`/`<a download>` 全 0 |
| 公开面 | **未扩张** | 恰好 2 条，且断言不存在 `/api/health` |
| 默认关闭兼容 | **逐字不变** | M1 全通；`admin_host_guard_test.py` 64 项旧断言未改全过 |
| 回归 | **36/36 exit=0** | 含新增 1 套；`compileall` rc=0 |
| 生产数据 | **零写入** | SHA256/大小/mtime/sidecar 全未变 |
| Git | **干净** | staged 0、未 commit、未 push |

### 本轮最有价值的产出不是测试数字，而是两个只有真实执行才能暴露的问题

1. **M3 的 reload 死循环**：令牌只存内存 + 填完就 reload = 永远进不去。所有 API 层测试都给它绿灯，
   只有真实点鼠标才暴露。**"接口 200"与"人能登录"是两件事。**
2. **200 也要带身份头**：测试首轮 76/77 的唯一失败项，指向的是"调用方判身份要走两条逻辑"的结构问题，
   而不是一个漏写的 assert。**响应头才是契约，body 形状会随版本变。**

### 建议的后续批次（不属本轮，仅记录）

| 优先级 | 事项 | 关联 |
|---|---|---|
| P2 | 认证失败速率限制 / 指数退避 | §10.1 U3 |
| P2 | `data/restore_backups`、`data/restore_logs` 纳入 `.gitignore` | U7 / N-2 |
| P3 | 令牌热轮换 | §10.2 U4 |
| P3 | HTTPS 部署指引细化（反代配置示例） | §9.1 |
| P3 | Firefox/Safari 冒烟 | U1 |

### 验收后仍未覆盖的（如实标注，不冒充通过）

- **U1** Firefox / Safari / 移动端：`NOT TESTED`
- **U2** 真实 DSH 宿主内重装插件的端到端巡检：`NOT TESTED`（已过语法与静态断言）
- **U3–U6** 速率限制、轮换、多用户、TLS：`NOT IMPLEMENTED`（已在 §9 与 §10 逐项给出风险与建议）
