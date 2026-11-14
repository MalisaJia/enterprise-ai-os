"""团队记忆：抽取后待确认、空间成员可见、召回权重次之。

与个人层最大的差别是 ``write_mode=confirm``：抽取结果不直接生效，
先进 pending 队列等人工确认。

为什么团队层必须加这道门槛
--------------------------
团队约定影响多人。LLM 从一次对话里抽出"我们的 PR 需要两人 approve"，
可能只是某人的个人主张而非团队共识——直接生效会让一个人的意见变成
所有人的约束，而且没人知道这条约束是怎么来的。

代价是确认队列需要有人维护。因此设了 ``pending_ttl_days``：
超时未处理即丢弃。不设过期会积累大量无人处理的 pending，
确认列表变成噪音，最终变成没人看的收件箱——那道门槛也就形同虚设了。
"""

from __future__ import annotations

import logging
from typing import Any, Sequence

from . import Memory, MemoryCandidate, MemoryScope, ScopePolicy
from .personal import WriteResult
from ..storage.vector import SearchFilter, SearchHit, VectorMemoryStore

logger = logging.getLogger(__name__)

__all__ = ["TeamMemoryManager"]


class TeamMemoryManager:
    """团队记忆的读写编排。

    与 :class:`~.personal.PersonalMemoryManager` 的接口刻意保持相似形状，
    但没有抽公共基类。原因：三层的写入路径差异（auto / confirm / manual）
    正是本组件的核心逻辑，硬抽基类会变成一堆 ``if self.policy.write_mode``
    的分支，反而比三个独立类更难读。共用的只有领域模型与 store。
    """

    scope = MemoryScope.TEAM

    def __init__(
        self,
        store: VectorMemoryStore,
        merger: Any,                     # consolidation.merger.MemoryMerger
        model_client: Any,
        policy: ScopePolicy,
        notifier: Any | None = None,     # 确认提醒的通知通道（octo-adapters）
        graph_store: Any | None = None,
    ) -> None:
        self.store = store
        self.merger = merger
        self.model_client = model_client
        self.policy = policy
        self.notifier = notifier
        self.graph_store = graph_store

    # ---- 写入（两阶段）--------------------------------------------------
    async def propose(
        self, tenant_id: str, space_id: str, candidate: MemoryCandidate, proposer: str
    ) -> WriteResult:
        """第一阶段：提议。抽取结果进入 pending 队列。

        这里就要做整合判定，而不是等确认之后再做。理由：如果新候选与既有
        团队记忆重复，应当在提议阶段直接判 NOOP 丢弃，不去打扰成员确认。
        把重复项也推给人确认，是让确认队列变成噪音的最快方式。

        判定为 UPDATE 时，pending 项要带上"将取代哪条既有记忆"的信息，
        确认界面必须展示新旧对比——只给用户看新内容，他无法判断该不该批。

        TODO: 实现。
        """

        raise NotImplementedError

    async def confirm(self, tenant_id: str, space_id: str, memory_id: str, actor: str) -> WriteResult:
        """第二阶段：确认。pending → active，并执行提议阶段的判定。

        必须做幂等：确认按钮被连点、或消息通知被重复投递，
        都会导致重复确认。第二次应返回"已确认"而不是再执行一次
        UPDATE（会把已被取代的旧记忆再取代一遍，产生错误的审计链）。

        TODO: 实现，用 ``(tenant_id, memory_id)`` 加行锁 + 状态检查。
        """

        raise NotImplementedError

    async def reject(self, tenant_id: str, space_id: str, memory_id: str, actor: str, reason: str = "") -> bool:
        """拒绝一条待确认记忆。

        置 REJECTED 而非删除。保留被拒记录才能统计抽取准确率——
        拒绝率是评估抽取质量最直接的信号，删掉就没有这个反馈了。

        TODO: 实现。
        """

        raise NotImplementedError

    async def list_pending(self, tenant_id: str, space_id: str, limit: int = 50) -> list[Memory]:
        """列出待确认队列。

        TODO: 实现；顺带把超过 ``pending_ttl_days`` 的过滤掉。
        """

        raise NotImplementedError

    async def expire_pending(self, tenant_id: str | None = None) -> int:
        """清理超时未确认的 pending，返回条数。由周期任务调用。

        TODO: 实现。
        """

        raise NotImplementedError

    async def notify_pending(self, tenant_id: str, space_id: str) -> int:
        """向空间成员推送待确认提醒，返回通知数。

        提醒必须**聚合**而非逐条推送：一次会话可能产出多条团队候选，
        逐条发通知会让成员被刷屏，结果是关掉通知，门槛再次失效。
        正确做法是攒成一条摘要（"有 3 条团队约定待确认"）。

        TODO: 实现，经 octo-adapters 发到飞书/Slack。
        """

        raise NotImplementedError

    # ---- 管理员直写 -----------------------------------------------------
    async def add_directly(
        self, tenant_id: str, space_id: str, content: str, category: Any, actor: str
    ) -> WriteResult:
        """空间管理员直接写入，跳过确认阶段。

        提供这个口子是必要的：团队 owner 明确知道要立一条约定时，
        走"提议→自己确认"的两步很荒谬。但要在审计里记清 actor，
        并且**只允许空间管理员角色**调用。

        TODO: 实现；权限校验在 API 层做，这里用断言兜底。
        """

        raise NotImplementedError

    # ---- 读取 -----------------------------------------------------------
    async def search(
        self,
        tenant_id: str,
        space_id: str,
        query: str,
        top_k: int = 10,
        categories: Sequence[Any] | None = None,
    ) -> list[SearchHit]:
        """语义检索团队记忆。

        只返回 ACTIVE 的。pending 的记忆绝不能参与召回——
        它还没被团队认可，让 Agent 提前按它行事就绕过了确认机制。

        TODO: 实现。
        """

        raise NotImplementedError

    async def list_all(
        self, tenant_id: str, space_id: str, limit: int = 50, cursor: str | None = None
    ) -> tuple[list[Memory], str | None]:
        """列出团队记忆。

        TODO: 实现。
        """

        raise NotImplementedError

    async def build_filter(self, tenant_id: str, space_id: str, top_k: int) -> SearchFilter:
        """构造只含团队层的检索过滤器。

        TODO: 实现。
        """

        raise NotImplementedError

    # ---- 删除 -----------------------------------------------------------
    async def forget(self, tenant_id: str, space_id: str, memory_id: str, actor: str) -> bool:
        """删除一条团队记忆（需管理员权限，权限在 API 层校验）。

        TODO: 实现，软删除 + 审计 + 清理图关系。
        """

        raise NotImplementedError

    async def on_space_deleted(self, tenant_id: str, space_id: str) -> int:
        """空间被删除时清理其记忆，返回条数。

        订阅 OCTO 的空间删除事件触发。不清理会留下永远无法访问也无法删除的
        孤儿记忆——它们仍占着 ``max_per_subject`` 配额，且在跨层检索时
        可能被误召回（如果哪天权限判断出 bug）。

        TODO: 实现。
        """

        raise NotImplementedError
