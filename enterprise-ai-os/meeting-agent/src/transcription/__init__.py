"""ASR 对接、说话人分离、术语纠错。

    transcription/asr.py   转写引擎抽象、Transcript 数据模型、说话人身份映射

``Transcript`` 是整条流水线的基础数据结构——摘要、抽取、分发全部依赖它。
因此它是本组件里**最该先定死**的东西：后面改一次，四个模块都要跟着改。
"""

from __future__ import annotations

from .asr import (
    MockEngine,
    OctoSpeechEngine,
    Speaker,
    Transcript,
    TranscriptSegment,
    TranscriptionEngine,
    build_engine,
)

__all__ = [
    "Transcript",
    "TranscriptSegment",
    "Speaker",
    "TranscriptionEngine",
    "OctoSpeechEngine",
    "MockEngine",
    "build_engine",
]
