"""TTS providers — 语音合成（Phase 7 记忆方舟语音）。"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from pathlib import Path

logger = logging.getLogger(__name__)


class TtsProvider(ABC):
    """语音合成接口。"""

    @abstractmethod
    async def synthesize(self, text: str, out_path: str | Path) -> Path:
        """把文本合成 mp3 写入 ``out_path`` 并返回该路径。"""


class EdgeTtsProvider(TtsProvider):
    """edge-tts（微软 Edge 在线语音：免费、无需 API key、中文自然音色）。

    默认 ``zh-CN-XiaoxiaoNeural``——温暖女声，贴合小安"陪在身边的晚辈"
    的人设。需网络；失败时调用方降级为纯文字回复（语音绝不能打断主链路）。
    """

    def __init__(
        self,
        voice: str = "zh-CN-XiaoxiaoNeural",
        rate: str = "+0%",
    ) -> None:
        self.voice = voice
        self.rate = rate

    async def synthesize(self, text: str, out_path: str | Path) -> Path:
        import edge_tts

        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        comm = edge_tts.Communicate(text, self.voice, rate=self.rate)
        await comm.save(str(out))
        return out
