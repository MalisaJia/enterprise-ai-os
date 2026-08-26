"""通义千问 / 阿里云百炼（DashScope）适配。

接入方式选择：百炼同时提供**原生 DashScope 协议**与 **OpenAI 兼容模式**。
我们走兼容模式（``/compatible-mode/v1``），理由是能直接复用
``OpenAICompatibleProvider``，且升级风险低。代价是少数原生特性
（如 DashScope 独有的插件调用）用不了——目前业务不需要。

Qwen 在本平台的定位：**综合均衡的主力模型**。中文能力强、tool calling 稳定、
qwen-plus 有 128K 上下文，同时其 ``text-embedding-v3`` 是我们的默认 embedding
与 rerank 供应方（见 registry 配置）。
"""

from __future__ import annotations

import logging
from typing import Any, Sequence

from ..registry.models import ModelSpec
from . import (
    EmbeddingResponse,
    OpenAICompatibleProvider,
    ProviderContentFilterError,
    ProviderError,
    RerankResponse,
)

logger = logging.getLogger(__name__)


class QwenProvider(OpenAICompatibleProvider):
    """Qwen（阿里云百炼）适配器。"""

    name = "qwen"

    #: 百炼 embedding 单批上限 25 条，超出会 400。必须自动切批。
    EMBEDDING_BATCH_LIMIT = 25

    #: 百炼内容安全拦截的错误码前缀
    CONTENT_FILTER_CODES = ("DataInspectionFailed", "InvalidParameter.DataInspection")

    def prepare_payload(self, spec: ModelSpec, payload: dict[str, Any]) -> dict[str, Any]:
        """转换为百炼兼容模式请求体。

        Qwen 特殊处理：
        - 流式默认是"增量输出"，与 OpenAI 语义一致，无需额外参数
        - 需要末块 usage 时必须传 ``stream_options={"include_usage": true}``
        - ``enable_search``（联网搜索）是 Qwen 独有开关，如业务需要则从
          扩展字段透传

        TODO: 实现上述映射。
        """

        raise NotImplementedError

    def normalize_error(self, status_code: int, body: dict[str, Any] | str) -> ProviderError:
        """识别百炼特有错误码。

        TODO: body 中 code 命中 CONTENT_FILTER_CODES → ProviderContentFilterError；
              其余交给父类默认映射。
        """

        raise NotImplementedError

    async def embed(self, spec: ModelSpec, texts: Sequence[str]) -> EmbeddingResponse:
        """text-embedding-v3 向量化。

        TODO:
          1. 按 EMBEDDING_BATCH_LIMIT 切批，并发（受 max_connections 限制）
          2. v3 支持指定 dimensions（1024/768/512），需与 rag-pipeline 索引维度一致，
             不一致要在启动校验时就拦住
          3. 合并结果时严格保持输入顺序
        """

        raise NotImplementedError

    async def rerank(self, spec: ModelSpec, query: str, documents: Sequence[str]) -> RerankResponse:
        """gte-rerank 重排。

        注意：百炼的 rerank 走 DashScope 原生端点
        （``/api/v1/services/rerank/text-rerank/text-rerank``），
        **不在** OpenAI 兼容路径下，需单独构造 URL 与请求体。

        TODO: 实现原生端点调用与结果归一化（返回 (原始下标, 分数) 降序）。
        """

        raise NotImplementedError
