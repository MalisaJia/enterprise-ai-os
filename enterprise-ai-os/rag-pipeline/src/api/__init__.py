"""REST API 层。

只做三件事：请求校验（Pydantic）、调用领域对象、响应序列化。
业务逻辑一律下沉到 ``ingestion`` / ``retrieval``，路由里不写算法。
"""

from .routes import router

__all__ = ["router"]
