"""Agent definition loader."""

from typing import Any

from pydantic import BaseModel, ValidationError

from ant.utils.config import Config, LLMConfig
from ant.utils.def_loader import (
    DefNotFoundError,
    InvalidDefError,
    parse_definition,
)


class AgentDef(BaseModel):
    """Loaded agent definition with merged settings."""

    id: str
    name: str
    description: str = ""
    agent_md: str
    llm: LLMConfig
    allow_skills: bool = False


class AgentLoader:
    """Loads agent definitions from AGENT.md files."""

    @staticmethod
    def from_config(config: Config) -> "AgentLoader":
        return AgentLoader(config)

    def __init__(self, config: Config):
        """Initialize AgentLoader."""
        self.config = config

    def load(self, agent_id: str) -> AgentDef:
        """Load agent by ID."""
        agent_file = self.config.agents_path / agent_id / "AGENT.md"
        if not agent_file.exists():
            raise DefNotFoundError("agent", agent_id)

        try:
            content = agent_file.read_text()
            agent_def = parse_definition(content, agent_id, self._parse_agent_def)
        except InvalidDefError:
            raise
        except Exception as e:
            raise InvalidDefError("agent", agent_id, str(e))

        return agent_def

    def _parse_agent_def(
        self, def_id: str, frontmatter: dict[str, Any], body: str
    ) -> AgentDef:
        """Parse agent definition from frontmatter (callback for parse_definition)."""
        llm_overrides = frontmatter.get("llm")
        merged_llm = self._merge_llm_config(llm_overrides)

        try:
            return AgentDef(
                id=def_id,
                name=frontmatter["name"],  # type: ignore[misc]
                description=frontmatter.get("description", ""),
                agent_md=body.strip(),
                llm=merged_llm,
                allow_skills=frontmatter.get("allow_skills", False),
            )
        except ValidationError as e:
            raise InvalidDefError("agent", def_id, str(e))

    def _merge_llm_config(self, agent_llm: dict[str, Any] | None) -> LLMConfig:
        """Deep merge agent's llm config with global defaults."""
        base = self.config.llm.model_dump()
        if agent_llm:
            base = {**base, **agent_llm}
        return LLMConfig(**base)
