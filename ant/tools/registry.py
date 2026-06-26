"""Tool registry for managing available tools."""
from typing import TYPE_CHECKING, Any

from ant.tools.base import BaseTool
from ant.tools.builtin_tools import bash, edit_file, read_file, write_file

if TYPE_CHECKING:
    from ant.core.agent import AgentSession


class ToolRegistry:
    """Registry for all available tools."""
    def __init__(self) -> None:
        """Initialize an empty tool registry"""
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        """Register a tool"""
        self._tools[tool.name] = tool

    def get(self, name: str) -> BaseTool | None:
        return self._tools.get(name)

    def list_all(self) -> list[BaseTool]:
        """List all registered tools"""
        return list(self._tools.values())

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        """Get tool schemas for all registered tools."""
        return [tool.get_tool_schema() for tool in self._tools.values()]

    async def execute_tool(
            self,name: str, session: "AgentSession", **kwargs: Any
    ) -> str:
        """Execute a tool by name"""
        tool = self.get(name)
        if tool is None:
            raise ValueError(f"tool not found name: {name}")
        return await tool.execute(session=session, **kwargs)

    @classmethod
    def with_builtins(cls) -> "ToolRegistry":
        """Created a ToolRegistry with builtin tools already registered"""
        registry = cls()
        registry.register(read_file)
        registry.register(write_file)
        registry.register(edit_file)
        registry.register(bash)

        return registry
