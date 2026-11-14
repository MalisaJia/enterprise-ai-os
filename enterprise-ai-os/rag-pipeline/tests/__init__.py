"""rag-pipeline 测试套件。

分层策略：

- **单元测试**（无外部依赖）：分块边界、RRF 融合、配置环境变量展开、
  幂等哈希判定。这类测试必须能在 CI 里裸跑，不依赖 Docker。
- **集成测试**（需要 postgres/kafka）：用 ``@pytest.mark.integration`` 标记，
  通过 ``pytest -m "not integration"`` 排除。

约定：
- 外部 HTTP（model-gateway / octo-search-indexer）统一用 ``respx`` 打桩，
  禁止在测试中真实出网。
- 向量库在单测中用实现了 ``VectorStore`` 协议的内存 fake 替换。

待补测试用例：
- test_splitter.py       —— chunk_overlap 边界、中文标点切分、表格不被拆开
- test_fusion.py         —— RRF 数学正确性、单路为空时的退化
- test_retrieval.py      —— 重排降级路径、tenant 过滤不可绕过
- test_ingestion.py      —— content_hash 幂等、失败重试与 DLQ
- test_api.py            —— 契约快照、401/404 而非 403 的越权响应
"""
