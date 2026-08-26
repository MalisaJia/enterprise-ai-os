"""三层记忆的领域模型与作用域策略。

本包的分层::

    memory/__init__.py   领域模型（Memory / MemoryScope / MemoryCategory）与作用域策略
    memory/personal.py   个人记忆：自动写入，仅本人可见
    memory/team.py       团队记忆：抽取后待确认，空间成员可见
    memory/org.py        组织记忆：管理员或审批流写入，租户全员可见

为什么本模块只放领域模型，不 re-export 三个 Manager
-----------------------------------------------------
``storage.vector`` 需要 ``Memory`` 来做行↔对象映射，而三个 Manager 又依赖
``storage.vector``。若 ``memory/__init__.py`` 直接 import 三个 Manager，
就形成 ``storage.vector → memory → memory.personal → storage.vector`` 的循环。

因此约定：领域模型放在本文件，Manager 一律从子模块显式导入::

    from .memory.personal import PersonalMemoryManager
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

__all__ = [
    "MemoryScope",
    "MemoryCategory",
    "WriteMode",
    "MemoryStatus",
    "ConsolidationOp",
    "Memory",
    "MemoryCandidate",
    "ScopePolicy",
    "ScopeRegistry",
]


class MemoryScope(str, Enum):
    """记忆的作用域层级。

    三层的差别不只是"可见范围"，还包括写入门槛与召回权重，
    完整定义见 :class:`ScopePolicy`。
    """

    PERSONAL = "personal"
    TEAM = "team"
    ORG = "org"


class MemoryCategory(str, Enum):
    """记忆类别。

    刻意不设 ``task`` 类：待办属于 octo-matter 的职责。
    在记忆里存一份待办，必然与 octo-matter 不同步，
    用户会看到自相矛盾的状态（记忆说没做完，任务列表说已关闭）。
    """

    PREFERENCE = "preference"   # "我习惯用 pnpm"
    FACT = "fact"               # "我负责支付模块"
    RULE = "rule"               # "我们的 PR 需要两人 approve"
    RELATION = "relation"       # "张三是我的直接上级"
    EVENT = "event"             # "下周三产品评审"——带时效，到期自动失效


class WriteMode(str, Enum):
    """写入门槛。

    门槛随影响面递增：个人记忆写错只影响一人且用户自己能改；
    组织记忆写错影响所有 Agent 且极难被发现。
    """

    AUTO = "auto"           # 抽取后直接生效
    CONFIRM = "confirm"     # 进入 pending，需人工确认
    MANUAL = "manual"       # 不接受自动抽取，仅管理员或审批流写入


class MemoryStatus(str, Enum):
    """记忆的生命周期状态。"""

    ACTIVE = "active"
    PENDING = "pending"          # 等待确认（team 层的抽取结果）
    SUPERSEDED = "superseded"    # 被新记忆取代，保留可追溯性
    FORGOTTEN = "forgotten"      # 衰减到阈值以下或用户显式遗忘
    EXPIRED = "expired"          # event 类过了时间点
    REJECTED = "rejected"        # 确认时被拒绝


class ConsolidationOp(str, Enum):
    """整合判定的四种操作，语义与 mem0 一致。"""

    ADD = "add"
    UPDATE = "update"
    DELETE = "delete"
    NOOP = "noop"


@dataclass
class Memory:
    """一条记忆。

    对应 ``memory.memories`` 表的一行。

    关于 ``content`` 的硬约束：**必须是原子化的单条陈述**。
    "我用 pnpm、在支付组、直属上级是张三" 这样的三合一记忆，
    将来其中一件过时了就无法单独更新——只能整条删掉，
    连带丢掉仍然有效的两件。抽取环节的 ``max_content_chars``
    就是为了拦住这种情况。
    """

    id: str
    tenant_id: str
    scope: MemoryScope
    # 作用域主体：personal → user_id，team → space_id，org → tenant_id。
    # 单独存一列而不是三列可空，是为了让 (tenant_id, scope, scope_id) 成为
    # 统一的检索前缀索引。
    scope_id: str
    category: MemoryCategory
    content: str

    embedding: list[float] | None = None
    status: MemoryStatus = MemoryStatus.ACTIVE

    # 抽取置信度（0~1）。低于 extraction.min_confidence 的不入库。
    confidence: float = 1.0
    # 记忆强度，参与检索打分。随时间衰减，命中后回升。
    strength: float = 1.0

    # 硬性约束标记，只对 org 层有意义。
    # 标记为 mandatory 的记忆在召回时强制保留，且不会被个人偏好覆盖——
    # 否则"生产发布必须双人复核"会被"我一个人够了"按权重挤掉。
    mandatory: bool = False

    # 溯源。用户问"你怎么知道我用 pnpm"时要能答出来。
    source_type: str = "extraction"     # extraction | manual | import | api
    source_session_id: str | None = None
    source_message_ids: list[str] = field(default_factory=list)

    # 取代关系。软删除时不物理删行，指向新记忆，保留审计链。
    superseded_by: str | None = None

    # event 类记忆的失效时间点
    expires_at: datetime | None = None

    created_at: datetime | None = None
    updated_at: datetime | None = None
    last_hit_at: datetime | None = None
    hit_count: int = 0

    metadata: dict[str, Any] = field(default_factory=dict)

    def is_recallable(self) -> bool:
        """是否可参与召回。

        TODO: 实现——ACTIVE 且未过期即可召回。
        """

        raise NotImplementedError


@dataclass
class MemoryCandidate:
    """抽取产出的候选记忆，尚未落库。

    与 :class:`Memory` 分开是有意的：候选还没有 id、没有 embedding、
    可能被整合判定为 NOOP 而根本不入库。混用同一个类会让"这个对象
    到底入库了没有"变得难以判断。
    """

    scope: MemoryScope
    scope_id: str
    category: MemoryCategory
    content: str
    confidence: float
    reason: str = ""                     # LLM 给出的抽取理由，便于人工复核
    source_message_ids: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ScopePolicy:
    """某一层记忆的策略。从 config 的 ``scopes.<scope>`` 反序列化。"""

    scope: MemoryScope
    enabled: bool = True
    # 从鉴权上下文里取哪个字段作为 scope_id
    scope_key: str = "user_id"
    write_mode: WriteMode = WriteMode.AUTO
    visibility: str = "owner"            # owner | space_members | tenant_members
    recall_weight: float = 1.0
    max_per_subject: int = 2000
    pending_ttl_days: int | None = None
    mandatory_override: bool = False

    @classmethod
    def from_dict(cls, scope: MemoryScope, data: dict[str, Any]) -> ScopePolicy:
        """从配置字典构造。

        TODO: 实现，并对非法组合报错。特别是 ``write_mode=auto`` 配
              ``visibility=tenant_members`` —— 这等于让 LLM 抽取的内容
              自动对全租户可见，是明显的配置错误，必须在启动时拒绝
              而不是等到线上出事。
        """

        raise NotImplementedError

    def resolve_scope_id(self, auth: dict[str, Any]) -> str | None:
        """从鉴权上下文解析 scope_id。

        TODO: 实现——取 ``auth[self.scope_key]``；缺失时返回 None
              表示当前请求无法访问该层（例如用户不在任何 space 里，
              team 层就该整层跳过而不是报错）。
        """

        raise NotImplementedError


class ScopeRegistry:
    """三层策略的持有者，装配在 ``AppContext.scope_registry``。

    存在的意义是把"哪些层启用、各层怎么写、召回权重多少"收敛到一处。
    散落在各 Manager 里会导致改一个权重要动三个文件，且很容易漏。
    """

    def __init__(self, policies: dict[MemoryScope, ScopePolicy]) -> None:
        self._policies = policies

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> ScopeRegistry:
        """从 ``config["scopes"]`` 构造。

        TODO: 实现。缺失的层按 enabled=false 处理。
        """

        raise NotImplementedError

    def get(self, scope: MemoryScope) -> ScopePolicy | None:
        """取某层策略；未启用返回 None。"""

        policy = self._policies.get(scope)
        if policy is None or not policy.enabled:
            return None
        return policy

    def enabled_scopes(self) -> list[MemoryScope]:
        """按 recall_weight 降序返回启用的层。

        降序是为了让调用方在预算不足时优先保留权重高的层。
        """

        return sorted(
            (s for s, p in self._policies.items() if p.enabled),
            key=lambda s: self._policies[s].recall_weight,
            reverse=True,
        )
