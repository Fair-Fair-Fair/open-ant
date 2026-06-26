"""Websocket worker for broadcasting evetnts to connected clients"""
import logging
import time
import dataclasses
from typing import TYPE_CHECKING, Set

from ant.core.agent import Agent

from fastapi import WebSocket
from fastapi.websockets import WebSocketDisconnect
from pydantic import ValidationError, BaseModel, Field

from .worker import SubscribeWorker
from ant.core.events import EventSource, Event, InboundEvent, OutboundEvent, WebSocketEventSource
from ant.utils.config import SourceSessionConfig

if TYPE_CHECKING:
    from ant.core.context import SharedContext

logger = logging.getLogger(__name__)


class WebsocketMessage(BaseModel):
    """Incoming WebSocket message from client"""
    source: str = Field(..., min_length=1, description="Client identifier")
    content: str = Field(..., min_length=1, description="Message content")
    agent_id: str | None = Field(
        None, description="Target agent ID (optional - uses routing if not specified)"
    )


class WebSocketWorker(SubscribeWorker):
    """Manages Websocket connections and event broadcasting"""
    def __init__(self, context: "SharedContext"):
        super().__init__(context)
        self.clients: Set[WebSocket] = set()

        # Auto-subscribe to event classes
        for event_class in [InboundEvent, OutboundEvent]:
            self.context.eventbus.subscribe(event_class, self.handle_event)
        self.logger.info("WebSocketWorker subscribed to event types")

    async def handle_event(self, event: Event) -> None:
        """Handle Eventbus event by broadcasting to websocket clients"""
        if not self.clients:
            return

        # Serialize event to dict with type information
        event_dict = {
            "type": event.__class__.__name__,
        }
        event_dict.update(dataclasses.asdict(event))

        # Convert EventSource to string for json serialization
        if "source" in event_dict and hasattr(event.source, "__str__"):
            event_dict["source"] = str(event.source)

        # Broadcast to all clients
        self.logger.debug(
            f"Broadcasting {event.__class__.__name__} to {len(self.clients)} clients"
        )

        for client in list(self.clients):
            try:
                await client.send_json(event_dict)
            except Exception as e:
                self.logger.error(f"Failed to send to client: {e}")
                self.clients.discard(client)

    async def handle_connection(self, web_socket: WebSocket) -> None:
        """Handle a single WebSocket connection lifecycle"""
        self.clients.add(web_socket)

        self.logger.info(
            f"Websocket client connected. Total clients: {len(self.clients)}"
        )

        try:
            await self._run_client_loop(web_socket)
        finally:
            self.clients.discard(web_socket)
            self.logger.info(
                f"Websocket client disconnected. Total clients: {len(self.clients)}"
            )

    async def _run_client_loop(self, web_socket: WebSocket) -> None:
        """Run message receiving loop for a single client"""
        while True:
            try:
                data = await web_socket.receive_json()
                msg = WebsocketMessage(**data)

                event = self._normalize_message(msg)

                await self.context.eventbus.publish(event)
                self.logger.debug(f"Emitted InboundEvent from WebSocket: {msg.source}")

            except WebSocketDisconnect:
                self.logger.info("Client disconnected normally")
                break
            except ValidationError as e:
                await web_socket.send_json(
                    {
                        "type": "error",
                        "message": f"Validation error: {e}",
                    }
                )
                self.logger.warning(f"Validation error from client: {e}")
            except Exception as e:
                self.logger.error(f"Unexpected error in client loop: {e}")
                break

    def _normalize_message(self, msg: "WebsocketMessage") -> InboundEvent:
        """Normalize WebSocketMessage to InboundEvent."""
        source = WebSocketEventSource(user_id=msg.source)

        session_id = self._get_or_create_session_id(source)

        return InboundEvent(
            session_id=session_id,
            source=source,
            content=msg.content,
            timestamp=time.time(),
        )

    def _get_or_create_session_id(self, source: "EventSource") -> str:
        """Get or create session ID for a given source."""
        source_str = str(source)

        source_session = self.context.config.sources.get(source_str)
        if source_session:
            return source_session.session_id

        agent_def = self.context.agent_loader.load(self.context.config.default_agent)
        agent = Agent(agent_def, self.context)
        session = agent.new_session(source)

        # Cache the session
        self.context.config.set_runtime(
            f"sources.{source_str}", SourceSessionConfig(session_id=session.session_id)
        )
        return session.session_id
