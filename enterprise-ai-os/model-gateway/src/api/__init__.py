"""HTTP API 层：OpenAI 兼容接口 + 管理接口。

拆分成两组路由的原因：``/v1/*`` 是给业务用的、必须与 OpenAI SDK 逐字段兼容；
``/admin/*`` 是给运维用的、可以自由设计。混在一个 router 里会让鉴权策略
（业务 key vs 管理员 key）难以区分。
"""

from .routes import admin_router, router

__all__ = ["router", "admin_router"]
