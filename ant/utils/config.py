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


class MemoryConfig(BaseModel):
    """Configuration for RAG memory system"""

    enabled: bool = False
    provider: Literal["chroma"] = "chroma"
    persist_directory: Path = Field(default=Path(".memory"))
    embedding_model: str = "BAAI/bge-small-zh-v1.5"
    embedding_provider: str | None = None  # "litellm" 或 "sentence_transformers"，空则自动判断
    top_k: int = Field(default=5, gt=0, le=20)
    extraction_threshold: int = Field(default=5, gt=0)
    min_importance: int = Field(default=5, ge=1, le=10)
    merge_top_k: int = Field(default=3, gt=0, le=10)
    merge_similarity: float = Field(default=0.85, gt=0.0, lt=1.0)
    chunk_size: int = Field(default=1000, gt=0, le=10000)
    chunk_overlap: int = Field(default=200, ge=0, le=2000)
    docs_path: str | None = None
    doc_similarity_threshold: float = Field(default=0.75, gt=0.0, lt=1.0)


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


# ── Sandbox configuration ───────────────────────────────────────────────


class PathSandboxConfig(BaseModel):
    """Filesystem access restrictions for the agent sandbox."""

    enabled: bool = True
    allowed_dirs: list[str] = Field(default_factory=list)
    """Extra directories (relative to workspace or absolute) allowed for
    read/write.  The workspace itself is always allowed."""
    blocked_patterns: list[str] | None = None
    """Glob patterns to block even inside allowed dirs.
    ``None`` = use built-in defaults (config files, .history/, .memory/, etc.)."""
    allow_all: bool = False
    """If True, disable path sandbox entirely (opt-in override)."""


class CommandSandboxConfig(BaseModel):
    """Shell command execution restrictions for the agent sandbox."""

    enabled: bool = True
    default_timeout: int = Field(default=30, ge=1, le=300)
    """Max seconds a command may run before being killed."""
    max_output_size: int = Field(default=100_000, ge=1_000)
    """Max characters returned from stdout+stderr combined."""
    blocked_patterns: list[str] | None = None
    """Regex patterns that block command execution. ``None`` = use built-in defaults."""
    allowed_patterns: list[str] = Field(default_factory=list)
    """Explicit allowlist overrides — these commands always pass regardless of blocked_patterns."""
    working_dir: str | None = None
    """Restrict command working directory. ``None`` = workspace root."""


class NetworkSandboxConfig(BaseModel):
    """Outbound HTTP(S) restrictions for the agent sandbox."""

    enabled: bool = True
    block_private_ips: bool = True
    """Block requests to 10.x, 192.168.x, 172.16-31.x, 127.x, ::1."""
    block_file_urls: bool = True
    """Block ``file://`` scheme requests."""
    allowed_domains: list[str] = Field(default_factory=list)
    """If non-empty, *only* these domains (and subdomains via ``*.example.com``) are allowed."""
    denied_domains: list[str] = Field(default_factory=list)
    """Domains always blocked, regardless of allowed_domains."""


class SandboxConfig(BaseModel):
    """Top-level sandbox configuration.  All sub-sandboxes default to safe values."""

    enabled: bool = True
    """Master switch — when ``False`` ALL sandbox checks are bypassed."""
    path: PathSandboxConfig = Field(default_factory=PathSandboxConfig)
    command: CommandSandboxConfig = Field(default_factory=CommandSandboxConfig)
    network: NetworkSandboxConfig = Field(default_factory=NetworkSandboxConfig)


# ── Guardrail configuration ──────────────────────────────────────────────


class InputGuardrailConfig(BaseModel):
    """Input validation and prompt injection protection."""

    enabled: bool = True
    max_message_length: int = Field(default=10_000, ge=0)
    """Max characters in a single user message. 0 = unlimited."""
    sanitize_control_chars: bool = True
    """Strip ASCII control characters (except \\n, \\r, \\t) from user input."""
    detect_injection: bool = True
    """Scan for prompt injection patterns in user messages."""
    block_injection: bool = True
    """When True, matching messages are blocked. When False, only logged (audit mode)."""
    blocked_patterns: list[str] | None = None
    """Custom regex patterns for injection detection. None = use built-in defaults."""


class OutputGuardrailConfig(BaseModel):
    """Output sanitization and content policy enforcement."""

    enabled: bool = True
    redact_secrets: bool = True
    """Scan and redact API keys, tokens, private keys from agent responses."""
    max_output_length: int = Field(default=100_000, ge=0)
    """Max characters in agent response. 0 = unlimited."""
    detect_tool_injection: bool = True
    """Scan tool results for prompt injection before they enter LLM context."""
    tool_result_action: Literal["warn", "strip", "block"] = "warn"
    """Action when injection is detected in a tool result.
    - ``warn``: prepend ⚠️ security warning (default — agent sees result + warning)
    - ``strip``: remove the injected portion using regex
    - ``block``: replace entire tool result with safety message"""
    redact_patterns: list[str] | None = None
    """Custom regex patterns for secret redaction. None = use built-in defaults."""
    blocked_patterns: list[str] | None = None
    """Content policy patterns — responses matching these are replaced with a block message."""


class GuardrailConfig(BaseModel):
    """Top-level guardrail configuration. Master switch controls all."""

    enabled: bool = True
    """Master switch — when False ALL guardrail checks are bypassed."""
    input: InputGuardrailConfig = Field(default_factory=InputGuardrailConfig)
    output: OutputGuardrailConfig = Field(default_factory=OutputGuardrailConfig)


class Config(BaseModel):
    """Main configuration for step 00."""

    workspace: Path
    llm: LLMConfig
    default_agent: str
    agents_path: Path = Field(default=Path("agents"))
    skills_path: Path = Field(default=Path("skills"))

    # 12-cron-heartbeat
    crons_path: Path = Field(default=Path("crons"))

    memories_path: Path = Field(default=Path("memories"))

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

    # 16-rag-memory
    memory: MemoryConfig = Field(default_factory=MemoryConfig)

    # sandbox — harness security boundary
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)

    # guardrails — input/output content safety layer
    guardrails: GuardrailConfig = Field(default_factory=GuardrailConfig)

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
            "memories_path",
        ):
            path = getattr(self, field_name)
            if not path.is_absolute():
                setattr(self, field_name, self.workspace / path)

        # Resolve memory persist_directory
        if self.memory and not self.memory.persist_directory.is_absolute():
            self.memory.persist_directory = self.workspace / self.memory.persist_directory

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
        """Set a config value for the runtime config file.

        Also updates the in-memory Config object immediately so API lookups
        (e.g. source→session_id) take effect without a server restart.
        """
        self._set_config_value(self.workspace / "config.runtime.yaml", key, value)

        # Keep in-memory state in sync — split by dots, update the right field
        keys = key.split(".")
        if keys[0] == "sources" and len(keys) == 2:
            if not isinstance(value, SourceSessionConfig):
                value = SourceSessionConfig(**value) if isinstance(value, dict) else value
            self.sources[keys[1]] = value

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


"""Observer (观察者)：核心组件，在后台线程中运行，负责向操作系统注册监控路径并监听事件，
当文件系统事件发生时，会调用相应的回调函数。它管理着所有已注册的观察者，并在事件发生时，根据事件的类型调用相应的回调函数。"""


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
