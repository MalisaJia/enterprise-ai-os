"""决策抽取。

核心难点：区分「已定」与「只是讨论过」
--------------------------------------
"我觉得应该用方案 A" —— 不是决策，是意见。
"那就按方案 A 做"     —— 是决策。
"方案 A 看起来不错"   —— 不是决策。
"OK 就 A，张三你安排" —— 是决策。

这个判别没有可靠的字面特征，必须靠语义理解，且是本组件最需要真实数据
调优的部分。因此：

- 阈值设 0.8（明显高于 actions 的 0.6）。宁可少抽。
- ``decided_by`` 设为必需字段。找不到决策人时模型会倾向于降低置信度，
  正好达到保守的效果——这是一道很有效的间接过滤。
- 保留 ``timestamp``，让争议可以回听原话解决。

抽错的代价与待办不对称：错误的决策记录会成为后续所有讨论的依据，
下次开会时有人拿着不存在的"上次结论"当前提，争议只会更大。
而一条多抽的待办删掉就好。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Sequence

from ..summarization.summarizer import MeetingSummary
from ..transcription.asr import Transcript

logger = logging.getLogger(__name__)

__all__ = [
    "DecisionCategory",
    "Decision",
    "DecisionExtractor",
    "PROMPT_DECISION_EXTRACT",
    "PROMPT_SENSITIVE_CONFIRM",
]


class DecisionCategory(str, Enum):
    """决策类别。

    分类的实际用途是决定**下游动作**，不是为了归档好看：
    - ``rule`` 会被推送到 memory-service 的 team 层（进待确认队列）
    - ``one_off`` 不进记忆——一次性决策进记忆会把记忆库塞满噪音
      （"这版就先这样发"没有长期价值）
    - ``sensitive`` 需走 workflow-engine 审批才能对外通告
    """

    RULE = "rule"                # 规范/约定，有长期效力
    ONE_OFF = "one_off"          # 一次性决策
    DIRECTION = "direction"      # 方向性选择（技术选型、优先级）
    RESOURCE = "resource"        # 资源投入（人力、预算）
    SENSITIVE = "sensitive"      # 敏感决策，需审批


@dataclass
class Decision:
    """一条会议决策。"""

    id: str
    meeting_id: str
    tenant_id: str

    # ---- 必需字段 ----
    statement: str                       # 结论本身
    decided_by: str                      # 决策人（说话人 label 或 user_id）
    rationale: str                       # 依据

    category: DecisionCategory = DecisionCategory.ONE_OFF
    confidence: float = 0.0

    # ---- 可选字段 ----
    # 被否掉的方案。记录它的价值常被低估：
    # 下次有人提出同一个方案时，能直接说明"上次为什么没选它"，
    # 避免把同一个讨论重来一遍。
    alternatives: list[str] = field(default_factory=list)
    impact_scope: str = ""
    # 在会议中的时间点，供回听核对。
    # 这是解决"系统说这是决策但我不记得定了"争议的唯一办法。
    timestamp: float | None = None
    # 原话引用。比时间戳更直接——争议时先看原话，不用真去回听。
    quote: str = ""

    decided_by_user_id: str | None = None
    # 敏感判定结果与命中的关键词，便于审计"为什么这条要走审批"
    is_sensitive: bool = False
    sensitive_reason: str = ""

    version: int = 1
    metadata: dict[str, Any] = field(default_factory=dict)


# 抽取 prompt。
#
# 设计要点：
# 1. 用正反例说明"已定 vs 讨论过"。这是最关键的一段——
#    不给例子模型会把大量意见表达当成决策。
# 2. 要求引用原话（quote）。它有双重作用：可审计，
#    以及**约束模型不要编造**——必须能从原文找到对应句子，
#    幻觉出的"决策"通常找不到可引用的原话。
# 3. 明确"宁可漏抽"。与 actions 的 prompt 取向相反，这是刻意的。
PROMPT_DECISION_EXTRACT = """\
下面是一场会议的转录片段（带说话人与时间）。请抽取其中**已经达成的决策**。

什么算决策（必须已经定下来）：
- "那就按方案 A 做" —— 是
- "OK，就用 PostgreSQL，张三你来搭" —— 是
- "我觉得应该用方案 A" —— 不是，这是意见
- "方案 A 看起来不错" —— 不是
- "要不要考虑 A？" —— 不是
- "下次再定" —— 不是

要求：
- 每条决策必须能找到明确的决策人。找不到就不要抽。
- 必须引用支撑该决策的原话（quote），从转录中原样摘取。
- 宁可漏抽也不要多抽。不确定是否已定的，不要输出。
- rationale 写清"为什么这么定"，转录中没提到依据就留空字符串。

会议片段：
{chunk}

输出 JSON 数组，每项：
{{"statement": "", "decided_by": "说话人标识", "rationale": "",
  "alternatives": [""], "impact_scope": "",
  "category": "rule|one_off|direction|resource",
  "quote": "原话", "timestamp": 秒数, "confidence": 0~1}}
"""


# 敏感判定的二级确认 prompt。
#
# 为什么要两级：只用关键词会大量误判（"预算"出现在闲聊里也命中），
# 只用 LLM 则每条决策都要多一次调用。关键词粗筛 + LLM 精判最经济。
PROMPT_SENSITIVE_CONFIRM = """\
判断下面这条会议决策是否属于敏感事项（涉及人事、薪酬、预算、并购、
对外承诺等，对外通告需要审批）。

决策：{statement}
依据：{rationale}
影响范围：{impact_scope}

只输出 JSON：{{"sensitive": true|false, "reason": "简要理由"}}
"""


class DecisionExtractor:
    """决策抽取器。"""

    def __init__(self, model_client: Any, config: dict[str, Any] | None = None) -> None:
        self.model_client = model_client
        cfg = config or {}
        # 明显高于 actions 的阈值，见模块 docstring。
        self.min_confidence: float = float(cfg.get("min_confidence", 0.8))
        self.max_per_meeting: int = int(cfg.get("max_per_meeting", 20))
        sensitive = cfg.get("sensitive", {}) or {}
        self.sensitive_enabled: bool = bool(sensitive.get("enabled", True))
        self.sensitive_keywords: list[str] = list(sensitive.get("keywords", []))
        self.sensitive_llm_confirm: bool = bool(sensitive.get("llm_confirm", True))

    async def extract(
        self, transcript: Transcript, summary: MeetingSummary | None = None
    ) -> list[Decision]:
        """抽取决策。

        为什么同时传 transcript 和 summary：
        - 从 **transcript** 抽，因为决策的判别依赖原话的语气与措辞，
          摘要已经把"那就这样吧"抹平成"确定了方案"，判别信号丢失了。
        - 用 **summary.topics** 做分段依据，让每次抽取的输入是一个完整议题
          而非机械切分的片段。跨议题的机械切分会把一个决策的
          结论与依据切到两段里。

        summary 为 None 时退化为按 token 切分——可行但效果差些。

        TODO: 实现。
        """

        raise NotImplementedError

    async def extract_from_chunk(self, chunk_text: str) -> list[dict[str, Any]]:
        """从单个片段抽取，返回原始 JSON。

        实现要点：
        - JSON 解析失败时返回空列表并记 warning，**不抛异常**。
          一个片段抽取失败不该让整场会议的处理失败——
          其他片段的结果仍然有价值。
        - 校验 ``quote`` 是否真的出现在原文里。找不到的说明模型在编，
          这类结果应当丢弃或大幅降低置信度。这是最有效的反幻觉手段。

        TODO: 实现。
        """

        raise NotImplementedError

    async def dedup(self, decisions: Sequence[Decision]) -> list[Decision]:
        """去重。

        同一决策可能在多个片段被抽出（议题有重叠，或结尾又复述了一遍）。
        合并时保留**置信度更高**的那条，并合并 alternatives。

        注意不要按 statement 字面去重：同一决策的两次表述措辞可能不同
        （"用 PG" vs "数据库选 PostgreSQL"），字面比较会漏掉。
        应当用语义相似度。

        TODO: 实现。
        """

        raise NotImplementedError

    async def classify(self, decision: Decision) -> DecisionCategory:
        """判定类别。

        ``rule`` 的判定要保守：只有明确表述为"以后都这样"的才算。
        把一次性决策误判为 rule 会让它进入团队记忆，
        从此影响所有后续会话——而当事人根本不知道自己立了条规矩。

        TODO: 实现。
        """

        raise NotImplementedError

    async def check_sensitive(self, decision: Decision) -> tuple[bool, str]:
        """敏感判定，返回 ``(是否敏感, 理由)``。

        两级：关键词粗筛 → LLM 精判。
        粗筛未命中直接返回 False，不调 LLM。

        判定为敏感的决策，分发时必须走 workflow-engine 审批。
        会议里可能定下涉及人事、预算、对外承诺的事项，
        这类决策的通告范围一旦搞错，后果不是"发错消息"而是合规事故。

        TODO: 实现。
        """

        raise NotImplementedError

    async def resolve_decider(
        self, decisions: Sequence[Decision], transcript: Transcript
    ) -> list[Decision]:
        """把 decided_by 从说话人 label 映射到 user_id。

        映射失败时保留 label 不写 user_id。与 actions 的责任人解析不同，
        决策人映射失败的影响较小——决策记录仍然可读可用，
        只是无法在系统里关联到人。不必因此丢弃这条决策。

        TODO: 实现。
        """

        raise NotImplementedError
