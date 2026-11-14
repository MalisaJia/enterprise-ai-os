"""Action Item 抽取。

核心难点有两个，都不在"识别出这是个任务"上
------------------------------------------
**1. 责任人归属。** 转录里出现的是人名甚至代称（"你跟一下"、"运维那边处理"），
要映射到系统里的 user_id。映射不上时**绝不能猜**：把任务派给错误的人，
对方会困惑，真正的责任人还不知情，两边都出问题。留空待指派是更好的选择。

**2. 隐式截止时间。** "下周三之前"、"月底"、"这周内" 在会议里极其常见，
必须能算成具体日期，否则待办进了任务系统也不会有提醒。
而基准日期必须是**会议日期**而非处理日期——会后第二天才处理录音时，
"明天"指的是会议的明天。用处理日期会让所有截止时间整体偏移一天，
这个 bug 极其隐蔽，因为每条都错得一样多，看不出异常。

取舍方向与 decisions 相反
-------------------------
阈值设 0.6（低于 decisions 的 0.8），宁可多抽。
漏一个任务的代价是"事情就是没人做"；多一个可以一键删掉。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any, Sequence

from ..summarization.summarizer import MeetingSummary
from ..transcription.asr import Transcript

logger = logging.getLogger(__name__)

__all__ = [
    "ActionPriority",
    "ActionStatus",
    "ActionItem",
    "ActionExtractor",
    "DueDateResolver",
    "AssigneeResolver",
    "PROMPT_ACTION_EXTRACT",
]


class ActionPriority(str, Enum):
    """优先级。抽不出时用 NORMAL，不要猜 HIGH。"""

    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"


class ActionStatus(str, Enum):
    """Action Item 在本服务内的状态。

    注意这不是任务的执行状态——那是 octo-matter 的职责。
    这里只跟踪"从抽取到分发"这段生命周期。
    两边各存一份执行状态必然不同步。
    """

    EXTRACTED = "extracted"              # 已抽取，待确认
    NEEDS_ASSIGNEE = "needs_assignee"    # 责任人未解析，待人工指派
    CONFIRMED = "confirmed"              # 人工确认过
    DISPATCHED = "dispatched"            # 已创建到 octo-matter
    DISCARDED = "discarded"              # 人工判定为误抽


@dataclass
class ActionItem:
    """一条 Action Item。"""

    id: str
    meeting_id: str
    tenant_id: str

    # ---- 必需字段 ----
    task: str                            # 做什么
    assignee: str                        # 责任人（说话人 label 或人名原文）

    assignee_user_id: str | None = None
    assignee_name: str | None = None
    # 责任人解析依据与置信度，便于排查"为什么派给了这个人"
    assignee_resolved_by: str = ""
    assignee_confidence: float = 0.0

    # ---- 可选字段 ----
    due_date: date | None = None
    # 原始的时间表述（"下周三之前"）。
    # 必须保留：解析错了才能发现——只存解析后的日期，
    # 就无法知道"这个日期是从哪句话算出来的"。
    due_date_raw: str = ""
    acceptance: str = ""                 # 验收标准
    priority: ActionPriority = ActionPriority.NORMAL
    depends_on: list[str] = field(default_factory=list)

    confidence: float = 0.0
    timestamp: float | None = None
    quote: str = ""                      # 原话，反幻觉 + 可审计

    status: ActionStatus = ActionStatus.EXTRACTED
    # 分发后回填的外部任务引用
    external_ref: str | None = None
    # 是否被人工修正过。
    # 这个字段撑起最重要的质量指标：修正率 = edited / extracted。
    # 它是抽取质量唯一的真实信号，自动评测测不出来。
    edited: bool = False

    version: int = 1
    created_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


# 抽取 prompt。
#
# 与 decisions 的 prompt 取向相反：这里鼓励多抽。
# 但"多抽"不等于"编造"——仍然要求 quote 必须来自原文。
# 区别在于对"不确定是否算任务"的处理：decisions 让它丢弃，
# 这里让它抽出来但降低置信度，交给人筛。
PROMPT_ACTION_EXTRACT = """\
下面是一场会议的转录片段（带说话人与时间）。请抽取其中的待办事项。

什么算待办：
- "张三你把接口文档补一下" —— 是
- "这个我来跟" —— 是（责任人是说这句话的人）
- "下周之前得把压测跑完" —— 是（责任人可能需要从上下文判断）
- "我们应该更重视测试" —— 不是，这是观点没有具体动作
- "已经做完了" —— 不是，这是进度汇报

要求：
- assignee 填转录中出现的人名或说话人标识。
  实在判断不出责任人时填空字符串，**不要猜**。
- due_date_raw 原样保留时间表述（"下周三之前"），不要自己换算日期。
- 同一件事在会议里被提到多次时只输出一条。
- 不确定是否算待办的，仍然输出但把 confidence 调低。

会议片段：
{chunk}
会议日期：{meeting_date}

输出 JSON 数组，每项：
{{"task": "", "assignee": "", "due_date_raw": "", "acceptance": "",
  "priority": "high|normal|low", "quote": "原话",
  "timestamp": 秒数, "confidence": 0~1}}
"""


class DueDateResolver:
    """自然语言时间 → 具体日期。

    基准日期必须可注入，不能用 ``date.today()``：
    1. 会后第二天才处理录音时，"明天"指会议的明天。
    2. 单测需要固定基准，否则测试会随运行日期变化而失败。
    """

    def __init__(self, timezone: str = "Asia/Shanghai", base: str = "meeting_date") -> None:
        self.timezone = timezone
        self.base = base

    def resolve(self, raw: str, base_date: date) -> date | None:
        """解析时间表述，无法解析时返回 None。

        **无法解析时不猜默认值。** 编一个截止时间比没有截止时间更糟：
        它会触发错误的逾期提醒，让责任人被无端催促，
        几次之后就没人再信任这些提醒了。

        需要处理的中文表述（dateparser 对中文支持有限，需补规则）：
        - 相对日："今天/明天/后天/下周三/下个月初"
        - 期限："X 之前/X 前/截止 X"
        - 模糊期："月底/季度末/年前"→ 取该周期的最后一个工作日
        - 时长："三天内/两周内"

        "本周五" 这类还要注意周起始约定：会议在周六说"本周五"
        通常指下一个周五，按 ISO 周算会得到已过去的日期。

        TODO: 实现，用 dateparser + 中文规则补充，写全上述用例的测试。
        """

        raise NotImplementedError


class AssigneeResolver:
    """人名/代称 → user_id。

    可靠性依次递减的三条依据：
    1. **参会人列表**。会议里说的"张三"几乎一定是在场的张三，
       这是最强的先验。
    2. **说话人身份**。"这个我来跟"的责任人就是当前说话人。
    3. **组织目录**（octo-auth）。兜底，用于提到不在场的人。
    """

    def __init__(self, config: dict[str, Any] | None = None, directory_client: Any | None = None) -> None:
        cfg = config or {}
        self.use_participant_list: bool = bool(cfg.get("use_participant_list", True))
        self.use_directory: bool = bool(cfg.get("use_directory", True))
        self.fuzzy_threshold: float = float(cfg.get("fuzzy_threshold", 0.9))
        self.on_unresolved: str = str(cfg.get("on_unresolved", "unassigned"))
        self.directory_client = directory_client

    async def resolve(
        self, action: ActionItem, transcript: Transcript, participants: Sequence[dict[str, Any]]
    ) -> ActionItem:
        """解析责任人，回填 user_id 与置信度。

        几个必须处理的情况：

        - **"我来"**：责任人是当前说话人。要靠 timestamp 找到对应分段
          的说话人，再映射到 user_id。这条覆盖了相当比例的待办。
        - **同名**：参会人里有两个"张三"时不能任选一个，
          置信度置低留待人工。
        - **模糊匹配的边界**："张三"匹配"张三丰"的相似度很高但显然不是同一人。
          阈值设 0.9 且要求**长度相近**——只靠相似度会把短名字
          匹配到包含它的长名字上。
        - **团队而非个人**（"运维那边处理"）：不要硬映射到某个人，
          置为 unassigned 并在 task 里保留"运维"这个线索，让人来指派。

        ``on_unresolved`` 三种策略：
        - ``unassigned``（推荐）：留空并标记 NEEDS_ASSIGNEE
        - ``organizer``：派给组织者兜底
        - ``drop``：丢弃（不推荐，会丢事情）

        TODO: 实现。
        """

        raise NotImplementedError


class ActionExtractor:
    """Action Item 抽取器。"""

    def __init__(
        self,
        model_client: Any,
        config: dict[str, Any] | None = None,
        assignee_resolver: AssigneeResolver | None = None,
        due_date_resolver: DueDateResolver | None = None,
    ) -> None:
        self.model_client = model_client
        cfg = config or {}
        # 低于 decisions 的阈值，见模块 docstring。
        self.min_confidence: float = float(cfg.get("min_confidence", 0.6))
        self.max_per_meeting: int = int(cfg.get("max_per_meeting", 50))
        dedup = cfg.get("dedup", {}) or {}
        self.dedup_enabled: bool = bool(dedup.get("enabled", True))
        self.dedup_threshold: float = float(dedup.get("similarity_threshold", 0.85))
        self.assignee_resolver = assignee_resolver
        self.due_date_resolver = due_date_resolver

    async def extract(
        self,
        transcript: Transcript,
        summary: MeetingSummary | None = None,
        meeting_date: date | None = None,
        participants: Sequence[dict[str, Any]] = (),
    ) -> list[ActionItem]:
        """抽取 Action Item。

        流程::

            1. 按议题分段（无 summary 时按 token 切）
            2. 逐段抽取（可并发，但要控并发度）
            3. 去重
            4. 解析责任人
            5. 解析截止时间（基准 = meeting_date）
            6. 置信度过滤
            7. 校验 quote 是否来自原文（反幻觉）

        第 4、5 步在过滤**之前**：一条置信度 0.65 的待办如果责任人和
        截止时间都解析成功了，实际可用性远高于置信度 0.8 但责任人未知的。
        先解析再过滤，能让后续的排序更合理。

        TODO: 实现。
        """

        raise NotImplementedError

    async def extract_from_chunk(self, chunk_text: str, meeting_date: date) -> list[dict[str, Any]]:
        """从单个片段抽取，返回原始 JSON。

        JSON 解析失败时返回空列表并记 warning，不抛异常——
        一个片段失败不该让整场会议的处理失败。

        TODO: 实现。
        """

        raise NotImplementedError

    async def dedup(self, actions: Sequence[ActionItem]) -> list[ActionItem]:
        """去重。

        同一件事在会议里常被提到多次（"张三跟一下" ... 结尾又说
        "张三别忘了那个事"），且片段有重叠会重复抽出。

        合并规则：
        - 保留置信度更高的
        - 截止时间取**更早**的（更保守，不会让人错过）
        - 责任人不一致时**不合并**，两条都留并标记冲突。
          这种情况通常意味着会议中改过责任人，需要人来判断哪个是最终的。
          自动选一个有 50% 概率派错人。

        TODO: 实现，用语义相似度而非字面比较。
        """

        raise NotImplementedError

    def merge_edits(self, action: ActionItem, edits: dict[str, Any]) -> ActionItem:
        """应用人工修正，并置 ``edited=True``。

        ``edited`` 标记必须打上：修正率（edited/extracted）是抽取质量
        唯一的真实信号。自动评测测不出"这个责任人对不对"，
        只有人工修正行为能告诉你。不打标记就永远拿不到这个反馈。

        TODO: 实现。
        """

        raise NotImplementedError
