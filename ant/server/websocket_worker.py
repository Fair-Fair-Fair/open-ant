"""Websocket worker for broadcasting evetnts to connected clients"""
import dataclasses
import logging
import time
from typing import TYPE_CHECKING, Set

from fastapi import WebSocket
from fastapi.websockets import WebSocketDisconnect
from pydantic import BaseModel, Field, ValidationError

from ant.core.agent import Agent
from ant.core.events import (
    ConfirmationRequestEvent,
    Event,
    EventSource,
    InboundEvent,
    OutboundEvent,
    StreamChunkEvent,
    WebSocketEventSource,
)
from ant.utils.config import SourceSessionConfig

from .auth import verify_ws_token
from .observability import record_event_consumed  # Phase 5A observability
from .rate_limit import SlidingWindowLimiter
from .worker import SubscribeWorker

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
    """Manages Websocket connections and event broadcasting

    Phase 4A (auth-security):
      * every connection is authenticated on handshake (``verify_ws_token``,
        fail = close 4401, no subscription, no broadcast);
      * broadcasts are user-filtered: an event is sent only to connections
        that declared the event's user (single-user mode is equivalent to
        the old broadcast-to-all; multi-user just works because each client
        declares its own source identity).  Events with no WS user (telegram/
        discord/cli/agent/cron) keep broadcasting to all clients — multi-user
        extension point: map channel → user to narrow this down.
      * confirmation responses are bound to the requesting user (request_id →
        user map maintained here; ConfirmationRequestEvent carries no
        user_id field, so the owner is derived from the session → source
        mapping recorded on inbound messages);
      * every inbound message is rate limited by IP + user when enabled.
    """
    def __init__(self, context: "SharedContext"):
        super().__init__(context)
        self.clients: Set[WebSocket] = set()
        # Phase 4A: ws → authenticated identity (from the handshake token).
        self._client_users: dict[WebSocket, str] = {}
        # Phase 4A: ws → the source identity the client declares in its
        # messages (used for user-filtered broadcasting).
        self._client_sources: dict[WebSocket, str] = {}
        # Phase 4A: session_id → source user_id (recorded on inbound).
        self._session_owner: dict[str, str] = {}
        # Phase 4A: request_id → source user_id of the requesting session.
        self._pending_confirm_owner: dict[str, str] = {}

        # Phase 4A: per-message rate limiting (fail-open when Redis is down).
        self._rate_limiter: SlidingWindowLimiter | None = None
        api_cfg = getattr(context.config, "api", None)
        rate_cfg = getattr(api_cfg, "rate_limit", None) if api_cfg is not None else None
        if rate_cfg is not None and getattr(rate_cfg, "enabled", False):
            self._rate_limiter = SlidingWindowLimiter(rate_cfg)

        # Auto-subscribe to event classes
        for event_class in [
            InboundEvent, OutboundEvent, StreamChunkEvent, ConfirmationRequestEvent,
        ]:
            self.context.eventbus.subscribe(event_class, self.handle_event)
        self.logger.info("WebSocketWorker subscribed to event types")

    async def handle_event(self, event: Event) -> None:
        """Handle Eventbus event by broadcasting to websocket clients"""
        # Phase 4A: record who owns each confirmation request so responses
        # can be bound to the requesting user (ConfirmationRequestEvent has
        # no user_id field — the owner is derived from the session mapping).
        if isinstance(event, ConfirmationRequestEvent):
            owner = self._session_owner.get(event.session_id)
            self._pending_confirm_owner[event.request_id] = owner
            if owner is None:
                self.logger.warning(
                    "Confirmation request id=%s session=%s has no known WS "
                    "owner — responses will be rejected (fail-closed)",
                    event.request_id, event.session_id,
                )
            event_user = owner
        elif isinstance(event.source, WebSocketEventSource):
            event_user = event.source.user_id
        else:
            # No WS user binding (telegram/discord/cli/agent/cron) — keep
            # broadcasting to all clients (status quo). Multi-user extension
            # point: map the channel → user to narrow this down.
            event_user = None

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

        # Broadcast to the owning user's connections only (Phase 4A).
        self.logger.debug(
            f"Broadcasting {event.__class__.__name__} to {len(self.clients)} clients"
        )

        for client in list(self.clients):
            if event_user is not None and self._client_sources.get(client) != event_user:
                continue  # belongs to another user's connection
            try:
                await client.send_json(event_dict)
            except Exception as e:
                self.logger.error(f"Failed to send to client: {e}")
                self.clients.discard(client)

    async def handle_connection(self, web_socket: WebSocket) -> None:
        """Handle a single WebSocket connection lifecycle

        Phase 4A: authenticate the handshake FIRST — a rejected connection
        is closed (code 4401) and never subscribed, never broadcast to.
        """
        user_id = await verify_ws_token(web_socket, self.context)
        if user_id is None:
            return  # rejected — verify_ws_token already closed the socket

        self.clients.add(web_socket)
        self._client_users[web_socket] = user_id

        self.logger.info(
            "Websocket client connected (user=%s). Total clients: %d",
            user_id, len(self.clients),
        )

        try:
            await self._run_client_loop(web_socket)
        finally:
            self.clients.discard(web_socket)
            self._client_users.pop(web_socket, None)
            self._client_sources.pop(web_socket, None)
            self.logger.info(
                "Websocket client disconnected (user=%s). Total clients: %d",
                user_id, len(self.clients),
            )

    async def _run_client_loop(self, web_socket: WebSocket) -> None:
        """Run message receiving loop for a single client"""
        while True:
            try:
                data = await web_socket.receive_json()
                msg = WebsocketMessage(**data)

                # Phase 4A: remember the client's declared source identity
                # (used by the user-filtered broadcast in handle_event).
                self._client_sources[web_socket] = msg.source

                # ── Phase 4A: per-message rate limiting (fail-open) ──
                if self._rate_limiter is not None:
                    ip = web_socket.client.host if web_socket.client else "unknown"
                    if not await self._rate_limiter.check_ip_and_user(ip, msg.source):
                        await web_socket.send_json(
                            {
                                "type": "error",
                                "message": "rate limit exceeded",
                            }
                        )
                        self.logger.warning(
                            "Rate limit exceeded: user=%s ip=%s", msg.source, ip,
                        )
                        continue

                # ── Confirmation response handling ──
                # If the message is a ConfirmationResponseEvent, route it
                # to the broker instead of creating an InboundEvent.
                # Phase 4A: the response is only honoured when the responding
                # client is the user who owns the pending request.
                if msg.content.startswith("__confirm__:") and ":" in msg.content:
                    try:
                        _, request_id, approved_str = msg.content.split(":", 2)
                        approved = approved_str == "true"
                    except ValueError as exc:
                        self.logger.warning(
                            "Malformed confirmation response: %s", exc,
                        )
                        continue

                    owner = self._pending_confirm_owner.get(request_id)
                    if owner is None:
                        self.logger.warning(
                            "Rejected confirmation response id=%s: unknown "
                            "request or request has no known owner", request_id,
                        )
                        continue
                    if msg.source != owner:
                        self.logger.warning(
                            "Rejected confirmation response id=%s: source %r "
                            "is not the requesting user %r",
                            request_id, msg.source, owner,
                        )
                        continue

                    self.context.confirmation_broker.respond(
                        request_id, approved,
                    )
                    self.logger.info(
                        "Confirmation response: id=%s approved=%s", request_id, approved,
                    )
                    continue
                # ─────────────────────────────────────

                event = await self._normalize_message(msg)

                # Phase 5A observability：客户端消息入站计数（观测永不打断主链路）。
                try:
                    record_event_consumed(event)
                except Exception:
                    pass

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

    async def _normalize_message(self, msg: "WebsocketMessage") -> InboundEvent:
        """Normalize WebSocketMessage to InboundEvent."""
        source = WebSocketEventSource(user_id=msg.source)

        session_id = await self._get_or_create_session_id(source)

        # Phase 4A: bind the session to its owning user so confirmation
        # requests can be attributed (and responses bound) to that user.
        self._session_owner[session_id] = msg.source

        return InboundEvent(
            session_id=session_id,
            source=source,
            content=msg.content,
            timestamp=time.time(),
        )

    async def _get_or_create_session_id(self, source: "EventSource") -> str:
        """Get or create session ID for a given source."""
        source_str = str(source)

        source_session = self.context.config.sources.get(source_str)
        if source_session:
            return source_session.session_id

        agent_def = self.context.agent_loader.load(self.context.config.default_agent)
        agent = Agent(agent_def, self.context)
        session = await agent.new_session(source)

        # Cache the session
        self.context.config.set_runtime(
            f"sources.{source_str}", SourceSessionConfig(session_id=session.session_id)
        )
        return session.session_id
