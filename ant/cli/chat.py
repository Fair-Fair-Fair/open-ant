"""Chat CLI command for interactive sessions with slash commands."""

import asyncio

import typer
from rich.console import Console
from rich.prompt import Confirm, Prompt
from rich.text import Text

from ant.core.agent import Agent
from ant.core.context import SharedContext
from ant.core.events import CliEventSource, ConfirmationRequestEvent, InboundEvent, OutboundEvent
from ant.server import AgentWorker, Worker
from ant.ui.logo import print_logo
from ant.utils.config import Config, ConfigReloader
from ant.utils.def_loader import DefNotFoundError
from ant.utils.logging import setup_logging


class ChatLoop:
    """Interactive chat session with slash commands."""

    def __init__(self, config: Config, agent_id: str | None = None):
        self.config = config
        self.console = Console()
        self.context = SharedContext(config=config, channels=[])
        self.config_reloader = ConfigReloader(config)

        self.workers: list[Worker] = [
            self.context.eventbus,
            AgentWorker(self.context),
        ]

        self.response_queue: asyncio.Queue[OutboundEvent] = asyncio.Queue()
        self.context.eventbus.subscribe(
            OutboundEvent, self.handle_outbound_event
        )
        self.context.eventbus.subscribe(
            ConfirmationRequestEvent, self.handle_confirmation_request
        )

        agent_id = agent_id or config.default_agent
        try:
            self.agent_def = self.context.agent_loader.load(agent_id)
        except DefNotFoundError:
            self._print_agent_not_found(agent_id)
            raise typer.Exit(1)

    def _print_agent_not_found(self, agent_id: str) -> None:
        """Friendly guidance instead of a raw traceback when the agent is missing."""
        agent_dir = self.config.agents_path / agent_id
        self.console.print(
            f"\n[red]✖ Agent '{agent_id}' not found — no {agent_dir / 'AGENT.md'}[/red]"
        )
        try:
            available = sorted(
                a.id for a in self.context.agent_loader.discover_agents()
            )
        except Exception:
            available = []
        if available:
            self.console.print(
                f"[dim]  Available agents: {', '.join(available)}[/dim]"
            )
            self.console.print(
                "[dim]  Fix: set [bold]default_agent[/bold] to one of these in "
                "config.user.yaml,[/dim]"
            )
            self.console.print(
                f"[dim]       or create {agent_dir / 'AGENT.md'}[/dim]"
            )
        else:
            self.console.print(
                f"[dim]  No agents found — run [bold]open-ant init -w "
                f"{self.config.workspace}[/bold] first.[/dim]"
            )

    async def handle_confirmation_request(self, event: ConfirmationRequestEvent) -> None:
        """Prompt the user to approve/deny a high-privilege tool call (HITL)."""
        approved = await asyncio.to_thread(self._ask_confirmation, event)
        if not self.context.confirmation_broker.respond(event.request_id, approved):
            self.console.print("[dim]⏱ Confirmation expired — request already resolved.[/dim]")

    def _ask_confirmation(self, event: ConfirmationRequestEvent) -> bool:
        """Blocking y/N prompt — runs in a worker thread so the broker's
        timeout keeps ticking while the user decides."""
        args_preview = str(event.tool_args or "")
        if len(args_preview) > 100:
            args_preview = args_preview[:100] + "…"
        # NOTE: rich's Prompt doesn't parse inline [markup] — use explicit
        # Text styles instead (plain strings would display the tags raw).
        prompt = Text()
        prompt.append("⚠ ", style="yellow")
        prompt.append(f"Allow {event.tool_name}", style="bold yellow")
        if args_preview:
            prompt.append(" ")
            prompt.append(args_preview, style="dim")
        prompt.append(f" ? (auto-deny in {int(event.timeout)}s)", style="dim")
        try:
            return bool(Confirm.ask(prompt, console=self.console, default=False))
        except (KeyboardInterrupt, EOFError):
            return False

    async def handle_outbound_event(self, event: OutboundEvent) -> None:
        """Handle outbound events by adding to response queue."""
        await self.response_queue.put(event)
        self.context.eventbus.ack(event)

    def get_user_input(self) -> str:
        """Get user input with styled prompt."""
        prompt_text = Text("You", style="cyan")
        user_input = Prompt.ask(prompt_text, console=self.console)
        return user_input.strip()

    def display_agent_response(self, content: str) -> None:
        """Display agent response with styled prefix."""
        prefix = Text(f"{self.agent_def.id}: ", style="green")

        self.console.print(prefix, end="")
        self.console.print(content)

    async def run(self) -> None:
        """Run the interactive chat loop."""
        print_logo(self.console)
        self.console.print(
            Text(
                f"workspace: {self.config.workspace}",
                style="dim",
            )
        )
        self.console.print(
            Text(
                f"agent: {self.agent_def.id}  ·  model: {self.config.llm.model}",
                style="dim",
            )
        )
        if "REPLACE_ME" in (self.config.llm.api_key or ""):
            self.console.print(
                "\n[yellow]⚠ api_key is still the placeholder — edit "
                f"{self.config.workspace / 'config.user.yaml'} first.[/yellow]\n"
            )
        for warning in self.context.sandbox.command.startup_warnings():
            self.console.print(f"\n[yellow]⚠ {warning}[/yellow]\n")
        self.console.print("Type 'quit' or 'exit' to end.\n")

        self.config_reloader.start()

        for worker in self.workers:
            worker.start()

        session_id = (
            Agent(self.agent_def, self.context).new_session(CliEventSource()).session_id
        )

        try:
            while True:
                user_input = await asyncio.to_thread(self.get_user_input)

                if user_input.lower() in ("quit", "exit", "q"):
                    self.console.print("\n[bold yellow]Goodbye![/bold yellow]")
                    break

                if not user_input:
                    continue

                event = InboundEvent(
                    session_id=session_id,
                    source=CliEventSource(),
                    content=user_input,
                )
                await self.context.eventbus.publish(event)

                try:
                    # cmd_response = await self.context.command_registry.dispatch(
                    #     user_input, self.session
                    # )
                    # if cmd_response is not None:
                    #     self.console.print(cmd_response)
                    #     continue
                    response = await asyncio.wait_for(
                        self.response_queue.get(),
                        timeout=60.0
                    )
                    self.display_agent_response(response.content)
                except Exception as e:
                    self.console.print(f"\n[bold red]Error:[/bold red] {e}\n")
                    self.console.print(
                        "[bold red]Error:[/bold red] Could not get a response from the agent.\n"
                    )

        except (KeyboardInterrupt, EOFError):
            self.console.print("\n[bold yellow]Goodbye![/bold yellow]")
        finally:
            for worker in self.workers:
                await worker.stop()
            self.config_reloader.stop()


def chat_command(ctx: typer.Context, agent_id: str | None = None) -> None:
    """Start interactive chat session."""
    config = ctx.obj.get("config")
    setup_logging(config, console_output=False)

    chat_loop = ChatLoop(config, agent_id=agent_id)
    asyncio.run(chat_loop.run())
