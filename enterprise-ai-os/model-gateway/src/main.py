"""FastAPI 入口：配置加载、注册表初始化、provider 连接池、路由注册。

本地运行::

    uvicorn src.main:app --reload --port 8200
"""

from __future__ import annotations

import logging
import os
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

import yaml
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from . import __version__
from .api.routes import admin_router, router as api_router

logger = logging.getLogger("model-gateway")

CONFIG_PATH_ENV = "GATEWAY_CONFIG_PATH"
DEFAULT_CONFIG_PATH = "config/config.yaml"

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
def _expand_env(value: Any) -> Any:
    """递归展开 ``${VAR:-default}``。凭证只走这条路进入配置，不落文件。"""

    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    if isinstance(value, str):

        def _sub(match: re.Match[str]) -> str:
            return os.environ.get(match.group(1), match.group(2) or "")

        return _ENV_PATTERN.sub(_sub, value)
    return value


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """加载并展开 YAML 配置。

    TODO: 用 pydantic 建模强校验，启动即拒绝非法配置——
          尤其是 routing.fallback.chains 中引用了不存在的 model id
          （这类错误在运行时才暴露，恰好发生在故障降级时，最糟糕的时机）。
    """

    config_path = Path(path or os.environ.get(CONFIG_PATH_ENV, DEFAULT_CONFIG_PATH))
    if not config_path.exists():
        logger.warning("config file %s not found, falling back to defaults", config_path)
        return {}
    with config_path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    return _expand_env(raw)


@dataclass
class AppContext:
    """进程级依赖容器，挂载在 ``app.state.ctx``。"""

    config: dict[str, Any] = field(default_factory=dict)
    registry: Any | None = None          # registry.models.ModelRegistry
    model_router: Any | None = None      # router.strategy.ModelRouter
    providers: dict[str, Any] = field(default_factory=dict)   # name -> BaseProvider
    rate_limiter: Any | None = None      # middleware.rate_limiter.RateLimiter
    breaker_registry: Any | None = None  # middleware.circuit_breaker.CircuitBreakerRegistry
    meter: Any | None = None             # 用量计量器
    ready: bool = False

    def get(self, dotted_key: str, default: Any = None) -> Any:
        """按 ``"routing.default_strategy"`` 形式读取嵌套配置。"""

        node: Any = self.config
        for part in dotted_key.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node


def configure_logging(config: dict[str, Any]) -> None:
    """初始化日志。

    TODO: structlog + JSON；processor 中强制剔除 messages / input 字段，
          从机制上保证 prompt 原文不可能被误打到日志。
    """

    level_name = str(config.get("logging", {}).get("level", "INFO")).upper()
    logging.basicConfig(
        level=getattr(logging, level_name, logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )


# ---------------------------------------------------------------------------
# 生命周期
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """启动装配 / 关闭清理。

    provider 的 httpx 连接池必须进程级复用：每请求新建连接会让 TLS 握手
    成为主要延迟来源，并迅速耗尽本地端口。
    """

    ctx: AppContext = app.state.ctx

    # --- 启动 ---
    # TODO: 1. 构建模型注册表并校验：每个 model 的 provider 是否 enabled 且有 api_key，
    #          没有可用凭证的模型应标记 unavailable 而非启动失败（允许只配一家厂商）
    #          ctx.registry = ModelRegistry.from_config(ctx.get("registry", {}), ctx.get("providers", {}))
    # TODO: 2. 为每个启用的 provider 建 httpx.AsyncClient（限连接数、开 HTTP/2）
    #          ctx.providers = build_providers(ctx.get("providers", {}))
    # TODO: 3. ctx.rate_limiter = RateLimiter.from_config(...)（连 Redis）
    # TODO: 4. ctx.breaker_registry = CircuitBreakerRegistry.from_config(...)
    # TODO: 5. ctx.model_router = ModelRouter(registry, breaker_registry, ctx.get("routing", {}))
    # TODO: 6. ctx.meter 启动后台 flush 协程（每 flush_interval_seconds 落账）
    # TODO: 7. registry.hot_reload 为真时启动配置监听协程
    logger.info("model-gateway starting, version=%s env=%s", __version__, ctx.get("service.env", "dev"))
    ctx.ready = True

    try:
        yield
    finally:
        # --- 关闭 ---
        # 先停止接收新请求（由编排层摘流量），再 flush 用量，最后关连接池。
        # 顺序错了会丢账单数据。
        ctx.ready = False
        # TODO: await ctx.meter.flush()
        # TODO: for p in ctx.providers.values(): await p.close()
        # TODO: await ctx.rate_limiter.close()
        logger.info("model-gateway stopped")


# ---------------------------------------------------------------------------
# 应用装配
# ---------------------------------------------------------------------------
def create_app(config: dict[str, Any] | None = None) -> FastAPI:
    """构造 FastAPI 应用（工厂模式，便于测试注入配置）。"""

    cfg = config if config is not None else load_config()
    configure_logging(cfg)

    service_cfg = cfg.get("service", {}) or {}
    app = FastAPI(
        title="Enterprise AI OS · Model Gateway",
        description="OpenAI 兼容的统一模型网关：路由、限流、熔断、降级、计量",
        version=str(service_cfg.get("version", __version__)),
        root_path=str(service_cfg.get("root_path", "") or ""),
        lifespan=lifespan,
    )
    app.state.ctx = AppContext(config=cfg)

    # TODO: 中间件注册顺序（自外向内）：
    #   1. RequestIdMiddleware  —— 生成 trace_id
    #   2. AuthMiddleware       —— octo-auth / static key，解析 tenant_id/user_id
    #   3. RateLimitMiddleware  —— 三级令牌桶，超限直接 429（不进业务逻辑）
    #   4. QuotaMiddleware      —— 月度预算
    #   5. MeteringMiddleware   —— 出口处采集 usage 与耗时
    #   顺序不能乱：限流必须在鉴权之后（要拿到 tenant_id），在业务之前（不浪费资源）。

    app.include_router(api_router, prefix="/v1", tags=["gateway"])
    app.include_router(admin_router, prefix="/admin", tags=["admin"])
    _register_probes(app)
    return app


def _register_probes(app: FastAPI) -> None:
    """探针与指标端点。"""

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> JSONResponse:
        """存活探针。"""

        return JSONResponse({"status": "ok", "service": "model-gateway", "version": __version__})

    @app.get("/readyz", include_in_schema=False)
    async def readyz(request: Request) -> JSONResponse:
        """就绪探针。

        判定标准：**至少一个模型可用**。全部厂商都不可用时才算未就绪——
        单家厂商挂掉不应把整个网关摘掉，那正是 fallback 存在的意义。

        TODO: 统计 registry 中 available 的模型数与各 breaker 状态。
        """

        ctx: AppContext = request.app.state.ctx
        checks = {
            "registry": ctx.registry is not None,
            "router": ctx.model_router is not None,
            "providers": len(ctx.providers),
            "rate_limiter": ctx.rate_limiter is not None,
        }
        return JSONResponse({"ready": ctx.ready, "checks": checks}, status_code=200 if ctx.ready else 503)

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> PlainTextResponse:
        """Prometheus 指标。

        TODO: 核心指标——
              gateway_requests_total{model,provider,status}
              gateway_request_duration_seconds{model}
              gateway_time_to_first_token_seconds{model}
              gateway_tokens_total{model,type=input|output}
              gateway_cost_usd_total{tenant,model}
              gateway_fallback_total{from,to,reason}
              gateway_circuit_state{model}  (0=closed 1=half_open 2=open)
              gateway_rate_limited_total{tier}
        """

        return PlainTextResponse("# TODO: expose prometheus metrics\n")


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "src.main:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8200")),
        reload=os.environ.get("ENV", "dev") == "dev",
    )
