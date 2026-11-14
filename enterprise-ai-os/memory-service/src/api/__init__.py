"""REST 路由与 Pydantic 契约。

    api/routes.py   /api/v1 下的全部端点

调用方主要是 octo-server 与各 Agent，不是浏览器。
因此接口设计偏向"一次调用拿全所需"而非 REST 纯粹性——
Agent 在关键路径上，每多一次往返都直接体现为首字延迟。
``/context`` 就是这个取舍的产物。
"""

from __future__ import annotations

from .routes import router

__all__ = ["router"]
