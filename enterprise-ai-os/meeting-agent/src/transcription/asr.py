"""ASR 引擎对接与转录数据模型。

为什么经 octo-speech 而不直连 Whisper
--------------------------------------
ASR 推理需要 GPU、需要模型缓存、需要按音频时长而非请求数伸缩——
这些特性与本服务（IO 密集、无状态、按请求数伸缩）完全不同。
混在一个进程里会导致：镜像从 200MB 涨到 6GB、每个副本都要占一张卡、
而绝大多数时间卡是闲的。``octo-speech`` 已经把这层做好了。

``Transcript`` 数据结构的重要性
-------------------------------
它是摘要、抽取、分发的共同输入。三条约束不能省：

1. **必须带时间戳**。抽取出的决策要能回听核对——"系统说这是决策，
   但我不记得定了"这种争议只能靠回听原话解决。
2. **必须带说话人**。没有说话人归属，Action Item 就没有责任人，
   整个组件的价值折损一半。
3. **说话人必须可修正**。ASR 只能给 SPEAKER_00/01，映射到真人的
   自动方案都不可靠。责任人认错的 Action Item 比没有更糟。
"""

from __future__ import annotations

import abc
import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, AsyncIterator, Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "TranscriptionStatus",
    "Speaker",
    "TranscriptSegment",
    "Transcript",
    "TranscriptionRequest",
    "TranscriptionEngine",
    "OctoSpeechEngine",
    "MockEngine",
    "SpeakerResolver",
    "TerminologyCorrector",
    "AudioPreprocessor",
    "build_engine",
]


class TranscriptionStatus(str, Enum):
    """转写任务状态。"""

    PENDING = "pending"
    PREPROCESSING = "preprocessing"     # ffmpeg 转码与分片
    TRANSCRIBING = "transcribing"
    POSTPROCESSING = "postprocessing"   # 说话人映射、术语纠错、分片拼接
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass
class Speaker:
    """一个说话人。

    ``label`` 与 ``user_id`` 分开保留：前者是 ASR 给的 SPEAKER_00，
    后者是映射后的系统用户。映射失败时 user_id 为 None 但 label 仍在，
    转录文本依然可读——只是那部分的 Action Item 需要人工指派。

    保留 label 还有一个用处：人工修正说话人映射后要能重新应用到
    所有分段，靠 label 关联比靠时间区间可靠得多。
    """

    label: str                          # SPEAKER_00
    user_id: str | None = None
    display_name: str | None = None
    # 映射置信度。低于阈值时不写 user_id，保留待人工确认。
    confidence: float = 0.0
    # 映射依据：participant_list | self_introduction | voiceprint | manual
    # 记这个是为了排查"为什么认错人"，也为了统计各策略的准确率。
    resolved_by: str = ""
    # 该说话人的总发言时长，用于识别主讲人
    total_speech_seconds: float = 0.0


@dataclass
class TranscriptSegment:
    """一个转录分段（通常对应一句话或一次连续发言）。"""

    index: int
    start: float                        # 相对会议开始的秒数
    end: float
    text: str
    speaker_label: str | None = None
    # ASR 给出的置信度。低置信度分段在渲染纪要时应标注，
    # 让读者知道这里可能转错了。
    confidence: float = 1.0
    # 原始文本（术语纠错之前）。
    # 保留是为了排查纠错是否改错了——纠错改错的词无法从上下文察觉，
    # 没有原文就永远发现不了。
    raw_text: str | None = None
    # 所属音频分片，用于回溯拼接问题
    chunk_index: int = 0


@dataclass
class Transcript:
    """一次会议的完整转录。

    这是整条流水线的核心数据结构。
    """

    meeting_id: str
    tenant_id: str
    segments: list[TranscriptSegment] = field(default_factory=list)
    speakers: list[Speaker] = field(default_factory=list)
    language: str = "zh"
    duration_seconds: float = 0.0
    engine: str = ""                    # 实际使用的引擎，便于对比效果
    status: TranscriptionStatus = TranscriptionStatus.PENDING
    # 版本号。/reprocess 会产生新版本，旧版本保留以便对比。
    version: int = 1
    created_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def full_text(self, with_speaker: bool = True, with_timestamp: bool = False) -> str:
        """拼成纯文本，供摘要与抽取使用。

        ``with_speaker=True`` 是默认且几乎总是需要的：
        不带说话人的文本喂给抽取模型，它无从判断责任人，
        只能靠"张三你来跟一下"这类显式称呼，召回率会大幅下降。

        ``with_timestamp`` 在抽取时开启：让模型在输出里回填时间点，
        决策与待办才能溯源到原话。

        TODO: 实现。注意合并同一说话人的连续分段——
              逐句标注说话人会让文本膨胀近一倍，白烧 token。
        """

        raise NotImplementedError

    def speaker_of(self, label: str) -> Speaker | None:
        """按 label 取说话人。"""

        for s in self.speakers:
            if s.label == label:
                return s
        return None

    def slice_by_time(self, start: float, end: float) -> list[TranscriptSegment]:
        """取时间区间内的分段，用于议题切分后的分段抽取。"""

        return [s for s in self.segments if s.start < end and s.end > start]

    def token_estimate(self) -> int:
        """估算总 token 数，用于决定分段策略。

        TODO: 用 tiktoken。中文约 1.5 字/token，英文约 0.75 词/token，
              按字符数×固定系数估会在中英混说的会议里偏差很大。
        """

        raise NotImplementedError


@dataclass
class TranscriptionRequest:
    """转写请求。"""

    meeting_id: str
    tenant_id: str
    # 对象存储 key，不传字节流。
    # 一场会议音频几百 MB，在服务间传字节流会让内存与网络都吃不消。
    audio_key: str
    language: str | None = None         # None 表示自动检测
    # 已知参会人列表。**务必传**：
    # 给定人数能显著提升说话人分离准确率，
    # 且人名列表是责任人解析最可靠的依据。
    participants: Sequence[dict[str, Any]] = ()
    # 术语纠错词表
    terminology: dict[str, str] = field(default_factory=dict)
    diarization: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)


class TranscriptionEngine(abc.ABC):
    """转写引擎抽象。

    定义为 ABC 而非 Protocol：需要 :class:`MockEngine` 与
    :class:`OctoSpeechEngine` 共享 ``__init__`` 与配置解析逻辑。
    """

    name: str = "base"

    @abc.abstractmethod
    async def transcribe(self, request: TranscriptionRequest) -> Transcript:
        """批量转写。长音频内部走异步提交 + 轮询。"""

    @abc.abstractmethod
    async def transcribe_stream(
        self, meeting_id: str, tenant_id: str, audio_chunks: AsyncIterator[bytes]
    ) -> AsyncIterator[TranscriptSegment]:
        """流式转写，逐段产出。

        流式路径的分段是**可能被修正的**：ASR 会在拿到后续音频后
        回头修正前面的识别结果。因此消费方不能假定"收到即最终"，
        要按 index 覆盖而非追加。这一点容易搞错，导致界面上出现重复句子。
        """

    async def close(self) -> None:
        """释放资源。默认无操作。"""

        return None

    async def ping(self) -> bool:
        """健康检查。"""

        return True


class OctoSpeechEngine(TranscriptionEngine):
    """经 octo-speech 转写。

    TODO: 实现。要点：
    - 长音频用异步接口（提交 → 轮询 job），不要用同步接口。
      同步接口在两小时音频上必然超时，且中途失败要从头再来。
    - ``preferred_engine`` 只是偏好，octo-speech 可能因引擎不可用而降级。
      返回的 Transcript 里必须记下**实际使用的引擎**——
      对比效果时不知道用的哪个引擎，数据就没有意义。
    - 重试要带幂等：转写是按音频时长计费的，
      重复提交同一段音频等于重复付费。
    """

    name = "octo-speech"

    def __init__(self, base_url: str, api_key: str = "", config: dict[str, Any] | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.config = config or {}
        self._client: Any | None = None   # httpx.AsyncClient

    async def transcribe(self, request: TranscriptionRequest) -> Transcript:
        raise NotImplementedError

    async def transcribe_stream(
        self, meeting_id: str, tenant_id: str, audio_chunks: AsyncIterator[bytes]
    ) -> AsyncIterator[TranscriptSegment]:
        raise NotImplementedError


class MockEngine(TranscriptionEngine):
    """读取本地 fixture 的假引擎，用于本地开发与测试。

    这不是"顺手加的测试工具"，而是必需品：抽取 prompt 通常要改几十轮，
    每轮都真跑一遍两小时音频的转写，迭代速度会慢到无法工作。
    有了 mock，摘要与抽取的调优可以在秒级完成。
    """

    name = "mock"

    def __init__(self, fixture_path: str) -> None:
        self.fixture_path = fixture_path

    async def transcribe(self, request: TranscriptionRequest) -> Transcript:
        """从 fixture JSON 构造 Transcript。

        TODO: 实现。fixture 格式与 octo-speech 的响应保持一致，
              这样两个引擎的后续处理路径完全相同——
              否则 mock 下跑通的逻辑在真实引擎下未必能跑。
        """

        raise NotImplementedError

    async def transcribe_stream(
        self, meeting_id: str, tenant_id: str, audio_chunks: AsyncIterator[bytes]
    ) -> AsyncIterator[TranscriptSegment]:
        """按 fixture 的时间戳节奏逐段吐出，模拟实时。

        TODO: 实现。
        """

        raise NotImplementedError


class AudioPreprocessor:
    """音频预处理：格式统一、分片。

    重活交给 ffmpeg 二进制，Python 侧只拼参数。
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}
        self.temp_dir: str = str(self.config.get("temp_dir", "/tmp/meeting-audio"))

    async def normalize(self, audio_key: str) -> str:
        """转成 16kHz 单声道 wav，返回新的对象 key。

        为什么自己转而不让 ASR 引擎转：会议音频格式五花八门
        （m4a / opus / webm / 各家会议软件的私有容器），各引擎支持的
        格式集合不同，让引擎自己处理会导致一部分输入被直接拒收。

        TODO: 实现。ffmpeg 命令要设 ``-nostdin`` 与超时，
              否则损坏的音频文件会让进程挂住不退。
        """

        raise NotImplementedError

    async def split(self, audio_key: str, max_chunk_seconds: int = 600) -> list[tuple[str, float, float]]:
        """按静音点分片，返回 ``[(key, start, end)]``。

        **分片边界必须落在静音处**，不能按固定时长硬切：
        从一个词的中间切开会让两个分片各产生一个错词，
        且拼接处的语义断裂会影响说话人归属的连续性。

        做法是在目标时长附近的窗口内找最长静音段，在其中点切。
        窗口内找不到静音时才退化为硬切——这种情况说明有人连续说了
        十分钟不停顿，罕见但要有兜底。

        TODO: 实现，用 ffmpeg 的 silencedetect 滤镜。
        """

        raise NotImplementedError

    def probe(self, audio_key: str) -> dict[str, Any]:
        """探测时长、编码、声道数。

        时长要在转写**之前**拿到：它决定分片策略，也用于成本预估
        与配额检查。转写完才发现是八小时音频就太晚了。

        TODO: 实现，用 mutagen（比启 ffprobe 子进程快）。
        """

        raise NotImplementedError


class SpeakerResolver:
    """把 SPEAKER_XX 映射到系统用户。

    三种依据，可靠性依次递减：
    1. 参会人列表 + 自我介绍语句（"我是张三"）
    2. 声纹库（需提前注册，隐私敏感，默认关闭）
    3. 发言特征启发式（主讲人通常是组织者）

    现实中这三条都不够可靠，所以人工修正接口是**必需**的而非补充。
    """

    def __init__(self, config: dict[str, Any] | None = None, directory_client: Any | None = None) -> None:
        self.config = config or {}
        self.directory_client = directory_client
        self.min_confidence: float = float(self.config.get("min_confidence", 0.8))

    async def resolve(
        self, transcript: Transcript, participants: Sequence[dict[str, Any]]
    ) -> list[Speaker]:
        """解析说话人身份。

        置信度低于阈值时**不写 user_id**，保留 SPEAKER_XX 原样。
        猜错身份不如不猜：错误的责任人会让任务派给无关的人，
        而真正的责任人还不知情，两边都出问题。

        另一条约束：一个 user_id 不应被映射给两个 label。
        出现这种情况说明分离把一个人切成了两个（很常见），
        应当合并而不是让两个 label 都指向同一人——
        后者会让"张三的发言"在纪要里出现两处，读者会困惑。

        TODO: 实现。
        """

        raise NotImplementedError

    async def apply_manual_mapping(
        self, transcript: Transcript, mapping: dict[str, str]
    ) -> Transcript:
        """应用人工修正的映射（``{"SPEAKER_00": "u_123"}``）。

        修正后必须**重跑抽取**：责任人变了，Action Item 的归属也变了。
        只改转录不重跑抽取，会让纪要显示"张三说的"但任务派给了李四。

        TODO: 实现，并触发下游重算。
        """

        raise NotImplementedError


class TerminologyCorrector:
    """术语纠错。

    会议里的专有名词（产品名、内部代号、人名）是 ASR 的重灾区，
    而纠错的成本极低——本质是字符串替换。

    但有个反向风险：**误改无法察觉**。把正常词改成术语后，
    读者从上下文看不出异常，只会觉得这句话有点怪。
    因此阈值要设高（0.9），且必须保留 ``raw_text``。
    """

    def __init__(self, terms: dict[str, str] | None = None, fuzzy_threshold: float = 0.9) -> None:
        self.terms = terms or {}
        self.fuzzy_threshold = fuzzy_threshold

    def correct(self, text: str) -> tuple[str, list[tuple[str, str]]]:
        """纠错，返回 ``(纠正后文本, [(原词, 新词)])``。

        返回改动列表是为了可审计——纠错改了什么必须能查，
        否则怀疑"这个词是不是被改错了"时无从下手。

        TODO: 实现。中文要先分词再匹配，
              直接做子串替换会误伤（"报名"里的"名"匹配到人名术语）。
        """

        raise NotImplementedError

    async def load_from_rag(self, tenant_id: str, rag_client: Any) -> None:
        """从 rag-pipeline 拉取本租户术语表。

        TODO: 实现，带缓存——术语表变动很少，每次会议都拉一遍没必要。
        """

        raise NotImplementedError


def build_engine(config: dict[str, Any]) -> TranscriptionEngine:
    """按配置构造转写引擎。

    TODO: 实现——``engine`` 为 ``mock`` 时返回 MockEngine，
          否则返回 OctoSpeechEngine。未知值应报错而非静默降级到 mock，
          否则生产环境配错会表现为"所有会议转出同一份内容"，
          而这个症状极难联想到配置问题。
    """

    raise NotImplementedError
