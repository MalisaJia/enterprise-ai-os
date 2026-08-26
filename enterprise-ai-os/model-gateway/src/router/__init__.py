"""模型路由：从候选集中选出目标模型，并在失败时按降级链换人。"""

from .strategy import (
    CostStrategy,
    ExplicitStrategy,
    LatencyStrategy,
    LoadStrategy,
    ModelRouter,
    PriorityStrategy,
    RoutingContext,
    RoutingDecision,
    RoutingStrategy,
    StrategyName,
    TaskType,
    TaskTypeStrategy,
    build_strategy,
)

__all__ = [
    "CostStrategy",
    "ExplicitStrategy",
    "LatencyStrategy",
    "LoadStrategy",
    "ModelRouter",
    "PriorityStrategy",
    "RoutingContext",
    "RoutingDecision",
    "RoutingStrategy",
    "StrategyName",
    "TaskType",
    "TaskTypeStrategy",
    "build_strategy",
]
