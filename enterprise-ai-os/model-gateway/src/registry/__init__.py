"""模型注册中心。

网关的"事实来源"：有哪些模型、各自能干什么、多少钱、当前健康与否。
路由决策完全依赖这里的数据。
"""

from .models import (
    ModelCapability,
    ModelHealth,
    ModelRegistry,
    ModelSpec,
    ModelType,
    ProviderConfig,
)

__all__ = [
    "ModelCapability",
    "ModelHealth",
    "ModelRegistry",
    "ModelSpec",
    "ModelType",
    "ProviderConfig",
]
