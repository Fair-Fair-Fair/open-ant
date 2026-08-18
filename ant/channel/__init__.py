"""Channel implementations for different platforms.

Telegram and Discord are optional integrations — their SDKs are
installed via ``pip install 'open-ant[telegram]'`` / ``'open-ant[discord]'``.
When the SDK is missing, the corresponding symbol is ``None`` and the
channel is skipped (with a warning) instead of crashing the whole app.
"""

import logging

from ant.channel.base import Channel

logger = logging.getLogger(__name__)

__all__ = ["Channel"]

try:
    from ant.channel.telegram_channel import TelegramChannel
    __all__.append("TelegramChannel")
except ImportError:
    logger.debug("python-telegram-bot not installed — Telegram channel disabled")
    TelegramChannel = None

try:
    from ant.channel.discord_channel import DiscordChannel
    __all__.append("DiscordChannel")
except ImportError:
    logger.debug("discord.py not installed — Discord channel disabled")
    DiscordChannel = None
