"""Chat 命令的语音输入输出助手（Phase 7 记忆方舟）。

对齐 OpenClaw 的 talk mode 思路：终端启动时选择文字/语音输入模式。
语音 = 又一个输入输出形态，底层仍是同一条 harness 管线——语音链路
任何一步失败都降级为纯文字（设计原则 11），绝不影响对话本身。
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


def speech_deps_available() -> bool:
    """语音依赖是否齐全（缺包时给出安装提示并降级文字模式）。"""
    try:
        import edge_tts  # noqa: F401
        import faster_whisper  # noqa: F401
        import miniaudio  # noqa: F401
        import sounddevice  # noqa: F401
        return True
    except ImportError:
        return False


class VoiceIO:
    """麦克风录音 → ASR 转写；TTS 合成 → 播放。懒加载，失败静默降级。"""

    def __init__(
        self,
        record_seconds: float = 6.0,
        voice: str = "zh-CN-XiaoxiaoNeural",
    ) -> None:
        self.record_seconds = record_seconds
        self.voice = voice
        self._asr = None
        self._tts = None
        self._out_dir: Path | None = None

    def _lazy(self):
        if self._asr is None:
            from ant.provider.speech.asr import FasterWhisperAsrProvider
            from ant.provider.speech.tts import EdgeTtsProvider

            self._asr = FasterWhisperAsrProvider()
            self._tts = EdgeTtsProvider(voice=self.voice)
            self._out_dir = Path(tempfile.mkdtemp(prefix="openant_voice_"))
        return self._asr, self._tts, self._out_dir

    async def listen(self) -> str:
        """回车后录 N 秒并转写；没听清返回空串。"""
        import sounddevice as sd

        await asyncio.to_thread(
            input,
            f"（按回车开始说话，{self.record_seconds:.0f} 秒自动停止）",
        )
        fs = 16000
        audio = await asyncio.to_thread(
            sd.rec,
            int(self.record_seconds * fs),
            samplerate=fs,
            channels=1,
            dtype="float32",
        )
        await asyncio.to_thread(sd.wait)
        asr, _, _ = self._lazy()
        return await asr.transcribe(audio.reshape(-1), fs)

    async def speak(self, text: str) -> None:
        """TTS 合成并播放。失败静默降级（文字已在终端显示）。"""
        from ant.provider.speech.playback import play_mp3

        try:
            _, tts, out_dir = self._lazy()
            path = out_dir / "reply.mp3"
            await tts.synthesize(text, path)
            await asyncio.to_thread(play_mp3, path)
        except Exception as exc:
            logger.debug("TTS/playback failed, degraded to text-only: %s", exc)
