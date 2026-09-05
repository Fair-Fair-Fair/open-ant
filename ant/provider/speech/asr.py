"""ASR providers — 语音转写（Phase 7 记忆方舟语音）。"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Any

logger = logging.getLogger(__name__)


class AsrProvider(ABC):
    """语音转写接口。

    ``audio`` 接受 numpy float32 数组（-1..1）或 wav/mp3 文件路径。
    """

    @abstractmethod
    async def transcribe(self, audio: Any, sample_rate: int = 16000) -> str:
        """转写为文本（无语音时返回空串）。"""


class FasterWhisperAsrProvider(AsrProvider):
    """faster-whisper CPU 转写（whisper-small，中文）。

    模型懒加载（首次调用从 HF 下载 ~460MB）；转写是 CPU 密集型同步调用，
    统一 ``asyncio.to_thread`` 卸载（设计原则 4）。选 faster-whisper 而
    非 funasr：ctranslate2 轻量、无 torchaudio 依赖链（本机 torch 2.12
    CPU 无 torchaudio，见 code.md Phase 3 记录）。
    """

    def __init__(self, model_size: str = "small", language: str = "zh") -> None:
        self.model_size = model_size
        self.language = language
        self._model: Any = None

    def _load_model(self) -> Any:
        if self._model is None:
            from faster_whisper import WhisperModel

            logger.info(
                "Loading faster-whisper %r (first call downloads the model)",
                self.model_size,
            )
            self._model = WhisperModel(
                self.model_size, device="cpu", compute_type="int8"
            )
        return self._model

    def _transcribe_sync(self, audio: Any, sample_rate: int) -> str:
        model = self._load_model()
        # faster-whisper 把 numpy 数组一律按 16kHz 解释（不接收采样率参数），
        # 非 16k 的输入（如 edge-tts 的 24kHz mp3）必须先重采样——否则语速
        # 错乱、转写全是幻觉文本（loopback 实测："我女儿叫什么名字" →
        # "永遠都受傷了"）。scipy 多相滤波重采样，质量好且已随依赖链安装。
        import numpy as np

        if isinstance(audio, np.ndarray) and sample_rate != 16000:
            from scipy.signal import resample_poly

            audio = resample_poly(audio, 16000, sample_rate).astype(np.float32)
        segments, _info = model.transcribe(
            audio, language=self.language, beam_size=5
        )
        return "".join(seg.text for seg in segments).strip()

    async def transcribe(self, audio: Any, sample_rate: int = 16000) -> str:
        return await asyncio.to_thread(self._transcribe_sync, audio, sample_rate)
