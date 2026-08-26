"""Telegram channel implementation"""
import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from ant.channel.base import Channel, _mask_pii
from ant.core.events import EventSource
from ant.utils.config import TelegramConfig

logger = logging.getLogger(__name__)


@dataclass
class TelegramEventSource(EventSource):
    """Source for Telegram-originated events"""

    _namespace = "platform-telegram"
    user_id: str
    chat_id: str

    def __str__(self) -> str:
        return f"platform-telegram:{self.user_id}:{self.chat_id}"

    @classmethod
    def from_string(cls, s: str) -> "TelegramEventSource":
        _, user_id, chat_id = s.split(":")
        return cls(user_id, chat_id)

    @property
    def platform_name(self) -> str:
        return "telegram"


class TelegramChannel(Channel[TelegramEventSource]):
    """Telegram platform implementation using python-telegram-bot"""
    platform_name = "telegram"

    def __init__(self, config: TelegramConfig):
        """Initialize TelegramChannel"""
        self.config = config
        self.application: Application | None = None
        self._running_task: asyncio.Task | None = None
        self._stop_event: asyncio.Event | None = None

    def is_allowed(self, source: TelegramEventSource) -> bool:
        """check if sender is whitelisted"""
        if not self.config.allowed_user_ids:
            return True
        return source.user_id in self.config.allowed_user_ids

    async def run(self, on_message: Callable[[str, TelegramEventSource], Awaitable[None]]) -> None:
        """Run the Telegram channel: Blocks until stop() is called.

        Re-entrant (improve.md #15 crash-loop fix): a previous run that
        crashed or was interrupted must never leave the channel stuck in
        "already running" — stale state (finished monitoring task, leftover
        application) is dropped up-front so a restart rebuilds cleanly.
        """
        if self.application is not None:
            # Stale state from a crashed/interrupted previous run: if the
            # monitoring task is gone or already finished the channel is
            # dead — reset the refs instead of crash-looping forever.
            if self._running_task is None or self._running_task.done():
                logger.warning(
                    "TelegramChannel stale state from previous run — resetting"
                )
                self.application = None
                self._running_task = None
                self._stop_event = None
            else:
                raise RuntimeError("TelegramChannel already running")

        try:
            logger.info(f"Channel enabled with platform: {self.platform_name}")
            self.application = (
                Application.builder().token(self.config.bot_token).build()
            )
            self._stop_event = asyncio.Event()

            async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
                """Handle incoming Telegram message"""
                if(
                    update.message
                    and update.message.text
                    and update.effective_chat
                    and update.message.from_user
                ):
                    # Extract user_id (the person) and chat_id(the conversation)
                    user_id = str(update.message.from_user.id)
                    chat_id = str(update.effective_chat.id)
                    message = update.message.text

                    # PII hardening (improve.md #19): never log the full
                    # message text — only a masked, length-truncated form.
                    logger.info(
                        f"Received Telegram message from user {user_id} in "
                        f"chat {chat_id}: {_mask_pii(message)}"
                    )

                    source = TelegramEventSource(user_id, chat_id)

                    try:
                        await on_message(message, source)
                    except Exception as e:
                        logger.error(f"Error in message callback: {e}")
            handler = MessageHandler(filters.TEXT, handle_message)
            self.application.add_handler(handler)

            # start the bot
            await self.application.initialize()
            await self.application.start()
            if self.application.updater:
                await self.application.updater.start_polling()

            logger.info("TelegramChannel started")

            # create the running task that monitors for stop
            async def run_until_stopped():
                """Run until stop() is called or updater stops unexpectedly"""
                while self.application and self.application.updater:
                    if self.application.updater.running:
                        if self._stop_event and self._stop_event.is_set():
                            return  # Graceful stop
                        await asyncio.sleep(1)
                    else:
                        if self._stop_event and not self._stop_event.is_set():
                            raise RuntimeError("telegram updater stopped unexpectedly")
                        return

            self._running_task = asyncio.create_task(run_until_stopped())
            await self._running_task
        finally:
            # Never leave stale refs behind (crashed setup/monitor or stop):
            # this is what makes run() re-entrant after a crash.
            self.application = None
            self._running_task = None
            self._stop_event = None

    async def reply(self, content: str, source: TelegramEventSource) -> None:
        """Reply to incoming message"""
        if not self.application:
            raise RuntimeError("TelegramChannel not started")

        try:
            await self.application.bot.send_message(
                chat_id=source.chat_id,
                text=content,
            )
            logger.debug(f"Sent Telegram reply to {source.chat_id}")
        except Exception as e:
            logger.error(f"Failed to send Telegram reply: {e}")
            raise

    async def stop(self) -> None:
        """Stop Telegram bot and cleanup.

        Idempotent and re-entrant (improve.md #15): state references are
        always reset (even if the PTB shutdown calls raise), so a stopped
        channel can be run() again.  A blocking updater.stop() is bounded
        by the ChannelWorker's stop timeout (worker layer), not here.
        """
        # Idempotent: skip if not running.  Capture local refs because
        # run()'s finally may clear the instance state while we await.
        app = self.application
        if app is None:
            logger.debug("TelegramChannel not running, skipping stop")
            return

        # Signal the running task to stop
        stop_event = self._stop_event
        if stop_event:
            stop_event.set()

        try:
            if app.updater and app.updater.running:
                await app.updater.stop()
            await app.stop()
            await app.shutdown()
        except Exception:
            logger.warning("Error while stopping Telegram bot", exc_info=True)
        finally:
            # Wait for running task to complete
            if self._running_task and not self._running_task.done():
                try:
                    await asyncio.wait_for(self._running_task, timeout=2.0)
                except asyncio.TimeoutError:
                    logger.warning("Running task did not complete in time")
                except Exception:
                    pass  # Task may have already failed

            self.application = None
            self._running_task = None
            self._stop_event = None
            logger.info("TelegramChannel stopped")