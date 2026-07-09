"""Webread tool factory."""

from typing import TYPE_CHECKING

from ant.tools.base import BaseTool, tool
from ant.provider.web_read import WebReadProvider

if TYPE_CHECKING:
    from ant.core.agent import AgentSession
    from ant.utils.config import Config


def create_webread_tool(config: "Config") -> BaseTool | None:
    """Factory to create webread tool with injected config."""
    if not config.webread:
        return None

    provider = WebReadProvider.from_config(config)

    @tool(
        name="webread",
        description=(
            "Read and extract content from a web page. "
            "Returns the page content as markdown."
        ),
        parameters={
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The URL to read",
                }
            },
            "required": ["url"],
        },
    )
    async def webread(url: str, session: "AgentSession") -> str:
        """Read a web page and return markdown content."""
        session.shared_context.sandbox.network.validate_url(url)

        result = await provider.read(url)

        if result.error:
            return f"Error reading {url}: {result.error}"

        return f"**{result.title}**\n\n{result.content}"

    return webread
