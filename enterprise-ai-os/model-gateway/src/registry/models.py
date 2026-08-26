"""模型注册表：模型元数据、能力标签、定价、健康状态。

为什么需要显式的注册表而不是硬编码模型名：

1. **路由需要结构化元数据**。"这个任务是写代码" → 只有带 ``code`` 能力标签的
   模型才是合法候选。字符串匹配模型名是不可维护的。
2. **换模型不改代码**。业务用别名 ``auto:code``，运维改配置即可切换底层模型。
3. **成本核算需要单价**。价格随厂商调价变化，必须是配置而非常量。
4. **参数兼容性差异需要声明**。如 ``deepseek-reasoner`` 不接受 ``temperature``，
   透传过去会直接 400。这类差异必须在注册表里声明，由网关统一裁剪。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Sequence

logger = logging.getLogger(__name__)


class ModelType(str, Enum):
    """模型类型。决定走哪个端点与请求体格式。"""

    CHAT = "chat"
    COMPLETION = "completion"
    EMBEDDING = "embedding"
    RERANK = "rerank"


class ModelCapability(str, Enum):
    """能力标签。``task_type`` 路由策略据此筛候选。

    标签是"声明式"的：新增能力只需在配置里加标签 + 在别名候选里引用。
    """

    CHAT = "chat"
    CODE = "code"
    FIM = "fim"                            # fill-in-the-middle，代码补全
    REASONING = "reasoning"
    DEEP_THINKING = "deep_thinking"
    TOOL_CALL = "tool_call"
    VISION = "vision"
    CHINESE = "chinese"
    MULTILINGUAL = "multilingual"
    LONG_CONTEXT = "long_context"
    ULTRA_LONG_CONTEXT = "ultra_long_context"
    FAST = "fast"
    EMBEDDING = "embedding"
    RERANK = "rerank"


class ModelHealth(str, Enum):
    """模型可用性。

    ``UNAVAILABLE`` 与 ``CIRCUIT_OPEN`` 要分开：前者是配置问题（没有 API Key），
    后者是运行时故障（会自动恢复）。运维处理方式完全不同。
    """

    HEALTHY = "healthy"
    DEGRADED = "degraded"          # 延迟高或部分失败，仍可用但降权
    CIRCUIT_OPEN = "circuit_open"  # 被熔断，暂时不可选
    RATE_LIMITED = "rate_limited"  # 触及配额上限
    UNAVAILABLE = "unavailable"    # 未配置凭证或被管理员下线


@dataclass
class ModelPrice:
    """单价（美元 / 1K tokens）。"""

    input: float = 0.0
    output: float = 0.0

    def estimate(self, input_tokens: int, output_tokens: int) -> float:
        """估算单次调用成本。"""

        return (input_tokens * self.input + output_tokens * self.output) / 1000.0


@dataclass
class ProviderConfig:
    """厂商接入配置（对应 config.yaml 的 ``providers.*``）。"""

    name: str
    base_url: str
    api_key: str = ""
    type: str = "openai_compatible"
    enabled: bool = True
    timeout_seconds: int = 120
    connect_timeout_seconds: int = 5
    max_connections: int = 100
    max_retries: int = 2
    limits: dict[str, int] = field(default_factory=dict)

    @property
    def usable(self) -> bool:
        """是否可用。

        私有化 vLLM 不需要真实 key，因此只要求 ``enabled`` + ``base_url``；
        公有云厂商必须有 api_key。
        """

        if not self.enabled or not self.base_url:
            return False
        return bool(self.api_key) or self.base_url.startswith("http://")


@dataclass
class ModelSpec:
    """单个模型的完整声明。

    Attributes:
        id: 网关对外暴露的稳定 ID（业务写这个）。
        provider: 所属厂商。
        upstream_name: 厂商侧真实模型名。与 ``id`` 解耦，
            这样厂商改名或我们做别名映射时业务无感。
        capabilities: 能力标签集合，路由筛选依据。
        priority: 越小越优先。
        weight: 同优先级内加权轮询的权重。
        unsupported_params: 该模型不接受的参数，网关请求前裁剪。
        enabled: 管理员可热更新此字段临时下线模型。
    """

    id: str
    provider: str
    upstream_name: str
    type: ModelType = ModelType.CHAT
    context_window: int = 8192
    max_output_tokens: int = 4096
    dimensions: int | None = None          # 仅 embedding 类型
    max_input_tokens: int | None = None
    capabilities: set[ModelCapability] = field(default_factory=set)
    price: ModelPrice = field(default_factory=ModelPrice)
    priority: int = 1
    weight: int = 100
    tags: list[str] = field(default_factory=list)
    unsupported_params: list[str] = field(default_factory=list)
    enabled: bool = True

    # -- 运行时状态（不来自配置，由网关维护）-------------------------------
    health: ModelHealth = ModelHealth.HEALTHY
    active_requests: int = 0
    latency_p95_ms: float = 0.0
    error_rate: float = 0.0
    last_error_at: float | None = None

    def has_capabilities(self, required: Iterable[ModelCapability | str]) -> bool:
        """是否满足全部所需能力。"""

        needed = {ModelCapability(c) for c in required}
        return needed.issubset(self.capabilities)

    def is_selectable(self) -> bool:
        """当前是否可被路由选中。"""

        return self.enabled and self.health in (ModelHealth.HEALTHY, ModelHealth.DEGRADED)

    def fits_context(self, estimated_tokens: int) -> bool:
        """上下文是否装得下。

        路由时先做这个判断，避免把 20 万 token 的请求发给 32K 窗口的模型
        白等一个 400。
        """

        return estimated_tokens <= self.context_window

    def sanitize_params(self, params: dict[str, Any]) -> dict[str, Any]:
        """剔除该模型不支持的参数。

        这是"网关吸收厂商差异"的核心动作：业务照 OpenAI 标准发请求，
        由我们负责让它在各家都能跑通。
        """

        return {k: v for k, v in params.items() if k not in self.unsupported_params}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ModelSpec:
        """从配置字典构造。

        TODO: 校验必填项（id/provider/upstream_name），非法能力标签快速失败。
        """

        raise NotImplementedError


@dataclass
class ModelRegistry:
    """模型注册表。

    职责边界：只管"有什么、什么状态"，**不做选择决策**——那是 router 的事。
    这样拆分让路由策略可以被独立单测（喂一个假注册表即可）。
    """

    models: dict[str, ModelSpec] = field(default_factory=dict)
    providers: dict[str, ProviderConfig] = field(default_factory=dict)
    aliases: dict[str, dict[str, Any]] = field(default_factory=dict)
    loaded_at: float = field(default_factory=time.time)

    @classmethod
    def from_config(
        cls,
        registry_cfg: dict[str, Any],
        providers_cfg: dict[str, Any],
        routing_cfg: dict[str, Any] | None = None,
    ) -> ModelRegistry:
        """从配置构建注册表。

        TODO:
          1. 解析 providers，未配 api_key 的标记 usable=False
          2. 解析 models；其 provider 不 usable 时把 health 置 UNAVAILABLE
             （而非跳过——管理面需要能看到"配置了但没凭证"的模型）
          3. 解析 routing.aliases，校验 candidates 引用的 model id 都存在，
             悬空引用直接启动失败（这类错误只在降级时暴露，必须提前拦住）
          4. 至少一个模型可用，否则记 ERROR 但仍允许启动（便于先起服务再补 Key）
        """

        raise NotImplementedError

    def get(self, model_id: str) -> ModelSpec | None:
        """按 ID 取模型。"""

        return self.models.get(model_id)

    def resolve_alias(self, name: str) -> tuple[list[ModelSpec], str | None]:
        """解析别名为候选模型列表与策略名。

        Args:
            name: 可能是别名（``auto:code``）或具体 model id。

        Returns:
            ``(候选列表, 策略名)``；若 ``name`` 是具体模型，返回 ``([该模型], "explicit")``。

        TODO: 别名不存在且模型也不存在时抛 ModelNotFoundError（映射为 404）。
        """

        raise NotImplementedError

    def candidates(
        self,
        *,
        model_type: ModelType = ModelType.CHAT,
        required_capabilities: Sequence[ModelCapability | str] = (),
        min_context_window: int = 0,
        exclude: Sequence[str] = (),
        only_selectable: bool = True,
    ) -> list[ModelSpec]:
        """按条件筛出候选模型。

        TODO: 过滤后按 (priority, -weight) 排序返回。
        """

        raise NotImplementedError

    def update_health(self, model_id: str, health: ModelHealth, *, error_rate: float | None = None) -> None:
        """更新模型健康状态（由熔断器与探针调用）。"""

        spec = self.models.get(model_id)
        if spec is None:
            logger.warning("update_health for unknown model: %s", model_id)
            return
        spec.health = health
        if error_rate is not None:
            spec.error_rate = error_rate
        if health is not ModelHealth.HEALTHY:
            spec.last_error_at = time.time()

    def record_latency(self, model_id: str, latency_ms: float) -> None:
        """记录延迟样本，供 ``latency`` 策略使用。

        TODO: 用滑动窗口（如 TDigest 或简单环形缓冲）算 P95，
              不要用简单平均——平均值会掩盖长尾。
        """

        raise NotImplementedError

    def list_models(self, *, include_unavailable: bool = False) -> list[ModelSpec]:
        """列出模型。

        ``/v1/models``（面向业务）不返回不可用模型；
        ``/v1/admin/models``（面向运维）全部返回。
        """

        return [m for m in self.models.values() if include_unavailable or m.is_selectable()]

    async def reload(self, registry_cfg: dict[str, Any], providers_cfg: dict[str, Any]) -> None:
        """热加载配置。

        TODO: 原子替换 models 字典，但**保留**各模型的运行时状态
              （health / latency / active_requests）。丢掉运行时状态会导致
              热加载后所有断路器复位，刚熔断的故障模型立刻又被选中。
        """

        raise NotImplementedError
