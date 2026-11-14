"""meeting-agent REST 路由与 API 契约。

契约设计原则
------------
- ``tenant_id`` / ``user_id`` 一律从鉴权上下文取，不接受 body 指定。
  会议内容常涉及人事、财务、战略，让调用方自报身份等于没有隔离。
- 会议可见范围继承所在 space 的成员关系，不自建会议权限模型。
- 长任务一律返回 ``job_id`` 异步化。转录一场两小时会议要几分钟到几十分钟，
  同步等待必然超时。
- 所有产出带 ``version``，``/reprocess`` 产生新版本而非覆盖。

一处刻意的不对称：读接口很多、写接口极少。用户对会议成果的操作
本质上只有三种——看、改（修正抽取错误）、发。不需要完整的 CRUD。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Query,
    Request,
    UploadFile,
    WebSocket,
    status,
)
from pydantic import BaseModel, Field, field_validator

from ..distribution.dispatcher import DispatchOutcome, DispatchTarget
from ..extraction.actions import ActionPriority, ActionStatus
from ..extraction.decisions import DecisionCategory
from ..transcription.asr import TranscriptionStatus

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
    # 服务间调用（octo-server / workflow-engine）时为 True。
    # workflow-engine 的审批回调走这个身份——它没有用户上下文。
    is_internal: bool = False

    def is_admin(self, admin_roles: list[str]) -> bool:
        """是否具备管理员角色。"""

        return any(r in admin_roles for r in self.roles)


async def get_auth(request: Request) -> AuthContext:
    """从请求中提取鉴权上下文。

    TODO: 调用 octo-auth 的 /v1/verify 校验 Authorization 头，
          结果按 auth.cache_ttl 缓存。
          若 config.auth.enabled 为 false（本地开发），返回 dev 默认身份。
    """

    ctx = request.app.state.ctx
    if not ctx.get("auth.enabled", True):
        return AuthContext(
            tenant_id="dev",
            user_id="dev",
            space_id="dev",
            roles=["meeting-admin"],
            is_internal=True,
        )
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="octo-auth integration not implemented yet",
    )


async def require_internal(request: Request, auth: AuthContext = Depends(get_auth)) -> AuthContext:
    """要求内部服务身份。用于审批回调等 service-to-service 端点。"""

    if not auth.is_internal:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="internal service token required"
        )
    return auth


async def _assert_visible(request: Request, meeting_id: str, auth: AuthContext) -> None:
    """校验调用方能看这场会议。

    判定依据是会议所在 space 的成员关系（``privacy.visibility: space_members``），
    不是参会人列表——未参会但同 space 的人也应当能看纪要，
    这正是"决策没通知到相关方"这个痛点的解法。

    TODO: 实现。找不到会议时返回 404，无权限时**也返回 404 而非 403**：
          403 会泄露"这场会议存在"，而会议标题本身就可能是敏感信息
          （"关于 X 部门裁员的讨论"）。
    """

    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="visibility check not implemented yet",
    )


def _pipeline(request: Request) -> Any:
    """取流水线编排器单例。

    TODO: 实现——从 ctx 上取。
    """

    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="pipeline not wired yet",
    )


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------
class MeetingCreateRequest(BaseModel):
    """创建会议记录。

    支持"先建后传"：日历事件触发时就建好记录，录音结束后再上传音频。
    参会人列表在建会时就拿到，这对后续的责任人解析价值很大——
    它是最强的先验。
    """

    title: str = Field(min_length=1, max_length=500)
    # 会议类型。决定是否走 dispatch.auto_dispatch_types 白名单自动分发。
    meeting_type: str = "general"
    started_at: datetime | None = None
    ended_at: datetime | None = None
    # 会议日期。截止时间解析的基准，**必须是会议日期不是处理日期**。
    # 不传则从 started_at 推导。
    meeting_date: date | None = None
    # 参会人。``{"user_id": "...", "name": "...", "role": "organizer|attendee"}``
    # 强烈建议传全：缺了它责任人解析只能靠组织目录模糊匹配，准确率明显下降。
    participants: list[dict[str, Any]] = Field(default_factory=list)
    space_id: str | None = None
    # 外部会议 id（飞书/Zoom 的会议号），用于去重与溯源
    external_id: str | None = None
    # 领域术语，转录时用于纠错（产品代号、内部缩写）
    terminology: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class MeetingOut(BaseModel):
    """会议详情。"""

    id: str
    tenant_id: str
    title: str
    meeting_type: str
    space_id: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    meeting_date: date | None = None
    participants: list[dict[str, Any]] = Field(default_factory=list)

    # 处理状态机。
    # 各阶段独立记录而非一个总状态：抽取失败时转录结果仍然有效可看，
    # 用单一状态会让"部分成功"无法表达，用户只能看到一个笼统的 failed。
    transcription_status: TranscriptionStatus = TranscriptionStatus.PENDING
    summarization_status: Literal["pending", "running", "done", "failed"] = "pending"
    extraction_status: Literal["pending", "running", "done", "failed"] = "pending"
    dispatch_status: Literal["pending", "awaiting_confirm", "running", "done", "partial", "failed"] = (
        "pending"
    )
    # 失败原因。分阶段记录，便于定位是哪一步出的问题。
    stage_errors: dict[str, str] = Field(default_factory=dict)

    version: int = 1
    available_versions: list[int] = Field(default_factory=list)
    audio_key: str | None = None
    duration_seconds: float | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class JobOut(BaseModel):
    """异步任务状态。"""

    job_id: str
    meeting_id: str
    kind: Literal["transcribe", "summarize", "extract", "dispatch", "full", "reprocess"]
    state: Literal["queued", "running", "succeeded", "failed", "cancelled"]
    # 当前阶段与进度。
    # 转录一场长会要几十分钟，没有进度反馈用户会以为卡死了然后重复提交。
    stage: str = ""
    progress: float = 0.0
    error: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None


class SegmentOut(BaseModel):
    """转录分段。"""

    index: int
    start: float
    end: float
    speaker_label: str
    speaker_name: str | None = None
    speaker_user_id: str | None = None
    text: str
    # 术语纠错前的原文。前端可用它展示"哪些词被改过"，
    # 纠错错了用户能一眼看出来——只给纠错后的文本，错误就永远发现不了。
    raw_text: str = ""
    confidence: float = 0.0


class TranscriptOut(BaseModel):
    """转录结果。"""

    meeting_id: str
    version: int
    status: TranscriptionStatus
    engine: str = ""
    language: str = ""
    duration_seconds: float = 0.0
    segments: list[SegmentOut] = Field(default_factory=list)
    speakers: list[dict[str, Any]] = Field(default_factory=list)
    # 未映射到 user_id 的说话人数量。
    # 大于 0 时前端应提示用户去修正——责任人解析依赖它。
    unmapped_speakers: int = 0


class SpeakerMappingRequest(BaseModel):
    """修正说话人映射。

    人工修正后应当**级联重新解析责任人**：
    把 SPEAKER_02 认成李四之后，那些"我来跟"的待办才能派对人。
    不级联的话用户改完映射发现待办还是没人，会以为改的没生效。
    """

    # ``{"SPEAKER_00": {"user_id": "u_1", "name": "张三"}}``
    mappings: dict[str, dict[str, str]]
    # 是否重新解析已抽取待办的责任人。默认 true，见上。
    reresolve_actions: bool = True


class TopicOut(BaseModel):
    """议题。"""

    title: str
    summary: str
    start: float | None = None
    end: float | None = None
    speakers: list[str] = Field(default_factory=list)


class DisagreementOut(BaseModel):
    """未达成一致的分歧点。"""

    topic: str
    description: str
    positions: list[dict[str, str]] = Field(default_factory=list)


class SummaryOut(BaseModel):
    """会议摘要。"""

    meeting_id: str
    version: int
    overview: str
    topics: list[TopicOut] = Field(default_factory=list)
    # 分歧点必须返回。
    # 只给结论会让未参会的人以为所有议题都定了，
    # 下次开会才发现有人还在等讨论——这比没有纪要更容易误事。
    disagreements: list[DisagreementOut] = Field(default_factory=list)
    # 归因信息：哪个模型、哪种策略产出的。
    # 对比不同版本效果时必须知道差异来自哪里。
    model: str = ""
    strategy: str = ""
    created_at: datetime | None = None


class DecisionOut(BaseModel):
    """决策。"""

    id: str
    meeting_id: str
    statement: str
    decided_by: str
    decided_by_user_id: str | None = None
    rationale: str = ""
    category: DecisionCategory = DecisionCategory.ONE_OFF
    alternatives: list[str] = Field(default_factory=list)
    impact_scope: str = ""
    confidence: float = 0.0
    # 原话与时间戳。争议时先看原话，不用真去回听。
    quote: str = ""
    timestamp: float | None = None
    is_sensitive: bool = False
    sensitive_reason: str = ""
    version: int = 1


class ActionOut(BaseModel):
    """Action Item。"""

    id: str
    meeting_id: str
    task: str
    assignee: str = ""
    assignee_user_id: str | None = None
    assignee_name: str | None = None
    assignee_resolved_by: str = ""
    assignee_confidence: float = 0.0
    due_date: date | None = None
    # 原始时间表述。必须返回：只给解析后的日期，
    # 用户无法判断"这个日期是从哪句话算出来的"，也就没法确认对不对。
    due_date_raw: str = ""
    acceptance: str = ""
    priority: ActionPriority = ActionPriority.NORMAL
    confidence: float = 0.0
    quote: str = ""
    timestamp: float | None = None
    status: ActionStatus = ActionStatus.EXTRACTED
    external_ref: str | None = None
    edited: bool = False
    version: int = 1


class ActionPatchRequest(BaseModel):
    """人工修正待办。

    全部字段可选，只改传了的。

    这个接口是**质量数据的来源**：每次修正都在告诉我们抽取哪里不准。
    因此实现时必须记录修正前后的值（进 metadata），
    并累加 ``meeting_actions_edited_total``。修正率是抽取质量唯一的
    真实信号，自动评测测不出"这个责任人对不对"。
    """

    task: str | None = None
    assignee_user_id: str | None = None
    due_date: date | None = None
    acceptance: str | None = None
    priority: ActionPriority | None = None
    # 置为 discarded 表示误抽。
    # 这个信号比修正更强——它说明抽取器把不是任务的东西当成了任务。
    status: ActionStatus | None = None

    @field_validator("status")
    @classmethod
    def _reject_dispatched(cls, v: ActionStatus | None) -> ActionStatus | None:
        """不允许手工置为 dispatched。

        它必须由分发流程回填。手工置位会让"以为发了其实没发"，
        而这种不一致极难发现——用户以为任务已经在系统里了。
        """

        if v == ActionStatus.DISPATCHED:
            raise ValueError("dispatched is set by the dispatch pipeline, not manually")
        return v


class DispatchRequest(BaseModel):
    """触发分发。"""

    # 指定目标；为空则按配置里所有 enabled 的目标。
    targets: list[DispatchTarget] = Field(default_factory=list)
    # 只发这些待办/决策；为空则全部。
    # 用户在页面上勾选后分发是常见用法——抽出 20 条只认 15 条。
    action_ids: list[str] = Field(default_factory=list)
    decision_ids: list[str] = Field(default_factory=list)
    # 人工确认标记。
    # ``dispatch.require_confirmation`` 为 true 时必须为 true，
    # 否则返回 409 并附上 plan——不能让调用方绕过确认。
    confirmed: bool = False
    version: int | None = None


class DispatchPlanOut(BaseModel):
    """分发计划预览。

    人工确认这一步必须能看清"要发生什么"，否则等于盲签。
    """

    meeting_id: str
    version: int
    targets: list[DispatchTarget] = Field(default_factory=list)
    action_count: int = 0
    decision_count: int = 0
    # 需审批的敏感决策与待指派待办单独列出——用户最需要先处理这两类。
    sensitive_decision_ids: list[str] = Field(default_factory=list)
    unassigned_count: int = 0
    im_channels: list[str] = Field(default_factory=list)
    requires_confirmation: bool = True
    minutes_preview: str = ""


class DispatchResultOut(BaseModel):
    """单个目标的分发结果。"""

    target: DispatchTarget
    outcome: DispatchOutcome
    attempts: int = 0
    external_refs: list[str] = Field(default_factory=list)
    error: str = ""
    next_retry_at: datetime | None = None
    finished_at: datetime | None = None


class ReprocessRequest(BaseModel):
    """重跑。

    从哪个阶段起跑由 ``from_stage`` 决定。默认从 summarize 起——
    转录是最贵的一步（真金白银的 ASR 时长），而绝大多数迭代
    改的是摘要与抽取的 prompt，没必要重新转录。
    """

    from_stage: Literal["transcribe", "summarize", "extract"] = "summarize"
    # 覆盖模型配置，用于 A/B 对比。
    # ``{"extraction": {"model": "deepseek-r1"}}``
    model_overrides: dict[str, Any] = Field(default_factory=dict)
    # 覆盖 prompt，用于调优时不发版就能试。
    prompt_overrides: dict[str, str] = Field(default_factory=dict)
    # 重跑后是否自动分发。默认 false——
    # 重跑通常是为了对比效果，自动分发会把实验结果发给真人。
    auto_dispatch: bool = False


class ApprovalCallbackRequest(BaseModel):
    """workflow-engine 的审批结果回调。"""

    run_id: str
    meeting_id: str
    approved: bool
    approver: str = ""
    comment: str = ""
    decision_ids: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 会议
# ---------------------------------------------------------------------------
@router.post("/meetings", response_model=MeetingOut, status_code=status.HTTP_201_CREATED)
async def create_meeting(
    payload: MeetingCreateRequest,
    request: Request,
    auth: AuthContext = Depends(get_auth),
) -> MeetingOut:
    """创建会议记录。

    ``external_id`` 传了就按它幂等：日历 webhook 与 Kafka
    ``octo.meeting.ended`` 可能都触发一次创建，不幂等会产生两条重复会议，
    然后同一场会被处理两遍、纪要发两份。

    TODO: 实现。
    """

    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="not implemented"
    )


@router.get("/meetings/{meeting_id}", response_model=MeetingOut)
async def get_meeting(
    meeting_id: str,
    request: Request,
    auth: AuthContext = Depends(get_auth),
) -> MeetingOut:
    """会议详情，含分阶段处理状态。

    TODO: 实现。
    """

    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="not implemented"
    )


@router.post("/meetings/{meeting_id}/audio", response_model=JobOut, status_code=status.HTTP_202_ACCEPTED)
async def upload_audio(
    meeting_id: str,
    request: Request,
    file: UploadFile = File(...),
    auth: AuthContext = Depends(get_auth),
) -> JobOut:
    """上传录音，触发批量流水线。

    实现要点：

    - **流式落盘到对象存储，不要 ``await file.read()``。** 会议录音可到
      数 GB，一次性读进内存会直接把进程打爆。用分块读 + 分片上传。
    - 先校验大小（``storage.max_upload_mb``）与格式，再落盘。
      让一个 4GB 的错误文件传完再拒绝，浪费的是用户的时间。
    - 落盘后只把 ``audio_key`` 交给流水线，不传字节流。
    - 立即返回 ``job_id``，转录在后台跑。一场两小时会议的转录
      要几分钟到几十分钟，同步等待必然超时。
    - 同一会议重复上传要拒绝（409）而不是覆盖：
      覆盖会让已有的转录/抽取结果与音频对不上。要换音频走 /reprocess。

    TODO: 实现。
    """

    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="not implemented"
    )


@router.websocket("/meetings/{meeting_id}/stream")
async def stream_meeting(websocket: WebSocket, meeting_id: str) -> None:
    """WebSocket：实时音频流入，增量转写与滚动摘要流出。

    协议约定：

    - 客户端发二进制音频分片（``streaming.chunk_ms``，默认 3s），
      发 ``{"type": "end"}`` 表示结束。
    - 服务端推 ``{"type": "segment", "index": n, ...}`` 与
      ``{"type": "rolling_summary", ...}``。
    - **segment 可能被修正**：ASR 拿到后续音频后会回头改前面的识别结果。
      客户端必须按 ``index`` 覆盖而非追加，否则界面上会出现重复句子。

    两条硬约束：

    - **会议进行中不抽取、不分发**（``streaming.extract_during_meeting: false``）。
      前半段说"这个交给张三"，后半段可能改成李四。边开边派任务会让
      张三收到一个马上作废的任务，而系统里已经留下记录。
    - 连接关闭或 ``idle_timeout`` 超时后，转入批量路径做完整处理。
      不能因为实时转过一遍就跳过批量——流式转写质量低于批量，
      而最终产出的成果应该用质量更好的那份。

    鉴权：WebSocket 不走 Depends，要在 accept 前手工校验
    query 里的 token。accept 之后再拒绝，客户端已经认为连上了。

    TODO: 实现。这是最后做的部分，它只是体验加分项。
    """

    await websocket.close(code=1011, reason="not implemented")


# ---------------------------------------------------------------------------
# 转录
# ---------------------------------------------------------------------------
@router.get("/meetings/{meeting_id}/transcript", response_model=TranscriptOut)
async def get_transcript(
    meeting_id: str,
    request: Request,
    version: int | None = Query(default=None, description="默认最新版本"),
    auth: AuthContext = Depends(get_auth),
) -> TranscriptOut:
    """转录文本，带说话人与时间戳。

    TODO: 实现。转录未完成时返回当前 status 与已有分段，
          不要返回 404——用户在等待时想看到已经转出来的部分。
    """

    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="not implemented"
    )


@router.post("/meetings/{meeting_id}/transcript/speakers", response_model=TranscriptOut)
async def update_speakers(
    meeting_id: str,
    payload: SpeakerMappingRequest,
    request: Request,
    auth: AuthContext = Depends(get_auth),
) -> TranscriptOut:
    """修正说话人映射。

    实现要点：
    - 校验一个 user_id 不被映射到两个 label。同一个人被拆成两个说话人，
      "我来跟"的归属就会错，且错得不明显。
    - ``reresolve_actions`` 为 true 时级联重新解析待办责任人。
    - 修正记录要留档：说话人分离的准确率需要这个数据来评估。

    TODO: 实现。
    """

    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="not implemented"
    )


# ---------------------------------------------------------------------------
# 摘要与抽取结果
# ---------------------------------------------------------------------------
@router.get("/meetings/{meeting_id}/summary", response_model=SummaryOut)
async def get_summary(
    meeting_id: str,
    request: Request,
    version: int | None = Query(default=None),
    auth: AuthContext = Depends(get_auth),
) -> SummaryOut:
    """摘要与议题。

    TODO: 实现。
    """

    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="not implemented"
    )


@router.get("/meetings/{meeting_id}/decisions", response_model=list[DecisionOut])
async def list_decisions(
    meeting_id: str,
    request: Request,
    version: int | None = Query(default=None),
    category: DecisionCategory | None = Query(default=None),
    include_sensitive: bool = Query(
        default=True, description="false 时过滤掉敏感决策，供受限视图使用"
    ),
    auth: AuthContext = Depends(get_auth),
) -> list[DecisionOut]:
    """决策列表。

    TODO: 实现。
    """

    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="not implemented"
    )


@router.get("/meetings/{meeting_id}/actions", response_model=list[ActionOut])
async def list_actions(
    meeting_id: str,
    request: Request,
    version: int | None = Query(default=None),
    status_filter: ActionStatus | None = Query(default=None, alias="status"),
    assignee_user_id: str | None = Query(default=None),
    auth: AuthContext = Depends(get_auth),
) -> list[ActionOut]:
    """Action Item 列表。

    排序：责任人未解析的排在**最前面**，其余按截止时间。
    待指派的才是需要人动手的，按截止时间排会把它们埋在中间没人管。

    TODO: 实现。
    """

    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="not implemented"
    )


@router.patch("/meetings/{meeting_id}/actions/{action_id}", response_model=ActionOut)
async def patch_action(
    meeting_id: str,
    action_id: str,
    payload: ActionPatchRequest,
    request: Request,
    auth: AuthContext = Depends(get_auth),
) -> ActionOut:
    """人工修正待办。

    实现要点：
    - 置 ``edited = True`` 并累加 ``meeting_actions_edited_total``。
      修正率是抽取质量唯一的真实信号。
    - 修正前的值存进 metadata。要知道"改成什么"更要知道"从什么改的"，
      否则无法回溯抽取器错在哪。
    - 已 ``dispatched`` 的待办被修正时，需要**同步更新 octo-matter**
      而不是只改本地。只改本地会让两边不一致，
      用户在任务系统里看到的还是错的责任人。

    TODO: 实现。
    """

    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="not implemented"
    )


# ---------------------------------------------------------------------------
# 分发
# ---------------------------------------------------------------------------
@router.post("/meetings/{meeting_id}/dispatch", response_model=list[DispatchResultOut])
async def dispatch(
    meeting_id: str,
    payload: DispatchRequest,
    request: Request,
    auth: AuthContext = Depends(get_auth),
) -> list[DispatchResultOut]:
    """触发分发。

    确认逻辑：
    ``dispatch.require_confirmation`` 为 true 且会议类型不在
    ``auto_dispatch_types`` 白名单里时，``confirmed`` 必须为 true，
    否则返回 **409 + plan**，让调用方先展示计划再确认。
    不提供绕过确认的途径——分发有外部副作用且不可撤回。

    响应是**每个目标一条结果**而不是一个总的成功/失败。
    部分成功是常态（任务建好了但 IM 挂了），用单一状态无法表达，
    也就无法只重试失败的那部分。

    TODO: 实现。
    """

    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="not implemented"
    )


@router.get("/meetings/{meeting_id}/dispatch", response_model=list[DispatchResultOut])
async def get_dispatch_status(
    meeting_id: str,
    request: Request,
    version: int | None = Query(default=None),
    auth: AuthContext = Depends(get_auth),
) -> list[DispatchResultOut]:
    """分发结果与重试状态。

    TODO: 实现。
    """

    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="not implemented"
    )


@router.get("/meetings/{meeting_id}/dispatch/plan", response_model=DispatchPlanOut)
async def get_dispatch_plan(
    meeting_id: str,
    request: Request,
    version: int | None = Query(default=None),
    auth: AuthContext = Depends(get_auth),
) -> DispatchPlanOut:
    """分发计划预览，无副作用。

    供确认页面使用。POST /dispatch 未带 confirmed 时返回的 409
    里也内嵌这个结构，省一次往返。

    TODO: 实现。
    """

    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="not implemented"
    )


# ---------------------------------------------------------------------------
# 重跑与任务
# ---------------------------------------------------------------------------
@router.post("/meetings/{meeting_id}/reprocess", response_model=JobOut, status_code=status.HTTP_202_ACCEPTED)
async def reprocess(
    meeting_id: str,
    payload: ReprocessRequest,
    request: Request,
    auth: AuthContext = Depends(get_auth),
) -> JobOut:
    """用新 prompt/模型重跑，产生新版本。

    **不覆盖旧版本。** prompt 与模型会持续迭代，需要拿历史会议重跑
    来对比效果；覆盖式重跑会毁掉基线，改完就不知道是变好还是变坏。
    超过 ``pipeline.versioning.max_versions`` 时淘汰最旧的，
    但**永不淘汰已分发的版本**——它对应着外部系统里真实存在的任务。

    TODO: 实现。
    """

    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="not implemented"
    )


@router.get("/jobs/{job_id}", response_model=JobOut)
async def get_job(
    job_id: str,
    request: Request,
    auth: AuthContext = Depends(get_auth),
) -> JobOut:
    """异步任务进度。

    job 状态存 Redis，TTL 24h（``storage.redis.job_ttl_seconds``）。
    过期后返回 404 而不是"未知状态"——含糊的响应会让客户端一直轮询。

    TODO: 实现。
    """

    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="not implemented"
    )


# ---------------------------------------------------------------------------
# 内部端点（service-to-service）
# ---------------------------------------------------------------------------
@router.post("/internal/dispatch/approved", response_model=list[DispatchResultOut])
async def on_approval(
    payload: ApprovalCallbackRequest,
    request: Request,
    auth: AuthContext = Depends(require_internal),
) -> list[DispatchResultOut]:
    """workflow-engine 审批结果回调，补发被扣住的敏感内容。

    必须**幂等**：审批系统的回调可能重复投递。用 ``run_id`` 判重，
    重复回调直接返回上次结果，否则同一份敏感纪要会被发两遍。

    TODO: 实现。
    """

    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="not implemented"
    )
