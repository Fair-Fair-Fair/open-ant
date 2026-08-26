"""Discord channel implementation."""

import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

import discord

from ant.channel.base import Channel, _mask_pii
from ant.core.events import EventSource
from ant.utils.config import DiscordConfig

logger = logging.getLogger(__name__)


@dataclass
class DiscordEventSource(EventSource):
    """Source for Discord-originated events."""

    _namespace = "platform-discord"
    user_id: str
    channel_id: str

    def __str__(self) -> str:
        return f"platform-discord:{self.user_id}:{self.channel_id}"

    @classmethod
    def from_string(cls, s: str) -> "DiscordEventSource":
        _, user_id, channel_id = s.split(":")
        return cls(user_id=user_id, channel_id=channel_id)

    @property
    def platform_name(self) -> str:
        return "discord"


class DiscordChannel(Channel[DiscordEventSource]):
    """Discord platform implementation using discord.py."""

    platform_name = "discord"

    def __init__(self, config: DiscordConfig):
        """Initialize DiscordChannel."""
        self.config = config
        self.client: discord.Client | None = None
        self._running_task: asyncio.Task | None = None

    async def run(
        self, on_message: Callable[[str, DiscordEventSource], Awaitable[None]]
    ) -> None:
        """Run the Discord channel. Blocks until stop() is called.

        Re-entrant (improve.md #15 crash-loop fix): a previous run that
        crashed or was interrupted must never leave the channel stuck in
        "already running" — stale state (finished start task, leftover
        client) is dropped up-front so a restart rebuilds cleanly.
        """
        if self._running_task is not None:
            # Stale state from a crashed/interrupted previous run: if the
            # start task is already finished the channel is dead — reset
            # the refs instead of crash-looping forever.
            if self._running_task.done():
                logger.warning(
                    "DiscordChannel stale state from previous run — resetting"
                )
                self.client = None
                self._running_task = None
            else:
                raise RuntimeError("DiscordChannel already running")
        if self.client is not None:
            # Defensive: a client with no live task can only be leftover
            # from a run that died mid-setup — drop it.
            logger.warning("DiscordChannel stale client — resetting")
            self.client = None

        try:
            logger.info(f"Channel enabled with platform: {self.platform_name}")

            # Configure intents
            intents = discord.Intents.default()
            intents.message_content = True
            intents.messages = True

            self.client = discord.Client(intents=intents)

            @self.client.event
            async def _on_discord_message(message: discord.Message) -> None:
                """Handle incoming Discord message."""
                # Ignore bot's own messages
                if self.client and message.author == self.client.user:
                    return

                # Check channel restriction (optional)
                if (
                    self.config.channel_id
                    and str(message.channel.id) != self.config.channel_id
                ):
                    return

                # Only handle text messages
                if not message.content:
                    return

                # Extract user_id (the person) and channel_id (the channel)
                user_id = str(message.author.id)
                channel_id = str(message.channel.id)
                content = message.content

                # PII hardening (improve.md #19): never log the full
                # message text — only a masked, length-truncated form.
                logger.info(
                    f"Received Discord message from user {user_id} in "
                    f"channel {channel_id}: {_mask_pii(content)}"
                )

                source = DiscordEventSource(user_id=user_id, channel_id=channel_id)

                try:
                    await on_message(content, source)
                except Exception as e:
                    logger.error(f"Error in message callback: {e}")

            # Start the bot and store the task
            self._running_task = asyncio.create_task(
                self.client.start(self.config.bot_token)
            )

            logger.info("DiscordChannel started")
            await self._running_task
        finally:
            # Never leave stale refs behind (crashed start/stop): this is
            # what makes run() re-entrant after a crash.
            self.client = None
            self._running_task = None

    def is_allowed(self, source: DiscordEventSource) -> bool:
        """Check if sender is whitelisted."""
        if not self.config.allowed_user_ids:
            return True
        return source.user_id in self.config.allowed_user_ids

    async def reply(self, content: str, source: DiscordEventSource) -> None:
        """Reply to incoming message in the same channel."""
        if not self.client:
            raise RuntimeError("DiscordChannel not started")

        try:
            channel = self.client.get_channel(int(source.channel_id))
            if not channel:
                raise ValueError(f"Channel {source.channel_id} not found")

            # Type ignore: discord.py returns a union, but we know text channels have send()
            await channel.send(content)  # type: ignore[union-attr]
            logger.debug(f"Sent Discord reply to {source.channel_id}")
        except Exception as e:
            logger.error(f"Failed to send Discord reply: {e}")
            raise

    async def stop(self) -> None:
        """Stop Discord bot and cleanup.

        Idempotent and re-entrant (improve.md #15): state references are
        always reset (even if close() raises), so a stopped channel can
        be run() again.  A blocking close() is bounded by the
        ChannelWorker's stop timeout (worker layer), not here.
        """
        # Idempotent: skip if not running.  Capture local refs because
        # run()'s finally may clear the instance state while we await.
        client = self.client
        if client is None:
            logger.debug("DiscordChannel not running, skipping stop")
            return

        try:
            await client.close()
        except Exception:
            logger.warning("Error while closing Discord client", exc_info=True)
        finally:
            # Wait for running task to complete
            if self._running_task and not self._running_task.done():
                try:
                    await asyncio.wait_for(self._running_task, timeout=2.0)
                except asyncio.TimeoutError:
                    logger.warning("Running task did not complete in time")
                except Exception:
                    pass  # Task may have already failed

            self.client = None
            self._running_task = None
            logger.info("DiscordChannel stopped")