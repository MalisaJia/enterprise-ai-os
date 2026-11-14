"""信息抽取：决策与 Action Item。

    extraction/decisions.py   决策抽取：结论 + 依据 + 决策人 + 影响范围
    extraction/actions.py     Action Item 抽取：任务 + 责任人 + 截止时间

为什么拆成两个模块而不是一个
----------------------------
两者的判定标准与取舍方向完全相反，混在一个 prompt 里两边都会变差：

| | 决策 | 待办 |
|---|---|---|
| 核心难点 | 区分"已定"与"只是讨论过" | 责任人归属、隐式截止时间 |
| 取舍 | 宁可少抽（阈值 0.8） | 宁可多抽（阈值 0.6） |
| 抽错的代价 | 记录了从未达成的共识，误导后续所有讨论 | 任务派错人，一键删掉即可 |

取舍方向相反是拆分的决定性理由：放在一个模块里没法分别配置阈值，
只能取一个折中值，结果是决策抽得太松、待办抽得太紧，两头不讨好。
"""

from __future__ import annotations

from .actions import ActionExtractor, ActionItem, ActionPriority
from .decisions import Decision, DecisionExtractor, DecisionCategory

__all__ = [
    "Decision",
    "DecisionCategory",
    "DecisionExtractor",
    "ActionItem",
    "ActionPriority",
    "ActionExtractor",
]
