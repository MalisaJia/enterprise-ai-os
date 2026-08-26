"""memory-service —— Enterprise AI OS 的长期记忆服务。

包结构::

    src/
      main.py          FastAPI 应用装配与配置加载
      memory/          三层记忆（个人 / 团队 / 组织）的作用域策略与写入编排
      storage/         存储层（pgvector 向量存储 + 可选 Neo4j 图存储）
      consolidation/   记忆整合（抽取结果与既有记忆的 ADD/UPDATE/DELETE/NOOP 判定）
      api/             REST 路由与 Pydantic 契约

设计约束：
- 所有模型调用（embedding / 抽取 / 冲突判定 / 摘要）统一经 ``model-gateway``，
  本服务不持有厂商凭证，也不在进程内跑推理。
- 记忆是隐私数据：所有查询必须携带 ``tenant_id``，并由 PG Row Level Security 兜底；
  日志默认不打印记忆内容（见 config 的 ``logging.log_memory_content``）。
- 单条记忆必须是原子化陈述。一条记忆里塞多件事，将来其中一件过时就无法单独更新。

与 rag-pipeline 的分工：rag-pipeline 检索"文档里写了什么"，
本服务记住"交互中学到了什么"。两者数据来源、粒度与生命周期都不同，
不要把记忆当成又一个文档库。
"""

__version__ = "0.1.0"
__all__ = ["__version__"]
