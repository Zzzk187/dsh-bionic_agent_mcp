"""MCP 工具的数据访问层（store）。

OPT-03 3.2 起，工具函数里的 SQL 逐步下沉到这里。约定见 `memory_store.py`
的模块 docstring：**store 接收调用方传入的 `conn`，不自己开事务**。
"""
