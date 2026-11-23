"""等嵌入模型就绪再评测。

为什么必须有这一步：`_semantic_scores` 在 `model_state()=='pending'` 时
直接返回空 dict（为了不阻塞 MCP 客户端超时），于是冷进程里语义分量恒为
0 —— 之前那轮「sem 0.38→0.0 全区间 @1 恒 12/16」的权重扫描，其实是在扫
一个根本不参与计算的变量。评测必须先等模型 ready，否则测的不是线上行为。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def wait_ready(timeout: float = 180.0) -> bool:
    from app import embeddings
    embeddings.ensure_warmup()
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        st = embeddings.model_state()
        if st.get("status") != last:
            last = st.get("status")
        if st.get("status") == "ready":
            return True
        if st.get("status") == "failed":
            print(f"[warn] 嵌入模型加载失败：{st.get('error')}", file=sys.stderr)
            return False
        time.sleep(0.2)
    return False


if __name__ == "__main__":
    t0 = time.time()
    ok = wait_ready()
    from app import embeddings
    print(f"ready={ok} 耗时={time.time() - t0:.1f}s state={embeddings.model_state()}")
