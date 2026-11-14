"""多模型 Embedding 提供者与重排器。

为什么要抽象层而不是直接调 HTTP：

1. **可替换**：内网无出口的客户现场需要切本地模型，业务代码不应感知。
2. **批量与缓存**：embedding 是摄入链路的成本大头，必须做批量合并 + Redis 缓存。
3. **维度守卫**：换模型会改变向量维度，与既有索引不兼容。必须在启动时校验，
   而不是等写入时报错——那时索引已被污染。
"""

from __future__ import annotations

import abc
import hashlib
import logging
from dataclasses import dataclass
from typing import Any, Sequence

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EmbeddingResult:
    """一批文本的向量化结果。

    Attributes:
        vectors: 与输入等长、顺序一致的向量列表。
        model: 实际使用的模型名（可能因 fallback 与请求不同）。
        dimensions: 向量维度。
        prompt_tokens: 计费用 token 数，用于成本归集。
        from_cache: 命中缓存的条目数，用于观测缓存收益。
    """

    vectors: list[list[float]]
    model: str
    dimensions: int
    prompt_tokens: int = 0
    from_cache: int = 0


@dataclass(frozen=True)
class RerankResult:
    """重排结果：原始下标 + 相关性分数，按分数降序。"""

    index: int
    score: float


class EmbeddingProvider(abc.ABC):
    """Embedding 提供者抽象。

    实现类必须保证：
    - ``embed_texts`` 输出顺序与输入严格一致（下游按位置对齐 chunk）
    - 超长文本在实现内部截断，不向上抛异常（摄入不应因一段超长文本整体失败）
    """

    def __init__(self, model: str, dimensions: int, *, normalize: bool = True) -> None:
        self.model = model
        self.dimensions = dimensions
        self.normalize = normalize

    @abc.abstractmethod
    async def embed_texts(self, texts: Sequence[str]) -> EmbeddingResult:
        """批量向量化文档片段。

        Args:
            texts: 待向量化文本，长度可为 0。

        Returns:
            EmbeddingResult，``vectors`` 长度等于 ``texts``。
        """

    @abc.abstractmethod
    async def embed_query(self, query: str) -> list[float]:
        """向量化检索查询。

        单独开一个方法是因为部分模型（如 bge / e5）对 query 与 passage
        使用不同的指令前缀，混用会显著掉点。
        """

    async def ping(self) -> bool:
        """健康探测，供 /readyz 使用。默认实现打一次极短文本。"""

        try:
            result = await self.embed_texts(["ping"])
        except Exception:  # noqa: BLE001 - 探测失败不区分原因
            logger.warning("embedding provider ping failed", exc_info=True)
            return False
        return len(result.vectors) == 1

    async def close(self) -> None:
        """释放连接池等资源。默认无操作。"""

    def assert_dimensions(self, expected: int) -> None:
        """启动时校验维度与索引一致，不一致直接快速失败。"""

        if self.dimensions != expected:
            raise ValueError(
                f"embedding dimension mismatch: provider={self.dimensions} index={expected}. "
                "更换模型需重建索引（见 README 迁移说明）"
            )

    @staticmethod
    def cache_key(model: str, text: str) -> str:
        """生成缓存键：模型 + 文本内容哈希。"""

        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return f"rag:emb:{model}:{digest}"


class GatewayEmbeddingProvider(EmbeddingProvider):
    """经 model-gateway 调用 embedding（线上默认）。

    走网关的收益：厂商凭证不下发到本服务、限流熔断统一、成本可按租户归集。
    """

    def __init__(
        self,
        model: str,
        dimensions: int,
        *,
        base_url: str,
        path: str = "/v1/embeddings",
        batch_size: int = 32,
        timeout_seconds: int = 30,
        max_retries: int = 3,
        normalize: bool = True,
        cache: Any | None = None,
    ) -> None:
        super().__init__(model, dimensions, normalize=normalize)
        self.base_url = base_url.rstrip("/")
        self.path = path
        self.batch_size = batch_size
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.cache = cache
        self._client: Any | None = None   # httpx.AsyncClient

    async def embed_texts(self, texts: Sequence[str]) -> EmbeddingResult:
        """分批调用网关的 OpenAI 兼容 embedding 端点。

        TODO:
          1. 查 Redis 缓存，未命中的按 batch_size 切批
          2. httpx.AsyncClient POST {base_url}{path}，带 X-Internal-Token
          3. tenacity 重试（仅对 429/5xx/超时重试，4xx 不重试）
          4. 回填缓存，按输入顺序合并缓存命中与新算结果
          5. normalize 为 True 时做 L2 归一化（配合 cosine 距离）
        """

        raise NotImplementedError

    async def embed_query(self, query: str) -> list[float]:
        """向量化 query。

        TODO: bge 系列需要给 query 加指令前缀
              （如 "为这个句子生成表示以用于检索相关文章："），passage 侧不加。
        """

        raise NotImplementedError

    async def close(self) -> None:
        """关闭 httpx 客户端。"""

        # TODO: if self._client: await self._client.aclose()


class LocalEmbeddingProvider(EmbeddingProvider):
    """本机 sentence-transformers 推理，用于内网无出口或网关不可用时兜底。

    注意：加载模型会占用数 GB 内存且首次需下载权重，
    容器中应挂载 ``hf-cache`` 卷（见 docker-compose.yml）。
    """

    def __init__(
        self,
        model: str,
        dimensions: int,
        *,
        model_path: str,
        device: str = "cpu",
        batch_size: int = 16,
        normalize: bool = True,
    ) -> None:
        super().__init__(model, dimensions, normalize=normalize)
        self.model_path = model_path
        self.device = device
        self.batch_size = batch_size
        self._model: Any | None = None    # sentence_transformers.SentenceTransformer

    def _ensure_loaded(self) -> Any:
        """懒加载模型：避免进程启动时就吃满内存。

        TODO: SentenceTransformer(self.model_path, device=self.device)
        """

        raise NotImplementedError

    async def embed_texts(self, texts: Sequence[str]) -> EmbeddingResult:
        """本地推理。

        TODO: 用 asyncio.to_thread 包裹同步 encode 调用，避免阻塞事件循环。
        """

        raise NotImplementedError

    async def embed_query(self, query: str) -> list[float]:
        """本地推理 query 向量。TODO: 同样处理指令前缀。"""

        raise NotImplementedError


class Reranker(abc.ABC):
    """Cross-encoder 重排器抽象。

    重排是 RAG 效果提升性价比最高的一步（召回 20 → 精排 5），
    但也是延迟最大的一步，因此必须支持降级。
    """

    def __init__(self, model: str, *, top_n: int = 5, fallback_on_error: bool = True) -> None:
        self.model = model
        self.top_n = top_n
        self.fallback_on_error = fallback_on_error

    @abc.abstractmethod
    async def rerank(self, query: str, documents: Sequence[str]) -> list[RerankResult]:
        """对候选文档按与 query 的相关性重新打分。

        Returns:
            按 score 降序排列、长度不超过 ``top_n`` 的结果。
        """


class GatewayReranker(Reranker):
    """经 model-gateway 调用重排模型。"""

    def __init__(
        self,
        model: str,
        *,
        base_url: str,
        path: str = "/v1/rerank",
        top_n: int = 5,
        batch_size: int = 16,
        timeout_seconds: int = 15,
        fallback_on_error: bool = True,
    ) -> None:
        super().__init__(model, top_n=top_n, fallback_on_error=fallback_on_error)
        self.base_url = base_url.rstrip("/")
        self.path = path
        self.batch_size = batch_size
        self.timeout_seconds = timeout_seconds

    async def rerank(self, query: str, documents: Sequence[str]) -> list[RerankResult]:
        """调用网关重排端点。

        TODO: 超时或 5xx 时，若 fallback_on_error 则返回原顺序（分数置 0）
              并让调用方把响应的 reranked 置 false，指标 rag_rerank_fallback_total +1。
        """

        raise NotImplementedError


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------
def build_embedding_provider(config: dict[str, Any]) -> EmbeddingProvider:
    """按配置 ``embedding.provider`` 构造提供者。

    Args:
        config: config.yaml 的 ``embedding`` 段。

    Raises:
        ValueError: provider 取值非法。

    TODO: 实例化后调用 assert_dimensions()，与向量索引的维度对齐。
    """

    provider = str(config.get("provider", "gateway")).lower()
    model = str(config.get("model", "bge-m3"))
    dimensions = int(config.get("dimensions", 1024))
    normalize = bool(config.get("normalize", True))

    if provider == "gateway":
        gw = config.get("gateway", {}) or {}
        return GatewayEmbeddingProvider(
            model,
            dimensions,
            base_url=str(gw.get("base_url", "http://model-gateway:8200")),
            path=str(gw.get("path", "/v1/embeddings")),
            batch_size=int(config.get("batch_size", 32)),
            timeout_seconds=int(config.get("timeout_seconds", 30)),
            max_retries=int(config.get("max_retries", 3)),
            normalize=normalize,
        )
    if provider == "local":
        local = config.get("local", {}) or {}
        return LocalEmbeddingProvider(
            model,
            dimensions,
            model_path=str(local.get("model_path", "BAAI/bge-m3")),
            device=str(local.get("device", "cpu")),
            batch_size=int(config.get("batch_size", 16)),
            normalize=normalize,
        )
    raise ValueError(f"unknown embedding provider: {provider}")


def build_reranker(config: dict[str, Any]) -> Reranker | None:
    """按配置 ``rerank`` 段构造重排器；``enabled=false`` 或 ``provider=none`` 返回 None。"""

    if not config.get("enabled", True):
        return None
    provider = str(config.get("provider", "gateway")).lower()
    if provider == "none":
        return None
    if provider == "gateway":
        return GatewayReranker(
            str(config.get("model", "bge-reranker-v2-m3")),
            base_url=str(config.get("base_url", "http://model-gateway:8200")),
            top_n=int(config.get("top_n", 5)),
            batch_size=int(config.get("batch_size", 16)),
            timeout_seconds=int(config.get("timeout_seconds", 15)),
            fallback_on_error=bool(config.get("fallback_on_error", True)),
        )
    # TODO: 支持 local cross-encoder（sentence_transformers.CrossEncoder）
    raise ValueError(f"unknown rerank provider: {provider}")
