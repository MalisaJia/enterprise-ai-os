"""文档分块。

分块质量直接决定检索上限——切坏了，后面的模型再强也救不回来。
"""

from .splitter import (
    BaseSplitter,
    Chunk,
    ChunkStrategy,
    CodeSplitter,
    FixedSplitter,
    MarkdownSplitter,
    RecursiveSplitter,
    SplitterConfig,
    build_splitter,
)

__all__ = [
    "BaseSplitter",
    "Chunk",
    "ChunkStrategy",
    "CodeSplitter",
    "FixedSplitter",
    "MarkdownSplitter",
    "RecursiveSplitter",
    "SplitterConfig",
    "build_splitter",
]
