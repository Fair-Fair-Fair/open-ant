from ant.core.agent_loader import AgentLoader
from ant.core.commands.registry import CommandRegistry
from ant.core.history import HistoryStore
from ant.core.skill_loader import SkillLoader
from ant.core.eventbus import EventBus
from ant.utils.config import Config
from typing import Any

from ant.channel.base import Channel

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from ant.server.websocket_worker import WebSocketWorker

from ant.core.routing import RoutingTable


class SharedContext:
    """Global shared state for the application"""

    config: Config
    agent_loader: AgentLoader
    skill_loader: SkillLoader
    command_registry: CommandRegistry
    history_store: HistoryStore
    eventbus: EventBus
    channels: list[Channel[Any]]
    websocket_worker: 'WebSocketWorker | None'

    # 11 multi-agent-routing
    routing_table: RoutingTable

    def __init__(self, config: Config,
                 channels: list[Channel[Any]] | None = None) -> None:
        self.config = config
        self.history_store = HistoryStore.from_config(config)
        self.agent_loader = AgentLoader.from_config(config)
        self.skill_loader = SkillLoader.from_config(config)
        self.command_registry = CommandRegistry.with_builtins()
        self.eventbus = EventBus(self)

        if channels is not None:
            self.channels = channels
        else:
            self.channels = Channel.from_config(config)

        self.websocket_worker = None

        self.routing_table = RoutingTable(self)
