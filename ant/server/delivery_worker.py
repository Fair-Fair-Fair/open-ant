"""Worker that delivers outbound messages to pltforms"""

import logging
from typing import TYPE_CHECKING, Any

from ant.core.events import EventSource, OutboundEvent
from ant.core.history import HistorySession

from .observability import record_event_consumed  # Phase 5A observability
from .worker import SubscribeWorker

if TYPE_CHECKING:
    from ant.channel.base import Channel
    from ant.core.context import SharedContext

logger = logging.getLogger(__name__)


# platform message size limit
PLATFORM_LIMITS: dict[str, float] = {
    "telegram": 4096,
    "discord": 2000,
    "cli": float("inf")  # no limit
}


def chunk_message(content: str, limit: int) -> list[str]:
    """Split message at paragraph boundaries, respecting limit"""
    if len(content) <= limit:
        return [content]

    chunks = []
    paragraphs = content.split("\n\n")
    current = ""

    for paragraph in paragraphs:
        # Try to add to current chunk
        if current:
            potential = current + "\n\n" + paragraph
        else:
            potential = paragraph

        if len(potential) <= limit:
            current = potential
        else:
            if current:
                chunks.append(current)

            # Handle paragraph that exceeds limit
            if len(paragraph) > limit:
                # Hard split
                for i in range(0, len(paragraph), limit):
                    chunks.append(paragraph[i: i + limit])
                current = ""
            else:
                current = paragraph
    if current:
        chunks.append(current)
    return chunks


class DeliveryWorker(SubscribeWorker):
    """Worker that delivers outbound messages to platforms.

    Delivery semantics (Phase 1, design principle "投递失败 = 不确认"):
      * rabbitmq backend — single delivery attempt; any failure (channel
        missing, reply error) RAISES so the broker wrapper nacks the
        message → DLX retry.  Success needs no explicit ack (RabbitMqBus
        acks automatically when the handler returns normally).
      * memory backend — Phase 0 semantics: failure leaves the event
        unacked (the pending file survives for restart redelivery);
        success acks explicitly.
    """

    def __init__(self, context: "SharedContext"):
        super().__init__(context)
        self.context.eventbus.subscribe(OutboundEvent, self.handle_event)
        self.logger.info("DeliveryWorker subscribed to OUTBOUND events")

    @property
    def _is_rabbitmq(self) -> bool:
        return self.context.bus_backend == "rabbitmq"

    async def _deliver(
            self, chunks: list[str], source: "EventSource", channel: "Channel[Any]"
    ) -> None:
        """Deliver all chunks in a single attempt; raises on failure.

        Phase 1: broker-level retry (nack → DLX) replaces the old in-process
        backoff loop, which used to sleep the whole bus for up to 10 minutes
        on one failing message.
        """
        for chunk in chunks:
            await channel.reply(chunk, source)

    async def _get_session_source(self, session_id: str) -> HistorySession | None:
        """Get session info from the history repository.

        Note: no lru_cache here — it cannot wrap an async method (it would
        cache the coroutine object and break on the second call).
        """
        for session in await self.context.history_store.list_sessions():
            if session.id == session_id:
                return session
        return None

    def _get_delivery_source(self,
                             session_info: HistorySession
    ) -> "EventSource | None":
        source = session_info.get_source()

        # If source already has a platform , use it
        if source.platform_name:
            return source

        # Try default delivery source for agent events
        default_source_str = self.context.config.default_delivery_source
        if default_source_str:
            try:
                source = EventSource.from_string(default_source_str)
                if not source.platform_name:
                    self.logger.error(
                        f"default_delivery_source '{default_source_str}' is not a platform source"
                    )
                    return None
                return source
            except ValueError as e:
                self.logger.error(f"Invalid default_delivery_source: {e}")
                return None
        else:
            self.logger.warning(
                f"No platform for session {session_info.id} and no default_delivery_source configured"  # noqa: E501
            )
            return None

    async def handle_event(self, event: OutboundEvent) -> None:
        """Handle an outbound message event"""
        # Phase 5A observability：事件消费计数（观测永不打断主链路，原则 11）。
        try:
            record_event_consumed(event)
        except Exception:
            pass
        try:
            session_info = await self._get_session_source(event.session_id)

            if not session_info or not session_info.source:
                self.logger.warning(
                    f"No source for session {event.session_id}, skipping delivery"
                )
                return

            source = self._get_delivery_source(session_info)
            if not source or not source.platform_name:
                # No valid delivery source - don't ack, let event be retried
                return

            limit = PLATFORM_LIMITS.get(source.platform_name, float("inf"))
            chunks = chunk_message(
                event.content,
                int(limit) if limit != float("inf") else len(event.content)
            )

            channel = self._get_channel(source.platform_name)
            if not channel:
                if self._is_rabbitmq:
                    # rabbitmq: raise → broker nack → DLX retry (the
                    # channel may come back up later).
                    raise RuntimeError(
                        f"No channel for platform {source.platform_name}"
                    )
                # 找不到对应平台的 channel：不 ack，保留持久化文件，重启后由 _recover 重新投递
                self.logger.error(
                    f"No channel for platform {source.platform_name}, event "
                    f"[session={event.session_id} ts={event.timestamp}] left unacked"
                )
                return

            await self._deliver(chunks, source, channel)
            if not self._is_rabbitmq:
                # memory mode: explicit ack deletes the pending file;
                # rabbitmq mode acks automatically on handler return.
                await self.context.eventbus.ack(event)
            self.logger.info(
                f"Delivered message to {source.platform_name} for session {event.session_id}"
            )
        except Exception as e:
            if self._is_rabbitmq:
                # 失败即抛出：触发 broker nack → DLX 重试（at-least-once）
                raise
            self.logger.error(f"Failed to deliver message: {e}")

    def _get_channel(self, platform: str) -> "Channel[Any] | None":
        for channel in self.context.channels:
            if channel.platform_name == platform:
                return channel
        return None
