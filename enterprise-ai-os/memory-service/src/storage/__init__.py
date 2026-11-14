"""存储层。

    storage/vector.py   pgvector 存储：记忆的 CRUD 与向量检索（必选）
    storage/graph.py    图存储：实体关系与多跳查询（可选，默认关闭）

两者的关系是"主 + 辅"而非平级：向量存储是唯一的事实来源，
图存储只是 relation 类记忆的派生视图。图挂了不影响记忆的读写，
只影响多跳查询——这也是 ``/readyz`` 不把图存储列入就绪条件的原因。
"""

from __future__ import annotations

from .graph import GraphMemoryStore, NullGraphStore
from .vector import SearchHit, VectorMemoryStore

__all__ = [
    "VectorMemoryStore",
    "SearchHit",
    "GraphMemoryStore",
    "NullGraphStore",
]
