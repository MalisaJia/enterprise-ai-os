"""pgvector 存储：记忆的持久化、向量检索、强度更新与软删除。

为什么是 pgvector 而不是专用向量库
-----------------------------------
rag-pipeline 里 Milvus 是合理选项，因为文档向量写入后基本只读。
记忆则相反——核心操作是 UPDATE 与 DELETE（整合、衰减、遗忘），
而专用向量库的删除多为标记删除、需要 compact 才真正回收，更新语义也弱。
加上记忆总量小（每用户数千条），pgvector 的性能完全够用，
换来的事务一致性更有价值：整合时"删旧记忆 + 写新记忆 + 记审计"
必须原子完成，跨两套存储做不到这一点。

表结构（DDL 见 README，实际以 alembic 迁移为准）::

    memory.memories        记忆主表，含 embedding vector(1024)
    memory.memory_events   变更审计流：写入 / 更新 / 遗忘 / 冲突判定
    memory.pending_reviews  team 层待确认队列

索引策略::

    (tenant_id, scope, scope_id, status)          检索前缀
    HNSW (embedding vector_cosine_ops)            向量召回
    (tenant_id, category, expires_at)             event 过期清理
    (tenant_id, strength)                         衰减批处理扫描
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Sequence

from ..memory import Memory, MemoryCategory, MemoryScope, MemoryStatus

__all__ = [
    "SearchHit",
    "SearchFilter",
    "VectorMemoryStore",
]


@dataclass
class SearchHit:
    """一条检索命中。

    ``similarity`` 与 ``score`` 分开保留是刻意的：前者是纯向量相似度，
    后者是叠加了 strength / recency / scope_weight 后的最终排序分。
    排查"为什么这条记忆没被召回"时必须能区分是向量不像，
    还是被强度或权重压下去了——只留一个数字就没法诊断。
    """

    memory: Memory
    similarity: float
    score: float
    # 打分各项的明细，便于调试与在 API 里暴露 explain
    score_breakdown: dict[str, float] = field(default_factory=dict)


@dataclass
class SearchFilter:
    """检索过滤条件。

    ``tenant_id`` 无默认值且必填。记忆是隐私数据，
    "忘记传 tenant_id 就查全库"这种失误的代价太大，
    所以让它在类型层面就无法省略。
    """

    tenant_id: str
    # 要查哪些层，以及各层对应的 scope_id。
    # 形如 {PERSONAL: "u_1", TEAM: "sp_9"}；不传的层不查。
    scopes: dict[MemoryScope, str] = field(default_factory=dict)
    categories: Sequence[MemoryCategory] | None = None
    statuses: Sequence[MemoryStatus] = (MemoryStatus.ACTIVE,)
    # 只要 mandatory 的（用于 /context 优先占预算的那一步）
    mandatory_only: bool = False
    min_similarity: float = 0.5
    top_k: int = 10
    # 排除指定 id（整合判定时排除自身）
    exclude_ids: Sequence[str] = ()
    # 是否包含已过期的 event（默认排除）
    include_expired: bool = False


class VectorMemoryStore:
    """pgvector 存储实现。

    连接池由本类持有，跨请求复用。所有方法都要求显式 tenant_id，
    并在会话上设置 ``app.tenant_id`` 供 RLS 使用。

    TODO: 用 psycopg_pool.AsyncConnectionPool 实现；
          注册 pgvector 的类型适配器（pgvector.psycopg.register_vector_async）。
    """

    def __init__(self, dsn: str, schema: str = "memory", config: dict[str, Any] | None = None) -> None:
        self.dsn = dsn
        self.schema = schema
        self.config = config or {}
        self._pool: Any | None = None
        # 低于此条数时跳过 HNSW 直接顺序扫描。
        # 不是过早优化：新租户前几十条记忆走索引反而慢，
        # 且 HNSW 在极小数据集上的召回率不稳定，会出现"明显该命中的没命中"。
        self.exact_search_threshold: int = int(self.config.get("exact_search_threshold", 200))

    # ---- 生命周期 -------------------------------------------------------
    @classmethod
    def from_config(cls, storage_config: dict[str, Any]) -> VectorMemoryStore:
        """从 ``config["storage"]`` 构造。

        TODO: 实现；``auto_migrate`` 为真时在此触发 alembic upgrade head。
              迁移要在拿到连接后、开放流量前完成。
        """

        raise NotImplementedError

    async def connect(self) -> None:
        """建立连接池并校验 schema。

        TODO: 实现。启动时必须校验两件事，否则会在运行期才暴露：
              1. ``CREATE EXTENSION IF NOT EXISTS vector`` 是否可用
              2. 表里 embedding 列的维度是否等于 config 的 dimensions。
                 维度不一致时 INSERT 才会报错，那时可能已经跑了半天抽取
                 却一条都没存进去——必须启动即失败。
        """

        raise NotImplementedError

    async def close(self) -> None:
        """关闭连接池。"""

        raise NotImplementedError

    async def ping(self) -> bool:
        """健康检查，供 ``/readyz`` 调用。"""

        raise NotImplementedError

    # ---- 写入 -----------------------------------------------------------
    async def insert(self, memory: Memory) -> str:
        """插入一条记忆，返回 id。

        TODO: 实现。同一事务内还要向 memory_events 追加一条 ``written`` 事件——
              审计流与主表必须同时成功，否则就出现"库里有记忆但查不到来源"。
        """

        raise NotImplementedError

    async def insert_many(self, memories: Sequence[Memory]) -> list[str]:
        """批量插入。

        TODO: 用 executemany / COPY。一次抽取通常产出数条，
              逐条 INSERT 会有明显的往返开销。
        """

        raise NotImplementedError

    async def update_content(
        self,
        memory_id: str,
        tenant_id: str,
        content: str,
        embedding: list[float],
        reason: str = "",
    ) -> bool:
        """更新记忆内容（整合判定为 UPDATE 时调用）。

        content 变了 embedding 必须同步更新——这是最容易漏的一处。
        只改文本不改向量，会让这条记忆在向量空间里停留在旧位置，
        表现为"内容明明改对了但检索不到"，且极难排查。
        因此这里把 embedding 设为必传参数，而不是可选。

        TODO: 实现，并写 ``updated`` 审计事件（含 reason 与旧内容快照）。
        """

        raise NotImplementedError

    async def supersede(self, memory_id: str, tenant_id: str, new_memory_id: str | None = None) -> bool:
        """软删除：置 status=SUPERSEDED 并指向取代者。

        为什么不物理删除：用户可能反悔（"你刚才为什么忘了我说的话"），
        也需要回答"这条记忆是被什么取代的"。物理删除之后这两个问题
        都无法回答。物理清理由 ``purge_after_days`` 的后台任务统一做。

        TODO: 实现。
        """

        raise NotImplementedError

    async def forget(self, memory_id: str, tenant_id: str, hard: bool = False) -> bool:
        """遗忘。``hard=True`` 时物理删除（用于 GDPR 数据主体删除请求）。

        TODO: 实现。hard 删除同样要留一条审计事件，
              但事件里不能存 content——否则"物理删除"名不副实。
        """

        raise NotImplementedError

    # ---- 检索 -----------------------------------------------------------
    async def search(self, query_embedding: list[float], flt: SearchFilter) -> list[SearchHit]:
        """向量检索 + 多因子打分。

        打分公式（权重见 config 的 ``retrieval.scoring``）::

            score = similarity     × w_similarity
                  + strength       × w_strength
                  + recency        × w_recency
                  + scope_weight   × w_scope

        为什么不只用相似度：一条三年前的、几乎没被用过的记忆，
        即使字面上最相似，也大概率不是用户当下想要的。

        实现要点：
        - 打分应在 SQL 里完成，不要把候选全捞到 Python 再排。
          记忆虽少，但 /context 在 Agent 的关键路径上，一次往返都要省。
        - recency 用 ``exp(-age_days / half_life)`` 而非线性，
          线性会让一年前和两年前的记忆区别过大而近三天的区别过小。

        TODO: 实现。
        """

        raise NotImplementedError

    async def find_similar(
        self,
        query_embedding: list[float],
        tenant_id: str,
        scope: MemoryScope,
        scope_id: str,
        top_k: int = 8,
        min_similarity: float = 0.6,
    ) -> list[SearchHit]:
        """整合判定用的候选召回。

        与 :meth:`search` 的区别：这里**只按纯相似度排序，不叠加 strength**。
        判断"新记忆是否与某条旧记忆矛盾"只取决于语义接近程度，
        与旧记忆有多强无关。若把强度掺进来，会导致一条被判定为矛盾的
        弱记忆排不进候选，矛盾就永远发现不了。

        TODO: 实现。
        """

        raise NotImplementedError

    async def get(self, memory_id: str, tenant_id: str) -> Memory | None:
        """按 id 取单条。"""

        raise NotImplementedError

    async def list_memories(
        self,
        tenant_id: str,
        scope: MemoryScope,
        scope_id: str,
        statuses: Sequence[MemoryStatus] = (MemoryStatus.ACTIVE,),
        categories: Sequence[MemoryCategory] | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> tuple[list[Memory], str | None]:
        """分页列出记忆，返回 ``(记忆列表, 下一页 cursor)``。

        用 cursor 而非 offset：记忆会被后台任务持续增删，
        offset 分页在这种场景下会漏行或重复行。

        TODO: 实现，cursor 用 ``(created_at, id)`` 编码。
        """

        raise NotImplementedError

    async def count(self, tenant_id: str, scope: MemoryScope, scope_id: str) -> int:
        """统计条数，用于 ``max_per_subject`` 上限检查。"""

        raise NotImplementedError

    # ---- 强度与衰减 -----------------------------------------------------
    async def record_hits(self, memory_ids: Sequence[str], tenant_id: str, reinforcement: float) -> None:
        """记录命中：``hit_count += 1``、``last_hit_at = now``、强度回升。

        必须是异步 fire-and-forget，不能阻塞检索返回。检索在 Agent 的
        关键路径上，为了记一次命中而多等一次数据库写入不划算。

        TODO: 实现，并考虑批量合并——高频检索会让同一条记忆在几秒内
              被命中多次，逐次 UPDATE 会造成明显的行锁竞争。
              可以在进程内攒 5 秒再合并写一次。
        """

        raise NotImplementedError

    async def decay_batch(
        self,
        tenant_id: str | None,
        half_life_days: float,
        forget_threshold: float,
        exempt_categories: Sequence[MemoryCategory] = (),
        exempt_mandatory: bool = True,
        batch_size: int = 500,
    ) -> dict[str, int]:
        """批量衰减，返回 ``{"decayed": n, "forgotten": m}``。

        豁免规则很重要：``rule`` 类不应因为最近没提到就淡忘——
        规则的有效性与提及频率无关。对所有类别一视同仁地衰减，
        会让制度性约束悄悄失效，这是记忆衰减最容易搞错的一点。

        实现上用单条 UPDATE 完成衰减而非"读出来算好再写回"：
        后者在多副本或任务重跑时会重复衰减，让强度掉得比预期快。

        TODO: 实现。
        """

        raise NotImplementedError

    async def expire_events(self, tenant_id: str | None, grace_days: int = 7) -> int:
        """把过了 ``expires_at`` 的 event 类记忆置为 EXPIRED，返回条数。

        留 grace 期是因为"下周三评审"这类记忆在会议当天之后的几天里
        仍有价值（"上次评审说了什么"）。

        TODO: 实现。
        """

        raise NotImplementedError

    async def purge_soft_deleted(self, before: datetime, batch_size: int = 1000) -> int:
        """物理清理软删除记录，返回条数。

        TODO: 实现。
        """

        raise NotImplementedError

    # ---- 待确认队列（team 层）------------------------------------------
    async def enqueue_pending(self, memory: Memory, ttl_days: int) -> str:
        """把候选记忆放入待确认队列。

        TODO: 实现。
        """

        raise NotImplementedError

    async def list_pending(self, tenant_id: str, scope_id: str, limit: int = 50) -> list[Memory]:
        """列出待确认项。

        TODO: 实现，顺带过滤掉已超 TTL 的——不设过期会积累大量
              无人处理的 pending，确认列表变成噪音，最终没人看。
        """

        raise NotImplementedError

    async def resolve_pending(self, memory_id: str, tenant_id: str, approved: bool, actor: str) -> bool:
        """确认或拒绝一条待确认记忆。

        TODO: 实现；approved 时置 ACTIVE，否则置 REJECTED（不删，
              留着才能统计抽取准确率）。
        """

        raise NotImplementedError

    # ---- 审计 -----------------------------------------------------------
    async def append_event(
        self,
        tenant_id: str,
        memory_id: str | None,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        """追加审计事件。

        TODO: 实现。注意 payload 里若含记忆内容，需遵守
              ``logging.log_memory_content`` 之外的另一套判断——
              审计表是业务表，允许存内容；日志不允许。
        """

        raise NotImplementedError
