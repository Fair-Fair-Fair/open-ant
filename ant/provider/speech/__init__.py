"""Speech providers — ASR/TTS（Phase 7 记忆方舟语音）。

设计对齐项目其他 provider：接口先行（ABC），外部库懒加载，
CPU 密集型工作统一 ``asyncio.to_thread``（设计原则 4），
任何语音能力失败都不能打断主链路（原则 11）。
"""

from ant.provider.speech.asr import AsrProvider, FasterWhisperAsrProvider
from ant.provider.speech.tts import EdgeTtsProvider, TtsProvider

__all__ = [
    "AsrProvider",
    "FasterWhisperAsrProvider",
    "TtsProvider",
    "EdgeTtsProvider",
]
