"""模型厂商适配层。

统一契约：所有 provider 对外暴露相同的 ``chat`` / ``chat_stream`` / ``embed`` /
``rerank`` 接口，把厂商差异（参数名、错误码、流式分块格式、usage 位置）
全部吸收在各自实现内部。上层（api / router）不感知任何厂商细节。

错误必须归一化为 ``ProviderError`` 子类：路由的降级判定依赖错误分类，
把原始 httpx 异常透传上去会让 fallback 逻辑变成一堆 if isinstance。
"""

from __future__ import annotations

import abc
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Sequence

from ..registry.models import ModelSpec, ProviderConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 错误分类 —— 与 router.strategy.FallbackReason 一一对应
# ---------------------------------------------------------------------------
class ProviderError(Exception):
    """厂商调用错误基类。"""

    #: 是否值得换个模型重试
    retryable: bool = False

    def __init__(self, message: str, *, provider: str = "", status_code: int | None = None) -> None:
        super().__init__(message)
        self.provider = provider
        self.status_code = status_code


class ProviderTimeoutError(ProviderError):
    """请求超时。可降级。"""

    retryable = True


class ProviderConnectionError(ProviderError):
    """网络连接失败（DNS / TLS / 拒连）。可降级。"""

    retryable = True


class ProviderRateLimitError(ProviderError):
    """厂商侧限流（429）。可降级到其他厂商。

    注意：**不计入熔断**。被限流说明服务是活的，开断路器只会误伤。
    """

    retryable = True


class ProviderServerError(ProviderError):
    """厂商 5xx。可降级，且计入熔断。"""

    retryable = True


class ProviderInvalidRequestError(ProviderError):
    """请求参数非法（4xx）。**不可重试**——换个模型也是一样的错。"""

    retryable = False


class ProviderContentFilterError(ProviderError):
    """内容被厂商安全策略拦截。不可重试，需原样告知业务。"""

    retryable = False


class ProviderAuthError(ProviderError):
    """凭证无效或过期。不可重试，需运维介入。"""

    retryable = False


# ---------------------------------------------------------------------------
# 归一化数据结构
# ---------------------------------------------------------------------------
@dataclass
class TokenUsage:
    """token 用量。

    Attributes:
        estimated: 流式响应时厂商常不返回 usage，只能本地估算。
            必须标记出来，否则账单对不上时无从查证。
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0        # 部分厂商支持 prompt 缓存，命中部分单价更低
    estimated: bool = False

    @property
    def total(self) -> int:
        """总 token 数。"""

        return self.input_tokens + self.output_tokens


@dataclass
class ChatResponse:
    """归一化的非流式对话响应。"""

    content: str
    model: str
    finish_reason: str = "stop"
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: TokenUsage = field(default_factory=TokenUsage)
    raw: dict[str, Any] = field(default_factory=dict)
    reasoning_content: str | None = None   # deepseek-reasoner 等推理模型的思维链


@dataclass
class ChatChunk:
    """归一化的流式分块。"""

    delta: str = ""
    finish_reason: str | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    reasoning_delta: str | None = None
    usage: TokenUsage | None = None        # 通常只在最后一块出现
    is_first: bool = False                 # 用于 time-to-first-token 打点


@dataclass
class EmbeddingResponse:
    """归一化的 embedding 响应。"""

    vectors: list[list[float]]
    model: str
    dimensions: int
    usage: TokenUsage = field(default_factory=TokenUsage)


@dataclass
class RerankResponse:
    """归一化的重排响应。``results`` 为 ``(原始下标, 分数)``，按分数降序。"""

    results: list[tuple[int, float]]
    model: str
    usage: TokenUsage = field(default_factory=TokenUsage)


# ---------------------------------------------------------------------------
# Provider 抽象
# ---------------------------------------------------------------------------
class BaseProvider(abc.ABC):
    """厂商适配基类。

    每个实例持有一个进程级复用的 ``httpx.AsyncClient``：
    每请求新建连接会让 TLS 握手成为主要延迟来源。
    """

    #: 厂商标识，与 config.yaml 的 providers 键一致
    name: str = ""

    def __init__(self, config: ProviderConfig) -> None:
        self.config = config
        self._client: Any | None = None    # httpx.AsyncClient

    # -- 生命周期 ------------------------------------------------------------

    async def startup(self) -> None:
        """建立连接池。

        TODO: httpx.AsyncClient(
                  base_url=config.base_url,
                  timeout=httpx.Timeout(config.timeout_seconds, connect=config.connect_timeout_seconds),
                  limits=httpx.Limits(max_connections=config.max_connections),
                  http2=True)
        """

        raise NotImplementedError

    async def close(self) -> None:
        """关闭连接池。"""

        # TODO: if self._client: await self._client.aclose()

    async def ping(self) -> bool:
        """健康探测。

        TODO: 优先调厂商的 /models 之类的轻量端点；没有则跳过（返回 True），
              不要为了探活去真发一次推理请求——那是在烧钱。
        """

        raise NotImplementedError

    # -- 能力接口 ------------------------------------------------------------

    @abc.abstractmethod
    async def chat(self, spec: ModelSpec, payload: dict[str, Any]) -> ChatResponse:
        """非流式对话补全。

        Args:
            spec: 目标模型（含 upstream_name 与不支持的参数清单）。
            payload: OpenAI 格式请求体（已由网关裁剪过参数）。

        Raises:
            ProviderError: 归一化后的错误。
        """

    @abc.abstractmethod
    def chat_stream(self, spec: ModelSpec, payload: dict[str, Any]) -> AsyncIterator[ChatChunk]:
        """流式对话补全。

        返回异步生成器。注意实现要点：
        - 首块必须置 ``is_first=True``，供 TTFT 打点
        - 上游断流要抛 ``ProviderConnectionError``，不能静默结束
          （静默结束会让业务以为回答完了，得到截断内容）
        """

    async def embed(self, spec: ModelSpec, texts: Sequence[str]) -> EmbeddingResponse:
        """向量化。默认抛不支持，由具备 embedding 能力的厂商覆写。"""

        raise NotImplementedError(f"{self.name} does not support embeddings")

    async def rerank(self, spec: ModelSpec, query: str, documents: Sequence[str]) -> RerankResponse:
        """重排。默认抛不支持。"""

        raise NotImplementedError(f"{self.name} does not support rerank")

    # -- 工具方法 ------------------------------------------------------------

    def build_headers(self) -> dict[str, str]:
        """构造认证头。多数厂商用 Bearer，个别厂商自定义头则覆写此方法。"""

        return {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }

    def normalize_error(self, status_code: int, body: dict[str, Any] | str) -> ProviderError:
        """把厂商错误响应映射为 ProviderError 子类。

        映射基线（各厂商可覆写补充自家错误码）：
        401/403 → Auth；429 → RateLimit；400/422 → InvalidRequest；5xx → ServerError

        TODO: 各厂商的内容安全拦截错误码不同，需在子类里识别并映射为
              ProviderContentFilterError，否则会被误判为 InvalidRequest 而不降级。
        """

        message = str(body)[:500]
        if status_code in (401, 403):
            return ProviderAuthError(message, provider=self.name, status_code=status_code)
        if status_code == 429:
            return ProviderRateLimitError(message, provider=self.name, status_code=status_code)
        if 400 <= status_code < 500:
            return ProviderInvalidRequestError(message, provider=self.name, status_code=status_code)
        return ProviderServerError(message, provider=self.name, status_code=status_code)

    def prepare_payload(self, spec: ModelSpec, payload: dict[str, Any]) -> dict[str, Any]:
        """把网关请求体转为厂商请求体。

        统一动作：替换 model 为 upstream_name、裁剪不支持的参数。
        厂商特有的参数改名在子类里做。
        """

        body = spec.sanitize_params(dict(payload))
        body["model"] = spec.upstream_name
        return body


class OpenAICompatibleProvider(BaseProvider):
    """OpenAI 兼容厂商的通用实现。

    国产主流厂商（Qwen 兼容模式、DeepSeek、GLM v4、Yi）都提供 OpenAI 兼容端点，
    因此绝大部分逻辑可复用；子类只需声明差异点。
    """

    #: 各端点路径，个别厂商不同则在子类覆盖
    chat_path: str = "/chat/completions"
    embedding_path: str = "/embeddings"
    rerank_path: str = "/rerank"

    async def chat(self, spec: ModelSpec, payload: dict[str, Any]) -> ChatResponse:
        """POST /chat/completions。

        TODO:
          1. body = self.prepare_payload(spec, payload)
          2. POST，非 2xx 走 normalize_error
          3. 解析 choices[0].message，含 tool_calls 与 reasoning_content
          4. 解析 usage（注意各厂商字段名差异，如 prompt_tokens vs input_tokens）
        """

        raise NotImplementedError

    async def chat_stream(self, spec: ModelSpec, payload: dict[str, Any]) -> AsyncIterator[ChatChunk]:
        """SSE 流式解析。

        TODO:
          1. body["stream"] = True；部分厂商需额外 stream_options={"include_usage": True}
             才会在末块返回 usage
          2. 用 client.stream("POST", ...) 逐行读，跳过空行与注释行
          3. "data: [DONE]" 结束
          4. 首个有内容的块置 is_first=True
          5. 上游异常中断 → 抛 ProviderConnectionError（不可静默 return）
        """

        raise NotImplementedError

    async def embed(self, spec: ModelSpec, texts: Sequence[str]) -> EmbeddingResponse:
        """POST /embeddings。

        TODO: 注意各厂商单次批量上限不同（如 25 / 64），需在子类声明并自动切批。
        """

        raise NotImplementedError


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------
def build_providers(providers_cfg: dict[str, Any]) -> dict[str, BaseProvider]:
    """按配置构造所有启用的 provider 实例。

    未配置凭证的厂商**不构造**（相关模型会在注册表里标记 UNAVAILABLE），
    但不阻止启动——允许只配一家厂商就跑起来。

    TODO: 导入各具体 provider 类并按 name 分派；
          对 type=openai_compatible 的未知厂商回退到 OpenAICompatibleProvider。
    """

    raise NotImplementedError


__all__ = [
    "BaseProvider",
    "ChatChunk",
    "ChatResponse",
    "EmbeddingResponse",
    "OpenAICompatibleProvider",
    "ProviderAuthError",
    "ProviderConnectionError",
    "ProviderContentFilterError",
    "ProviderError",
    "ProviderInvalidRequestError",
    "ProviderRateLimitError",
    "ProviderServerError",
    "ProviderTimeoutError",
    "RerankResponse",
    "TokenUsage",
    "build_providers",
]
