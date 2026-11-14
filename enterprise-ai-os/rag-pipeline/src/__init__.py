"""rag-pipeline —— Enterprise AI OS 的检索增强生成管线。

包结构::

    src/
      main.py        FastAPI 应用装配与配置加载
      embeddings/    多后端 embedding 提供者
      chunking/      文档分块策略
      ingestion/     摄入管道（fetch → parse → chunk → embed → upsert）
      retrieval/     混合检索引擎（向量 + BM25 + 重排）
      api/           REST 路由与 Pydantic 契约

设计约束：
- 所有模型调用（embedding / rerank）统一经 ``model-gateway``，本服务不持有厂商凭证。
- 所有读写必须携带 ``tenant_id`` / ``space_id``，由 ``octo-auth`` 解析后注入。
"""

__version__ = "0.1.0"
__all__ = ["__version__"]
