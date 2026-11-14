"""图存储（可选）：实体关系与多跳查询。

默认关闭。开启的判断标准是**出现真实的多跳需求**，例如
"张三所在团队的所有项目的负责人是谁"——这种查询用向量检索
无论如何都答不好，因为答案不在任何单条记忆里，而在关系的组合里。

反过来说，绝大多数记忆场景（偏好、事实、规则）纯向量检索就够了。
提前引入 Neo4j 只是凭空多一套需要备份、监控、升级的有状态组件。
所以这里提供 :class:`NullGraphStore`，让上层代码无需到处写
``if self.graph is not None``。

数据关系：图是 ``category=relation`` 记忆的**派生视图**，不是事实来源。
向量存储里的记忆才是权威。图可以随时从记忆全量重建——
这个性质很重要，它意味着图坏了可以重建而不是恢复备份。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

__all__ = [
    "Entity",
    "Relation",
    "GraphPath",
    "GraphMemoryStore",
    "Neo4jGraphStore",
    "NullGraphStore",
]


@dataclass
class Entity:
    """图中的实体节点。

    ``type`` 取自配置的白名单（person/team/project/product/document/concept）。
    为什么必须限定类型：开放式实体抽取会产生大量近义类型
    （"人"/"人员"/"员工"/"同事"），让图变得无法查询——
    写 Cypher 时你不知道该匹配哪个 label。限定类型是保持图可用的前提。
    """

    id: str
    tenant_id: str
    type: str
    name: str
    # 归一化后的名字，用于消歧。"张三"/"张三(zhangsan)"/"zhangsan"
    # 应指向同一实体，否则图会碎成一堆孤立节点。
    normalized_name: str = ""
    aliases: list[str] = field(default_factory=list)
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass
class Relation:
    """实体间的有向关系。"""

    source_id: str
    target_id: str
    type: str                            # belongs_to | manages | owns | depends_on | ...
    tenant_id: str = ""
    # 来源记忆 id。关系必须可溯源到具体记忆，
    # 否则那条记忆被遗忘后，图里会残留无主的关系。
    memory_id: str | None = None
    confidence: float = 1.0
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass
class GraphPath:
    """一条多跳路径，用于把图查询结果喂给 LLM。

    保留完整路径而不只是终点，是因为 LLM 需要理由：
    "李四是负责人" 不如 "张三 属于→ 支付组 拥有→ 结算项目 负责人→ 李四" 有说服力，
    后者还能让用户发现推理链里的错误。
    """

    entities: list[Entity]
    relations: list[Relation]
    score: float = 0.0


class GraphMemoryStore:
    """图存储接口。

    定义为基类而非 Protocol，是为了让 :class:`NullGraphStore` 能继承并
    直接复用签名，同时 ``isinstance`` 检查在装配阶段可用。
    """

    async def connect(self) -> None:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError

    async def ping(self) -> bool:
        raise NotImplementedError

    @property
    def enabled(self) -> bool:
        """图能力是否可用。上层据此决定是否降级为纯向量检索。"""

        return True

    async def upsert_entity(self, entity: Entity) -> str:
        """写入或更新实体，返回实体 id。

        必须是 upsert 而非 insert：同一个人会在几十条记忆里被提到，
        每次都建新节点会让图爆炸。合并键用 ``(tenant_id, type, normalized_name)``。
        """

        raise NotImplementedError

    async def upsert_relation(self, relation: Relation) -> None:
        """写入或更新关系。"""

        raise NotImplementedError

    async def delete_by_memory(self, tenant_id: str, memory_id: str) -> int:
        """删除某条记忆派生出的全部关系，返回删除数。

        记忆被遗忘或更新时必须调用，否则图与向量存储会不一致——
        用户以为删掉了"张三是我上级"，但图里还在，多跳查询仍会用到它。

        注意只删关系不删实体：实体可能被其他记忆共用，
        级联删实体会连带破坏无关的关系。孤立实体交给后台任务清理。
        """

        raise NotImplementedError

    async def neighbors(
        self,
        tenant_id: str,
        entity_id: str,
        relation_types: Sequence[str] | None = None,
        max_hops: int = 1,
        limit: int = 100,
    ) -> list[GraphPath]:
        """从某实体出发做多跳遍历。

        ``max_hops`` 与 ``limit`` 都必须有上限且不可为 0/None——
        不设上限的图遍历在稠密子图上会扫全库，一条查询就能把 Neo4j 打满。

        TODO: 实现。Cypher 里用 ``MATCH p=(n)-[*1..$hops]-(m)`` 并显式
              ``LIMIT``，同时给查询设 transaction timeout 兜底。
        """

        raise NotImplementedError

    async def resolve_entity(self, tenant_id: str, name: str, type_hint: str | None = None) -> Entity | None:
        """按名字找实体（消歧）。

        TODO: 实现。先精确匹配 normalized_name，再退化到别名与模糊匹配。
              模糊匹配要设阈值——把"张三"匹配到"张三丰"会污染整条推理链。
        """

        raise NotImplementedError

    async def rebuild_from_memories(self, tenant_id: str, batch_size: int = 500) -> dict[str, int]:
        """从 relation 类记忆全量重建图，返回统计。

        提供这个方法是因为图是派生视图。抽取逻辑改进、实体类型白名单调整、
        或者图数据被写坏，都可以重建而不必恢复备份。

        TODO: 实现。重建期间应写入新的临时标签再原子切换，
              直接清空重建会有一段时间图查询返回空结果。
        """

        raise NotImplementedError


class Neo4jGraphStore(GraphMemoryStore):
    """Neo4j 实现。

    TODO: 用官方 neo4j 异步驱动实现；启动时创建约束与索引：
          - ``CREATE CONSTRAINT ... FOR (n:Entity) REQUIRE (n.tenant_id, n.type, n.normalized_name) IS UNIQUE``
            这条约束就是 upsert 的合并依据，没有它 upsert 会退化成 insert。
          - 每种 label 上建 tenant_id 索引，避免跨租户扫描。
    """

    def __init__(self, uri: str, user: str, password: str, database: str = "neo4j", **options: Any) -> None:
        self.uri = uri
        self.user = user
        self.password = password
        self.database = database
        self.options = options
        self._driver: Any | None = None


class NullGraphStore(GraphMemoryStore):
    """图存储关闭时的空实现。

    所有写操作静默成功、所有读操作返回空。这样上层的
    "抽取到 relation 记忆 → 同步到图" 流程无需分支判断，
    ``graph_backend=none`` 时自然什么也不做。

    读操作返回空而不是抛异常，是为了让多跳查询接口能优雅降级为
    "图能力未启用，仅返回向量检索结果"，而不是给调用方一个 500。
    """

    @property
    def enabled(self) -> bool:
        return False

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def ping(self) -> bool:
        return True

    async def upsert_entity(self, entity: Entity) -> str:
        return entity.id

    async def upsert_relation(self, relation: Relation) -> None:
        return None

    async def delete_by_memory(self, tenant_id: str, memory_id: str) -> int:
        return 0

    async def neighbors(
        self,
        tenant_id: str,
        entity_id: str,
        relation_types: Sequence[str] | None = None,
        max_hops: int = 1,
        limit: int = 100,
    ) -> list[GraphPath]:
        return []

    async def resolve_entity(self, tenant_id: str, name: str, type_hint: str | None = None) -> Entity | None:
        return None

    async def rebuild_from_memories(self, tenant_id: str, batch_size: int = 500) -> dict[str, int]:
        return {"entities": 0, "relations": 0}


def build_graph_store(storage_config: dict[str, Any]) -> GraphMemoryStore:
    """按配置构造图存储。

    TODO: 实现——``graph_backend`` 为 ``neo4j`` 时返回 Neo4jGraphStore，
          否则返回 NullGraphStore。
    """

    raise NotImplementedError
