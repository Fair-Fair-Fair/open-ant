"""Post message tool factory for agent-initiated messaging"""

import time
from typing import TYPE_CHECKING

from ant.core.events import AgentEventSource, OutboundEvent
from ant.tools.base import BaseTool, tool

if TYPE_CHECKING:
    from ant.core.agent import AgentSession
    from ant.core.context import SharedContext


def create_post_message_tool(context: "SharedContext") -> BaseTool | None:
    """Factory to create post_message tool"""
    config = context.config

    # Return None if channels not enabled or no channel configured
    if not config.channels.enabled:
        return None

    # Check if we have any channels configured
    if not config.channels:
        return None

    @tool(
        name="post_message",
        description="Send a message to the user via the default messaging platform. Use this to proactively notify the user about completed tasks, cron results, or important updates.",
        parameters={
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The message content to be sent."
                }
            },
            "required": ["content"]
        }
    )
    async def post_message(content: str, session: "AgentSession") -> str:
        """Send a message to the default user on the default platform"""
        try:
            # Publish OUTBOUND event for the DeliveryWorker to handle
            event = OutboundEvent(
                session_id=session.session_id,
                source=AgentEventSource(agent_id=session.agent.agent_def.id),
                content=content,
                timestamp=time.time(),
            )
            await context.eventbus.publish(event)
            return "Message queueed for delivery"
        except Exception as e:
            return f"failed to send message: {e}"

    return post_message
