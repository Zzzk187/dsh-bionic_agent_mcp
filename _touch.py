"""补齐访问统计：`touch_access()` 写好了却从没被调用。

现状（实测）：36 条记忆 access_count 全是 0、last_accessed_at 全 NULL、
memory_feedback 表 0 行。「按访问热度分组」会得到「36 条从没翻过」，
分组等于白分 —— 功能没坏，是数据源空的。

两处接法：
1) recall_memories 真正返回结果后记一笔（走 governance_service.touch_access）
2) 面板 `/api/memories/{id}`（打开详情）算一次访问；列表不算

为什么列表不算：一屏 60 条，翻一页就说"访问 60 条"，那"常翻的"就等于
"最近列出来的"，这个数字只会反映 UI 布局，不反映注意力。

为什么这不动"曝光不计分"的规矩：touch_access 只加 access_count（频次分量
的原料），不改 confidence、不写反馈信号、不 rescore。之前定的是"曝光不能
让记忆升格"，这里记的是访问次数，两码事。
"""
from pathlib import Path

# ---------------------------------------------------------------- 1) 召回时记账
p = Path(r"D:/Cache/Cache_Project/bionic-agent-mcp/app/memory_lifecycle.py")
s = p.read_text(encoding="utf-8")

old = """        result = scored[:top_k]
        if include_related:
            ids = [r["id"] for r in result]
            related = _related_subgraph(c, ids, max_depth=1)
            for r in result:
                r["_related"] = related.get(r["id"], [])
        return result"""
new = """        result = scored[:top_k]
        if include_related:
            ids = [r["id"] for r in result]
            related = _related_subgraph(c, ids, max_depth=1)
            for r in result:
                r["_related"] = related.get(r["id"], [])
        # 记一次真实访问。只加 access_count / last_accessed_at（频次分量的
        # 原料），不改 confidence、不写反馈信号、不 rescore —— "曝光不能让
        # 记忆升格"这条规矩不受影响。
        #
        # 放在 return 之前、拿到了 result 之后：这样记的是"真正返回给调用方
        # 的那些"，不是"参与打分的全部候选"。
        if result:
            try:
                from app import governance_service as _gov
                _gov.touch_access([r["id"] for r in result])
            except Exception:  # noqa: BLE001 —— 记账失败不该让召回本身失败
                pass
        return result"""
assert old in s, "recall_memories 尾部锚点没找到"
s = s.replace(old, new, 1)
p.write_text(s, encoding="utf-8")
print("memory_lifecycle.py: 召回后记一次访问")

# ---------------------------------------------------------------- 2) 详情算访问
q = Path(r"D:/Cache/Cache_Project/bionic-agent-mcp/app/admin_ui.py")
h = q.read_text(encoding="utf-8")

old2 = '''@app.get("/api/memories/{memory_id}")
def api_get_memory(memory_id: int):'''
if old2 not in h:
    # 兜底：看看实际长什么样
    import re
    m = re.search(r'@app\.get\("/api/memories/\{memory_id\}"\)\ndef (\w+)\(memory_id: int\):', h)
    assert m, "没找到详情路由"
    old2 = m.group(0)

idx = h.index(old2)
end = h.index("\n\n\n", idx)
new2 = '''@app.get("/api/memories/{memory_id}")
def api_get_memory(memory_id: int):
    """打开详情算一次真实访问；列表不算。
    列表一屏 60 条，若列表也记，"常翻的"就等于"最近列出来的"。"""
    data = _run(core.get_memory, memory_id)
    try:
        gov.touch_access([int(memory_id)])
    except Exception:  # noqa: BLE001 —— 记账失败不该让详情打不开
        pass
    return data'''
h = h[:idx] + new2 + h[end:]
q.write_text(h, encoding="utf-8")
print("admin_ui.py: 打开详情记一次访问")
