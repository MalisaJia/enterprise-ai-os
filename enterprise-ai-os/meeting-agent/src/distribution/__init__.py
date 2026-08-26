"""分发：把会议成果送到真正会被看到的地方。

    distribution/dispatcher.py   五个分发目标的编排与幂等重试

这是整个组件价值的落点。前面三步（转录/摘要/抽取）做得再好，
如果结果只是躺在本服务的数据库里等人来查，痛点一点没解决——
用户原来不看纪要，现在也不会来看这个页面。

分发的特殊性在于**有外部副作用且不可撤回**：
任务已经创建、消息已经发到群里，撤回的社交成本远高于多点一次确认。
因此本模块的所有设计都围绕两点：默认需人工确认、重试必须幂等。
"""

from __future__ import annotations

from .dispatcher import DispatchOutcome, DispatchResult, DispatchTarget, Dispatcher

__all__ = [
    "DispatchTarget",
    "DispatchOutcome",
    "DispatchResult",
    "Dispatcher",
]
