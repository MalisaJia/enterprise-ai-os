"""零一万物 Yi（Lingyiwanwu）适配。

Yi 在本平台的定位：**第三方备份**。在 registry 中 priority=3、weight=60，
正常流量不会路由到它；只在 Qwen 与 DeepSeek 同时不可用时兜底。

为什么仍要接：厂商级单点故障是真实存在的风险（备案、限流政策、区域网络），
降级链上必须至少有一个"不同厂商、不同网络路径"的选项。
"""

from __future__ import annotations

import logging
from typing import Any

from ..registry.models import ModelSpec
from . import OpenAICompatibleProvider, ProviderError

logger = logging.getLogger(__name__)


class YiProvider(OpenAICompatibleProvider):
    """零一万物 Yi 适配器。"""

    name = "yi"

    def prepare_payload(self, spec: ModelSpec, payload: dict[str, Any]) -> dict[str, Any]:
        """构造 Yi 请求体。

        Yi 的 OpenAI 兼容度较高，主要差异：
        - 不支持 ``n > 1``（多候选生成），传了会 400
        - ``max_tokens`` 上限 4096

        TODO: 实现参数裁剪与上限截断。
        """

        raise NotImplementedError

    def normalize_error(self, status_code: int, body: dict[str, Any] | str) -> ProviderError:
        """Yi 错误映射。

        TODO: 沿用父类默认映射；补充识别其内容审核错误码。
              作为兜底 provider，这里的错误应该额外打 CRITICAL 日志——
              走到 Yi 说明前面两家都挂了，属于需要立刻介入的状况。
        """

        raise NotImplementedError
