"""Embedding 与重排能力。

两种后端：
- ``gateway``：经 model-gateway 调用（线上默认，便于集中限流与计量）
- ``local``：本机 sentence-transformers（离线/内网无出口场景兜底）
"""

from .provider import (
    EmbeddingProvider,
    EmbeddingResult,
    GatewayEmbeddingProvider,
    LocalEmbeddingProvider,
    Reranker,
    build_embedding_provider,
    build_reranker,
)

__all__ = [
    "EmbeddingProvider",
    "EmbeddingResult",
    "GatewayEmbeddingProvider",
    "LocalEmbeddingProvider",
    "Reranker",
    "build_embedding_provider",
    "build_reranker",
]
