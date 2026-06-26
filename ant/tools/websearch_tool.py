"""Websearch tool factory."""

from typing import TYPE_CHECKING

from ant.tools.base import BaseTool, tool
from ant.provider.web_search import WebSearchProvider

if TYPE_CHECKING:
    from ant.core.agent import AgentSession
    from ant.utils.config import Config


def create_websearch_tool(config: "Config") -> BaseTool | None:
    """Factory to create websearch tool with injected config."""
    if not config.websearch:
        return None

    provider = WebSearchProvider.from_config(config)

    @tool(
        name="websearch",
        description=(
            "Search the web for information. "
            "Returns a list of results with titles, URLs, and snippets."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query",
                }
            },
            "required": ["query"],
        },
    )
    async def websearch(query: str, session: "AgentSession") -> str:
        """Search the web and return formatted results."""

        results = await provider.search(query)

        if not results:
            return "No results found."

        output = []
        for i, r in enumerate(results, 1):
            output.append(f"{i}. **{r.title}**\n   {r.url}\n   {r.snippet}")
        return "\n\n".join(output)

    return websearch
