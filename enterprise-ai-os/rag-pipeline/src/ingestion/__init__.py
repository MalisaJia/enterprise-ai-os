"""数据摄入管道。

摄入是慢任务（解析大 PDF + 数千次 embedding），必须与查询链路解耦：
HTTP 只受理任务，实际执行走 Kafka 消费者或后台工作池。
"""

from .pipeline import (
    DocumentParser,
    IngestionJob,
    IngestionPipeline,
    JobStatus,
    ParsedDocument,
    SourceType,
)

__all__ = [
    "DocumentParser",
    "IngestionJob",
    "IngestionPipeline",
    "JobStatus",
    "ParsedDocument",
    "SourceType",
]
