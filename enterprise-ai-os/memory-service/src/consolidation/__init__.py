"""记忆整合：把抽取产出的候选与既有记忆合并成一致的记忆库。

    consolidation/merger.py   整合判定（ADD/UPDATE/DELETE/NOOP）、去重、摘要压缩

为什么整合是本组件的核心难点
----------------------------
不做整合的记忆库会退化成一堆互相矛盾的碎片："我用 npm"和"我用 pnpm"
同时存在，Agent 每次随机挑一条，行为不可预测。用户会觉得它"时好时坏"，
这比完全不记忆更糟——不记忆至少是可预期的。

而整合本身是个双向风险的判断：过于激进会误删有效记忆
（用户发现"它忘了我说过的话"），过于保守会积累矛盾。
所以 ``shadow_mode`` 默认开启，先在真实数据上评估准确率再放开执行。
"""

from __future__ import annotations

from .merger import ConsolidationDecision, MemoryMerger

__all__ = [
    "MemoryMerger",
    "ConsolidationDecision",
]
