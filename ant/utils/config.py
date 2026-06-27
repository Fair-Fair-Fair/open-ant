"""Configuration management with hot reload support."""
import logging
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer


class LLMConfig(BaseModel):
    """LLM provider configuration."""

    provider: str
    model: str
    api_key: str
    api_base: str | None = None
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    max_tokens: int = Field(default=2048, gt=0)

    @field_validator("api_base")
    @classmethod
    def api_base_must_be_url(cls, v: str | None) -> str | None:
        if v is not None and not v.startswith(("http://", "https://")):
            raise ValueError("api_base must be a valid URL")
        return v


class BraveWebSearchConfig(BaseModel):
    """Configuration for web search provider"""

    provider: Literal["brave"] = "brave"
    api_key: str


class TavilyWebSearchConfig(BaseModel):
    """Configuration for Tavily web search provider"""

    provider: Literal["tavily"] = "tavily"
    api_key: str


class Crawl4AIWebSearchConfig(BaseModel):
    """Configuration for web search provider"""
    provider: Literal["crawl4ai"] = "crawl4ai"


class LangChainWebReadConfig(BaseModel):
    """Configuration for LangChain web read provider"""
    provider: Literal["langchain"] = "langchain"


class TelegramConfig(BaseModel):
    """Telegram platform configuration"""

    enabled: bool = True
    bot_token: str
    allowed_user_ids: list[str] = Field(default_factory=list)


class DiscordConfig(BaseModel):
    """Discord platform configuration"""

    enabled: bool = True
    bot_token: str
    channel_id: str | None = None
    allowed_user_ids: list[str] = Field(default_factory=list)


class SourceSessionConfig(BaseModel):
    """Source session configuration"""
    session_id: str


class ChannelConfig(BaseModel):
    """Channel configuration"""

    enabled: bool = True
    telegram: TelegramConfig | None = None
    discord: DiscordConfig | None = None


class ApiConfig(BaseModel):
    """HTTP API configuration"""

    host: str = "127.0.0.1"
    port: int = Field(default=8000, gt=0, lt=65536)


class Config(BaseModel):
    """Main configuration for step 00."""

    workspace: Path
    llm: LLMConfig
    default_agent: str
    agents_path: Path = Field(default=Path("agents"))
    skills_path: Path = Field(default=Path("skills"))

    # 12-cron-heartbeat
    crons_path: Path = Field(default=Path("crons"))

    logging_path: Path = Field(default=Path(".logs"))
    history_path: Path = Field(default=Path(".history"))
    event_path: Path = Field(default=Path(".events"))
    websearch: BraveWebSearchConfig | TavilyWebSearchConfig | None = None
    webread: Crawl4AIWebSearchConfig | LangChainWebReadConfig | None = None
    channels: ChannelConfig = Field(default_factory=ChannelConfig)
    sources: dict[str, SourceSessionConfig] = Field(default_factory=dict)
    default_delivery_source: str | None = None
    # 10-websocket
    api: ApiConfig = Field(default_factory=ApiConfig)

    # 11-multi-agent-routing
    routing: dict = Field(default_factory=lambda: {"bindings": []})

    @model_validator(mode="after")
    def resolve_paths(self) -> "Config":
        """Resolve relative paths to absolute using workspace."""
        for field_name in (
            "agents_path",
            "skills_path",
            "history_path",
            "logging_path",
            "event_path",
            "crons_path",
        ):
            path = getattr(self, field_name)
            if not path.is_absolute():
                setattr(self, field_name, self.workspace / path)
        return self

    @classmethod
    def load(cls, workspace_dir: Path) -> "Config":
        """Load configuration from workspace directory."""
        config_data = cls._load_merged_configs(workspace_dir)
        config_data["workspace"] = workspace_dir
        return cls.model_validate(config_data)

    @classmethod
    def _load_merged_configs(cls, workspace_dir: Path) -> dict[str, Any]:
        """Load and merge user and runtime config files"""
        config_data: dict[str, Any] = {}

        user_config = workspace_dir / "config.user.yaml"
        runtime_config = workspace_dir / "config.runtime.yaml"

        if user_config.exists():
            with open(user_config) as f:
                config_data = cls._deep_merge(config_data, yaml.safe_load(f) or {})
        if runtime_config.exists():
            with open(runtime_config) as f:
                config_data = cls._deep_merge(config_data, yaml.safe_load(f) or {})

        return config_data

    @staticmethod
    def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
        """Deep merge override dict into base dict"""
        result = base.copy()

        for key, value in override.items():
            if(
                key in result
                and isinstance(result[key], dict)
                and isinstance(value, dict)
            ):
                result[key] = Config._deep_merge(result[key], value)
            else:
                result[key] = value

        return result

    def _set_nested(self, obj: dict, key: str, value: Any) -> None:
        """Set a nested value in a dict using dot notation"""
        keys = key.split(".")
        for k in keys[:-1]:
            if k not in obj or not isinstance(obj[k], dict):
                obj[k] = {}
            obj = obj[k]
        obj[keys[-1]] = value

    def _set_config_value(self, config_path: Path, key: str, value: Any) -> None:
        """Update a config value in a yaml file"""
        # Load existing or start fresh
        if config_path.exists():
            with open(config_path) as f:
                data = yaml.safe_load(f) or {}
        else:
            data = {}

        if isinstance(value, BaseModel):
            value = value.model_dump()

        # Update the key(supports nested via dot notation)
        self._set_nested(data, key, value)

        # Write back
        with open(config_path, "w") as f:
            yaml.safe_dump(data, f)

    def set_user(self, key: str, value: Any) -> None:
        """Set a config value for the user config file"""
        self._set_config_value(self.workspace / "config.user.yaml", key, value)

    def set_runtime(self, key: str, value: Any) -> None:
        """Set a config value for the runtime config file"""
        self._set_config_value(self.workspace / "config.runtime.yaml", key, value)

    def reload(self) -> bool:
        """Re-load config.user.yaml and merge with runtime"""
        try:
            config_data = self._load_merged_configs(self.workspace)
            config_data["workspace"] = self.workspace

            # Create new instance and copy values
            new_config = Config.model_validate(config_data)

            # Update all fields from new config
            for field_name in Config.model_fields:
                setattr(self, field_name, getattr(new_config, field_name))

            return True
        except Exception as e:
            logging.debug("Config reload failed: %s", e)
            return False


class ConfigHandler(FileSystemEventHandler):
    """Handles config file modification events"""
    def __init__(self, config: Config):
        self._config = config

    def on_modified(self, event):
        """Reload config when config.user.yaml changes"""
        if not event.is_directory and event.src_path.endswith("config.user.yaml"):
            logging.info("Config file modified, reloading...")
            success = self._config.reload()
            if success:
                logging.info("Config reloaded successfully")
            else:
                logging.warning("Config reload failed")

    def on_created(self, event):
        """Handle file creation (some editors delete+create on save)"""
        if not event.is_directory and event.src_path.endswith("config.user.yaml"):
            logging.info("Config file created, reloading...")
            success = self._config.reload()
            if success:
                logging.info("Config reloaded successfully")
            else:
                logging.warning("Config reload failed")


class ConfigReloader:
    """Manages watchdog observe for config hot reload"""
    def __init__(self, config: Config):
        self._config = config
        self._observer = Observer()

    def start(self) -> None:
        """Start watchdog config file for changes"""
        handler = ConfigHandler(self._config)
        self._observer.schedule(handler, str(self._config.workspace), recursive=False)
        self._observer.start()
        logging.info("Config reloader started, watching: %s", self._config.workspace)

    def stop(self) -> None:
        """Stop watching"""
        self._observer.stop()
        self._observer.join()
        del self._observer
