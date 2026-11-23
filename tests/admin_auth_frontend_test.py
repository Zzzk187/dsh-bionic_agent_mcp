"""Admin UI 令牌认证的「前端链路 + 服务身份」测试（OPT-01-B-FINAL）。

这个文件补的是一个在蓝图上很容易漏、但线上一定会咬人的缺口：
**后端认证正确 ≠ 浏览器用得起来**。

它钉五件事：

1. **401 必须带身份标记**。面板插件（`dsh-panel-client`）靠"端口上是不是我们的
   服务"决定要不要复用，判据是 `/api/stats` 的响应形状。开启令牌后这条请求
   变 401 —— 若插件把它当成"别人占着端口"，它会 `pickFreePort` 另起一个实例，
   同一份 agent.db 被两个进程打开。所以 401 里必须能认出"这是我"，靠
   `X-Bionic-Service` + `WWW-Authenticate`。这里同时校验后端真的会发、
   以及三个消费方（插件主半、插件客户端半、安装自检）真的会认。
   **认标记而不是认"任何 401"**——别的服务也可能用 Bearer。

2. **不能新增公开路由来"顺便"解决兼容**。白名单只能是 `/` 与 `/static/*`；
   新开一个 `/api/health` 等于为兼容扩大未认证面，还会打破既有白名单断言。

3. **所有前端请求必须经过同一个封装**。逐个 `fetch` 打补丁一定会漏一处，
   而漏掉的那处表现是"某个功能莫名 401"。这里按"网络原语"计数：
   `fetch(` 只允许出现在 `api()` 与 `sendPing()` 里，`XMLHttpRequest` /
   `WebSocket` / `EventSource` / `sendBeacon` / `window.open` / `<a download>`
   一律 0 处（有下载需求时它们会绕过令牌）。

4. **令牌只活在内存**。写进 `localStorage`/`sessionStorage` 后同源任何脚本都能
   读走；而"输入令牌后 reload 页面"听起来无害，实则把内存令牌扔掉、页面永远
   停在"需要访问令牌"——这是本批验收时真踩到的死循环。两条都要钉住。

5. **认证后数据链真的通**。默认关闭时逐项 200，开启后逐项 401，带正确令牌
   逐项 200 —— 包括备份导出、恢复预览这些容易只测读取的地方。

浏览器行为最后仍然由手工验收（M1–M9）承担，本文件不做像素级断言。
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path
import _env_guard  # noqa: E402,F401  OPT-04a：数据库隔离闸门（必须在任何 app.* 之前）

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 必须在 import app.* 之前隔离数据库：_lifespan 会调 core._ensure_db()，
# 若沿用默认 BIONIC_DB_PATH 就会碰生产库 data/agent.db。
_TMP = tempfile.mkdtemp(prefix="bionic-auth-front-")
os.environ["BIONIC_DB_PATH"] = str(Path(_TMP) / "agent.db")

_ENV_KEY = "BIONIC_ADMIN_TOKEN"
_SAVED_ENV = os.environ.pop(_ENV_KEY, None)

RESULTS: list[tuple[str, bool, str]] = []
TOKEN = "tok-front-7c1b"
PROD_DB = ROOT / "data" / "agent.db"
HTML = ROOT / "app" / "admin_ui.html"
PACKAGE_CLIENT = ROOT / "dsh-panel-client"


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def _prod_fingerprint() -> tuple[int, int] | None:
    if not PROD_DB.is_file():
        return None
    st = PROD_DB.stat()
    return (st.st_size, st.st_mtime_ns)


def _html() -> str:
    return HTML.read_text(encoding="utf-8")


def main() -> int:
    from fastapi.testclient import TestClient

    from app import admin_ui as ui
    from app import db

    db.init_db()
    client = TestClient(ui.app)
    prod_before = _prod_fingerprint()

    # ==================================================================
    # 组 1：401 身份标记（后端真发）
    # ==================================================================
    print("\n--- 组 1：401 身份标记 ---")

    ui.configure_auth(TOKEN)
    check("已启用认证", ui.auth_enabled() is True)

    r = client.get("/api/stats")
    check("无令牌 → 401", r.status_code == 401, str(r.status_code))
    sid = r.headers.get("x-bionic-service")
    check("401 带 X-Bionic-Service", sid == ui.SERVICE_ID, f"{sid!r} vs {ui.SERVICE_ID!r}")
    check("SERVICE_ID 是稳定标识（非 boot 期随机值）",
          ui.SERVICE_ID == "bionic-agent-admin" and ui.SERVICE_ID != ui.BOOT_ID,
          f"{ui.SERVICE_ID!r}")
    www = r.headers.get("www-authenticate") or ""
    check("401 带 WWW-Authenticate: Bearer", www.lower().startswith("bearer"), www)
    check("401 的 realm 含 bionic-admin", "bionic-admin" in www, www)

    # 令牌不对时同样要带标记：插件看到的是"这是我们的服务，只是要令牌"
    r_bad = client.get("/api/stats", headers={"Authorization": "Bearer nope"})
    check("令牌错 → 也是 401 且带身份标记",
          r_bad.status_code == 401 and r_bad.headers.get("x-bionic-service") == ui.SERVICE_ID)
    check("401 响应体不泄漏令牌",
          TOKEN not in r_bad.text and "nope" not in r_bad.text.replace("Bearer", ""),
          r_bad.text[:80])

    # 200 也必须能认出"这是我"，否则没开认证时插件反倒认不出
    r_ok = client.get("/api/stats", headers={"Authorization": f"Bearer {TOKEN}"})
    check("带令牌 → 200", r_ok.status_code == 200, str(r_ok.status_code))
    check("200 也带 X-Bionic-Service", r_ok.headers.get("x-bionic-service") == ui.SERVICE_ID,
          str(r_ok.headers.get("x-bionic-service")))

    # ==================================================================
    # 组 2：白名单不许扩张
    # ==================================================================
    print("\n--- 组 2：公开路由白名单 ---")

    public = sorted({
        (route.path or "")
        for route in ui.app.routes
        if route.path is not None and ui.is_public_path(route.path)
    })
    check("公开路径恰好是 / 与 /static/{name}", public == ["/", "/static/{name}"], str(public))
    check("没有 /api/health 之类的兼容性公开接口",
          not any(p.startswith("/api/") for p in public), str(public))
    check("is_public_path('/api/stats') 为假", ui.is_public_path("/api/stats") is False)
    check("is_public_path('/api/ping') 为假", ui.is_public_path("/api/ping") is False)

    # ==================================================================
    # 组 3：前端只允许一个请求入口
    # ==================================================================
    print("\n--- 组 3：前端网络原语计数 ---")

    html = _html()
    lines = html.split("\n")

    fetch_lines = [i + 1 for i, ln in enumerate(lines) if re.search(r"\bfetch\s*\(", ln)]
    check("裸 fetch( 恰好 2 处（api 与 sendPing）", len(fetch_lines) == 2, str(fetch_lines))

    # 两处都必须带上 authHeaders()，否则其中一处会漏令牌
    for n in fetch_lines:
        ctx = "\n".join(lines[max(0, n - 4):n + 3])
        check(f"第 {n} 行的 fetch 带了 authHeaders()", "authHeaders(" in ctx,
              ctx.strip().splitlines()[0][:60])

    for pat, label in [
        (r"\bXMLHttpRequest\b", "XMLHttpRequest"),
        (r"\bnew\s+WebSocket\b", "WebSocket"),
        (r"\bnew\s+EventSource\b", "EventSource"),
        (r"\bnavigator\.sendBeacon\b", "sendBeacon"),
        (r"\baxios\b", "axios"),
        (r"window\.open\s*\(", "window.open"),
        (r"location\.href\s*=", "location.href="),
        (r"createObjectURL", "createObjectURL"),
        (r"<a\b[^>]*\bdownload\b", "<a download"),
    ]:
        hits = [i + 1 for i, ln in enumerate(lines) if re.search(pat, ln)]
        check(f"没有绕过统一封装的 {label}", not hits, str(hits))

    # ==================================================================
    # 组 4：令牌不落盘、不 reload 死循环
    # ==================================================================
    print("\n--- 组 4：令牌存储与刷新 ---")

    check("api() 注入 Authorization",
          bool(re.search(r"authHeaders\(Object\.assign\(\{'Content-Type'", html))
          or "h['Authorization'] = 'Bearer ' + _memToken" in html)
    check("令牌变量是内存变量 _memToken", "let _memToken = ''" in html)

    storage_sets = re.findall(
        r"(localStorage|sessionStorage)\s*\.\s*setItem\s*\(\s*['\"`]([^'\"`]*)", html
    )
    bad = [(k, name) for k, name in storage_sets if "token" in name.lower()]
    check("没有任何令牌写进 storage", not bad, str(bad))

    reload_sites = [i + 1 for i, ln in enumerate(lines)
                    if re.search(r"location\.reload\s*\(", ln)]
    check("没有 location.reload 清掉内存令牌", not reload_sites, str(reload_sites))

    check("有 hideTokenUi（解锁后收起输入框）", "function hideTokenUi(" in html)
    check("有 refreshAll（原地重拉，替代 reload）", "async function refreshAll(" in html)
    check("填令牌后走 refreshAll 而不是重新加载",
          "await refreshAll();" in html and "location.reload" not in html.split("async function applyTokenFromInput")[1][:700])

    # 启动流程也用 refreshAll，保证"刷新后"与"解锁后"喂同样的数据
    check("启动 IIFE 复用 refreshAll", "await refreshAll();" in html)

    # ==================================================================
    # 组 5：备份/恢复三阶段都经统一封装
    # ==================================================================
    print("\n--- 组 5：备份与恢复链路 ---")

    for fn, need in [
        ("exportBkPackage", "/api/backup/export"),
        ("previewBkRestore", "/api/backup/preview"),
        ("doBkExecute", "/api/backup/confirm"),
        ("doBkExecute-execute", "/api/backup/execute"),
    ]:
        check(f"{fn} 走 {need}", need in html and re.search(
            r"api\(\s*['\"]" + re.escape(need) + r"['\"]", html) is not None)
    check("恢复三阶段都只用 api()（无裸 fetch）",
          len(fetch_lines) == 2)

    # ==================================================================
    # 组 6：插件侧认身份标记（三个消费方）
    # ==================================================================
    print("\n--- 组 6：面板插件兼容 ---")

    exports = {}
    for rel in ("lib/index.js", "lib/client.js", "verify-install.mjs"):
        p = PACKAGE_CLIENT / rel
        check(f"{rel} 存在", p.is_file(), str(p))
        if p.is_file():
            exports[rel] = p.read_text(encoding="utf-8")

    idx = exports.get("lib/index.js", "")
    check("插件主半认 401", "res.statusCode === 401" in idx)
    check("插件主半用 SERVICE_ID 判断身份", "bionic-agent-admin" in idx)
    check("插件主半返回 needsToken", "needsToken" in idx)
    check("插件主半不看别的服务（不认任何 401）",
          'www.includes("bionic-admin")' in idx)

    cli = exports.get("lib/client.js", "")
    check("插件客户端半认 401", "resp.status === 401" in cli)
    check("客户端半用 SERVICE_ID 判断身份", "bionic-agent-admin" in cli)
    check("客户端半有 auth 态提示", '"auth"' in cli and "panel.authTitle" in cli)
    check("客户端半英文文案也在", "panel.authHint" in cli and "needs an access token" in cli)

    vi = exports.get("verify-install.mjs", "")
    check("自检认 401", "res.status === 401" in vi)
    check("自检用 SERVICE_ID 判断身份", "bionic-agent-admin" in vi)
    check("自检区分'要令牌'与'没探到'", "needsToken" in vi)

    # 三处身份值必须与服务端一致，写错就是"看着改了其实不认"
    for rel, text in exports.items():
        stray = set(re.findall(r"bionic-[a-z-]*admin[a-z-]*", text)) - {"bionic-admin", "bionic-agent-admin"}
        check(f"{rel} 没有拼错的身份值", not stray, str(sorted(stray)))

    # ==================================================================
    # 组 7：开关前后各链路状态
    # ==================================================================
    print("\n--- 组 7：关闭/开启后的链路 ---")

    ui.configure_auth("")  # 关掉
    check("清空令牌后认证关闭", ui.auth_enabled() is False)
    for path in ("/api/stats", "/api/memories?limit=1", "/api/backup/packages"):
        r = client.get(path)
        check(f"关闭时 {path} 不被认证挡（非 401）", r.status_code != 401, str(r.status_code))

    ui.configure_auth(TOKEN)
    for path in ("/api/stats", "/api/memories?limit=1", "/api/backup/packages",
                 "/api/backup/restores", "/docs", "/openapi.json"):
        r = client.get(path)
        check(f"开启后 {path} 无令牌 401", r.status_code == 401, str(r.status_code))

    # POST 也要挡：备份导出/恢复预览是最容易被漏掉的写入口
    for path, payload in [
        ("/api/backup/export", {"backup_type": "source_only"}),
        ("/api/backup/preview", {"package_id": "x", "restore_mode": "merge"}),
        ("/api/backup/confirm", {"restore_id": "x", "confirmation_token": "y"}),
        ("/api/backup/execute", {"restore_id": "x"}),
        ("/api/memories", {"content": "不该落地"}),
    ]:
        r = client.post(path, json=payload)
        check(f"开启后 POST {path} 无令牌 401", r.status_code == 401, str(r.status_code))

    # 带令牌时不应被认证挡（业务是否成功由别的测试负责，这里只看没被 401）
    for path in ("/api/stats", "/api/backup/packages"):
        r = client.get(path, headers={"Authorization": f"Bearer {TOKEN}"})
        check(f"带令牌 {path} 非 401", r.status_code != 401, str(r.status_code))

    # ==================================================================
    # 组 8：数据保护
    # ==================================================================
    print("\n--- 组 8：数据保护 ---")

    prod_after = _prod_fingerprint()
    check("生产库 data/agent.db 指纹未变", prod_before == prod_after,
          f"{prod_before} vs {prod_after}")
    check("未产生生产库 sidecar",
          not (ROOT / "data" / "agent.db-wal").exists()
          and not (ROOT / "data" / "agent.db-shm").exists())
    tmp_db = Path(os.environ["BIONIC_DB_PATH"])
    check("使用独立临时库", tmp_db.is_file() and _TMP in str(tmp_db), str(tmp_db))
    check("临时库不在项目 data/ 下",
          (ROOT / "data") not in tmp_db.parents, str(tmp_db))

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

    if _SAVED_ENV is not None:
        os.environ[_ENV_KEY] = _SAVED_ENV
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
