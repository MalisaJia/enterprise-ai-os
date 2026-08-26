"""会议摘要生成。

流程::

    Transcript
      │
      ├─ 1. 按 token 切段（不按字符！中英 token 密度差三倍）
      │     切点尽量落在说话人切换处，避免把一次完整发言切断
      │
      ├─ 2. map：逐段摘要（便宜模型，可并发）
      │
      ├─ 3. reduce：归并（中等模型，段数多时树形多轮归并）
      │
      └─ 4. 从归并结果 + 原始分段产出：
            overview / topics / disagreements / next_steps

第 4 步为什么要回看原始分段：归并后的摘要已经损失了时间戳，
而议题切分需要起止时间（供回听核对）。只靠摘要文本无法定位时间，
必须结合原文。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Sequence

from ..transcription.asr import Transcript, TranscriptSegment

logger = logging.getLogger(__name__)

__all__ = [
    "Topic",
    "Disagreement",
    "MeetingSummary",
    "Summarizer",
    "PROMPT_CHUNK_SUMMARY",
    "PROMPT_MERGE_SUMMARY",
]


# 分段摘要 prompt。
#
# 关键要求："保留原话中的人名与时间表述"。
# 摘要模型天然倾向于把"张三说下周三之前给出方案"压缩成
# "确定了方案时间"——信息量看起来还在，但责任人与截止时间没了，
# 后续的 Action Item 抽取就无从下手。摘要不是压缩比竞赛。
PROMPT_CHUNK_SUMMARY = """\
下面是一场会议的一个片段的转录（带说话人）。请提炼这个片段的要点。

要求：
- 保留人名、时间表述、具体数字，不要泛化成"相关人员"、"近期"。
- 区分"已经确定的"和"还在讨论的"，用不同措辞表达。
- 不要补充转录中没有的信息。

片段转录：
{chunk}

输出要点（每条一行，不超过 8 条）：
"""


# 归并 prompt。
#
# 关键要求："识别前后矛盾并保留后者"。
# 会议中改主意是常态：前半段说交给张三，后半段改成李四。
# 归并时若把两者并列，最终纪要会同时出现两个责任人。
# 必须显式要求模型识别这种覆盖关系。
PROMPT_MERGE_SUMMARY = """\
下面是同一场会议若干片段的要点，按时间顺序排列。请归并成完整的会议摘要。

要求：
- 按议题组织，不要按时间罗列。
- 前后矛盾时以**较晚**的表述为准，并在摘要中体现这是调整后的结论。
- 明确区分三类内容：已达成的结论 / 讨论过但未定的分歧 / 待办事项。
- 未定的分歧必须保留，不要因为"没有结论"就省略掉。

片段要点：
{chunks}

输出 JSON：
{{"overview": "一段话概要",
  "topics": [{{"title": "", "points": [""], "conclusion": "" }}],
  "disagreements": [{{"topic": "", "description": "", "positions": [""] }}],
  "next_steps": [""]}}
"""


@dataclass
class Topic:
    """一个议题。"""

    title: str
    points: list[str] = field(default_factory=list)
    # 结论。空字符串表示没有结论——这是有意义的信息，不要用 None 混淆。
    conclusion: str = ""
    # 起止时间，供回听核对。归并后需要回看原文才能定位。
    start: float = 0.0
    end: float = 0.0
    speaker_labels: list[str] = field(default_factory=list)


@dataclass
class Disagreement:
    """一个未达成一致的分歧点。

    为什么要单独抽这个
    ------------------
    只记录结论的纪要会掩盖真实状态。一个议题"没定"和"定了"对后续工作的
    影响完全不同，而传统纪要往往把没定的部分省略掉，导致所有人以为已经
    有结论——直到下次开会才发现大家理解不一致。

    显式记录分歧是让纪要可信的关键，也是本组件区别于通用摘要工具的地方。
    """

    topic: str
    description: str
    # 各方立场。带上立场而不只是"存在分歧"，
    # 下次开会才能直接从分歧点继续，不必从头重议。
    positions: list[str] = field(default_factory=list)
    confidence: float = 0.0
    start: float = 0.0
    end: float = 0.0


@dataclass
class MeetingSummary:
    """会议摘要的完整产出。"""

    meeting_id: str
    tenant_id: str
    overview: str = ""
    topics: list[Topic] = field(default_factory=list)
    disagreements: list[Disagreement] = field(default_factory=list)
    # 下一步。区别于 Action Item：这些没有明确责任人，
    # 只是方向性的"接下来要做什么"。混在一起会让待办列表里
    # 出现一堆无法执行的条目。
    next_steps: list[str] = field(default_factory=list)

    # 生成参数记录。
    # prompt 与模型会持续迭代，对比不同版本效果时必须知道
    # 这份摘要是用什么配置生成的——否则无法归因。
    model: str = ""
    strategy: str = "map_reduce"
    chunk_count: int = 0
    merge_depth: int = 0
    version: int = 1
    tokens_used: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


class Summarizer:
    """会议摘要生成器。"""

    def __init__(self, model_client: Any, config: dict[str, Any] | None = None) -> None:
        self.model_client = model_client
        cfg = config or {}
        self.strategy: str = str(cfg.get("strategy", "map_reduce"))
        self.chunk_max_tokens: int = int(cfg.get("chunk_max_tokens", 6000))
        self.chunk_overlap_tokens: int = int(cfg.get("chunk_overlap_tokens", 300))
        self.max_merge_depth: int = int(cfg.get("max_merge_depth", 3))
        self.min_topic_seconds: int = int(cfg.get("min_topic_seconds", 60))

    async def summarize(self, transcript: Transcript) -> MeetingSummary:
        """生成完整摘要。

        短会（token 数低于 chunk_max_tokens）走 single_pass，
        不必强行分段——分段归并会额外损失一层信息，短会没必要付这个代价。

        TODO: 实现。
        """

        raise NotImplementedError

    def chunk(self, transcript: Transcript) -> list[list[TranscriptSegment]]:
        """按 token 数切段。

        两条约束：

        1. **按 token 而非字符**。中文约 1.5 字/token，英文约 0.75 词/token，
           按字符切会让纯英文段落的实际 token 数超出预期两倍，直接超上下文。
        2. **切点尽量落在说话人切换处**。把一个人的连续发言切成两段，
           会让两段各自缺少上下文——前半段不知道结论，后半段不知道前提。

        重叠（overlap）用于缓解切点处的上下文丢失，但不能替代第 2 条：
        重叠只是让模型多看几句，说话人切换处仍是更自然的边界。

        TODO: 实现。
        """

        raise NotImplementedError

    async def summarize_chunk(self, segments: Sequence[TranscriptSegment], index: int) -> str:
        """摘要单段（map 阶段）。

        可以并发。但并发度要受控——一场长会可能切出十几段，
        全部并发会瞬间打满 model-gateway 的租户限流，
        导致本会议的请求互相排队，反而更慢。

        TODO: 实现，用 Semaphore 控制并发。
        """

        raise NotImplementedError

    async def merge(self, chunk_summaries: Sequence[str], depth: int = 0) -> dict[str, Any]:
        """归并（reduce 阶段），返回结构化摘要。

        段数很多时需要树形多轮归并：先两两归并，再归并归并结果。
        ``max_merge_depth`` 是防止极端长会导致无限递归的兜底。

        归并的核心难点是**处理前后矛盾**：会议中改主意是常态，
        前半段说交给张三后半段改成李四。归并时若并列保留，
        最终纪要会同时出现两个责任人，读者不知道信哪个。
        prompt 里必须显式要求"以较晚的表述为准"。

        TODO: 实现。归并到最后一轮时才要求输出 JSON，
              中间轮次输出自然语言——中间轮强制 JSON 会让模型
              把精力花在格式上，归并质量下降。
        """

        raise NotImplementedError

    async def split_topics(self, transcript: Transcript, merged: dict[str, Any]) -> list[Topic]:
        """把归并结果对回原文，补上议题的起止时间。

        做法：用议题标题与要点去原始分段里做语义匹配，取匹配区间的
        首尾时间。不必精确——回听核对时前后多几十秒无妨，
        但完全没有时间戳就无法核对。

        ``min_topic_seconds`` 过滤掉太碎的切分：
        一分钟以内的"议题"通常只是插话，单独列出会让议题列表失去可读性。

        TODO: 实现。
        """

        raise NotImplementedError

    async def rolling_summary(
        self, meeting_id: str, segments: Sequence[TranscriptSegment], previous: str = ""
    ) -> str:
        """滚动摘要，供会议中实时查看。

        与批量摘要的区别：这里是增量的——把新分段并入已有摘要，
        而不是重新摘要全部内容。重新摘要会让每次更新的成本随会议时长
        线性增长，两小时会议的最后一次更新要处理全部 3 万字。

        注意滚动摘要**不用于抽取**。会议中的表述随时被推翻，
        基于滚动摘要抽任务会派出马上作废的任务。

        TODO: 实现。
        """

        raise NotImplementedError

    async def detect_disagreements(
        self, transcript: Transcript, merged: dict[str, Any]
    ) -> list[Disagreement]:
        """识别分歧点。

        判定要点：一个议题有多个立场被表述、且没有出现收敛信号
        （"那就这样"、"同意"、"就按 A 来"）时，算分歧。

        容易误判的情况：有人提出反对后被说服了，收敛信号可能很弱
        （只是没再反对）。这种会被误判为分歧。宁可误判也不要漏判——
        把已达成的共识误记为分歧，代价只是下次会议多花一分钟确认；
        把分歧误记为共识，代价是两拨人按不同理解各做了一周。

        TODO: 实现。
        """

        raise NotImplementedError
