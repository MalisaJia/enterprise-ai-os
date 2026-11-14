"""整合判定与合并执行。

判定流程（沿用 mem0 的核心思想）::

    新候选
      │
      ├─ 1. 字面相似度预筛 ──── ≥0.95 ──→ NOOP（不调 LLM）
      │
      ├─ 2. 向量召回 top_k 条相近既有记忆（纯相似度排序，不掺 strength）
      │
      ├─ 3. 无候选 ──────────────────────→ ADD
      │
      └─ 4. 有候选 → LLM 判定 ───┬──→ ADD    （新信息，与旧记忆无冲突）
                                  ├──→ UPDATE （表达同一件事的新版本）
                                  ├──→ DELETE （新信息否定了旧记忆）
                                  └──→ NOOP   （已有等价记忆）

第 1 步能省掉相当比例的 LLM 调用——重复表述在真实对话里非常常见。
第 2 步不掺 strength 是关键：判断"是否矛盾"只取决于语义接近程度，
若把强度掺进来，一条弱的矛盾记忆会排不进候选，矛盾就永远发现不了。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Sequence

from ..memory import ConsolidationOp, Memory, MemoryCandidate, MemoryScope
from ..storage.vector import SearchHit, VectorMemoryStore

logger = logging.getLogger(__name__)

__all__ = [
    "ConsolidationDecision",
    "MemoryMerger",
    "PROMPT_CONSOLIDATION",
]


# 判定 prompt 模板。
#
# 关键设计：
# 1. 要求输出 reason。不是为了展示，而是为了让判定可审计——
#    影子模式下评估准确率时，光看 op 无法判断 LLM 是"想对了"还是"猜对了"。
# 2. 明确区分"补充"与"取代"。这是误判最集中的地方：
#    "我负责支付模块" + "我也负责结算模块" 是补充（ADD），
#    "我负责支付模块" + "我现在负责结算模块" 是取代（UPDATE）。
#    只差一个"现在"，语义完全不同，不在 prompt 里点明 LLM 会大量搞错。
# 3. 明确"不确定时返回 ADD"。这是有意的偏向：
#    多留一条记忆的代价（占点空间、可能有轻微矛盾）远小于误删的代价。
PROMPT_CONSOLIDATION = """\
你在维护一个用户记忆库。给定一条新记忆和若干条既有记忆，判断应执行的操作。

操作定义：
- ADD：新记忆包含既有记忆没有的信息，且不与它们矛盾。
- UPDATE：新记忆是某条既有记忆所述同一件事的更新版本（时间上更晚、更准确）。
- DELETE：新记忆明确否定了某条既有记忆（例如"我不再负责支付了"）。
- NOOP：新记忆与某条既有记忆语义等价，没有新增信息。

判断要点：
- 区分"补充"与"取代"。"我也负责结算"是补充（ADD）；
  "我现在负责结算"是取代（UPDATE，取代旧的负责范围）。
- 同类事实并列存在是正常的，不要因为主题相同就判 UPDATE。
- 不确定时返回 ADD。宁可多留一条，不要误删。

新记忆：
{candidate}

既有记忆（带编号）：
{existing}

只输出 JSON：
{{"op": "ADD|UPDATE|DELETE|NOOP", "target_index": 编号或 null, "reason": "简要理由"}}
"""


@dataclass
class ConsolidationDecision:
    """一次判定的结果。

    ``target`` 与 ``target_id`` 都保留：前者供调用方展示新旧对比
    （团队层确认界面需要），后者供执行时定位。只留 id 会让确认界面
    不得不再查一次库。
    """

    op: ConsolidationOp
    target: Memory | None = None
    target_id: str | None = None
    reason: str = ""
    # 判定来源：literal_dedup（预筛）/ no_candidate（无候选直接 ADD）/ llm
    # 记这个是为了在指标里区分"LLM 判的"和"规则判的"。
    # 若 llm 占比极低，说明预筛阈值定得太松，判定实际没在工作。
    decided_by: str = "llm"
    # 参与判定的候选，便于排查"为什么判成这样"
    candidates: Sequence[SearchHit] = ()
    # 影子模式下为 True
    shadowed: bool = False
    raw_response: dict[str, Any] = field(default_factory=dict)


class MemoryMerger:
    """整合判定器与合并执行器。

    与三个 Manager 的分工：Manager 决定"哪一层、要不要人工确认"，
    Merger 只回答"这条新记忆与既有记忆是什么关系"。
    Merger 不感知 scope 语义，这让它可以被三层共用并单独测试。
    """

    def __init__(
        self,
        store: VectorMemoryStore,
        model_client: Any,
        config: dict[str, Any] | None = None,
    ) -> None:
        self.store = store
        self.model_client = model_client
        cfg = config or {}
        on_write = cfg.get("on_write", {}) or {}
        self.candidate_top_k: int = int(on_write.get("candidate_top_k", 8))
        self.candidate_min_similarity: float = float(on_write.get("candidate_min_similarity", 0.6))
        literal = on_write.get("literal_dedup", {}) or {}
        self.literal_dedup_enabled: bool = bool(literal.get("enabled", True))
        self.literal_dedup_threshold: float = float(literal.get("threshold", 0.95))
        # 上线初期必须为 True。见模块 docstring 与 config 注释。
        self.shadow_mode: bool = bool(on_write.get("shadow_mode", True))

    # ---- 判定 -----------------------------------------------------------
    async def decide(
        self,
        tenant_id: str,
        scope: MemoryScope,
        scope_id: str,
        candidate: MemoryCandidate,
        embedding: list[float],
    ) -> ConsolidationDecision:
        """对单条候选做判定。

        实现顺序见模块 docstring 的流程图。三点注意：

        1. LLM 返回的 ``target_index`` 必须校验范围。越界或指向不存在的编号
           时应降级为 ADD 而不是抛异常——一次判定失败不该让整条抽取流程挂掉。
        2. 判定为 UPDATE/DELETE 但 target 的相似度低于阈值时，视为 LLM 幻觉，
           降级为 ADD。LLM 有时会"为了给出答案"随便指一条。
        3. ``json_mode`` 也可能返回非法 JSON，解析失败同样降级为 ADD。

        这三处都遵循同一原则：判定不可靠时偏向保守（ADD），
        因为误删记忆是不可逆且用户可感知的，多留一条不是。

        TODO: 实现。
        """

        raise NotImplementedError

    async def decide_batch(
        self,
        tenant_id: str,
        scope: MemoryScope,
        scope_id: str,
        candidates: Sequence[MemoryCandidate],
        embeddings: Sequence[list[float]],
    ) -> list[ConsolidationDecision]:
        """批量判定。

        必须**串行**：同一批候选里可能有两条彼此矛盾的记忆
        （抽取时上下文不同导致），并发判定时它们各自看不到对方，
        两条都会入库，矛盾被固化。串行让后一条能看到前一条的结果。

        TODO: 实现。
        """

        raise NotImplementedError

    def literal_similarity(self, a: str, b: str) -> float:
        """字面相似度（0~1），用于预筛。

        用 rapidfuzz 而非 difflib：预筛是 O(候选数) 的比对且在写入路径上，
        rapidfuzz 的 C 实现快两个数量级。

        中文需要先分词再比对，否则"我用 pnpm"和"我不用 pnpm"
        的字符级相似度高达 0.9，会被错误预筛为 NOOP——
        这是中文场景下最容易踩的坑，字符相似度对否定词几乎不敏感。

        TODO: 实现——jieba 分词后用 rapidfuzz.fuzz.token_set_ratio。
              并且要单独处理否定词：含否定词差异的一律不预筛，交 LLM 判。
        """

        raise NotImplementedError

    # ---- 执行 -----------------------------------------------------------
    async def apply(
        self,
        tenant_id: str,
        decision: ConsolidationDecision,
        candidate: MemoryCandidate,
        embedding: list[float],
    ) -> tuple[ConsolidationOp, str | None]:
        """执行判定，返回 ``(实际执行的操作, 记忆 id)``。

        影子模式下只写审计事件与指标，不动数据，返回的 op 保持原判定
        但 ``decision.shadowed`` 为 True——调用方据此知道"库里其实没变"。

        UPDATE 的执行必须在单个事务内完成三件事：更新内容与 embedding、
        把旧记忆置 SUPERSEDED、写审计事件。拆开会出现"旧的删了新的没写进去"
        这种最坏情况，直接丢数据。

        TODO: 实现。
        """

        raise NotImplementedError

    # ---- 周期性整合 -----------------------------------------------------
    async def dedup(
        self,
        tenant_id: str,
        scope: MemoryScope,
        scope_id: str,
        similarity_threshold: float = 0.92,
        batch_size: int = 500,
    ) -> dict[str, int]:
        """去重合并：把语义等价的多条记忆合成一条。

        写入时判定已经拦掉了大部分重复，这里处理的是漏网的：
        两条记忆分别写入时都不相似，但经过若干次 UPDATE 后变得等价了。

        合并时保留 ``strength`` 与 ``hit_count`` 的**较大值**而非求和。
        求和会让被合并过的记忆强度虚高，在检索里不合理地压过其他记忆——
        合并只是消除冗余表述，不代表这件事变得更重要了。

        TODO: 实现。用并查集处理传递性重复（A≈B、B≈C 但 A≉C），
              否则会出现"合了 AB 又合了 BC"的重复执行。
        """

        raise NotImplementedError

    async def summarize_cluster(
        self,
        tenant_id: str,
        scope: MemoryScope,
        scope_id: str,
        min_cluster_size: int = 5,
        keep_originals: bool = True,
    ) -> dict[str, int]:
        """摘要压缩：同一主题的碎片记忆合并为概括性记忆。

        例：十条关于代码风格的零散偏好 → 一条"偏好函数式风格、
        小函数、显式类型标注"。这能显著节省 ``/context`` 的 token 预算。

        ``keep_originals=True`` 时保留原始记忆的引用而不物理删除。
        必须如此：要能回答"这条概括是从哪些原始记忆来的"，
        否则用户看到一条自己从没说过的概括，会认为 Agent 在编造。

        代价是原始记忆仍占空间且可能被召回。因此保留的原始记忆应置为
        SUPERSEDED（不参与召回但可查询），而不是留在 ACTIVE ——
        否则概括与原文会同时被召回，白白浪费预算。

        TODO: 实现。聚类可以先用 embedding 做简单的层次聚类，
              不必上 HDBSCAN——记忆量级不需要。
        """

        raise NotImplementedError

    async def detect_conflicts(
        self, tenant_id: str, scope: MemoryScope, scope_id: str, limit: int = 100
    ) -> list[tuple[Memory, Memory, str]]:
        """主动扫描既有矛盾，返回 ``(记忆A, 记忆B, 理由)`` 列表。

        与写入时判定的区别：这里是**事后体检**，找出历史遗留的矛盾
        （判定逻辑上线前写入的、或影子模式期间放过的）。

        发现的矛盾不自动处理，而是发 ``aios.memory.conflict`` 事件
        并列在管理界面等人裁决。自动处理历史矛盾风险太大——
        无法判断两条里哪条更该留下，时间戳新的不一定是对的。

        TODO: 实现。
        """

        raise NotImplementedError
