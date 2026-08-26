"""memory-service REST 路由与 API 契约。

契约设计原则：
- ``tenant_id`` / ``user_id`` / ``space_id`` 一律从鉴权上下文取，**不接受**
  客户端在 body 里指定。记忆是隐私数据，让调用方自报身份等于没有隔离。
- 写入是同步的但可能返回 ``op=noop``：调用方必须能接受"写了但没存"。
- ``/context`` 是关键路径接口，必须快。它不做整合、不调 LLM（除非显式
  要求 summarize），只做检索与组装。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field, field_validator

from ..memory import ConsolidationOp, MemoryCategory, MemoryScope, MemoryStatus

router = APIRouter()


# ---------------------------------------------------------------------------
# 鉴权上下文
# ---------------------------------------------------------------------------
class AuthContext(BaseModel):
    """由 octo-auth 校验后注入的调用方身份。"""

    tenant_id: str
    user_id: str | None = None
    space_id: str | None = None
    roles: list[str] = Field(default_factory=list)
    # 服务间调用（octo-server / workflow-engine / meeting-agent）时为 True。
    # 内部调用需要代表某个用户操作，此时 user_id 由 X-On-Behalf-Of 头传入——
    # 但只信任内部令牌带的这个头，普通令牌带了要忽略，否则就是越权入口。
    is_internal: bool = False

    def is_admin(self, admin_roles: list[str]) -> bool:
        """是否具备管理员角色。"""

        return any(r in admin_roles for r in self.roles)


async def get_auth(request: Request) -> AuthContext:
    """从请求中提取鉴权上下文。

    TODO: 调用 octo-auth 的 /v1/verify 校验 Authorization 头，
          支持 session / bot-key / user-key 三种令牌，结果按 auth.cache_ttl 缓存。
          若 config.auth.enabled 为 false（本地开发），返回 dev 默认身份。
    """

    ctx = request.app.state.ctx
    if not ctx.get("auth.enabled", True):
        return AuthContext(
            tenant_id="dev", user_id="dev", space_id="dev", roles=["memory-admin"], is_internal=True
        )
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="octo-auth integration not implemented yet",
    )


async def require_admin(request: Request, auth: AuthContext = Depends(get_auth)) -> AuthContext:
    """要求管理员权限。用于组织层写入与跨用户查询。"""

    admin_roles = request.app.state.ctx.get("auth.admin_roles", []) or []
    if not (auth.is_internal or auth.is_admin(admin_roles)):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin role required")
    return auth


def _manager(request: Request, scope: MemoryScope) -> Any:
    """按 scope 取对应的 Manager 单例。

    TODO: 实现——从 ctx 上取三个 Manager。未启用的层应返回 400
          而不是 500：调用方查一个未启用的层是请求问题，不是服务故障。
    """

    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="memory managers not wired yet",
    )


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------
class MemoryOut(BaseModel):
    """记忆的对外表示。

    刻意**不输出 embedding**：1024 维浮点数组会让响应体膨胀几十倍，
    而调用方无一例外都用不上它。需要向量的场景（离线分析）走专门的导出。
    """

    id: str
    scope: MemoryScope
    scope_id: str
    category: MemoryCategory
    content: str
    status: MemoryStatus
    confidence: float
    strength: float
    mandatory: bool = False
    source_type: str = "extraction"
    source_session_id: str | None = None
    superseded_by: str | None = None
    expires_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    last_hit_at: datetime | None = None
    hit_count: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)


class MemoryHitOut(MemoryOut):
    """检索结果：记忆 + 打分。

    ``score_breakdown`` 默认返回。排查"为什么这条没被召回"时必须能区分
    是向量不像还是被强度压下去了，把它藏在 debug 开关后面会让排查
    需要改配置重启，得不偿失。
    """

    similarity: float
    score: float
    score_breakdown: dict[str, float] = Field(default_factory=dict)


class AddMemoryRequest(BaseModel):
    """显式添加一条记忆。"""

    content: str = Field(min_length=1, max_length=1000)
    category: MemoryCategory = MemoryCategory.FACT
    scope: MemoryScope = MemoryScope.PERSONAL
    # 只对 org 层有效，且需要更高权限
    mandatory: bool = False
    # 跳过整合判定。默认 False——用户说"记住我现在用 pnpm"时，
    # 意图往往是更新旧偏好而非并列添加。
    skip_consolidation: bool = False
    expires_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("content")
    @classmethod
    def _check_atomic(cls, v: str) -> str:
        """校验原子性。

        TODO: 实现——超过 config 的 max_content_chars 时报错，
              并在错误信息里说明"请拆成多条"。直接截断是错的：
              截断会产生半句话的记忆，比拒绝更糟。
        """

        return v


class WriteMemoryResponse(BaseModel):
    """写入结果。

    ``op`` 必须回传：整合可能判成 NOOP 或 UPDATE，此时并没有产生新 id。
    调用方若假定"写入必得新 id"就会拿到 null 而困惑。
    """

    op: ConsolidationOp
    memory_id: str | None = None
    affected_ids: list[str] = Field(default_factory=list)
    reason: str = ""
    # 影子模式下为 True：判定已产生但库里没变。
    # 上线初期调用方会大量看到这个，必须在文档里写清。
    shadowed: bool = False


class SearchRequest(BaseModel):
    """记忆检索请求。"""

    query: str = Field(min_length=1, max_length=2000)
    scopes: list[MemoryScope] = Field(default_factory=lambda: list(MemoryScope))
    categories: list[MemoryCategory] | None = None
    top_k: int = Field(default=10, ge=1, le=50)
    min_similarity: float | None = Field(default=None, ge=0.0, le=1.0)
    # 是否记录命中以回升强度。
    # 默认 True，但离线评测、管理界面浏览应传 False——
    # 否则评测跑一轮会把整个库的强度都拉高，衰减机制失真。
    record_hit: bool = True


class SearchResponse(BaseModel):
    hits: list[MemoryHitOut]
    took_ms: int = 0
    # 各层实际召回数，便于诊断"为什么团队记忆一条都没出来"
    scope_counts: dict[str, int] = Field(default_factory=dict)


class ContextRequest(BaseModel):
    """上下文组装请求。

    这是本服务最重要的接口——Agent 每轮对话都会调它。
    """

    query: str = Field(min_length=1, max_length=4000)
    scopes: list[MemoryScope] = Field(default_factory=lambda: list(MemoryScope))
    # token 预算。必须由调用方指定。
    #
    # 为什么强调：不设预算会让记忆无节制地挤占对话历史的空间，
    # 反而降低整体效果——Agent 记得你的偏好但忘了三轮前说过什么，
    # 是更糟的体验。这里给默认值只是兜底，调用方应当显式传。
    token_budget: int | None = Field(default=None, ge=50, le=4000)
    # truncate（默认，快）| summarize（信息保留更多但多一次 LLM 调用）
    on_overflow: Literal["truncate", "summarize"] | None = None
    # 是否返回结构化的记忆列表（除了拼好的文本）。
    # Agent 通常只要文本；管理界面预览要结构化。
    include_memories: bool = False


class ContextResponse(BaseModel):
    """组装好的上下文。

    ``text`` 是可以直接拼进 system prompt 的成品。
    分组呈现（组织规定 / 团队约定 / 用户偏好）比平铺列表效果好得多：
    LLM 能区分"这是硬约束"和"这是个人偏好"，从而在两者冲突时正确取舍。
    """

    text: str
    token_count: int = 0
    memory_count: int = 0
    # 因预算不足被丢弃的条数。非 0 时说明预算偏紧，值得监控。
    truncated_count: int = 0
    # mandatory 记忆是否全部纳入。
    # 若为 False 是**严重信号**——组织硬约束因预算不足被截断了，
    # 说明 mandatory 记忆数量已超出合理范围，需要人工清理。
    mandatory_complete: bool = True
    memories: list[MemoryHitOut] = Field(default_factory=list)


class ExtractRequest(BaseModel):
    """从会话抽取记忆。

    通常由 Kafka 消费触发，这个 HTTP 入口用于手动重跑与调试。
    """

    session_id: str
    messages: list[dict[str, Any]] = Field(default_factory=list)
    # 抽取到哪些层。personal 自动生效，team 进待确认队列，org 不接受抽取。
    scopes: list[MemoryScope] = Field(
        default_factory=lambda: [MemoryScope.PERSONAL, MemoryScope.TEAM]
    )
    # 只返回抽取结果不写库，用于调优 prompt
    dry_run: bool = False

    @field_validator("scopes")
    @classmethod
    def _reject_org(cls, v: list[MemoryScope]) -> list[MemoryScope]:
        """拒绝把 org 作为抽取目标。

        组织记忆只接受管理员写入或审批流。留这个校验是因为
        调用方很容易顺手传 ``scopes=["personal","team","org"]``。
        """

        if MemoryScope.ORG in v:
            raise ValueError("org scope does not accept extraction; use admin write or approval flow")
        return v


class ExtractResponse(BaseModel):
    session_id: str
    results: list[WriteMemoryResponse] = Field(default_factory=list)
    # 抽出但因置信度不足被丢弃的条数。
    # 这个数字持续为 0 说明阈值可能太松，持续很高说明抽取 prompt 有问题。
    discarded_count: int = 0
    took_ms: int = 0


class ForgetRequest(BaseModel):
    """按语义批量遗忘。"""

    query: str = Field(min_length=1, max_length=1000)
    scope: MemoryScope = MemoryScope.PERSONAL
    # 必须默认 True。按语义批量删除是危险操作，
    # 一个宽泛的 query 可能命中大半个库。
    dry_run: bool = True


# ---------------------------------------------------------------------------
# 记忆读写
# ---------------------------------------------------------------------------
@router.post("/memories", response_model=WriteMemoryResponse, status_code=status.HTTP_201_CREATED)
async def add_memory(
    payload: AddMemoryRequest,
    request: Request,
    auth: AuthContext = Depends(get_auth),
) -> WriteMemoryResponse:
    """显式添加一条记忆。

    按 scope 分派：
    - personal → PersonalMemoryManager.add_explicit
    - team     → TeamMemoryManager.propose（进待确认，返回 pending 状态）
    - org      → 需管理员，走 OrgMemoryManager.add_by_admin

    org 层的权限校验必须在这里做而不是依赖 Manager：Manager 拿不到
    完整的角色上下文，把权限判断下沉会导致每个 Manager 都要重复实现一遍。

    TODO: 实现。
    """

    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="not implemented")


@router.post("/memories/search", response_model=SearchResponse)
async def search_memories(
    payload: SearchRequest,
    request: Request,
    auth: AuthContext = Depends(get_auth),
) -> SearchResponse:
    """跨层语义检索。

    实现要点：
    - 各层并发检索（asyncio.gather），不要串行。三层串行会让延迟三倍。
    - 某一层失败时降级返回其他层的结果而非整体报错——
      个人记忆能用就该给用户用，不该因为团队层出问题而全查不到。
    - query 的 embedding 只算一次，三层复用。

    TODO: 实现。
    """

    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="not implemented")


@router.get("/memories", response_model=list[MemoryOut])
async def list_memories(
    request: Request,
    scope: MemoryScope = Query(default=MemoryScope.PERSONAL),
    category: MemoryCategory | None = Query(default=None),
    status_filter: MemoryStatus = Query(default=MemoryStatus.ACTIVE, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None),
    auth: AuthContext = Depends(get_auth),
) -> list[MemoryOut]:
    """列出记忆（供"我的记忆"管理页与管理后台）。

    这个接口必须做好：让用户能看到、能删掉 Agent 记住的东西，
    是记忆功能可信的前提。用户无法审视的记忆库会让人不安。

    TODO: 实现，把 next_cursor 放响应头 ``X-Next-Cursor``——
          用 list 作为响应体是为了让前端直接消费，
          分页信息放头里避免破坏这个形状。
    """

    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="not implemented")


@router.get("/memories/{memory_id}", response_model=MemoryOut)
async def get_memory(
    memory_id: str,
    request: Request,
    auth: AuthContext = Depends(get_auth),
) -> MemoryOut:
    """取单条记忆详情（含溯源信息）。

    TODO: 实现。注意 404 与 403 的区分：查别人的记忆应返回 404 而非 403，
          否则可以靠状态码差异探测"某条记忆是否存在"。
    """

    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="not implemented")


@router.delete("/memories/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
async def forget_memory(
    memory_id: str,
    request: Request,
    hard: bool = Query(default=False, description="物理删除，仅用于合规删除请求"),
    auth: AuthContext = Depends(get_auth),
) -> None:
    """遗忘一条记忆。

    默认软删除，保留可追溯性也让误删可恢复。
    ``hard=True`` 需要管理员权限或数据主体身份。

    TODO: 实现，同时清理图里由这条记忆派生的关系。
    """

    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="not implemented")


@router.post("/memories/forget", response_model=list[MemoryOut])
async def forget_by_query(
    payload: ForgetRequest,
    request: Request,
    auth: AuthContext = Depends(get_auth),
) -> list[MemoryOut]:
    """按语义批量遗忘。

    ``dry_run=True``（默认）时只返回将被删除的列表，不实际删除。
    正确交互是先预览让用户确认，再带 ``dry_run=false`` 执行。

    TODO: 实现。
    """

    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="not implemented")


# ---------------------------------------------------------------------------
# 上下文组装（关键路径）
# ---------------------------------------------------------------------------
@router.post("/context", response_model=ContextResponse)
async def build_context(
    payload: ContextRequest,
    request: Request,
    auth: AuthContext = Depends(get_auth),
) -> ContextResponse:
    """组装可直接注入 prompt 的记忆上下文。

    为什么要有这个组合接口，而不让调用方自己调 /search 再拼：
    "检索 → 按层分组 → 按预算截断 → 渲染模板" 这套逻辑如果放在调用方，
    每个接入方都要实现一遍，且必然实现得不一致——有的忘了处理 mandatory，
    有的预算算错。收在服务端能保证所有 Agent 拿到的记忆口径一致。

    组装顺序（顺序本身就是策略）::

        1. 无条件取全部 mandatory 组织记忆，优先占预算
           （不经向量检索：安全约束常常与当前问题字面无关，
            但恰恰这种时候最需要它）
        2. 剩余预算按 budget_split 分给三层
        3. 各层并发检索，按分数取到填满各自配额
        4. 某层配额没用满时，余量让给其他层（不浪费预算）
        5. 按模板渲染，分组呈现
        6. 超预算按 on_overflow 处理

    第 4 步容易漏：新用户的团队层可能一条记忆都没有，
    不做余量转移就白白浪费 30% 的预算。

    TODO: 实现。这个接口的 p99 延迟要盯住，它在每轮对话的关键路径上。
    """

    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="not implemented")


# ---------------------------------------------------------------------------
# 抽取
# ---------------------------------------------------------------------------
@router.post("/extract", response_model=ExtractResponse)
async def extract_memories(
    payload: ExtractRequest,
    request: Request,
    auth: AuthContext = Depends(get_auth),
) -> ExtractResponse:
    """从会话消息抽取记忆。

    正常路径是 Kafka 消费 ``octo.message.created`` 后自动触发，
    这个 HTTP 入口用于手动重跑与调 prompt。

    幂等：同一 session 的抽取只执行一次（Redis 键 ``extract:{tenant}:{session}``）。
    Kafka 至少一次投递会导致重复消费，不去重会产生大量重复记忆。
    但手动重跑需要能绕过幂等——TODO 里要支持 ``force`` 参数。

    TODO: 实现。
    """

    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="not implemented")


# ---------------------------------------------------------------------------
# 团队层待确认
# ---------------------------------------------------------------------------
@router.get("/pending", response_model=list[MemoryOut])
async def list_pending(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    auth: AuthContext = Depends(get_auth),
) -> list[MemoryOut]:
    """列出当前空间的待确认记忆。

    TODO: 实现，顺带过滤掉超过 pending_ttl_days 的。
    """

    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="not implemented")


@router.post("/pending/{memory_id}/confirm", response_model=WriteMemoryResponse)
async def confirm_pending(
    memory_id: str,
    request: Request,
    auth: AuthContext = Depends(get_auth),
) -> WriteMemoryResponse:
    """确认一条待确认记忆，使其生效。

    必须幂等：确认按钮被连点、消息卡片被重复点击都会重复调用。
    第二次应返回"已确认"而不是再执行一次 UPDATE。

    TODO: 实现。
    """

    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="not implemented")


@router.post("/pending/{memory_id}/reject", status_code=status.HTTP_204_NO_CONTENT)
async def reject_pending(
    memory_id: str,
    request: Request,
    reason: str = Query(default=""),
    auth: AuthContext = Depends(get_auth),
) -> None:
    """拒绝一条待确认记忆。

    置 REJECTED 不删除——拒绝率是评估抽取质量最直接的信号。

    TODO: 实现。
    """

    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="not implemented")


# ---------------------------------------------------------------------------
# 组织层管理
# ---------------------------------------------------------------------------
@router.post("/org/memories/{memory_id}/mandatory", response_model=MemoryOut)
async def set_mandatory(
    memory_id: str,
    request: Request,
    mandatory: bool = Query(...),
    auth: AuthContext = Depends(require_admin),
) -> MemoryOut:
    """切换组织记忆的 mandatory 标记。

    影响全租户所有 Agent 的行为，必须写审计并记录 actor。

    TODO: 实现。同时校验 mandatory 记忆总数上限——
          超过约 20 条它们就能吃掉整个 token 预算，
          让 /context 的 mandatory_complete 变成 false。
    """

    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="not implemented")


@router.post("/org/memories/import", response_model=dict)
async def import_org_memories(
    request: Request,
    payload: list[AddMemoryRequest],
    dry_run: bool = Query(default=True),
    auth: AuthContext = Depends(require_admin),
) -> dict:
    """批量导入组织记忆（从规章制度文档初始化）。

    与 rag-pipeline 的分工：整篇规范进 rag-pipeline 供检索原文，
    只有"Agent 每次都必须遵守"的硬约束才提炼进组织记忆。

    TODO: 实现。
    """

    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="not implemented")


# ---------------------------------------------------------------------------
# 图查询（可选能力）
# ---------------------------------------------------------------------------
@router.get("/graph/neighbors", response_model=dict)
async def graph_neighbors(
    request: Request,
    entity: str = Query(..., description="实体名或实体 id"),
    max_hops: int = Query(default=2, ge=1, le=3),
    limit: int = Query(default=50, ge=1, le=200),
    auth: AuthContext = Depends(get_auth),
) -> dict:
    """多跳关系查询。

    图未启用时返回 ``{"enabled": false, "paths": []}`` 而不是 501——
    这是可选能力，调用方应能优雅降级为纯向量检索，
    给它一个错误码会导致上层报错。

    TODO: 实现。
    """

    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="not implemented")


# ---------------------------------------------------------------------------
# 运维与合规
# ---------------------------------------------------------------------------
@router.post("/admin/consolidate", response_model=dict)
async def trigger_consolidation(
    request: Request,
    scope: MemoryScope = Query(default=MemoryScope.PERSONAL),
    scope_id: str | None = Query(default=None),
    tasks: list[str] = Query(default=["dedup"]),
    auth: AuthContext = Depends(require_admin),
) -> dict:
    """手动触发整合任务。

    正常由 APScheduler 按 cron 执行，这个入口用于排查与补跑。
    必须抢 Redis 锁，避免与定时任务并发执行造成重复合并。

    TODO: 实现。
    """

    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="not implemented")


@router.post("/admin/decay", response_model=dict)
async def trigger_decay(
    request: Request,
    tenant_id: str | None = Query(default=None),
    auth: AuthContext = Depends(require_admin),
) -> dict:
    """手动触发衰减。

    注意这个接口**不可随意重跑**：衰减是按时间推进的，
    一天内跑两次会让强度掉得比预期快，记忆被提前遗忘。
    实现时应记录上次执行时间并拒绝过近的重复调用。

    TODO: 实现。
    """

    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="not implemented")


@router.get("/admin/conflicts", response_model=list[dict])
async def list_conflicts(
    request: Request,
    scope: MemoryScope = Query(default=MemoryScope.PERSONAL),
    scope_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    auth: AuthContext = Depends(require_admin),
) -> list[dict]:
    """列出检测到的矛盾记忆，等人裁决。

    不自动处理历史矛盾：无法判断两条里哪条更该留，
    时间戳新的不一定是对的。

    TODO: 实现。
    """

    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="not implemented")


@router.get("/privacy/export", response_model=dict)
async def export_my_memories(
    request: Request,
    auth: AuthContext = Depends(get_auth),
) -> dict:
    """导出本人全部记忆（数据主体访问权）。

    必须包含软删除的记录——访问权覆盖"你们还持有的关于我的数据"，
    软删除的数据仍在持有中。

    TODO: 实现。数据量可能较大，考虑改为异步生成下载链接。
    """

    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="not implemented")


@router.delete("/privacy/erase", response_model=dict)
async def erase_my_memories(
    request: Request,
    confirm: str = Query(..., description="必须等于 user_id，作为二次确认"),
    auth: AuthContext = Depends(get_auth),
) -> dict:
    """物理删除本人全部记忆（数据主体删除权）。

    必须 hard delete，软删除不满足合规要求。
    ``confirm`` 参数要求调用方回填自己的 user_id 作为二次确认——
    这个接口误调一次就无法挽回，光靠 DELETE 方法不足以防呆。

    TODO: 实现，同时清理图里的相关节点与关系。
    """

    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="not implemented")
