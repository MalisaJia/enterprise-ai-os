"""rag-pipeline REST 路由与 API 契约。

契约设计原则：
- 摄入是异步的：``POST /documents`` 立即返回 ``job_id``，客户端轮询 ``/jobs/{id}``。
- 检索是同步的：``POST /search`` 必须在 1s 内返回，超时降级（跳过重排）而非报错。
- 多租户强隔离：``tenant_id`` / ``space_id`` 从鉴权上下文取，**不接受**客户端在 body 里指定。
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

router = APIRouter()


# ---------------------------------------------------------------------------
# 鉴权上下文
# ---------------------------------------------------------------------------
class AuthContext(BaseModel):
    """由 octo-auth 校验后注入的调用方身份。"""

    tenant_id: str
    space_id: str | None = None
    user_id: str | None = None
    # 服务间调用（memory-service / meeting-agent）时为 True，跳过用户级权限过滤
    is_internal: bool = False


async def get_auth(request: Request) -> AuthContext:
    """从请求中提取鉴权上下文。

    TODO: 调用 octo-auth 的 /v1/verify 校验 Authorization 头，
          支持 session / bot-key / user-key 三种令牌，结果按 auth.cache_ttl_seconds 缓存。
          若 config.auth.enabled 为 false（本地开发），返回 dev 默认租户。
    """

    ctx = request.app.state.ctx
    if not ctx.get("auth.enabled", True):
        return AuthContext(tenant_id="dev", space_id="dev", user_id="dev", is_internal=True)
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="octo-auth integration not implemented yet",
    )


def get_retrieval_engine(request: Request) -> Any:
    """取进程级 RetrievalEngine 单例。"""

    engine = request.app.state.ctx.retrieval_engine
    if engine is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="retrieval engine not initialized",
        )
    return engine


def get_ingestion_pipeline(request: Request) -> Any:
    """取进程级 IngestionPipeline 单例。"""

    pipeline = request.app.state.ctx.ingestion_pipeline
    if pipeline is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ingestion pipeline not initialized",
        )
    return pipeline


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------
class SourceType(str, Enum):
    """文档来源。决定 fetch 阶段的取数方式。"""

    S3 = "s3"                 # 对象存储 key
    URL = "url"               # 可访问的 HTTP 链接
    INLINE = "inline"         # 请求体直接带文本
    OCTO_FILE = "octo_file"   # OCTO 频道上传的文件


class JobStatus(str, Enum):
    """摄入任务状态机：pending → running → (succeeded | failed | skipped)。"""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"       # content_hash 未变，幂等跳过


class DocumentIngestRequest(BaseModel):
    """提交一篇文档进入摄入管道。"""

    collection: str = Field(..., description="知识集合，通常与 OCTO Space 一一对应")
    source_type: SourceType
    source: str = Field(..., description="s3 key / url / 内联文本 / octo file id")
    document_id: str | None = Field(
        default=None,
        description="业务侧稳定 ID；不传则由 content_hash 生成。重复提交同 ID 视为更新",
    )
    title: str | None = None
    mime_type: str | None = Field(default=None, description="留空则服务端嗅探")
    chunk_strategy: Literal["auto", "recursive", "markdown", "code", "semantic", "fixed"] = "auto"
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="随 chunk 一起存储，可用于检索过滤（如 doc_type / owner / version）",
    )
    force_reindex: bool = Field(default=False, description="忽略 content_hash 幂等判定，强制重建")


class IngestJobResponse(BaseModel):
    """摄入任务受理回执。"""

    job_id: str
    document_id: str
    status: JobStatus
    created_at: datetime


class IngestJobDetail(IngestJobResponse):
    """摄入任务进度详情。"""

    chunk_total: int = 0
    chunk_indexed: int = 0
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class DocumentDetail(BaseModel):
    """已入库文档的元信息。"""

    document_id: str
    collection: str
    title: str | None = None
    mime_type: str | None = None
    content_hash: str
    chunk_count: int
    metadata: dict[str, Any] = Field(default_factory=dict)
    indexed_at: datetime | None = None


class SearchFilters(BaseModel):
    """检索过滤条件。会被下推到向量库的 WHERE 子句，而非事后过滤。"""

    document_ids: list[str] | None = None
    doc_type: list[str] | None = None
    updated_after: datetime | None = None
    updated_before: datetime | None = None
    # 自定义 metadata 精确匹配
    metadata_match: dict[str, Any] | None = None


class SearchRequest(BaseModel):
    """混合检索请求。"""

    query: str = Field(..., min_length=1, max_length=4096)
    collection: str
    top_k: int = Field(default=20, ge=1, le=200, description="每路召回量")
    top_n: int = Field(default=5, ge=1, le=50, description="重排后返回量")
    alpha: float = Field(default=0.5, ge=0.0, le=1.0, description="0=纯BM25，1=纯向量")
    rerank: bool = True
    filters: SearchFilters | None = None
    # 是否返回相邻 chunk 拼接后的扩展上下文
    expand_context: bool = False


class SearchResultItem(BaseModel):
    """单条命中。分数拆开返回，便于线上调参与效果归因。"""

    chunk_id: str
    document_id: str
    content: str
    score: float = Field(..., description="最终排序分（重排后）")
    vector_score: float | None = None
    bm25_score: float | None = None
    rerank_score: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class SearchResponse(BaseModel):
    """检索响应。"""

    results: list[SearchResultItem]
    latency_ms: int
    reranked: bool = Field(..., description="false 表示重排降级或未启用")
    trace_id: str | None = None


class EmbeddingRequest(BaseModel):
    """OpenAI 兼容 embedding 请求（供 memory-service 等复用）。"""

    input: str | list[str]
    model: str | None = None
    normalize: bool = True


class EmbeddingResponse(BaseModel):
    """OpenAI 兼容 embedding 响应。"""

    model: str
    dimensions: int
    data: list[list[float]]
    usage: dict[str, int] = Field(default_factory=dict)


class CollectionInfo(BaseModel):
    """知识集合统计。"""

    name: str
    document_count: int
    chunk_count: int
    embedding_model: str
    dimensions: int
    updated_at: datetime | None = None


# ---------------------------------------------------------------------------
# 摄入
# ---------------------------------------------------------------------------
@router.post(
    "/documents",
    response_model=IngestJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="提交文档摄入（异步）",
)
async def ingest_document(
    payload: DocumentIngestRequest,
    background: BackgroundTasks,
    auth: AuthContext = Depends(get_auth),
    pipeline: Any = Depends(get_ingestion_pipeline),
) -> IngestJobResponse:
    """受理摄入请求并立即返回 job_id。

    返回 202 而非 200：解析大 PDF 可能耗时数分钟，同步等待必然超时。

    TODO:
      1. 校验 collection 是否属于 auth.tenant_id / space_id
      2. pipeline.submit(job) 落库 job 记录
      3. 高吞吐场景改为投递 Kafka(aios.document.ingest)，由消费者处理；
         BackgroundTasks 仅用于单机开发模式
    """

    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "ingest_document not implemented")


@router.get("/jobs/{job_id}", response_model=IngestJobDetail, summary="查询摄入任务进度")
async def get_job(
    job_id: str,
    auth: AuthContext = Depends(get_auth),
    pipeline: Any = Depends(get_ingestion_pipeline),
) -> IngestJobDetail:
    """查询任务状态与已索引 chunk 数。

    TODO: pipeline.get_job(job_id, tenant_id=auth.tenant_id)，跨租户访问返回 404 而非 403（避免探测）。
    """

    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "get_job not implemented")


@router.get("/documents/{document_id}", response_model=DocumentDetail, summary="查询文档索引状态")
async def get_document(
    document_id: str,
    auth: AuthContext = Depends(get_auth),
) -> DocumentDetail:
    """返回文档元信息与 chunk 数量。

    TODO: 从 vector_store 的 documents 表读取。
    """

    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "get_document not implemented")


@router.delete(
    "/documents/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除文档及其全部向量",
)
async def delete_document(
    document_id: str,
    auth: AuthContext = Depends(get_auth),
) -> None:
    """物理删除文档、chunk、向量与 BM25 索引条目。

    合规要求（数据下线 / 员工离职）必须真删而非软删。

    TODO: 事务内删 chunk → 删 document → 通知 octo-search-indexer 删稀疏索引 →
          发 Kafka 事件 aios.document.deleted。
    """

    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "delete_document not implemented")


# ---------------------------------------------------------------------------
# 检索
# ---------------------------------------------------------------------------
@router.post("/search", response_model=SearchResponse, summary="混合检索（主力接口）")
async def search(
    payload: SearchRequest,
    auth: AuthContext = Depends(get_auth),
    engine: Any = Depends(get_retrieval_engine),
) -> SearchResponse:
    """向量 + BM25 混合召回，RRF 融合后 cross-encoder 重排。

    这是被 Agent 桥接器高频调用的接口，性能与稳定性优先级最高：
    重排超时不报错，降级为融合分数排序并把 ``reranked`` 置 false。

    TODO:
      1. 构造 SearchQuery（注入 tenant_id/space_id 做行级过滤）
      2. await engine.search(query)
      3. 记录慢查询（> observability.slow_search_ms）
      4. 命中 Redis 短缓存则直接返回
    """

    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "search not implemented")


@router.post("/embeddings", response_model=EmbeddingResponse, summary="Embedding 直通")
async def create_embeddings(
    payload: EmbeddingRequest,
    request: Request,
    auth: AuthContext = Depends(get_auth),
) -> EmbeddingResponse:
    """暴露 embedding 能力给其他自研服务，避免各服务各自维护模型客户端。

    TODO: 复用 ctx.embedding_provider.embed_texts()，透传 usage 用于成本归集。
    """

    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "create_embeddings not implemented")


@router.get("/collections", response_model=list[CollectionInfo], summary="列出知识集合")
async def list_collections(
    auth: AuthContext = Depends(get_auth),
) -> list[CollectionInfo]:
    """列出当前租户可见的集合。

    TODO: 按 tenant_id/space_id 聚合统计；结果可缓存 60s。
    """

    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "list_collections not implemented")
