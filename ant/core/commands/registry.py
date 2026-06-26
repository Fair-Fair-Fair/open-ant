"""Command registry for managing slash commands"""
from typing import TYPE_CHECKING

from ant.core.commands.base import Command

if TYPE_CHECKING:
    from ant.core.agent import AgentSession


class CommandRegistry:
    """Registry for slash commands"""
    def __init__(self) -> None:
        self._commands: dict[str, Command] = {}

    def register(self, cmd: Command) -> None:
        """Register a command and its aliases"""
        self._commands[cmd.name] = cmd
        for alias in cmd.aliases:
            self._commands[alias] = cmd

    def list_commands(self) -> list[Command]:
        """Return list of unique commands (deduplicated by name)."""
        seen = set()
        commands = []
        for cmd in self._commands.values():
            if cmd.name not in seen:
                seen.add(cmd.name)
                commands.append(cmd)
        return commands

    def resolve(self, input: str) -> tuple[Command, str] | None:
        """Parse input and return (command, args) if it mathces"""
        if not input.startswith("/"):
            return None

        parts = input[1:].split(None, 1)
        if not parts:
            return None

        cmd_name = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""

        cmd = self._commands.get(cmd_name)
        if cmd:
            return (cmd, args)
        return None

    async def dispatch(self, input: str, session: "AgentSession") -> str | None:
        """Parse and execute a slash command"""
        resolved = self.resolve(input)
        if not resolved:
            return None
        cmd, args = resolved
        return await cmd.execute(args, session)

    @classmethod
    def with_builtins(cls) -> "CommandRegistry":
        """Create registry with built-in commands registered"""
        from ant.core.commands.handlers import (
            HelpCommand, SkillsCommand, SessionCommand,
            ContextCommand, CompactCommand
        )

        registry = cls()
        registry.register(HelpCommand())
        registry.register(SkillsCommand())
        registry.register(SessionCommand())

        registry.register(CompactCommand())
        registry.register(ContextCommand())

        return registry
