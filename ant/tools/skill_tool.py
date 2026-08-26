"""Skill tool factory for creating dynamic skill tool."""

from typing import TYPE_CHECKING

from ant.tools.base import tool

if TYPE_CHECKING:
    from ant.core.agent import AgentSession
    from ant.core.skill_loader import SkillLoader


def create_skill_tool(skill_loader: "SkillLoader"):
    """Factory function to create skill tool with dynamic schema."""
    skill_metadata = skill_loader.discover_skills()

    if not skill_metadata:
        return None

    # Build XML description of available skills
    skills_xml = "<skills>\n"
    for meta in skill_metadata:
        skills_xml += f'  <skill name="{meta.name}">{meta.description}</skill>\n'
    skills_xml += "</skills>"

    # Build enum of skill IDs
    skill_enum = [meta.id for meta in skill_metadata]

    @tool(
        name="skill",
        description=(
            f"Load and invoke a specialized skill. Returns the skill definition "
            f"content, or an error string if the skill cannot be loaded. "
            f"{skills_xml}"
        ),
        parameters={
            "type": "object",
            "properties": {
                "skill_name": {
                    "type": "string",
                    "enum": skill_enum,
                    "description": "The name of the skill to load (must be one of the enum values)"  # noqa: E501
                }
            },
            "required": ["skill_name"],
        },
    )
    async def skill_tool(skill_name: str, session: "AgentSession") -> str:
        """Load and return skill content."""
        try:
            skill_def = skill_loader.load_skill(skill_name)
            return skill_def.content
        except Exception:
            return f"Error: Skill '{skill_name}' not found. It may have been removed or is unavailable."  # noqa: E501

    return skill_tool
