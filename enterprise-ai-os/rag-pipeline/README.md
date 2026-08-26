# rag-pipeline

> **组件定位**：把企业非结构化文档变成 Agent 可检索的向量知识库——摄入、分块、向量化、混合检索、重排，一条完整管线。

## 为什么需要

OCTO 生态在检索侧只有 `octo-search-indexer`（Kafka → OpenSearch 的**全文/BM25** 消息索引），**完全没有 embedding 能力和向量检索**。这意味着：

- Agent 无法基于语义相似度找到"意思相近但用词不同"的文档
- 没有分块策略，长文档无法喂给上下文窗口有限的模型
- 没有统一的文档摄入管道，PDF / Word / Confluence 等格式无从入库

`octo-spec` 提供的是 Git 仓库里的文件级项目记忆，粒度太粗且不可检索。

**我们补的部分**：Embedding 服务 + 分块管道 + 向量存储 + 混合检索（向量 + BM25 融合）+ 重排。
**我们不重复造的部分**：BM25 侧直接调用 `octo-search-indexer` 的 OpenSearch 索引，不自建全文引擎。

## 技术选型

| 关注点 | 选型 | 理由 |
| --- | --- | --- |
| 语言 / 框架 | Python 3.11 + FastAPI + Uvicorn | 生态（LangChain / sentence-transformers）最完整；异步 IO 适配大量外部调用 |
| 向量存储 | **pgvector**（默认） / Milvus（可选） | pgvector 与业务库同源，事务一致、运维成本低；> 5000 万向量时切 Milvus |
| Embedding | `bge-m3`（中英双语 + 多粒度），经 `model-gateway` 调用；本地 `sentence-transformers` 作离线兜底 | 中文场景 bge 系列显著优于 OpenAI ada；统一走网关便于限流与计量 |
| 重排 | `bge-reranker-v2-m3` (cross-encoder) | 召回 top-20 → 精排 top-5，是 RAG 效果提升最高性价比的一步 |
| 分块 | LangChain `RecursiveCharacterTextSplitter` + 自研结构感知分块 | Markdown / 代码 / 表格需要按结构切，纯字符切分会破坏语义 |
| 文档解析 | `unstructured` + `pypdf` + `python-docx` | 覆盖 PDF / Office / HTML / Markdown |
| 异步摄入 | Kafka 消费 + 内部工作池 | 摄入是慢任务，必须与查询链路解耦（见架构文档 3.2 知识摄入流） |

## 架构设计

```
                          ┌──────────────────────────────────────┐
   POST /v1/documents ───▶│ ingestion.pipeline.IngestionPipeline │
   Kafka: aios.document   │  ① fetch  从 S3/URL 取原文            │
          .ingest    ────▶│  ② parse  解析为纯文本 + 结构元数据    │
                          │  ③ chunk  → chunking.splitter        │
                          │  ④ embed  → embeddings.provider      │
                          │  ⑤ upsert → 向量库 + BM25 索引        │
                          └───────────────┬──────────────────────┘
                                          │ 产出事件 aios.document.indexed
                                          ▼
   POST /v1/search  ───▶  ┌────────────────────────────────────────┐
                          │ retrieval.engine.RetrievalEngine       │
                          │  ① 向量召回 (pgvector / Milvus)         │
                          │  ② BM25 召回 (octo-search-indexer)     │
                          │  ③ RRF 融合 (alpha 加权)               │
                          │  ④ 重排 (cross-encoder)                │
                          │  ⑤ 元数据过滤 + 权限裁剪 (tenant/space) │
                          └────────────────────────────────────────┘
```

核心模块：

| 模块 | 职责 | 关键抽象 |
| --- | --- | --- |
| `embeddings/provider.py` | 多后端 embedding，批量、缓存、维度校验 | `EmbeddingProvider` 抽象基类；`GatewayEmbeddingProvider` / `LocalEmbeddingProvider` |
| `chunking/splitter.py` | 按文档类型选择分块策略，保留结构上下文 | `Chunk` 数据模型；`BaseSplitter` → `RecursiveSplitter` / `MarkdownSplitter` / `CodeSplitter` |
| `ingestion/pipeline.py` | 编排摄入五步，支持断点续传与幂等 upsert | `IngestionPipeline`、`IngestionJob`、`DocumentParser` |
| `retrieval/engine.py` | 混合检索 + 融合 + 重排 + 权限过滤 | `RetrievalEngine`、`VectorStore` 协议、`SearchQuery` / `SearchResult` |
| `api/routes.py` | REST 端点与 Pydantic 契约 | `router` |

## API 设计

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/v1/documents` | 提交文档摄入（返回 `job_id`，异步处理） |
| `GET` | `/v1/documents/{document_id}` | 查询文档及其分块索引状态 |
| `DELETE` | `/v1/documents/{document_id}` | 删除文档及全部向量（GDPR / 数据下线） |
| `GET` | `/v1/jobs/{job_id}` | 查询摄入任务进度 |
| `POST` | `/v1/search` | **主力接口**：混合检索，返回重排后的 chunk 列表 |
| `POST` | `/v1/embeddings` | OpenAI 兼容 embedding 直通（供其他服务复用） |
| `GET` | `/v1/collections` | 列出知识集合（按 space 隔离） |
| `GET` | `/healthz` `/readyz` `/metrics` | 探针与 Prometheus 指标 |

检索请求/响应示例：

```jsonc
// POST /v1/search
{
  "query": "线上服务扩容的审批流程是什么",
  "collection": "ops-runbook",
  "top_k": 20,           // 召回量
  "top_n": 5,            // 重排后返回量
  "alpha": 0.5,          // 0=纯BM25 1=纯向量
  "filters": { "doc_type": ["runbook", "sop"], "updated_after": "2026-01-01" },
  "rerank": true
}
// 200
{
  "results": [
    {
      "chunk_id": "doc_8f2a::c07",
      "document_id": "doc_8f2a",
      "content": "扩容需先在 workflow-engine 提交 scale-out 流程...",
      "score": 0.913,
      "vector_score": 0.88,
      "bm25_score": 12.4,
      "metadata": { "title": "生产环境扩容 SOP", "section": "3.2 审批", "source_url": "..." }
    }
  ],
  "latency_ms": 142,
  "trace_id": "01JC..."
}
```

## 与 OCTO 的集成方式

1. **认证**：所有 `/v1/*` 请求经 `octo-auth` 校验，从中解析 `tenant_id` / `space_id` / `user_id`，写入与检索均按此三元组做行级隔离。集合（collection）与 OCTO 的 Space 一一对应。
2. **自动摄入 OCTO 内容**：消费 `octo.file.uploaded` 与 `octo.message.created`，将频道文件和高价值对话自动入库；这样 Agent 在 IM 里的历史讨论天然可检索。
3. **BM25 复用**：混合检索的稀疏侧通过 HTTP 调 `octo-search-indexer` 的查询接口，不自建 OpenSearch。
4. **被谁调用**：`codex-channel-octo` / `cc-channel-octo` 等 Agent 桥接器在构造 prompt 前调 `/v1/search` 注入上下文；`memory-service` 复用本服务的 embedding 端点；`meeting-agent` 把会议纪要回写为文档。
5. **模型调用**：embedding / rerank 一律走 `model-gateway`，不直连厂商。

## 开发指南

```bash
cd rag-pipeline
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp config/config.yaml.example config/config.yaml
cp ../.env.example ../.env                            # 填 DATABASE_URL / MODEL_GATEWAY_URL

# 依赖的基础设施（在仓库根目录）
docker compose up -d postgres redis kafka minio

# 启动开发服务（热重载）
uvicorn src.main:app --reload --host 0.0.0.0 --port 8100

# 接口文档
open http://localhost:8100/docs
```

测试与检查：

```bash
pytest -q                      # 单测
ruff check src tests           # lint
mypy src                       # 类型检查
```

容器方式：

```bash
docker compose up -d --build rag-pipeline
docker compose logs -f rag-pipeline
```

## 当前状态

**脚手架阶段，待实现。** 已完成：目录结构、接口与数据模型定义、配置模板、Docker 编排。

实现顺序建议：

- [ ] `embeddings/provider.py` — 先打通 `model-gateway` embedding 调用
- [ ] `chunking/splitter.py` — 递归分块 + Markdown 结构分块
- [ ] pgvector schema 与 HNSW 索引（DDL 迁移脚本待补 `migrations/`）
- [ ] `ingestion/pipeline.py` — 同步摄入路径，再补 Kafka 消费
- [ ] `retrieval/engine.py` — 先纯向量，再叠加 BM25 融合与重排
- [ ] 权限过滤与多租户隔离的集成测试
