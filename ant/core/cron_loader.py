"""Cron job definition loader"""

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

from croniter import croniter
from pydantic import BaseModel, ValidationError, field_validator

from ant.utils.def_loader import (
    DefNotFoundError,
    InvalidDefError,
    discover_definitions,
    parse_definition
)

if TYPE_CHECKING:
    from ant.utils.config import Config

logger = logging.getLogger(__name__)


class CronDef(BaseModel):
    """Loaded cron job definition"""
    id: str
    name: str
    description: str
    agent: str
    schedule: str
    prompt: str
    one_off: bool = False

    @field_validator("schedule")
    @classmethod
    def validate_schedule(cls, v: str) -> str:
        """Validate cron expression and enforce 5-minute minimum granularity"""
        if not croniter.is_valid(v):
            raise ValueError(f"Invalid cron expression: {v}")

        # Check minimum 5-minute granularity using croniter
        # Get the frist two run times and check the gap
        base = datetime(2024, 1, 1, 0, 0)
        cron = croniter(v, base)
        first_run = cron.get_next(datetime)
        second_run = cron.get_next(datetime)
        gap_minutes = (second_run - first_run).total_seconds() / 60

        if gap_minutes < 5:
            raise ValueError(
                f"Schedule must have minimum 5-minute granularity. Got: {v} (runs every {gap_minutes:.0f} min)"
            )

        return v


class CronLoader:
    """Loads cron job definition from CRON.md files"""

    @staticmethod
    def from_config(config: "Config") -> "CronLoader":
        """Create CronLoader from config"""
        return CronLoader(config)

    def __init__(self, config: "Config"):
        """Initialize CronLoader"""
        self.config = config
        self.config.crons_path.mkdir(parents=True, exist_ok=True)

    def discover_crons(self) -> list[CronDef]:
        """Scan crons directory, return definitions fro all valid crons"""
        return discover_definitions(
            self.config.crons_path,
            "CRON.md",
            self._parse_cron_def
        )

    def _parse_cron_def(self,
        def_id: str, frontmatter: dict[str, Any], body: str
    ) -> CronDef:
        try:
            return CronDef(
                id=def_id,
                name=frontmatter["name"],  # type: ignore[misc]
                description=frontmatter["description"],  # type: ignore[misc]
                agent=frontmatter["agent"],  # type: ignore[misc]
                schedule=frontmatter["schedule"],  # type: ignore[misc]
                prompt=body.strip(),
                one_off=frontmatter.get("one_off", False),
            )
        except ValidationError as e:
            logger.warning(f"Invalid cron '{def_id}': {e}")
            return None
        except KeyError as e:
            logger.warning(f"Missing required field in cron '{def_id}': {e}")
            return None

    def load(self, cron_id: str) -> CronDef:
        """Load cron by id"""
        cron_file = self.config.crons_path / cron_id / "CRON.md"
        if not cron_file.exists():
            raise DefNotFoundError("cron", cron_id)

        try:
            content = cron_file.read_text()
            cron_def = parse_definition(content, cron_id, self._parse_cron_def)
        except InvalidDefError:
            raise
        except Exception as e:
            raise InvalidDefError("cron", cron_id, str(e))

        if cron_def is None:
            raise InvalidDefError("cron", cron_id, "validation failed")

        return cron_def
