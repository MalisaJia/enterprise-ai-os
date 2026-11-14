"""分发编排。

五个目标，各自独立
------------------
| 目标 | 送什么 | 通道 | 失败的影响 |
|---|---|---|---|
| ``octo_matter`` | Action Item → 任务 | HTTP | 待办不落地，本组件核心价值失效 |
| ``octo_adapters`` | 纪要 + @责任人 | HTTP | 没人知道会开完了 |
| ``rag_pipeline`` | 纪要文档 → 检索库 | Kafka | 下次会议查不到"上次结论" |
| ``memory_service`` | rule 类决策 → team 层 | HTTP | 团队约定不入记忆 |
| ``workflow_engine`` | 敏感决策 → 审批流 | HTTP | 敏感内容阻塞在待审批 |

为什么必须**逐目标独立重试**
---------------------------
这是本模块最容易写错的地方。若把五个目标当成一个事务整体重试：
任务创建成功但 IM 推送失败时，整体重试会**把任务再创建一遍**。
用户看到两个一模一样的待办，且第二个的创建时间还比第一个晚——
比不分发更糟，因为它让人怀疑整个系统的可靠性。

因此：每个目标各自持久化状态与幂等键，重试只重试 ``failed`` 的目标。
幂等键含 ``version``，让 ``/reprocess`` 产生的新版本能正常分发，
而同一版本的重试始终命中同一个键。

顺序上的一个考量
----------------
``octo_matter`` 必须在 ``octo_adapters`` **之前**：IM 消息里要带任务链接，
先发消息就只能发一条没有链接的、用户点不动的纪要。
而 ``rag_pipeline``/``memory_service`` 与前两者无依赖，可以并发。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Sequence

from ..extraction.actions import ActionItem
from ..extraction.decisions import Decision
from ..summarization.summarizer import MeetingSummary

logger = logging.getLogger(__name__)

__all__ = [
    "DispatchTarget",
    "DispatchOutcome",
    "DispatchResult",
    "DispatchPlan",
    "Dispatcher",
    "MinutesRenderer",
]


class DispatchTarget(str, Enum):
    """分发目标。取值与 config 里 ``dispatch.targets`` 的键一一对应。"""

    OCTO_MATTER = "octo_matter"
    OCTO_ADAPTERS = "octo_adapters"
    RAG_PIPELINE = "rag_pipeline"
    MEMORY_SERVICE = "memory_service"
    WORKFLOW_ENGINE = "workflow_engine"


class DispatchOutcome(str, Enum):
    """单个目标的分发结果。"""

    SUCCESS = "success"
    FAILED = "failed"                # 可重试
    SKIPPED = "skipped"              # 未启用，或没有需要送的内容
    PENDING_APPROVAL = "pending_approval"   # 已提交审批，等回调
    # 重试次数耗尽。与 FAILED 分开是为了让告警只盯这一种——
    # 中间态的 FAILED 会自愈，盯它会产生大量噪音告警。
    EXHAUSTED = "exhausted"


@dataclass
class DispatchResult:
    """单个目标的分发结果记录。每个目标一条，独立持久化。"""

    target: DispatchTarget
    outcome: DispatchOutcome
    idempotency_key: str = ""
    attempts: int = 0
    # 外部系统返回的引用（任务 id、消息 id、审批 run_id）。
    # 必须存：重试时先查它，存在就说明上次其实成功了，直接标记成功。
    # 这是幂等的兜底手段——外部系统不一定支持幂等键。
    external_refs: list[str] = field(default_factory=list)
    error: str = ""
    started_at: datetime | None = None
    finished_at: datetime | None = None
    # 下次重试时间。由调度器扫描，不占用请求线程。
    next_retry_at: datetime | None = None


@dataclass
class DispatchPlan:
    """分发计划：确认前先让人看清"到底要做什么"。

    存在的理由是人工确认这一步。让用户在一个"确认分发"按钮上
    做决定，却看不到会发生什么，等于让他盲签。
    计划里要明确列出：创建几个任务、发到哪几个群、哪些决策要走审批。
    """

    meeting_id: str
    tenant_id: str
    version: int
    targets: list[DispatchTarget] = field(default_factory=list)
    action_count: int = 0
    decision_count: int = 0
    # 需要走审批的敏感决策 id。单独列出——这是用户最需要注意的部分。
    sensitive_decision_ids: list[str] = field(default_factory=list)
    # 责任人未解析的待办数。也要单独提示：
    # 用户看到"3 条待指派"时往往会先去补责任人再分发。
    unassigned_count: int = 0
    im_channels: list[str] = field(default_factory=list)
    requires_confirmation: bool = True


class MinutesRenderer:
    """纪要渲染（Jinja2）。

    模板放 config 而非硬编码：不同组织对纪要格式的偏好差异很大，
    改格式不应该需要改代码发版。
    """

    def __init__(self, templates: dict[str, str] | None = None) -> None:
        self.templates = templates or {}

    def render(
        self,
        fmt: str,
        meeting: dict[str, Any],
        summary: MeetingSummary,
        decisions: Sequence[Decision],
        actions: Sequence[ActionItem],
    ) -> str:
        """渲染纪要。

        实现要点：
        - **未达成一致的分歧点必须渲染出来。** 只列决策会让未参会的人
          以为所有议题都有了结论，下次开会时才发现有人还在等讨论。
        - 责任人未解析的待办渲染成"待指派"而不是省略。
          省略等于把问题藏起来，没人会去补。
        - 模板渲染要开 ``autoescape=False``（输出是 Markdown 不是 HTML）
          但要注意会议内容里的 ``{{`` 不会被当模板语法——
          用 ``Template(...).render()`` 而非字符串拼接。

        TODO: 实现，用 jinja2.Environment 预编译模板（每次都编译很浪费）。
        """

        raise NotImplementedError


class Dispatcher:
    """分发编排器。"""

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        http_client: Any | None = None,
        kafka_producer: Any | None = None,
        renderer: MinutesRenderer | None = None,
        store: Any | None = None,
    ) -> None:
        cfg = config or {}
        self.require_confirmation: bool = bool(cfg.get("require_confirmation", True))
        self.auto_dispatch_types: list[str] = list(cfg.get("auto_dispatch_types", []))
        self.targets_config: dict[str, Any] = cfg.get("targets", {}) or {}
        idem = cfg.get("idempotency", {}) or {}
        self.key_template: str = str(
            idem.get("key_template", "dispatch:{tenant_id}:{meeting_id}:{version}:{target}")
        )
        self.key_ttl_seconds: int = int(idem.get("ttl_seconds", 604800))
        retry = cfg.get("retry", {}) or {}
        self.max_attempts: int = int(retry.get("max_attempts", 5))
        self.initial_delay_seconds: int = int(retry.get("initial_delay_seconds", 30))
        self.max_delay_seconds: int = int(retry.get("max_delay_seconds", 1800))
        self.http_client = http_client
        self.kafka_producer = kafka_producer
        self.renderer = renderer
        self.store = store

    # ------------------------------------------------------------------
    # 计划与确认
    # ------------------------------------------------------------------

    async def plan(
        self,
        meeting: dict[str, Any],
        decisions: Sequence[Decision],
        actions: Sequence[ActionItem],
    ) -> DispatchPlan:
        """生成分发计划，不产生任何副作用。

        ``requires_confirmation`` 的判定：
        全局 ``require_confirmation`` 为 false → 不需要；
        为 true 但会议类型在 ``auto_dispatch_types`` 白名单里 → 也不需要。
        白名单机制让站会这类低风险会议先放开，逐步积累信心，
        而不是一刀切地全部要确认或全部自动。

        TODO: 实现。
        """

        raise NotImplementedError

    # ------------------------------------------------------------------
    # 执行
    # ------------------------------------------------------------------

    async def dispatch(
        self,
        meeting: dict[str, Any],
        summary: MeetingSummary,
        decisions: Sequence[Decision],
        actions: Sequence[ActionItem],
        version: int = 1,
        only_targets: Sequence[DispatchTarget] | None = None,
    ) -> list[DispatchResult]:
        """执行分发，每个目标一条结果。

        执行顺序::

            1. octo_matter        （先建任务，拿到任务链接）
            2. workflow_engine    （敏感决策提交审批）
            3. octo_adapters      （纪要推送，带任务链接）
            4. rag_pipeline / memory_service（可与 3 并发）

        关键约束：

        - **任一目标失败不影响其他目标。** 每个目标独立 try/except，
          失败只记录在自己的 DispatchResult 里。IM 服务挂了不该导致
          任务也没创建。
        - 敏感决策在审批通过前**不进入 octo_adapters 的推送内容**。
          这是最容易漏的一处：审批流起来了，但纪要已经把敏感内容
          发到群里了，审批就成了摆设。
        - ``only_targets`` 用于重试：只重跑失败的目标。

        TODO: 实现。
        """

        raise NotImplementedError

    async def retry_failed(self, meeting_id: str, tenant_id: str, version: int) -> list[DispatchResult]:
        """重试失败的目标。

        只重试 ``FAILED`` 的目标，跳过 ``SUCCESS``/``SKIPPED``/``EXHAUSTED``。
        重试前先用 ``external_refs`` 反查外部系统：若已存在，
        说明上次是"执行成功但响应丢失"，直接标记成功而不是重发。

        超过 ``max_attempts`` 转 ``EXHAUSTED`` 并告警。
        不无限重试——外部系统的配置错误（如 api_key 失效）
        重试一万次也不会成功，只会刷满日志掩盖真正的问题。

        TODO: 实现。
        """

        raise NotImplementedError

    def idempotency_key(
        self, tenant_id: str, meeting_id: str, version: int, target: DispatchTarget
    ) -> str:
        """生成幂等键。

        含 ``version``：``/reprocess`` 产生新版本时能正常分发，
        而同一版本的重试始终命中同一个键。
        若键里不含 version，重跑后的新结果会被当成重复请求丢弃。

        TODO: 按 key_template 格式化。
        """

        raise NotImplementedError

    # ------------------------------------------------------------------
    # 各目标
    # ------------------------------------------------------------------

    async def to_octo_matter(
        self, meeting: dict[str, Any], actions: Sequence[ActionItem], version: int
    ) -> DispatchResult:
        """Action Item → octo-matter 任务。

        实现要点：
        - ``external_ref`` 按模板回填会议与 action id。
          没有它，用户看到任务时无法确认"我真的答应过这件事吗"，
          可追溯性是这类自动创建任务被接受的前提。
        - ``create_unassigned: true`` 时未解析责任人的 action 也创建，
          派给组织者待指派。创建比丢弃好——事情至少不会消失。
        - **逐条创建但整体记一个 DispatchResult**：
          部分成功时把成功的 id 记进 ``external_refs``，
          重试时跳过已创建的。这里不能只记"成功/失败"，
          否则部分成功的重试会重复创建剩余任务之外的那些。

        TODO: 实现。
        """

        raise NotImplementedError

    async def to_octo_adapters(
        self,
        meeting: dict[str, Any],
        summary: MeetingSummary,
        decisions: Sequence[Decision],
        actions: Sequence[ActionItem],
        task_links: dict[str, str] | None = None,
    ) -> DispatchResult:
        """纪要 → IM（飞书/Slack）。

        实现要点：
        - ``aggregate: true`` 时整场会议合并为一条消息。
          逐条推送会刷屏，刷屏的结果是没人看——
          这不是体验问题，是功能失效。
        - ``format: card`` 时用飞书卡片：能折叠长内容，
          且能带"确认"/"这不是我的任务"交互按钮。
          交互回执是抽取质量的重要反馈来源。
        - ``mention_assignees: true`` 时 @ 到责任人。不 @ 等于没通知。
        - 敏感决策未审批通过时**不出现在推送内容里**。

        TODO: 实现。
        """

        raise NotImplementedError

    async def to_rag_pipeline(
        self, meeting: dict[str, Any], minutes_markdown: str, version: int
    ) -> DispatchResult:
        """纪要 → rag-pipeline（Kafka 摄入）。

        走 Kafka 而非 HTTP：摄入是异步的（切块 + embedding），
        让分发流程等它完成没有意义。

        ``doc_id`` 要用 ``meeting_id`` 而非随机 id，且带 version——
        ``/reprocess`` 后新纪要应当**替换**旧的而不是叠加。
        否则检索"上次结论"时会同时命中同一场会议的多个版本，
        模型看到互相矛盾的两份纪要，输出会很奇怪。

        TODO: 实现。
        """

        raise NotImplementedError

    async def to_memory_service(
        self, meeting: dict[str, Any], decisions: Sequence[Decision]
    ) -> DispatchResult:
        """rule 类决策 → memory-service 的 team 层。

        两处约束：
        - 只推 ``only_categories``（默认仅 ``rule``）。全推会让记忆库
          被一次性决策（"这版就先这样发"）塞满，检索质量急剧下降。
        - 写 team 层，它是 confirm 模式，进待确认队列而非直接生效。
          从一次会议讨论就推断出团队规范，可靠性不足以直接生效。

        调用的是 memory-service 的 ``/api/v1/memories``（scope=team），
        由它自己走两阶段确认流程，本组件不关心后续。

        TODO: 实现。
        """

        raise NotImplementedError

    async def to_workflow_engine(
        self, meeting: dict[str, Any], decisions: Sequence[Decision]
    ) -> DispatchResult:
        """敏感决策 → workflow-engine 审批流。

        只提交，不等待。返回 ``PENDING_APPROVAL``。
        审批通过后 workflow-engine 回调 ``callback_path``，
        本服务再补发这部分内容。

        为什么不自己做审批：审批涉及多级审批人、超时升级、
        代理审批、审批记录留档，这些 workflow-engine 已经做了。
        在这里重做一遍必然做成简化版，而合规场景恰恰不能简化。

        TODO: 实现。
        """

        raise NotImplementedError

    async def on_approval_result(
        self, run_id: str, approved: bool, meeting_id: str, tenant_id: str
    ) -> list[DispatchResult]:
        """审批回调处理。

        通过 → 补发被扣住的敏感内容；驳回 → 标记不分发并通知发起人。

        必须**幂等**：审批系统的回调可能重复投递。
        用 ``run_id`` 判重，重复回调直接返回上次结果。

        TODO: 实现。
        """

        raise NotImplementedError
