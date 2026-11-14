"""路由策略：按任务类型 / 负载 / 成本 / 延迟决定用哪个模型。

设计取舍记录（避免后人重复讨论）：

1. **显式指定优先**。业务写了具体 model id 就直连，不擅自改写。
   例外：成本超支降级（``allow_override_explicit=true`` 时），且必须在响应头里
   告知实际用了哪个模型，否则业务无法排查"为什么效果变了"。

2. **task_type 是默认策略**，而不是 cost。理由：成本最优往往效果最差，
   默认应该是"任务适配"，省钱是显式选择（``auto:cheap``）。

3. **降级链是有向的、有深度上限的**。无上限的 fallback 在厂商大面积故障时
   会把一个请求放大成 N 个请求，加速雪崩。上限设 2。

4. **降级必须保持能力等价**。代码任务不能降到没有 ``code`` 能力的模型上——
   那样虽然"成功返回了"，但结果是错的，比失败更糟。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, Sequence

from ..registry.models import ModelRegistry, ModelSpec, ModelType

logger = logging.getLogger(__name__)


class StrategyName(str, Enum):
    """路由策略枚举。"""

    EXPLICIT = "explicit"      # 直连指定模型
    PRIORITY = "priority"      # 按 priority + weight 加权轮询
    TASK_TYPE = "task_type"    # 按任务类型匹配能力标签
    COST = "cost"              # 同等能力选最便宜
    LATENCY = "latency"        # 选 P95 最低
    LOAD = "load"              # 选活跃请求数最少


class TaskType(str, Enum):
    """任务类型。可由调用方显式声明，也可由网关启发式推断。"""

    CHAT = "chat"
    CODE = "code"
    REASONING = "reasoning"
    LONG_CONTEXT = "long_context"
    EMBEDDING = "embedding"
    RERANK = "rerank"
    TOOL_CALL = "tool_call"


class FallbackReason(str, Enum):
    """触发降级的原因。会写入指标与响应头，用于事后分析。"""

    TIMEOUT = "timeout"
    CONNECTION_ERROR = "connection_error"
    RATE_LIMITED = "rate_limited"
    SERVER_ERROR = "server_error"
    CIRCUIT_OPEN = "circuit_open"
    CONTEXT_OVERFLOW = "context_overflow"
    QUOTA_EXCEEDED = "quota_exceeded"


@dataclass
class RoutingContext:
    """路由决策的输入。

    Attributes:
        requested_model: 业务传入的 model 字段（可能是别名）。
        model_type: chat / embedding / rerank。
        task_type: 显式声明的任务类型；为 None 时由 ``infer_task_type`` 推断。
        estimated_input_tokens: 预估输入 token，用于上下文窗口筛选。
        requires_streaming: 是否流式（个别模型不支持流式）。
        requires_tools: 是否用到 tool calling。
        exclude: 排除的模型 ID（如已失败过的）。
        max_latency_ms: 延迟上限，用于筛掉慢模型。
        tenant_id / user_id: 用于配额与审计。
    """

    requested_model: str
    model_type: ModelType = ModelType.CHAT
    task_type: TaskType | None = None
    estimated_input_tokens: int = 0
    requires_streaming: bool = False
    requires_tools: bool = False
    exclude: list[str] = field(default_factory=list)
    max_latency_ms: int | None = None
    tenant_id: str | None = None
    user_id: str | None = None
    strategy_override: StrategyName | None = None


@dataclass
class RoutingDecision:
    """路由结果。

    要把"为什么选它"一并返回：这些信息进响应头与日志，
    是排查"效果为什么变了"的唯一线索。
    """

    model: ModelSpec
    strategy: StrategyName
    fallback_depth: int = 0
    fallback_reason: FallbackReason | None = None
    candidates_considered: list[str] = field(default_factory=list)
    reason: str = ""

    def to_headers(self) -> dict[str, str]:
        """转为响应头，便于客户端与排障工具读取。"""

        headers = {
            "X-Model-Selected": self.model.id,
            "X-Model-Provider": self.model.provider,
            "X-Routing-Strategy": self.strategy.value,
            "X-Fallback-Depth": str(self.fallback_depth),
        }
        if self.fallback_reason is not None:
            headers["X-Fallback-Reason"] = self.fallback_reason.value
        return headers


class RoutingStrategy(Protocol):
    """路由策略协议。

    用 Protocol：策略实现之间没有共享行为，继承基类只会带来空实现。
    """

    name: StrategyName

    def select(self, candidates: Sequence[ModelSpec], context: RoutingContext) -> ModelSpec | None:
        """从候选中选一个；无合适候选返回 None。"""
        ...


class ExplicitStrategy:
    """直连业务指定的模型。"""

    name = StrategyName.EXPLICIT

    def select(self, candidates: Sequence[ModelSpec], context: RoutingContext) -> ModelSpec | None:
        """返回第一个可选候选。

        TODO: 候选存在但不可选（熔断中）时返回 None，交给 fallback 处理；
              要在日志里明确区分"模型不存在"（404）与"模型暂时不可用"（503）。
        """

        raise NotImplementedError


class PriorityStrategy:
    """按 priority 分组、组内按 weight 加权轮询。

    这是最朴素也最可预测的策略，适合作为其他策略的兜底。
    """

    name = StrategyName.PRIORITY

    def __init__(self) -> None:
        # 加权轮询的游标。多 worker 进程下各自独立，整体仍近似均衡。
        self._cursor: dict[str, int] = {}

    def select(self, candidates: Sequence[ModelSpec], context: RoutingContext) -> ModelSpec | None:
        """取最高优先级组，组内加权轮询。

        TODO: 实现平滑加权轮询（SWRR），避免朴素实现造成的突发倾斜。
        """

        raise NotImplementedError


class TaskTypeStrategy:
    """按任务类型匹配能力标签（默认策略）。

    例：``code`` 任务 → 要求 ``ModelCapability.CODE``；
    超长上下文任务 → 要求 ``LONG_CONTEXT`` 且 ``context_window`` 足够。
    """

    name = StrategyName.TASK_TYPE

    def __init__(self, capability_map: dict[str, list[str]] | None = None) -> None:
        self.capability_map = capability_map or {}

    def select(self, candidates: Sequence[ModelSpec], context: RoutingContext) -> ModelSpec | None:
        """筛能力匹配的候选，再按 (priority, -weight) 取首个。

        TODO:
          1. task_type 为 None 时调 infer_task_type 推断
          2. 按 capability_map 取必需能力，用 spec.has_capabilities 过滤
          3. requires_tools 时额外要求 TOOL_CALL
          4. 按 estimated_input_tokens 用 fits_context 过滤
          5. 全部筛完为空时，放宽到"同类型任意可用模型"并记 WARN
             （宁可用能力不完美的模型，也比直接失败好——但要留下告警痕迹）
        """

        raise NotImplementedError


class CostStrategy:
    """同等能力下选最便宜的。

    注意：便宜模型往往输出更长（能力弱、啰嗦），实际成本可能反而更高。
    因此按"预估总成本"而非单纯输入单价排序。
    """

    name = StrategyName.COST

    def select(self, candidates: Sequence[ModelSpec], context: RoutingContext) -> ModelSpec | None:
        """按预估成本升序选择。

        TODO: 用 price.estimate(estimated_input_tokens, 预估输出 tokens) 排序；
              输出 token 预估可用该模型的历史平均输出长度。
        """

        raise NotImplementedError


class LatencyStrategy:
    """选 P95 延迟最低的。

    仅在有足够样本时可靠；样本不足应退回 PriorityStrategy，
    否则冷启动阶段会把所有流量打到第一个碰巧被采样的模型上。
    """

    name = StrategyName.LATENCY

    def __init__(self, min_samples: int = 20) -> None:
        self.min_samples = min_samples

    def select(self, candidates: Sequence[ModelSpec], context: RoutingContext) -> ModelSpec | None:
        """按 latency_p95_ms 升序选择。

        TODO: 样本不足或 max_latency_ms 过滤后为空时，退回 priority 策略。
        """

        raise NotImplementedError


class LoadStrategy:
    """最少活跃连接数优先。

    比轮询更能应对"请求耗时差异大"的场景（长文生成 vs 短问答）。
    """

    name = StrategyName.LOAD

    def select(self, candidates: Sequence[ModelSpec], context: RoutingContext) -> ModelSpec | None:
        """按 active_requests 升序，相同则按 weight 降序。

        TODO: 单实例的 active_requests 不代表集群整体，
              高精度场景需把计数放 Redis；先用本地近似值即可。
        """

        raise NotImplementedError


def build_strategy(name: StrategyName | str, config: dict[str, Any] | None = None) -> RoutingStrategy:
    """按名字构造策略实例。"""

    cfg = config or {}
    name = StrategyName(name)
    if name is StrategyName.EXPLICIT:
        return ExplicitStrategy()
    if name is StrategyName.PRIORITY:
        return PriorityStrategy()
    if name is StrategyName.TASK_TYPE:
        return TaskTypeStrategy(cfg.get("task_capability_map"))
    if name is StrategyName.COST:
        return CostStrategy()
    if name is StrategyName.LATENCY:
        return LatencyStrategy()
    if name is StrategyName.LOAD:
        return LoadStrategy()
    raise ValueError(f"unknown routing strategy: {name}")


def infer_task_type(payload: dict[str, Any]) -> TaskType:
    """从请求体启发式推断任务类型。

    仅在调用方未显式声明时使用。启发规则保持简单可解释——
    复杂的意图分类器会引入额外延迟与不可预测性，收益不划算。

    TODO:
      - 有 tools/tool_choice → TOOL_CALL
      - 消息含代码块（```）或 diff 特征 → CODE
      - 预估 input tokens > 32K → LONG_CONTEXT
      - 出现"逐步分析/证明/推导"等强推理提示 → REASONING
      - 兜底 → CHAT
    """

    raise NotImplementedError


class ModelRouter:
    """路由器：编排"解析别名 → 筛候选 → 应用策略 → 降级"全过程。"""

    def __init__(
        self,
        registry: ModelRegistry,
        *,
        breaker_registry: Any | None = None,
        config: dict[str, Any] | None = None,
    ) -> None:
        self.registry = registry
        self.breaker_registry = breaker_registry
        self.config = config or {}
        self.default_strategy = StrategyName(self.config.get("default_strategy", "task_type"))
        self.fallback_chains: dict[str, list[str]] = (
            (self.config.get("fallback", {}) or {}).get("chains", {}) or {}
        )
        self.max_fallback_depth = int((self.config.get("fallback", {}) or {}).get("max_depth", 2))
        self._strategies: dict[StrategyName, RoutingStrategy] = {}

    def route(self, context: RoutingContext) -> RoutingDecision:
        """做出首次路由决策。

        Raises:
            ModelNotFoundError: 请求的模型/别名不存在（→ 404）。
            NoAvailableModelError: 候选都不可用（→ 503）。

        TODO:
          1. registry.resolve_alias(context.requested_model)
          2. 排除 context.exclude 与断路器 open 的模型
          3. 选策略：strategy_override > 别名指定 > default_strategy
          4. strategy.select(...)；None 则抛 NoAvailableModelError
          5. 组装 RoutingDecision（含 candidates_considered，便于排障）
        """

        raise NotImplementedError

    def next_fallback(
        self,
        current: RoutingDecision,
        reason: FallbackReason,
        context: RoutingContext,
    ) -> RoutingDecision | None:
        """取降级链上的下一个模型；已达深度上限或无可用候选则返回 None。

        TODO:
          1. current.fallback_depth >= max_fallback_depth → None
          2. reason 不在 fallback.trigger_on 中 → None（如 4xx 参数错误重试无意义）
          3. 按 fallback_chains[current.model.id] 依次找第一个可选且能力等价的模型
             —— 能力等价校验必须做，否则代码任务可能被降级到纯聊天模型
          4. 链上都不行时，退回 registry.candidates 同能力兜底
        """

        raise NotImplementedError

    def _get_strategy(self, name: StrategyName) -> RoutingStrategy:
        """惰性构造并缓存策略实例（PriorityStrategy 有轮询游标，必须复用）。"""

        if name not in self._strategies:
            self._strategies[name] = build_strategy(name, self.config)
        return self._strategies[name]


class ModelNotFoundError(Exception):
    """请求的模型或别名未注册。映射为 HTTP 404。"""


class NoAvailableModelError(Exception):
    """候选模型全部不可用（熔断 / 限流 / 无凭证）。映射为 HTTP 503。"""
