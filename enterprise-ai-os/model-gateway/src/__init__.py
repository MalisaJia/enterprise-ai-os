"""model-gateway —— Enterprise AI OS 的统一模型出口。

包结构::

    src/
      main.py        FastAPI 应用装配
      registry/      模型注册中心（能力、单价、健康状态）
      router/        路由策略与降级链
      providers/     厂商适配（Qwen / DeepSeek / GLM / Yi）
      middleware/    限流、熔断、审计
      api/           OpenAI 兼容端点

安全红线：
- 厂商 API Key 只存在于本服务的环境变量中，不下发给任何其他组件。
- 不记录 prompt / completion 原文，只记录哈希、长度与 token 数。
"""

__version__ = "0.1.0"
__all__ = ["__version__"]
