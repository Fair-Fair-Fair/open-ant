"""Event types and data classes for the event bus."""

import time
from dataclasses import dataclass, field
from typing import Any, ClassVar
from abc import ABC, abstractmethod


class EventSource(ABC):
    """Base class for event sources."""

    _registry: ClassVar[dict[str, type["EventSource"]]] = {}
    _namespace: ClassVar[str] = ""

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if cls._namespace:
            cls._registry[cls._namespace] = cls

    @property
    def is_platform(self) -> bool:
        return self._namespace.startswith("platform-")

    @property
    def is_agent(self) -> bool:
        return self._namespace == "agent"

    @property
    def is_cron(self) -> bool:
        return self._namespace == "cron"

    @property
    def platform_name(self) -> str | None:
        if not self.is_platform:
            return None
        return self._namespace.split("-", 1)[1]

    @classmethod
    def from_string(cls, s: str) -> "EventSource":
        """Parse string to EventSource using namespace registry"""
        namespace = s.split(":")[0]
        source_cls = cls._registry.get(namespace)
        if source_cls is None:
            raise ValueError(f"Unknown event source: {namespace}")
        return source_cls.from_string(s)

    @abstractmethod
    def __str__(self) -> str: ...


@dataclass
class AgentEventSource(EventSource):
    """Source for agent-generated events"""
    _namespace: ClassVar[str] = "agent"
    agent_id: str

    def __str__(self) -> str:
        return f"agent: {self.agent_id}"

    @classmethod
    def from_string(cls, s: str) -> "EventSource":
        _, agent_id = s.split(":", maxsplit=1)
        return cls(agent_id=agent_id)


@dataclass
class CliEventSource(EventSource):
    """Source for CLI-generated events"""
    _namespace: ClassVar[str] = "platform-cli"

    def __str__(self) -> str:
        return "platform-cli:cli-user"

    @classmethod
    def from_string(cls, s: str) -> "CliEventSource":
        return cls()

    @property
    def platform_name(self) -> str | None:
        return "cli"


@dataclass
class WebSocketEventSource(EventSource):
    """Event from websocket client"""

    _namespace = "platform-webSocket"
    user_id: str

    @classmethod
    def from_string(cls, s: str) -> "WebSocketEventSource":
        """Parse source string into WebSocketEventSource"""
        parts = s.split(":", 1)
        if len(parts) != 2 or parts[0] != cls._namespace or not parts[1]:
            raise ValueError(f"Invalid WebSocketEventSource: {s}")
        return cls(user_id=parts[1])

    def __str__(self) -> str:
        return f"{self._namespace}:{self.user_id}"

    @property
    def is_platform(self) -> bool:
        """WebSocket sources are platform sources"""
        return True


@dataclass
class Event:
    """Base class for all typed events."""

    session_id: str
    source: EventSource # Changed from str to typed EventSource
    content: str
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        """Serialize event to dictionary, including type."""
        result: dict[str, Any] = {"type": self.__class__.__name__}
        for field_name in self.__dataclass_fields__:
            value = getattr(self, field_name)
            if field_name == "source":
                result[field_name] = str(value)
            else:
                result[field_name] = value

        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Event":
        """Deserialize event from dictionary."""
        kwargs = {}
        for k, v in data.items():
            if k == "type":
                continue
            if k == "source":
                kwargs[k] = EventSource.from_string(v)
            elif k in cls.__dataclass_fields__:
                kwargs[k] = v
        return cls(**kwargs)


@dataclass
class InboundEvent(Event):
    """Event for external work entering the system (platforms, cron, retry)."""

    retry_count: int = 0


@dataclass
class OutboundEvent(Event):
    """Event for agent responses to deliver to platforms."""

    error: str | None = None


# Registry mapping event class names to event classes
_EVENT_CLASSES: dict[str, type[Event]] = {
    "InboundEvent": InboundEvent,
    "OutboundEvent": OutboundEvent,
}


def serialize_event(event: Event) -> dict[str, Any]:
    """Serialize any event type to dict."""
    return event.to_dict()


def deserialize_event(data: dict[str, Any]) -> Event:
    """Deserialize dict to appropriate event type."""
    event_type: str = data.get("type", "")

    event_class = _EVENT_CLASSES.get(event_type)
    if event_class is None:
        raise ValueError(f"Unknown event type: {event_type}")

    return event_class.from_dict(data)
