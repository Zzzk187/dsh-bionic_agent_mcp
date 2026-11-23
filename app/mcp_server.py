"""向后兼容 shim：真正的实现已搬到 app/mcp/（runtime / core_tools / gov_tools）。

为什么留这个文件而不是改路径：
  1. pyproject.toml 的 console-script 是 `bionic-agent = app.mcp_server:_run_cli`；
  2. DSH 的 MCP registry（C:\\Users\\xing\\.dsh\\skill-mcp-manager\\registry.json）
     把启动命令钉成 `python -m app.mcp_server`；
  3. scripts/ 与 tests/ 里大量 `from app import mcp_server as S` 的引用。
这三处任一改动都会在别处静默断掉，所以老路径保持不变，实现下沉一层。
"""
from __future__ import annotations

from app.mcp.server import *          # noqa: F401,F403
from app.mcp.server import __all__    # noqa: F401

if __name__ == "__main__":
    # Default launch = core instance (36 tools). Launch the governance
    # instance with: python -m app.mcp_server_gov
    from app.mcp.server import _run_cli
    _run_cli()
