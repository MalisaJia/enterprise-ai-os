"""REST / WebSocket 路由与 Pydantic 契约。

    api/routes.py   /api/v1 下的全部端点

调用方是 octo-server 的会议模块与运营后台，也有浏览器（纪要查看与人工修正
页面），因此这里比 memory-service 更接近常规 REST 风格，并保留 CORS。

一条贯穿全局的设计：**所有产出都带 version**。prompt 与模型会持续迭代，
``/reprocess`` 每次产生新版本而不覆盖，接口默认返回最新版本、
可用 ``?version=`` 取历史版本做效果对比。
"""

from __future__ import annotations

from .routes import router

__all__ = ["router"]
