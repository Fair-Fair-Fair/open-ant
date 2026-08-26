"""Abstract base class for channel implementations"""
import logging
from abc import ABC, abstractmethod
from typing import Any, Awaitable, Callable, Generic, TypeVar

from ant.core.events import EventSource
from ant.utils.config import Config

logger = logging.getLogger(__name__)


def _mask_pii(text: str) -> str:
    """Mask user message text for logging (PII hardening, improve.md #19).

    Message content is user data and may contain secrets, so the baseline
    rule is: the FULL text must never reach the logs.  This helper keeps
    only the first 50 characters (plus a ``…(len=N)`` suffix carrying the
    total length when the message is longer) — enough to debug routing
    and size without leaking the message itself.  Messages of at most 50
    characters are logged verbatim, which is already bounded by the same
    truncation rule.
    """
    if len(text) <= 50:
        return text
    return f"{text[:50]}…(len={len(text)})"


T = TypeVar('T', bound=EventSource)


class Channel(ABC, Generic[T]):
    """Abstract base for messaging platform with Eventsource-based context"""
    @property
    @abstractmethod
    def platform_name(self) -> str:
        """Platform identifier"""
        pass

    @abstractmethod
    async def run(self, on_message: Callable[[str, T], Awaitable[None]]) -> None:
        """Run the channel Blocks until stop() is called"""
        pass

    @abstractmethod
    def is_allowed(self, source: T) -> bool:
        """Check if sender is whitelisted"""
        pass

    @abstractmethod
    async def reply(self, content: str, source: T) -> None:
        """Reply to incoming message"""
        pass

    @abstractmethod
    async def stop(self) -> None:
        """Stop the channel"""
        pass

    @staticmethod
    def from_config(config: Config) -> list["Channel[Any]"]:
        """Create channel instances from configuration.

        Channels whose SDK is not installed are skipped with a warning
        instead of crashing (install them via ``pip install
        'open-ant[telegram]'`` / ``'open-ant[discord]'``).
        """
        channels: list["Channel[Any]"] = []
        channel_config = config.channels

        if channel_config.telegram and channel_config.telegram.enabled:
            try:
                from ant.channel.telegram_channel import TelegramChannel
            except ImportError:
                logger.warning(
                    "Telegram channel configured but python-telegram-bot is "
                    "not installed — run: pip install 'open-ant[telegram]'"
                )
            else:
                channels.append(TelegramChannel(channel_config.telegram))

        if channel_config.discord and channel_config.discord.enabled:
            try:
                from ant.channel.discord_channel import DiscordChannel
            except ImportError:
                logger.warning(
                    "Discord channel configured but discord.py is not "
                    "installed — run: pip install 'open-ant[discord]'"
                )
            else:
                channels.append(DiscordChannel(channel_config.discord))

        return channels
