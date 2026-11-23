"""MCP 服务器实现包（OPT-03 3.1 从单文件 app/mcp_server.py 拆出）。

runtime.py      两个 FastMCP 实例 + 85 个工具共用的 helper
core_tools.py   36 个日常工具
gov_tools.py    49 个治理工具
server.py       装配层（import 两套工具触发注册）
"""
