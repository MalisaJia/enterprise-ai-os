"""会议摘要：分段摘要 → 归并 → 议题切分。

    summarization/summarizer.py   map-reduce 摘要、议题切分、分歧点识别

为什么不用单次长上下文
----------------------
两小时会议约 3 万字。即使模型支持 128k 上下文，单次塞进去也会让中段
信息被严重稀释（"lost in the middle"），表现为开头结尾抓得准、
中间的议题被整段漏掉。而会议的关键决策恰恰常出现在中段——
开头在对齐背景，结尾在收尾确认。
"""

from __future__ import annotations

from .summarizer import (
    Disagreement,
    MeetingSummary,
    Summarizer,
    Topic,
)

__all__ = [
    "Summarizer",
    "MeetingSummary",
    "Topic",
    "Disagreement",
]
