"""Abstract base class for channel implementations"""
import logging
from abc import ABC, abstractmethod
from typing import Callable, Awaitable, Generic, TypeVar, Any

from ant.core.events import EventSource
from ant.utils.config import Config

logger = logging.getLogger(__name__)


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
