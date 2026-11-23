"""Admin UI 可选令牌认证（OPT-01-B）的独立测试。

要钉住的不是"能开认证"，而是几个会静默出错的地方：

1. **默认关闭必须逐字兼容历史行为**。原有 3 个测试（`admin_backup_restore_test`、
   `timeline_test`、`views_test`）直接 `TestClient(admin_ui.app)` 不带任何凭据；
   如果默认值一改，它们会一起 401，而那种失败看起来像"新功能有问题"、实际是
   把兼容性弄坏了。所以这里第一条就验"不配令牌时一切照旧"。

2. **认证必须全路由覆盖，不能只护住部分 API**。手工给 75 条路由各加一次校验
   一定会漏；这里改成遍历 `app.routes` 逐个打，把"漏了哪条"变成可计数的断言。

3. **白名单只能是 `/` 与 `/static/*`**。首页必须公开（否则页面本身都拿不到），
   静态资源必须公开（否则 HTML 里的 <script src> 被 401，页面白屏）。

4. **令牌不能出现在任何它会泄漏的地方**：URL、响应体、响应头、日志、异常。

5. **远程绑定授权不能被令牌架空，令牌也不能代替远程授权**——两件事独立。

6. **非法令牌必须在启动期就拒掉，不能静默降级成"认证关闭"**。最典型的非法情形
   是中文令牌：HTTP 头值只能是 ASCII，浏览器和 httpx 都在**发请求之前**就抛
   UnicodeEncodeError，现场表现是"令牌明明复制对了却全 401"。这种配置等于没配，
   必须当场报错。

浏览器行为不在这里假装验证：本测试只到 TestClient 层。真实浏览器的手动验证
步骤写在报告里（见 OPT-01-B 交付报告「浏览器兼容测试结果」一节），标注为手工项。
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import _env_guard  # noqa: E402,F401  OPT-04a：数据库隔离闸门（必须在任何 app.* 之前）

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 必须在 import app.* 之前隔离数据库：_lifespan 会调 core._ensure_db()，
# 若沿用默认 BIONIC_DB_PATH 就会碰生产库 data/agent.db。
_TMP = tempfile.mkdtemp(prefix="bionic-token-auth-")
os.environ["BIONIC_DB_PATH"] = str(Path(_TMP) / "agent.db")

# run_tests.py 的子进程环境里可能带着外部设的 BIONIC_ADMIN_TOKEN。
# 它是"默认关闭"这条测试的前提，所以先把外部值摘掉，需要时再由本测试自己设。
_ENV_KEY = "BIONIC_ADMIN_TOKEN"
_SAVED_ENV = os.environ.pop(_ENV_KEY, None)

RESULTS: list[tuple[str, bool, str]] = []

TOKEN = "tok-OPT01B-9f3c2a"
PROD_DB = ROOT / "data" / "agent.db"


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def _sha(p: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _prod_fingerprint() -> tuple:
    """生产库指纹：只用文件 API，不连接数据库（连接会建 -shm/-wal）。"""
    if not PROD_DB.is_file():
        return ("absent",)
    st = PROD_DB.stat()
    return (_sha(PROD_DB), st.st_size, st.st_mtime_ns)


def _all_route_paths(app) -> list[tuple[str, tuple]]:
    out = []
    for route in app.routes:
        path = getattr(route, "path", None)
        if path is None:
            continue
        methods = tuple(sorted(getattr(route, "methods", []) or []))
        out.append((path, methods))
    return out


def main() -> int:
    from fastapi.testclient import TestClient
    from app import admin_ui as ui
    from app import db

    db.init_db()

    # 生产库指纹（前）。全程只读文件属性，不连接。
    prod_before = _prod_fingerprint()

    # 起始状态：确保认证是关的（外部环境变量已摘掉）
    ui.configure_auth("")
    check("初始状态认证默认关闭", ui.auth_enabled() is False,
          f"auth_enabled={ui.auth_enabled()}")

    # ==================================================================
    # 组 1：配置来源与优先级
    # ==================================================================
    print("\n--- 组 1：配置来源与优先级 ---")

    check("环境变量名固定为 BIONIC_ADMIN_TOKEN", ui.ADMIN_TOKEN_ENV == "BIONIC_ADMIN_TOKEN",
          ui.ADMIN_TOKEN_ENV)

    # 环境变量 → 生效
    os.environ[_ENV_KEY] = "env-token-1"
    ui.configure_auth(None)
    check("环境变量可配置令牌", ui.auth_enabled() and ui._ADMIN_TOKEN == "env-token-1",
          ui._ADMIN_TOKEN)

    # CLI 值 → 覆盖环境变量（优先级 CLI > 环境变量）
    ui.configure_auth("cli-token-wins")
    check("CLI 参数优先于环境变量",
          ui._ADMIN_TOKEN == "cli-token-wins",
          f"env=env-token-1, 生效={ui._ADMIN_TOKEN}")

    # 空串 → 关闭
    ui.configure_auth("")
    check("空串关闭认证", ui.auth_enabled() is False)
    check("关闭后仍能读到环境变量（未显式覆盖时）",
          (ui.configure_auth(None), ui._ADMIN_TOKEN == "env-token-1")[1],
          ui._ADMIN_TOKEN)

    os.environ.pop(_ENV_KEY, None)
    ui.configure_auth("")
    check("摘掉环境变量后回落为关闭", ui.auth_enabled() is False)

    # 控制字符与空白：粘贴常见错法。控制字符非法；首尾空白由调用方 strip 掉，合法。
    check("控制字符视为非法",
          ui.token_problem("a\tb") != "" and ui.token_problem("a\x7fb") != "")
    check("首尾空白不算错（由调用方裁剪）",
          ui.token_problem(" abc") == "" and ui.token_problem("abc ") == "")
    check("裁剪发生在 configure_auth",
          (ui.configure_auth("  padded-token  "), ui._ADMIN_TOKEN == "padded-token")[1],
          ui._ADMIN_TOKEN)
    check("中文令牌视为非法（HTTP 头只能 ASCII）",
          ui.token_problem("中文令牌") != "", ui.token_problem("中文令牌")[:40])
    check("纯 ASCII 令牌合法",
          ui.token_problem("aZ0-_.~!@#$%^&*()") == "" and ui.token_problem(TOKEN) == "")

    # 非法令牌不能静默降级
    _old = ui._ADMIN_TOKEN
    raised = False
    try:
        ui.configure_auth("中文令牌")
    except ValueError:
        raised = True
    check("非法令牌抛 ValueError 而不是静默关闭认证", raised)
    check("抛异常后原状态未被破坏", ui._ADMIN_TOKEN == _old, ui._ADMIN_TOKEN)

    # 空 token 与 None 的区分
    ui.configure_auth(TOKEN)
    ui.configure_auth(None)
    os.environ[_ENV_KEY] = ""
    ui.configure_auth(None)
    check("环境变量为空串等价于未配置", ui.auth_enabled() is False)
    os.environ.pop(_ENV_KEY, None)

    # ==================================================================
    # 组 2：白名单与全路由覆盖
    # ==================================================================
    print("\n--- 组 2：白名单与全路由覆盖 ---")

    check("白名单：首页", ui.is_public_path("/") is True)
    check("白名单：静态资源", ui.is_public_path("/static/x.js") is True,
          "/static/3d-force-graph.min.js")
    check("非白名单：/api/stats", ui.is_public_path("/api/stats") is False)
    check("非白名单：/docs", ui.is_public_path("/docs") is False)
    check("非白名单：/openapi.json", ui.is_public_path("/openapi.json") is False)
    # 前缀匹配不能把 /staticx 误放进白名单
    check("白名单前缀匹配不越界", ui.is_public_path("/staticx") is False)

    client = TestClient(ui.app)

    # 开启认证后遍历全部路由：除白名单外都必须 401
    ui.configure_auth(TOKEN)
    paths = _all_route_paths(ui.app)
    leaked: list[str] = []
    public_seen: list[str] = []
    for path, methods in paths:
        if ui.is_public_path(path):
            public_seen.append(path)
            continue
        # 路径参数用 1 填，保证路由能匹配上（匹配不上会 404，同样不是 200/401，
        # 会被下面的断言按"既非 401 也非 404"单独抓出来）
        probe = path.replace("{name}", "x.js").replace("{memory_id}", "1")
        probe = probe.replace("{task_id}", "1").replace("{memory_type}", "fact")
        probe = probe.replace("{conflict_id}", "1").replace("{package_id}", "pkg")
        probe = probe.replace("{restore_id}", "1").replace("{snapshot_id}", "1")
        probe = probe.replace("{group}", "g").replace("{id}", "1")
        method = "GET" if "GET" in methods else (methods[0] if methods else "GET")
        try:
            r = client.request(method, probe, json={} if method in ("POST", "PUT") else None)
        except Exception as exc:  # 路由内部异常不应发生，但要记录而不是崩掉整轮
            leaked.append(f"{method} {path} raised {type(exc).__name__}")
            continue
        if r.status_code not in (401, 404):
            leaked.append(f"{method} {path} -> {r.status_code}")

    check(f"全路由认证覆盖（共 {len(paths)} 条路由，公开 {len(public_seen)} 条）",
          not leaked,
          f"未受保护 {leaked[:8]}" if leaked else f"公开={public_seen}")
    check("白名单恰好是 / 与 /static/{name}",
          sorted(public_seen) == ["/", "/static/{name}"], str(sorted(public_seen)))

    # 白名单本身在开启认证后仍可用
    r = client.get("/")
    check("开启认证后首页仍可访问", r.status_code == 200 and "text/html" in r.headers.get("content-type", ""),
          f"{r.status_code} {r.headers.get('content-type')}")
    r = client.get("/static/3d-force-graph.min.js")
    check("开启认证后静态资源仍可访问", r.status_code == 200, str(r.status_code))
    # 白名单不绕过 static_file 自身的校验。
    # 注意 `/static/../app/admin_ui.py` 会被客户端/服务端规范化成 `/app/admin_ui.py`，
    # 于是它根本不在白名单里 → 401。这也是安全的结果（不是 200 就行），
    # 所以这里断言"没有拿到文件内容"，而不是钉死某个具体状态码。
    for probe in ["/static/../app/admin_ui.py",
                  "/static/..%2f..%2fapp%2fadmin_ui.py",
                  "/static/%2e%2e/app/admin_ui.py"]:
        r = client.get(probe)
        got_file = r.status_code == 200 and "import" in r.text[:200]
        check(f"白名单不绕过静态校验：{probe}", not got_file, f"{r.status_code}")

    # ==================================================================
    # 组 3：认证行为
    # ==================================================================
    print("\n--- 组 3：认证行为 ---")

    H = {"Authorization": f"Bearer {TOKEN}"}

    r = client.get("/api/stats")
    check("无凭据 → 401", r.status_code == 401, str(r.status_code))
    check("401 带 WWW-Authenticate",
          'Bearer realm="bionic-admin"' == r.headers.get("www-authenticate"),
          str(r.headers.get("www-authenticate")))
    check("401 响应体形状与 _err 一致（ok/message）",
          r.json().get("ok") is False and isinstance(r.json().get("message"), str),
          json.dumps(r.json(), ensure_ascii=False))

    cases = [
        ("正确令牌", f"Bearer {TOKEN}", 200),
        ("错误令牌", "Bearer wrong-token", 401),
        ("空令牌", "Bearer ", 401),
        ("只有 Bearer 无值", "Bearer", 401),
        ("裸令牌无 scheme", TOKEN, 401),
        ("Basic scheme", "Basic czNjcmV0", 401),
        ("scheme 大小写不敏感 bearer", f"bearer {TOKEN}", 200),
        ("scheme 大小写不敏感 BeArEr", f"BeArEr {TOKEN}", 200),
        ("多余空格", f"Bearer   {TOKEN}  ", 200),
        ("前缀相近但错误", f"Bearer {TOKEN}x", 401),
        ("令牌为前缀但短", f"Bearer {TOKEN[:-1]}", 401),
    ]
    for label, hdr, want in cases:
        got = client.get("/api/stats", headers={"Authorization": hdr}).status_code
        check(f"Authorization 处理：{label} → {want}", got == want, f"实得 {got}")

    # 不区分错误类型：三种错法的响应体必须完全一致（不泄漏"错在哪一步"）
    bodies = set()
    for hdr in ["", "Bearer wrong", "Basic x", "Bearer "]:
        bodies.add(client.get("/api/stats", headers={"Authorization": hdr} if hdr else {}).text)
    check("不同错误类型返回同一响应体（不泄漏内部信息）", len(bodies) == 1,
          f"出现 {len(bodies)} 种响应体")

    # 未认证不得触发任何业务写操作
    r = client.get("/api/stats", headers=H)
    before_total = r.json()["data"]["memories_total"]
    r = client.post("/api/memories", json={"content": "未认证不该落库"})
    check("未认证写操作被拒（401）", r.status_code == 401, str(r.status_code))
    mid_total = client.get("/api/stats", headers=H).json()["data"]["memories_total"]
    check("未认证写操作未落库", mid_total == before_total, f"{before_total} -> {mid_total}")

    # 已认证写操作正常
    r = client.post("/api/memories", json={"content": "已认证写入"}, headers=H)
    check("已认证写操作成功", r.status_code == 200 and r.json().get("ok") is True,
          f"{r.status_code} {r.text[:80]}")
    after_total = client.get("/api/stats", headers=H).json()["data"]["memories_total"]
    check("已认证写操作确实落库", after_total == mid_total + 1, f"{mid_total} -> {after_total}")

    # 删除类危险接口同样被挡住
    r = client.delete(f"/api/backup/packages/{TOKEN}")
    check("DELETE 未认证被拒", r.status_code == 401, str(r.status_code))

    # 备份/恢复相关（本任务重点关注）
    for method, path in [("POST", "/api/backup/export"), ("POST", "/api/backup/preview"),
                         ("POST", "/api/backup/execute")]:
        r = client.request(method, path, json={})
        check(f"备份恢复接口 {method} {path} 未认证被拒", r.status_code == 401, str(r.status_code))

    # ==================================================================
    # 组 4：令牌不泄漏
    # ==================================================================
    print("\n--- 组 4：令牌不泄漏 ---")

    r401 = client.get("/api/stats")
    r200 = client.get("/api/stats", headers=H)
    check("401 响应体不含令牌", TOKEN not in r401.text)
    check("200 响应体不含令牌", TOKEN not in r200.text)
    check("200 响应头不含令牌",
          not any(TOKEN in v for v in r200.headers.values()),
          str([k for k, v in r200.headers.items() if TOKEN in v]))
    check("401 响应头不含令牌",
          not any(TOKEN in v for v in r401.headers.values()))

    # 令牌不能通过 URL/查询参数生效（GET 参数不是凭据）
    r = client.get(f"/api/stats?token={TOKEN}")
    check("URL 查询参数不是凭据（仍 401）", r.status_code == 401, str(r.status_code))
    r = client.get(f"/api/stats?access_token={TOKEN}")
    check("URL access_token 参数不是凭据（仍 401）", r.status_code == 401, str(r.status_code))

    # 日志：/api/ping 会往 data/panel-client.log 写记录，里面不能有令牌
    log_path = ROOT / "data" / "panel-client.log"
    check("ping 未认证时不记录报到（401 即拒绝）",
          client.get("/api/ping").status_code == 401)
    client.get("/api/ping", headers=H)
    if log_path.is_file():
        text = log_path.read_text(encoding="utf-8", errors="replace")
        check("panel-client.log 不含令牌", TOKEN not in text)
    else:
        check("panel-client.log 不含令牌（文件不存在，视为通过）", True, "日志未生成")

    # 异常信息不泄漏：非法令牌错误文本里只有问题描述，不回显完整令牌
    problem = ui.token_problem("中文令牌abc")
    check("非法令牌报错不回显整个令牌", "中文令牌abc" not in problem, problem[:50])
    check("非法令牌报错给出可操作提示（建议 ASCII）", "ASCII" in problem, problem[:50])

    # ==================================================================
    # 组 5：远程绑定与令牌互相独立
    # ==================================================================
    print("\n--- 组 5：远程绑定与令牌互相独立 ---")

    ok_remote_no_token, msg = ui.check_bind_safety("0.0.0.0", False, has_token=False)
    check("远程+无令牌+未授权 → 拒绝", ok_remote_no_token is False)
    check("拒绝文案提到「没有任何身份认证」", "没有任何身份认证" in msg)
    check("拒绝文案提到 --allow-remote", "--allow-remote" in msg)
    # 「不是认证」这句出现在**放行后的警告**里（授权了才需要提醒你别把它当认证），
    # 拒绝分支不需要——它压根没启动。
    _ok_w, _msg_w = ui.check_bind_safety("0.0.0.0", True, has_token=False)
    check("无令牌放行警告说明 --allow-remote 不是认证",
          _ok_w is True and "不是认证" in _msg_w)

    ok_remote_with_token, msg = ui.check_bind_safety("0.0.0.0", False, has_token=True)
    check("远程+有令牌+未授权 → 仍然拒绝（令牌不能代替远程授权）",
          ok_remote_with_token is False)
    check("该文案说明两者互相独立不可替代",
          "互相独立" in msg and "不能互相替代" in msg, msg[:80])

    ok_allow, msg = ui.check_bind_safety("0.0.0.0", True, has_token=True)
    check("远程+有令牌+已授权 → 放行", ok_allow is True)
    check("放行文案说明远程访问要令牌", "令牌" in msg, msg[:80])

    ok_allow2, msg2 = ui.check_bind_safety("0.0.0.0", True, has_token=False)
    check("远程+无令牌+已授权 → 放行（提示风险）", ok_allow2 is True)
    check("无令牌放行文案提醒没有身份认证", "没有任何身份认证" in msg2, msg2[:80])

    for host in ["127.0.0.1", "localhost", "::1", "[::1]", "127.0.0.2"]:
        ok, _ = ui.check_bind_safety(host, False, has_token=False)
        check(f"本机地址 {host} 放行", ok is True)
        ok, _ = ui.check_bind_safety(host, False, has_token=True)
        check(f"本机地址 {host} 有令牌时也放行", ok is True)

    # 已有守卫测试的默认路径不能被破坏：has_token 默认 False
    ok, msg = ui.check_bind_safety("0.0.0.0", False)
    check("check_bind_safety 默认 has_token=False（兼容旧断言）",
          ok is False and "没有任何身份认证" in msg)

    # ==================================================================
    # 组 6：CORS 未被放宽
    # ==================================================================
    print("\n--- 组 6：CORS 未被放宽 ---")

    r = client.get("/api/stats", headers={"Origin": "http://127.0.0.1:59647"})
    check("跨源 401 仍带 ACAO（前端能读到错误体）",
          r.headers.get("access-control-allow-origin") == "http://127.0.0.1:59647",
          str(r.headers.get("access-control-allow-origin")))
    r = client.get("/api/stats", headers={"Origin": "http://evil.example.com"})
    check("非本机来源不获得 ACAO",
          r.headers.get("access-control-allow-origin") is None,
          str(r.headers.get("access-control-allow-origin")))
    r = client.options("/api/stats", headers={
        "Origin": "http://127.0.0.1:59647",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type,authorization"})
    check("preflight 不被认证挡住（200）", r.status_code == 200, str(r.status_code))
    check("preflight 允许 authorization 头",
          "authorization" in (r.headers.get("access-control-allow-headers") or "").lower(),
          str(r.headers.get("access-control-allow-headers")))
    check("CORS 未开启凭据（allow_credentials 仍为 False）",
          r.headers.get("access-control-allow-credentials") is None,
          str(r.headers.get("access-control-allow-credentials")))

    # ==================================================================
    # 组 7：CLI 参数与启动顺序
    # ==================================================================
    print("\n--- 组 7：CLI 参数与启动顺序 ---")

    # --token 被解析、非法值 exit(2)、且不 import uvicorn、不碰 DB
    import types
    fake_uv = types.ModuleType("uvicorn")
    calls: list = []

    class FakeServer:
        def run(self, app=None, **kw):
            calls.append(kw)

    def fake_run(app=None, **kw):
        calls.append(kw)

    fake_uv.run = fake_run
    fake_uv.Server = FakeServer
    saved_uv = sys.modules.get("uvicorn")
    sys.modules["uvicorn"] = fake_uv
    saved_argv = sys.argv[:]
    saved_ensure = getattr(ui.core, "_ensure_db", None)

    def _no_db(*a, **k):
        calls.append({"__db_touched__": True})

    try:
        ui.core._ensure_db = _no_db

        def run_cli(argv):
            calls.clear()
            sys.argv = ["app.admin_ui"] + argv
            buf_out, buf_err = io.StringIO(), io.StringIO()
            code = 0
            try:
                with redirect_stdout(buf_out), redirect_stderr(buf_err):
                    ui._run_cli()
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
            return code, buf_out.getvalue(), buf_err.getvalue()

        code, out, err = run_cli(["--host", "0.0.0.0", "--token", TOKEN])
        check("远程未授权时 --token 不放宽绑定（exit 2）", code == 2, f"exit={code}")
        check("被拒时没有启动 uvicorn", not any("__db_touched__" not in c for c in calls), str(calls))

        code, out, err = run_cli(["--host", "0.0.0.0", "--allow-remote", "--token", TOKEN])
        check("远程+授权+令牌 正常启动（exit 0）", code == 0, f"exit={code}")
        check("确实调用了 uvicorn.run", len(calls) >= 1, str(calls))
        check("启动未触碰数据库", not any("__db_touched__" in c for c in calls), str(calls))

        code, out, err = run_cli(["--token", ""])
        check("--token 空值 exit 2", code == 2, f"exit={code}")
        check("空值提示不传空串", "空串" in err or "空" in err, err.strip()[:80])

        code, out, err = run_cli(["--token", "中文令牌"])
        check("非 ASCII 令牌 exit 2", code == 2, f"exit={code}")
        check("非 ASCII 令牌提示含 ASCII 字样", "ASCII" in err, err.strip()[:100])

        code, out, err = run_cli(["--token", " tok-with-space"])
        check("首尾空白令牌被裁剪后接受（粘贴常态，不算错）",
              code == 0 and ui._ADMIN_TOKEN == "tok-with-space",
              f"exit={code} token={ui._ADMIN_TOKEN!r}")

        # 优先级：CLI 必须压过环境变量
        os.environ[_ENV_KEY] = "env-loses"
        code, out, err = run_cli(["--host", "127.0.0.1", "--token", TOKEN])
        check("CLI 令牌压过环境变量", ui._ADMIN_TOKEN == TOKEN, ui._ADMIN_TOKEN)
        os.environ.pop(_ENV_KEY, None)

        # 不给 --token 时读环境变量
        os.environ[_ENV_KEY] = TOKEN
        code, out, err = run_cli([])
        check("不传 --token 时读环境变量", ui._ADMIN_TOKEN == TOKEN, ui._ADMIN_TOKEN)
        os.environ.pop(_ENV_KEY, None)

        # 都不给 → 认证关闭
        code, out, err = run_cli([])
        check("CLI 与环境变量都没有 → 认证关闭", ui.auth_enabled() is False)

        # 日志不打印令牌明文
        ui.configure_auth(TOKEN)
        code, out, err = run_cli(["--host", "0.0.0.0", "--allow-remote", "--token", TOKEN])
        check("启动输出不含令牌明文", TOKEN not in out and TOKEN not in err,
              (out + err).strip()[:80])

    finally:
        sys.argv = saved_argv
        if saved_uv is None:
            sys.modules.pop("uvicorn", None)
        else:
            sys.modules["uvicorn"] = saved_uv
        if saved_ensure is not None:
            ui.core._ensure_db = saved_ensure

    # ==================================================================
    # 组 8：默认关闭时与历史行为逐字一致
    # ==================================================================
    print("\n--- 组 8：默认关闭 → 历史行为 ---")

    ui.configure_auth("")
    check("关闭认证后 auth_enabled False", ui.auth_enabled() is False)
    for path in ["/", "/api/stats", "/api/memories?limit=1", "/docs", "/openapi.json"]:
        r = client.get(path)
        check(f"未配令牌时 {path} 可访问（200）", r.status_code == 200, str(r.status_code))
    r = client.post("/api/memories", json={"content": "默认关闭时可直接写"})
    check("未配令牌时写操作无需凭据", r.status_code == 200 and r.json().get("ok") is True,
          str(r.status_code))

    # methods 覆盖：POST/PUT/DELETE 在关闭时也放行
    r = client.put("/api/memories/1", json={"content": "x"})
    check("未配令牌时 PUT 不被认证挡（非 401）", r.status_code != 401, str(r.status_code))

    # ==================================================================
    # 组 9：数据保护
    # ==================================================================
    print("\n--- 组 9：数据保护 ---")

    prod_after = _prod_fingerprint()
    check("生产库 data/agent.db 指纹未变", prod_before == prod_after,
          f"{prod_before[:2]} vs {prod_after[:2]}" if prod_before and prod_after else "absent")
    check("测试未连接生产库（无 -wal/-shm 生成）",
          not (ROOT / "data" / "agent.db-wal").exists()
          and not (ROOT / "data" / "agent.db-shm").exists())
    tmp_db = Path(os.environ["BIONIC_DB_PATH"])
    check("测试用独立临时库", tmp_db.is_file() and _TMP in str(tmp_db), str(tmp_db))
    check("临时库不在项目 data/ 下", "bionic-agent-mcp" not in str(tmp_db), str(tmp_db))

    # ==================================================================
    # 汇总
    # ==================================================================
    total = len(RESULTS)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = [(n, d) for n, ok, d in RESULTS if not ok]
    print(f"\n{'=' * 60}")
    print(f"{passed}/{total} PASS")
    if failed:
        print(f"{len(failed)} FAIL:")
        for n, d in failed:
            print(f"  [FAIL] {n} — {d}")
    else:
        print("全部通过")

    # 恢复外部环境变量，避免影响其它测试（虽然每个测试是独立子进程）
    if _SAVED_ENV is not None:
        os.environ[_ENV_KEY] = _SAVED_ENV
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
