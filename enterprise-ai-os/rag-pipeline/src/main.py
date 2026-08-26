"""FastAPI 入口：配置加载、依赖装配、生命周期管理、路由注册。

本地运行::

    uvicorn src.main:app --reload --port 8100
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
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse

from . import __version__
from .api.routes import router as api_router

logger = logging.getLogger("rag-pipeline")

CONFIG_PATH_ENV = "RAG_CONFIG_PATH"
DEFAULT_CONFIG_PATH = "config/config.yaml"

# 匹配 ${VAR} 与 ${VAR:-default}
_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
def _expand_env(value: Any) -> Any:
    """递归展开配置中的 ``${VAR:-default}`` 占位符。

    环境变量优先级高于 YAML 字面值，便于容器化注入。
    """

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
    """加载 YAML 配置并展开环境变量。

    配置缺失时不报错——脚手架阶段允许全部走默认值，方便裸跑 ``uvicorn``。

    TODO: 用 pydantic-settings 建模为强类型 Settings，在启动时校验必填项
          （DATABASE_URL / MODEL_GATEWAY_URL / embedding.dimensions 等）。
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
    """进程级单例容器，挂载在 ``app.state.ctx``。

    统一持有需要复用连接池的长生命周期对象，避免每请求重建。
    """

    config: dict[str, Any] = field(default_factory=dict)
    embedding_provider: Any | None = None       # embeddings.provider.EmbeddingProvider
    reranker: Any | None = None                 # embeddings.provider.Reranker
    vector_store: Any | None = None             # retrieval.engine.VectorStore
    retrieval_engine: Any | None = None         # retrieval.engine.RetrievalEngine
    ingestion_pipeline: Any | None = None       # ingestion.pipeline.IngestionPipeline
    kafka_consumer: Any | None = None
    ready: bool = False

    def get(self, dotted_key: str, default: Any = None) -> Any:
        """按 ``"embedding.model"`` 形式读取嵌套配置。"""

        node: Any = self.config
        for part in dotted_key.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node


def configure_logging(config: dict[str, Any]) -> None:
    """按配置初始化日志。

    TODO: 接入 structlog，输出 JSON 结构化日志，并注入 trace_id / tenant_id。
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
    """启动时装配依赖，关闭时优雅释放。

    启动顺序有讲究：向量库先就绪，再起 Kafka 消费，否则消息到达时无处落盘。
    """

    ctx: AppContext = app.state.ctx

    # --- 启动 ---
    # TODO: 1. 初始化 embedding provider
    #          ctx.embedding_provider = build_embedding_provider(ctx.get("embedding", {}))
    # TODO: 2. 初始化 reranker（provider=none 时为 None）
    # TODO: 3. 初始化向量存储（pgvector / milvus），执行 schema 迁移与索引检查
    #          ctx.vector_store = build_vector_store(ctx.get("vector_store", {}))
    # TODO: 4. 组装 RetrievalEngine（注入 vector_store / sparse client / reranker）
    # TODO: 5. 组装 IngestionPipeline
    # TODO: 6. 若 kafka.enabled，启动消费者协程（aios.document.ingest / octo.file.uploaded）
    # TODO: 7. OTel instrumentation（observability.tracing_enabled）
    logger.info("rag-pipeline starting, version=%s env=%s", __version__, ctx.get("service.env", "dev"))
    ctx.ready = True

    try:
        yield
    finally:
        # --- 关闭 ---
        # 顺序与启动相反：先停止接收新消息，再关闭连接池，避免处理中任务丢失。
        ctx.ready = False
        # TODO: await ctx.kafka_consumer.stop()
        # TODO: await ctx.vector_store.close()
        # TODO: await ctx.embedding_provider.close()
        logger.info("rag-pipeline stopped")


# ---------------------------------------------------------------------------
# 应用装配
# ---------------------------------------------------------------------------
def create_app(config: dict[str, Any] | None = None) -> FastAPI:
    """构造 FastAPI 应用。

    抽成工厂函数是为了让测试能注入自定义配置，而不依赖磁盘上的 config.yaml。
    """

    cfg = config if config is not None else load_config()
    configure_logging(cfg)

    service_cfg = cfg.get("service", {}) or {}
    app = FastAPI(
        title="Enterprise AI OS · RAG Pipeline",
        description="文档摄入、分块、向量化与混合检索服务",
        version=str(service_cfg.get("version", __version__)),
        root_path=str(service_cfg.get("root_path", "") or ""),
        lifespan=lifespan,
    )
    app.state.ctx = AppContext(config=cfg)

    cors_origins = service_cfg.get("cors_origins") or []
    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    # TODO: 注册中间件
    #   - RequestIdMiddleware: 生成/透传 X-Request-Id 作为 trace_id
    #   - OctoAuthMiddleware: 校验 octo-auth 令牌，注入 tenant_id/space_id 到 request.state
    #   - MetricsMiddleware: 请求量/时延/错误率打点

    app.include_router(api_router, prefix="/v1", tags=["rag"])
    _register_probes(app)
    return app


def _register_probes(app: FastAPI) -> None:
    """注册探针与指标端点（不走 /v1 前缀，也不需要鉴权）。"""

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> JSONResponse:
        """存活探针：进程活着即返回 200，不做下游依赖检查。"""

        return JSONResponse({"status": "ok", "service": "rag-pipeline", "version": __version__})

    @app.get("/readyz", include_in_schema=False)
    async def readyz(request: Request) -> JSONResponse:
        """就绪探针：依赖未就绪时返回 503，避免流量打到未初始化实例。

        TODO: 实际探测 vector_store.ping() / embedding_provider.ping() / kafka 连接。
        """

        ctx: AppContext = request.app.state.ctx
        checks = {
            "vector_store": ctx.vector_store is not None,
            "embedding_provider": ctx.embedding_provider is not None,
            "retrieval_engine": ctx.retrieval_engine is not None,
        }
        status_code = 200 if ctx.ready else 503
        return JSONResponse({"ready": ctx.ready, "checks": checks}, status_code=status_code)

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> PlainTextResponse:
        """Prometheus 指标。

        TODO: 返回 prometheus_client.generate_latest()；核心指标：
              rag_search_latency_seconds / rag_ingest_documents_total /
              rag_embedding_tokens_total / rag_rerank_fallback_total
        """

        return PlainTextResponse("# TODO: expose prometheus metrics\n")


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "src.main:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8100")),
        reload=os.environ.get("ENV", "dev") == "dev",
    )
