"""智谱 GLM（BigModel 开放平台）适配。

GLM 在本平台的定位：**超长上下文专精 + 高质量备选**。
``glm-4-long`` 支持 1M token 上下文且单价极低，是"整本手册塞进去问一句"
这类场景的唯一经济选择——因此在 ``auto:long`` 别名中排第一。

需要注意的差异：

1. GLM v4 的 base_url 是 ``/api/paas/v4``，OpenAI 兼容但路径前缀不同。
2. GLM 的鉴权历史上用过 JWT（api_key 拆分为 id.secret 后签名）。
   v4 已支持直接用 api_key 作 Bearer，我们只支持后者——
   JWT 方案需要在网关里做时钟同步与缓存，复杂度不划算。
3. ``do_sample=false`` 时 GLM 会忽略 temperature/top_p，语义与 OpenAI 不同。
"""

from __future__ import annotations

import logging
from typing import Any, Sequence

from ..registry.models import ModelSpec
from . import EmbeddingResponse, OpenAICompatibleProvider, ProviderError

logger = logging.getLogger(__name__)


class GLMProvider(OpenAICompatibleProvider):
    """智谱 GLM 适配器。"""

    name = "glm"

    #: embedding-3 单批上限
    EMBEDDING_BATCH_LIMIT = 64

    #: GLM 内容安全拦截：错误码 1301（内容不合规）
    CONTENT_FILTER_CODES = ("1301", "1302")

    def prepare_payload(self, spec: ModelSpec, payload: dict[str, Any]) -> dict[str, Any]:
        """构造 GLM 请求体。

        TODO:
          1. 调父类基础转换
          2. temperature 取值范围是 (0, 1]，**不含 0**。OpenAI 允许 0，
             业务传 0 时需改写为一个极小值（如 0.01），否则 400
          3. GLM 的 tools 支持 web_search / retrieval 等内置工具，
             与业务自定义 function 混用时需保留原顺序
          4. glm-4-long 的 max_tokens 上限较小（4096），超出时截断
        """

        raise NotImplementedError

    def normalize_error(self, status_code: int, body: dict[str, Any] | str) -> ProviderError:
        """GLM 错误映射。

        GLM 的业务错误码放在响应体 ``error.code`` 里，且 **HTTP 状态可能是 200**。
        这点必须小心：只看 status_code 会把失败当成功，把错误文案当作模型回答返回给用户。

        TODO:
          1. 先看 body.error.code；1301/1302 → ProviderContentFilterError
          2. 1113（余额不足）→ ProviderServerError（可降级）
          3. 1302（并发超限）→ ProviderRateLimitError
        """

        raise NotImplementedError

    async def embed(self, spec: ModelSpec, texts: Sequence[str]) -> EmbeddingResponse:
        """embedding-3 向量化（2048 维）。

        注意维度是 2048，与默认的 bge-m3（1024 维）不同。
        作为 embedding fallback 时**不能**直接混用——向量维度不同无法写进同一索引。
        因此 embedding 的降级链只在同维度模型间生效，或需要整库重建。

        TODO: 实现调用；并在 registry 加载时校验 embedding 降级链的维度一致性。
        """

        raise NotImplementedError
