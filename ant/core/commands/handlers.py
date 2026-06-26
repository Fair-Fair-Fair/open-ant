"""Built-in slash command handlers."""

from typing import TYPE_CHECKING

from ant.core.commands.base import Command
from ant.utils.def_loader import DefNotFoundError

if TYPE_CHECKING:
    from ant.core.agent import AgentSession


class SessionCommand(Command):
    """Show current session details."""

    name = "session"
    description = "Show current session details"

    async def execute(self, args: str, session: "AgentSession") -> str:
        info = session.shared_context.history_store.get_session_info(session.session_id)

        # Handle case where session not found in index
        created_str = info.created_at if info else "Unknown"

        lines = [
            f"**Session ID:** `{session.session_id}`",
            f"**Agent:** {session.agent.agent_def.name} (`{session.agent.agent_def.id}`)",
            f"**Created:** {created_str}",
            f"**Messages:** {len(session.state.messages)}",
        ]
        return "\n".join(lines)


class HelpCommand(Command):
    """Show available commands."""

    name = "help"
    aliases = ["?"]
    description = "Show available commands"

    async def execute(self, args: str, session: "AgentSession") -> str:
        lines = ["**Available Commands:**"]
        for cmd in session.shared_context.command_registry.list_commands():
            names = [f"/{cmd.name}"] + [f"/{a}" for a in cmd.aliases]
            lines.append(f"{', '.join(names)} - {cmd.description}")
        return "\n".join(lines)


class CompactCommand(Command):
    """Trigger manual context compaction."""

    name = "compact"
    description = "Compact conversation context manually"

    async def execute(self, args: str, session: "AgentSession") -> str:
        # Force compaction regardless of threshold
        await session.context_guard._compact_messages(session.state)
        msg_count = len(session.state.messages)
        return f"✓ Context compacted. {msg_count} messages retained."


class ContextCommand(Command):
    """Show session context information."""

    name = "context"
    description = "Show session context information"

    async def execute(self, args: str, session: "AgentSession") -> str:
        token_count = session.context_guard.estimate_tokens(session.state)
        threshold = session.context_guard.token_threshold
        usage_pct = (token_count / threshold) * 100 if threshold > 0 else 0

        lines = [
            f"**Messages:** {len(session.state.messages)}",
            f"**Tokens:** {token_count:,} ({usage_pct:.1f}% of {threshold:,} threshold)",
        ]
        return "\n".join(lines)


class SkillsCommand(Command):
    """List all skills or show skill details."""

    name = "skills"
    description = "List all skills or show skill details"

    async def execute(self, args: str, session: "AgentSession") -> str:
        if not args:
            skills = session.shared_context.skill_loader.discover_skills()
            if not skills:
                return "No skills configured."

            lines = ["**Skills:**"]
            for skill in skills:
                lines.append(f"- `{skill.id}`: {skill.description}")
            return "\n".join(lines)

        # Show specific skill details
        skill_id = args.strip()
        try:
            skill = session.shared_context.skill_loader.load_skill(skill_id)
        except DefNotFoundError:
            return f"✗ Skill `{skill_id}` not found."

        lines = [
            f"**Skill:** `{skill.id}`",
            f"**Name:** {skill.name}",
            f"**Description:** {skill.description}",
            f"\n---\n\n**SKILL.md:**\n```\n{skill.content}\n```",
        ]
        return "\n".join(lines)
