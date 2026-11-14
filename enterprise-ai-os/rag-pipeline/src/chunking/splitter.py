"""文档分块策略。

核心设计决策：

1. **按 token 而非字符计长**。中文字符与 token 不是 1:1，按字符切会让
   chunk 实际 token 数飘忽，导致拼 prompt 时溢出上下文窗口。
2. **保留结构上下文**（``prepend_context_header``）。给每个 chunk 前置
   "文档标题 > 章节路径"，让脱离上下文的片段仍可被正确召回。这一步对
   长文档的召回率提升非常明显。
3. **表格整体成块**。表格被切开后行列错位，语义完全破坏。
4. **策略按类型分派**。Markdown 按标题层级切、代码按函数/类边界切、
   PDF 退化为递归字符切分。
"""

from __future__ import annotations

import abc
import hashlib
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Sequence

logger = logging.getLogger(__name__)


class ChunkStrategy(str, Enum):
    """分块策略枚举。"""

    RECURSIVE = "recursive"   # 递归按分隔符层级切分（通用默认）
    MARKDOWN = "markdown"     # 按标题层级切，保留 heading path
    CODE = "code"             # 按语言语法边界（函数/类）切
    SEMANTIC = "semantic"     # 按相邻句向量相似度断点切（贵，仅高价值文档）
    FIXED = "fixed"           # 定长滑窗，兜底


@dataclass
class Chunk:
    """一个可被独立检索的文本片段。

    Attributes:
        chunk_id: 全局唯一，约定格式 ``{document_id}::c{index:03d}``，便于人工排查。
        document_id: 所属文档。
        index: 在文档内的序号，用于 ``context_window_expand`` 取相邻块。
        content: 用于 embedding 与返回给模型的正文（可能含 context header）。
        raw_content: 不含 context header 的原始文本，展示给用户时用这个。
        token_count: token 数，用于拼 prompt 时的预算控制。
        heading_path: 章节路径，如 ``["生产环境扩容 SOP", "3. 审批", "3.2 二级审批"]``。
        metadata: 随向量一同存储，支持检索过滤。
    """

    chunk_id: str
    document_id: str
    index: int
    content: str
    raw_content: str = ""
    token_count: int = 0
    heading_path: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    # 向量在 embedding 阶段回填
    embedding: list[float] | None = None

    @staticmethod
    def make_id(document_id: str, index: int) -> str:
        """生成稳定的 chunk_id。同文档重新摄入时 ID 可复现，便于幂等 upsert。"""

        return f"{document_id}::c{index:03d}"

    def content_hash(self) -> str:
        """内容哈希，用于跳过未变更 chunk 的重复 embedding。"""

        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()[:16]


@dataclass
class SplitterConfig:
    """分块参数。

    Attributes:
        chunk_size: 目标 token 数。512 是检索精度与上下文完整性的经验平衡点。
        chunk_overlap: 相邻块重叠 token 数，防止答案正好落在切点上。
        min_chunk_size: 小于此值的尾块并入前一块，避免产生无意义碎片。
        prepend_context_header: 是否给 chunk 前置标题/章节路径。
        table_as_single_chunk: 表格是否整体成块（可能超过 chunk_size）。
    """

    chunk_size: int = 512
    chunk_overlap: int = 64
    min_chunk_size: int = 64
    tokenizer: str = "cl100k_base"
    prepend_context_header: bool = True
    keep_separator: bool = True
    table_as_single_chunk: bool = True

    def validate(self) -> None:
        """参数自检，非法配置在启动时暴露而非运行时。"""

        if self.chunk_overlap >= self.chunk_size:
            raise ValueError("chunk_overlap 必须小于 chunk_size，否则会无限循环")
        if self.min_chunk_size > self.chunk_size:
            raise ValueError("min_chunk_size 不能大于 chunk_size")


class BaseSplitter(abc.ABC):
    """分块器抽象基类。"""

    strategy: ChunkStrategy

    def __init__(self, config: SplitterConfig) -> None:
        config.validate()
        self.config = config

    @abc.abstractmethod
    def split(
        self,
        text: str,
        *,
        document_id: str,
        title: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> list[Chunk]:
        """把整篇文本切成 Chunk 列表。

        Args:
            text: 已解析为纯文本的文档内容。
            document_id: 文档 ID，用于生成 chunk_id。
            title: 文档标题，用于 context header。
            metadata: 透传到每个 chunk 的元数据。

        Returns:
            按文档顺序排列的 Chunk 列表；空文本返回空列表。
        """

    # -- 供子类复用的工具方法 ------------------------------------------------

    def count_tokens(self, text: str) -> int:
        """统计 token 数。

        TODO: tiktoken.get_encoding(self.config.tokenizer)，编码器进程内缓存复用。
        """

        raise NotImplementedError

    def build_context_header(self, title: str | None, heading_path: Sequence[str]) -> str:
        """构造 context header。

        格式：``[文档标题 > 一级标题 > 二级标题]\\n``
        这段文本会参与 embedding，让片段携带自身在文档中的位置语义。
        """

        parts = [p for p in ([title] if title else []) + list(heading_path) if p]
        return f"[{' > '.join(parts)}]\n" if parts else ""

    def merge_small_tail(self, chunks: list[Chunk]) -> list[Chunk]:
        """把过短的尾块并入前一块。

        TODO: 当最后一块 token_count < min_chunk_size 且存在前块时合并，
              合并后重新计算 token_count 与 chunk_id 序号。
        """

        raise NotImplementedError


class RecursiveSplitter(BaseSplitter):
    """递归分隔符分块（通用默认策略）。

    按 ``分隔符优先级``（段落 → 句 → 子句 → 字符）逐层降级切分，
    尽量在语义边界断开。中文需要额外的标点分隔符（。！？；）。
    """

    strategy = ChunkStrategy.RECURSIVE

    #: 分隔符优先级，从粗到细。中文标点必须显式加入，LangChain 默认列表不含。
    SEPARATORS: tuple[str, ...] = ("\n\n", "\n", "。", "！", "？", "；", ". ", " ", "")

    def split(
        self,
        text: str,
        *,
        document_id: str,
        title: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> list[Chunk]:
        """递归切分。

        TODO: 用 langchain_text_splitters.RecursiveCharacterTextSplitter
              (separators=SEPARATORS, length_function=self.count_tokens)，
              再包装为 Chunk 并按需前置 context header。
        """

        raise NotImplementedError


class MarkdownSplitter(BaseSplitter):
    """Markdown 结构感知分块。

    先按 ``#`` 层级建立章节树，再对超长章节做递归切分；
    每个 chunk 记录完整 ``heading_path``。代码块与表格不被拆开。
    """

    strategy = ChunkStrategy.MARKDOWN

    def split(
        self,
        text: str,
        *,
        document_id: str,
        title: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> list[Chunk]:
        """按标题层级切分。

        TODO:
          1. 解析 heading 结构（注意跳过 fenced code block 内的 # 行）
          2. 表格（| ... |）与代码块（```）识别为原子单元
          3. 章节 token 数超 chunk_size 时委托 RecursiveSplitter 二次切分，
             heading_path 继承父章节
        """

        raise NotImplementedError

    def parse_sections(self, text: str) -> list[tuple[list[str], str]]:
        """解析为 ``(heading_path, body)`` 序列。

        TODO: 返回扁平化的章节列表，heading_path 为从根到当前的标题栈。
        """

        raise NotImplementedError


class CodeSplitter(BaseSplitter):
    """代码分块：按语法边界（函数 / 类 / 顶层语句）切分。

    代码按字符切会把函数腰斩，检索出来的片段无法理解。
    """

    strategy = ChunkStrategy.CODE

    def __init__(self, config: SplitterConfig, language: str = "python") -> None:
        super().__init__(config)
        self.language = language

    def split(
        self,
        text: str,
        *,
        document_id: str,
        title: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> list[Chunk]:
        """按语言语法切分。

        TODO: langchain_text_splitters.RecursiveCharacterTextSplitter.from_language()
              覆盖主流语言；heading_path 记录 ``模块 > 类 > 方法`` 便于定位。
        """

        raise NotImplementedError


class FixedSplitter(BaseSplitter):
    """定长滑窗分块。兜底策略：结构解析失败时保证仍能入库。"""

    strategy = ChunkStrategy.FIXED

    def split(
        self,
        text: str,
        *,
        document_id: str,
        title: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> list[Chunk]:
        """按固定 token 窗口 + overlap 滑动切分。

        TODO: 实现最简滑窗，作为其他策略异常时的 fallback。
        """

        raise NotImplementedError


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------
def build_splitter(
    strategy: ChunkStrategy | str,
    config: SplitterConfig,
    *,
    language: str | None = None,
) -> BaseSplitter:
    """按策略名构造分块器。

    Args:
        strategy: 策略枚举或其字符串值。
        config: 分块参数。
        language: 仅 ``code`` 策略使用。

    Raises:
        ValueError: 策略未实现。
    """

    strategy = ChunkStrategy(strategy)
    if strategy is ChunkStrategy.RECURSIVE:
        return RecursiveSplitter(config)
    if strategy is ChunkStrategy.MARKDOWN:
        return MarkdownSplitter(config)
    if strategy is ChunkStrategy.CODE:
        return CodeSplitter(config, language=language or "python")
    if strategy is ChunkStrategy.FIXED:
        return FixedSplitter(config)
    # TODO: SEMANTIC 需要 embedding provider，实现时改为工厂注入依赖
    raise ValueError(f"splitter strategy not implemented: {strategy}")


def resolve_strategy(
    mime_type: str | None,
    strategy_by_type: dict[str, str],
    default: ChunkStrategy | str = ChunkStrategy.RECURSIVE,
) -> ChunkStrategy:
    """根据 MIME 类型解析应使用的策略（对应配置 ``chunking.strategy_by_type``）。"""

    if mime_type and mime_type in strategy_by_type:
        return ChunkStrategy(strategy_by_type[mime_type])
    return ChunkStrategy(default)
