"""组织记忆：管理员或审批流写入、租户全员可见、支持强制生效。

三层里唯一 ``write_mode=manual`` 的一层——不接受自动抽取。
组织记忆写错会影响该租户下所有 Agent 的所有会话，且极难被发现
（没人会怀疑"Agent 为什么这么做"是因为一条错误的组织记忆），
所以必须人工把关或走 workflow-engine 的审批流。

mandatory 机制
--------------
本层的 ``recall_weight`` 最低（0.6），但 ``mandatory=True`` 的记忆
会在召回结果中**强制保留并标注不可覆盖**。

这解决一个真实矛盾：个人偏好本应优先（"我习惯用 pnpm"），
但组织硬约束不能被个人偏好推翻（"生产发布必须双人复核" 不能因为
某人说"我一个人够了"而失效）。若不做这个机制，权重排序会让个人记忆
挤掉组织约束，形成一个很隐蔽的合规漏洞——功能看起来一切正常，
只是在关键时刻少了一道约束。
"""

from __future__ import annotations

import logging
from typing import Any, Sequence

from . import Memory, MemoryScope, ScopePolicy
from .personal import WriteResult
from ..storage.vector import SearchFilter, SearchHit, VectorMemoryStore

logger = logging.getLogger(__name__)

__all__ = ["OrgMemoryManager"]


class OrgMemoryManager:
    """组织记忆的读写编排。

    注意本类**没有** ``write`` 方法（区别于个人层）。
    没有自动写入入口是刻意的设计而非遗漏：只要留了这个口子，
    早晚会有人为了省事把抽取结果直接怼进组织层。
    """

    scope = MemoryScope.ORG

    def __init__(
        self,
        store: VectorMemoryStore,
        merger: Any,                     # consolidation.merger.MemoryMerger
        model_client: Any,
        policy: ScopePolicy,
        workflow_client: Any | None = None,   # workflow-engine 客户端，走审批流时用
        graph_store: Any | None = None,
    ) -> None:
        self.store = store
        self.merger = merger
        self.model_client = model_client
        self.policy = policy
        self.workflow_client = workflow_client
        self.graph_store = graph_store

    # ---- 写入 -----------------------------------------------------------
    async def add_by_admin(
        self,
        tenant_id: str,
        content: str,
        category: Any,
        actor: str,
        mandatory: bool = False,
        skip_consolidation: bool = False,
    ) -> WriteResult:
        """管理员直接写入。

        ``mandatory=True`` 的写入应当在 API 层要求更高权限
        （platform-admin 而非普通 memory-admin）：把一条记忆标为不可覆盖，
        影响面等同于给全租户加了一条硬性规则。

        ``skip_consolidation`` 在这里默认 False 但常常需要设为 True：
        组织规定往往互相补充而非互斥（"发布需复核" 与 "发布需双人" 都成立），
        整合判定容易把它们误判为 UPDATE 而丢掉一条。管理员写入时
        由人来判断是否重复，比 LLM 判断更靠得住。

        TODO: 实现。
        """

        raise NotImplementedError

    async def submit_for_approval(
        self, tenant_id: str, content: str, category: Any, submitter: str, mandatory: bool = False
    ) -> str:
        """提交组织记忆变更走审批流，返回 workflow run_id。

        对接 workflow-engine：起一个 ``org-memory-change`` 流程，
        审批通过后回调本服务的 ``/internal/org-memory/apply``。

        为什么走 workflow-engine 而不在本服务内建审批：审批的通知渠道、
        多人策略（all/any/quorum）、超时处理、审计，workflow-engine 已经做全了。
        在这里重造一套简化版，最后一定会缺少某个必要能力（比如超时升级），
        而那时数据已经在两套系统里了。

        TODO: 实现。
        """

        raise NotImplementedError

    async def apply_approved(self, tenant_id: str, run_id: str, payload: dict[str, Any]) -> WriteResult:
        """审批通过后落库。由 workflow-engine 回调触发。

        必须幂等：workflow-engine 的回调是至少一次投递，
        重试会导致同一条组织记忆入库两次。用 run_id 做幂等键。

        TODO: 实现。
        """

        raise NotImplementedError

    async def import_batch(self, tenant_id: str, memories: Sequence[dict[str, Any]], actor: str) -> dict[str, int]:
        """批量导入（从现有的规章制度文档初始化组织记忆）。

        典型用法：把公司的《研发规范》拆成若干条原子规则一次性导入。

        注意与 rag-pipeline 的分工：整篇规范文档应当进 rag-pipeline
        供检索原文；只有那些"Agent 每次都必须遵守"的硬约束才提炼进组织记忆。
        把整篇文档拆碎全塞进记忆，会让每次 /context 都被规章挤满预算，
        真正相关的个人上下文反而进不来。

        TODO: 实现，支持 dry_run 预览。
        """

        raise NotImplementedError

    # ---- 读取 -----------------------------------------------------------
    async def search(
        self,
        tenant_id: str,
        query: str,
        top_k: int = 10,
        categories: Sequence[Any] | None = None,
    ) -> list[SearchHit]:
        """语义检索组织记忆。

        TODO: 实现。
        """

        raise NotImplementedError

    async def list_mandatory(self, tenant_id: str) -> list[Memory]:
        """列出全部 mandatory 记忆。

        这个方法在 ``/context`` 的组装流程里被**无条件调用**，
        不经过向量检索——mandatory 约束必须每次都注入，
        不能因为"与当前问题语义不相关"而被漏掉。
        安全类约束恰恰常常与当前问题字面无关
        （"不得在日志打印用户手机号" 与 "帮我加个日志" 语义相似度很低），
        但正是这种时候最需要它。

        因此数量必须可控：mandatory 记忆总量应有硬上限
        （建议不超过 20 条），否则光它们就能吃掉整个 token 预算。

        TODO: 实现，并加缓存——这个查询每次 /context 都会跑，
              但内容极少变动，缓存命中率会非常高。
        """

        raise NotImplementedError

    async def list_all(
        self, tenant_id: str, limit: int = 50, cursor: str | None = None
    ) -> tuple[list[Memory], str | None]:
        """列出组织记忆（供管理后台）。

        TODO: 实现。
        """

        raise NotImplementedError

    async def build_filter(self, tenant_id: str, top_k: int) -> SearchFilter:
        """构造只含组织层的检索过滤器。

        TODO: 实现。
        """

        raise NotImplementedError

    # ---- 修改与删除 -----------------------------------------------------
    async def set_mandatory(self, tenant_id: str, memory_id: str, mandatory: bool, actor: str) -> bool:
        """切换 mandatory 标记。

        必须写审计事件并记录 actor。把一条记忆改成 mandatory 或取消，
        都是影响全租户行为的操作，事后要能回答"这是谁改的、什么时候"。

        TODO: 实现。
        """

        raise NotImplementedError

    async def forget(self, tenant_id: str, memory_id: str, actor: str) -> bool:
        """删除一条组织记忆（需管理员权限）。

        mandatory 的记忆建议要求二次确认或同样走审批流——
        删除一条合规约束的影响与添加它是对称的。

        TODO: 实现。
        """

        raise NotImplementedError
