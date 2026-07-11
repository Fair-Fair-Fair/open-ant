"""Human-in-the-Loop confirmation broker.

Manages the lifecycle of tool-call confirmation requests:
  1. Pipeline pauses before executing a high-privilege tool.
  2. A ``ConfirmationRequestEvent`` is sent to the WebSocket frontend.
  3. The frontend shows an approve/deny dialog to the user.
  4. The user's response is sent back via ``ConfirmationResponseEvent``.
  5. The broker resumes the tool execution or blocks it.

Timeout handling: if the user doesn't respond within the configured
timeout, the tool call is auto-denied (fail-closed).
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ant.core.events import ConfirmationRequestEvent, ConfirmationResponseEvent, AgentEventSource

if TYPE_CHECKING:
    from ant.core.context import SharedContext

logger = logging.getLogger(__name__)

# Default timeout for user response (seconds)
DEFAULT_CONFIRMATION_TIMEOUT = 30.0


@dataclass
class PendingConfirmation:
    """Internal state for a pending confirmation request."""

    request_id: str
    session_id: str
    tool_name: str
    tool_args: str
    future: asyncio.Future  # resolves to bool (True=approved, False=denied)
    created_at: float = field(default_factory=time.time)


class ConfirmationBroker:
    """Broker managing the lifecycle of confirmation requests.

    Instantiated once in ``SharedContext`` and accessed via
    ``session.shared_context.confirmation_broker``.
    """

    def __init__(self, timeout: float = DEFAULT_CONFIRMATION_TIMEOUT):
        self._timeout = timeout
        self._pending: dict[str, PendingConfirmation] = {}
        # Per-session cache: remember denied tools so retries within the
        # same turn don't trigger redundant approval dialogs.
        self._denied: dict[str, set[str]] = {}   # session_id -> {tool_name, ...}

    @property
    def timeout(self) -> float:
        return self._timeout

    def reset_turn(self, session_id: str) -> None:
        """Clear the per-turn denial cache at the start of a new user turn."""
        self._denied.pop(session_id, None)

    async def request_approval(
        self,
        session_id: str,
        tool_name: str,
        tool_args: str,
        context: SharedContext,
        agent_id: str = "",
    ) -> bool:
        """Request user approval for a tool call. Returns True if approved.

        Sends a ``ConfirmationRequestEvent`` to the frontend and waits for
        the user's response (or timeout).  Returns ``False`` on timeout
        or explicit denial.

        If the user already denied this tool in the current turn, returns
        ``False`` immediately without showing another dialog.
        """
        # If this tool was already denied in this session, auto-deny.
        denied_tools = self._denied.get(session_id, set())
        if tool_name in denied_tools:
            logger.info(
                "Auto-deny %s for session %s (previously denied this turn)",
                tool_name, session_id,
            )
            return False

        request_id = str(uuid.uuid4())[:8]
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()

        pending = PendingConfirmation(
            request_id=request_id,
            session_id=session_id,
            tool_name=tool_name,
            tool_args=tool_args,
            future=future,
        )
        self._pending[request_id] = pending

        # Send confirmation request to frontend
        request_event = ConfirmationRequestEvent(
            session_id=session_id,
            source=AgentEventSource(agent_id) if agent_id else AgentEventSource("system"),
            content=request_id,  # carry request_id to frontend for response
            request_id=request_id,
            tool_name=tool_name,
            tool_args=tool_args,
            timeout=self._timeout,
        )
        await context.eventbus.publish(request_event)

        logger.info(
            "Confirmation requested: id=%s tool=%s session=%s",
            request_id, tool_name, session_id,
        )

        try:
            approved = await asyncio.wait_for(future, timeout=self._timeout)
            logger.info(
                "Confirmation resolved: id=%s approved=%s", request_id, approved,
            )
            return approved
        except asyncio.TimeoutError:
            logger.warning(
                "Confirmation timed out after %.0fs: id=%s tool=%s",
                self._timeout, request_id, tool_name,
            )
            return False
        finally:
            self._pending.pop(request_id, None)

    def respond(self, request_id: str, approved: bool) -> bool:
        """Handle a user's response to a confirmation request.

        Returns True if the request was found and resolved, False if
        the request was already resolved or doesn't exist.
        """
        pending = self._pending.get(request_id)
        if pending is None:
            logger.debug("Confirmation response for unknown request: %s", request_id)
            return False

        if pending.future.done():
            logger.debug("Confirmation already resolved: %s", request_id)
            return False

        # Cache denial so retries within the same turn don't re-prompt
        if not approved:
            denied = self._denied.setdefault(pending.session_id, set())
            denied.add(pending.tool_name)
            logger.info(
                "Tool %s denied for session %s — caching for this turn",
                pending.tool_name, pending.session_id,
            )

        pending.future.set_result(approved)
        return True

    def pending_count(self) -> int:
        """Return the number of pending confirmations (for monitoring)."""
        return len(self._pending)
