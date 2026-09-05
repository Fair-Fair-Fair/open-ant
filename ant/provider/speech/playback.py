"""音频解码与播放（Phase 7 记忆方舟语音）。

miniaudio 解码 mp3 → numpy；sounddevice 播放。两者均懒导入，
语音链路整体失败不影响主链路（调用方 catch）。
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def decode_mp3(path: str | Path) -> tuple:
    """解码 mp3 → (float32 单声道 numpy 数组, 采样率)。"""
    import miniaudio
    import numpy as np

    decoded = miniaudio.decode_file(
        str(path), output_format=miniaudio.SampleFormat.FLOAT32
    )
    samples = np.frombuffer(decoded.samples, dtype=np.float32)
    if decoded.nchannels > 1:
        samples = samples.reshape(-1, decoded.nchannels).mean(axis=1)
    return samples, decoded.sample_rate


def play_audio(samples, sample_rate: int) -> None:
    """阻塞播放直到结束。无音频设备时抛出异常（调用方降级）。"""
    import sounddevice as sd

    sd.play(samples, samplerate=sample_rate)
    sd.wait()


def play_mp3(path: str | Path) -> None:
    """解码并播放一个 mp3 文件。"""
    samples, rate = decode_mp3(path)
    play_audio(samples, rate)
