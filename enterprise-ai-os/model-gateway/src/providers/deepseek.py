"""DeepSeek 适配。

DeepSeek 在本平台的定位：**性价比主力 + 代码与推理专精**。
单价约为同档模型的 1/5，因此在 registry 中给了最高权重（120）。

两个必须特殊处理的地方：

1. ``deepseek-reasoner``（推理模型）**不接受** ``temperature`` / ``top_p`` /
   ``tools`` / ``tool_choice``，传了直接 400。已在 config 的
   ``unsupported_params`` 中声明，由 ``ModelSpec.sanitize_params`` 统一裁剪。
2. 推理模型返回 ``reasoning_content``（思维链）与 ``content`` 两个字段。
   思维链**不应**默认回传给业务：它很长（占大量带宽与前端渲染成本）
   且不该被拼进下一轮对话的 messages（官方明确要求剔除，否则会 400）。
"""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator

from ..registry.models import ModelSpec
from . import ChatChunk, ChatResponse, OpenAICompatibleProvider, ProviderError

logger = logging.getLogger(__name__)


class DeepSeekProvider(OpenAICompatibleProvider):
    """DeepSeek 适配器。"""

    name = "deepseek"

    #: 推理模型 ID（上游名），需要特殊的字段处理
    REASONER_MODELS = ("deepseek-reasoner",)

    #: 是否默认把思维链回传给业务。默认关闭，业务需显式请求。
    return_reasoning_by_default = False

    def is_reasoner(self, spec: ModelSpec) -> bool:
        """判断是否推理模型。"""

        return spec.upstream_name in self.REASONER_MODELS

    def prepare_payload(self, spec: ModelSpec, payload: dict[str, Any]) -> dict[str, Any]:
        """构造 DeepSeek 请求体。

        TODO:
          1. 调父类做基础转换（含 unsupported_params 裁剪）
          2. 若 is_reasoner：从历史 messages 中剥离 assistant 的 reasoning_content
             字段——带上去会被上游拒绝（400）
          3. max_tokens 超过模型上限时截到 spec.max_output_tokens 而非报错
        """

        raise NotImplementedError

    async def chat(self, spec: ModelSpec, payload: dict[str, Any]) -> ChatResponse:
        """非流式调用。

        TODO: 解析 reasoning_content 到 ChatResponse.reasoning_content；
              usage 中的 prompt_cache_hit_tokens 映射到 TokenUsage.cached_tokens
              （命中缓存部分单价低 10 倍，成本核算必须区分）。
        """

        raise NotImplementedError

    async def chat_stream(self, spec: ModelSpec, payload: dict[str, Any]) -> AsyncIterator[ChatChunk]:
        """流式调用。

        推理模型的流式有两阶段：先吐 ``reasoning_content`` 增量，
        再吐 ``content`` 增量。要分别映射到 ``reasoning_delta`` 与 ``delta``，
        混在一起会让前端把思维链当成正式回答展示。

        TODO: 实现两阶段分流；末块通过 stream_options.include_usage 取 usage。
        """

        raise NotImplementedError

    def normalize_error(self, status_code: int, body: dict[str, Any] | str) -> ProviderError:
        """DeepSeek 错误映射。

        特别注意：DeepSeek 在余额不足时返回 402 Insufficient Balance。
        这不是"参数错误"，必须映射为可降级错误，否则余额耗尽会导致
        请求直接失败而不切换到其他厂商。

        TODO: status_code == 402 → ProviderServerError（可降级）并打 CRITICAL 告警。
        """

        raise NotImplementedError
