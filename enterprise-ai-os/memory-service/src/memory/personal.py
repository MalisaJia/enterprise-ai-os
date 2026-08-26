"""个人记忆：自动抽取、仅本人可见、召回权重最高。

三层里唯一 ``write_mode=auto`` 的一层。理由是代价对称：写错只影响一人，
且用户自己就能看到并改掉。团队与组织层不具备这个性质，
所以必须加确认或审批门槛。

召回权重设为 1.0（最高），因为个人记忆最贴合当前用户的上下文。
但注意组织层的 ``mandatory`` 记忆会绕过权重强制保留——
个人偏好优先，个人偏好却不能推翻组织硬约束。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Sequence

from . import (
    ConsolidationOp,
    Memory,
    MemoryCandidate,
    MemoryScope,
    ScopePolicy,
)
from ..storage.vector import SearchFilter, SearchHit, VectorMemoryStore

logger = logging.getLogger(__name__)

__all__ = ["WriteResult", "PersonalMemoryManager"]


@dataclass
class WriteResult:
    """一次写入的结果。

    为什么要把判定操作回传给调用方：整合可能把新记忆判成 NOOP 或
    UPDATE 旧记忆，此时并没有产生新 id。调用方若假定"写入必得新 id"
    就会拿到 None 而困惑。显式回传 op 让语义清楚。
    """

    op: ConsolidationOp
    memory_id: str | None = None
    # UPDATE / DELETE 时受影响的既有记忆
    affected_ids: Sequence[str] = ()
    reason: str = ""
    # 影子模式下为 True：判定已产生但未真正执行
    shadowed: bool = False


class PersonalMemoryManager:
    """个人记忆的读写编排。

    职责边界：本类负责"作用域策略 + 写入编排"，
    不负责向量计算（交给 model client）、不负责冲突判定（交给 merger）、
    不负责 SQL（交给 store）。把这三件事塞进 Manager 会让它无法单测。
    """

    scope = MemoryScope.PERSONAL

    def __init__(
        self,
        store: VectorMemoryStore,
        merger: Any,                     # consolidation.merger.MemoryMerger
        model_client: Any,               # model-gateway 客户端
        policy: ScopePolicy,
        graph_store: Any | None = None,
    ) -> None:
        self.store = store
        self.merger = merger
        self.model_client = model_client
        self.policy = policy
        self.graph_store = graph_store

    # ---- 写入 -----------------------------------------------------------
    async def write(self, tenant_id: str, user_id: str, candidate: MemoryCandidate) -> WriteResult:
        """写入一条候选记忆，经整合判定后落库。

        完整流程::

            1. 置信度过滤（低于 min_confidence 直接丢弃）
            2. 长度校验（超 max_content_chars 说明不够原子化，拒绝）
            3. 计算 embedding
            4. 字面相似度预筛：与既有记忆近乎相同则直接 NOOP，不调 LLM
            5. 召回语义相近的候选（find_similar，纯相似度排序）
            6. 交 merger 判定 ADD / UPDATE / DELETE / NOOP
            7. 影子模式下只记录判定，不执行
            8. 执行判定并发 aios.memory.* 事件

        第 4 步值得强调：重复表述在真实对话里非常常见
        （用户反复说"记住我用 pnpm"），预筛能省掉相当比例的 LLM 调用。

        TODO: 实现。
        """

        raise NotImplementedError

    async def write_many(
        self, tenant_id: str, user_id: str, candidates: Sequence[MemoryCandidate]
    ) -> list[WriteResult]:
        """批量写入（一次抽取的产出）。

        必须**串行**处理而非并发，这一点容易搞错：同一批候选里可能有两条
        彼此矛盾的记忆（LLM 抽取时上下文不同导致），并发判定时它们各自
        看不到对方，最终两条都会入库，矛盾就被固化下来。串行处理让后一条
        能看到前一条已经落库的结果。

        批量的性能优化应放在 embedding 上（一次批量算完），而不是判定上。

        TODO: 实现。
        """

        raise NotImplementedError

    async def add_explicit(
        self,
        tenant_id: str,
        user_id: str,
        content: str,
        category: Any,
        skip_consolidation: bool = False,
    ) -> WriteResult:
        """用户显式添加（"记住：我用 pnpm"）。

        ``skip_consolidation`` 默认 False —— 即使是用户显式添加也要走整合。
        用户说"记住我现在用 pnpm"时，意图往往是**更新**旧偏好而非并列添加，
        跳过整合会让库里同时留着 npm 和 pnpm 两条偏好，Agent 无从选择。

        TODO: 实现。
        """

        raise NotImplementedError

    # ---- 读取 -----------------------------------------------------------
    async def search(
        self,
        tenant_id: str,
        user_id: str,
        query: str,
        top_k: int = 10,
        categories: Sequence[Any] | None = None,
    ) -> list[SearchHit]:
        """语义检索本人记忆。

        检索后要异步记录命中（record_hits）以回升强度，
        但不能阻塞返回——检索在 Agent 的关键路径上。

        TODO: 实现。
        """

        raise NotImplementedError

    async def list_all(
        self, tenant_id: str, user_id: str, limit: int = 50, cursor: str | None = None
    ) -> tuple[list[Memory], str | None]:
        """列出本人记忆（供"我的记忆"管理页）。

        这个接口必须做好：让用户能看到、能删掉 Agent 记住的东西，
        是记忆功能可信的前提。用户无法审视的记忆库会让人不安。

        TODO: 实现。
        """

        raise NotImplementedError

    async def build_filter(self, tenant_id: str, user_id: str, top_k: int) -> SearchFilter:
        """构造只含个人层的检索过滤器。

        TODO: 实现。
        """

        raise NotImplementedError

    # ---- 删除 -----------------------------------------------------------
    async def forget(self, tenant_id: str, user_id: str, memory_id: str) -> bool:
        """用户显式遗忘某条记忆。

        走软删除（置 FORGOTTEN），保留可追溯性，也让"我不小心让它忘了"
        可以恢复。物理清理由 ``purge_after_days`` 的后台任务做。

        同时要清理图里由这条记忆派生的关系，否则多跳查询仍会用到它。

        TODO: 实现。
        """

        raise NotImplementedError

    async def forget_by_query(self, tenant_id: str, user_id: str, query: str, dry_run: bool = True) -> list[Memory]:
        """按语义遗忘（"忘掉我关于包管理器的偏好"）。

        ``dry_run`` 默认 True 且**必须默认 True**：按语义批量删除是危险操作，
        一个宽泛的 query 可能命中大半个记忆库。正确交互是先返回命中列表
        让用户确认，再带 ``dry_run=False`` 执行。

        TODO: 实现。
        """

        raise NotImplementedError

    async def export_all(self, tenant_id: str, user_id: str) -> list[Memory]:
        """导出全部个人记忆（GDPR / 个保法的数据主体访问权）。

        TODO: 实现。注意要包含软删除的记录——数据主体访问权覆盖
              "你们还持有的关于我的数据"，软删除的数据仍在持有中。
        """

        raise NotImplementedError

    async def delete_all(self, tenant_id: str, user_id: str) -> int:
        """物理删除全部个人记忆（数据主体删除权），返回条数。

        这里必须是 hard delete。软删除不满足合规要求。
        同时要删图里的相关节点与关系。

        TODO: 实现，并要求调用方带二次确认标记——
              这个接口误调一次就无法挽回。
        """

        raise NotImplementedError
