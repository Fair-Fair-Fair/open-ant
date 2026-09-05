"""Chat CLI command for interactive sessions with slash commands."""

import asyncio
import sys

import typer
from rich.console import Console
from rich.prompt import Confirm, Prompt
from rich.text import Text

from ant.cli.voice import VoiceIO, speech_deps_available
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

    def __init__(
        self,
        config: Config,
        agent_id: str | None = None,
        voice_mode: bool | None = None,
    ):
        self.config = config
        self.console = Console()
        self.context = SharedContext(config=config, channels=[])
        self.config_reloader = ConfigReloader(config)
        # True=语音 / False=文字 / None=启动时交互选择（对齐 OpenClaw talk
        # mode 思路）；语音依赖缺失时自动降级文字并提示。
        self.voice_mode = voice_mode
        self.voice_io: VoiceIO | None = None

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
        await self.context.eventbus.ack(event)

    def _resolve_voice_mode(self) -> bool:
        """确定本轮会话的输入模式（文字/语音）。

        ``--voice`` / ``--text`` 显式指定时直接采用；都没给时交互询问
        （非交互终端直接文字）。语音依赖缺失 → 降级文字 + 安装提示。
        """
        if self.voice_mode is False:
            return False

        want_voice = self.voice_mode is True
        if self.voice_mode is None and sys.stdin.isatty():
            choice = Prompt.ask(
                "输入模式  [1] 文字  [2] 语音",
                default="1",
                console=self.console,
            )
            want_voice = choice.strip() in ("2", "语音", "voice", "v")

        if want_voice and speech_deps_available():
            self.voice_io = VoiceIO()
            return True

        if want_voice:
            self.console.print(
                "[yellow]⚠ 语音依赖未安装，已降级为文字模式。安装：[/yellow] "
                "[dim]pip install sounddevice edge-tts faster-whisper miniaudio[/dim]"
            )
        return False

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

        voice_on = self._resolve_voice_mode()
        if voice_on:
            self.console.print(
                "[bold cyan]🎙 语音模式[/bold cyan][dim]：回车开始说话（6 秒自动停止），"
                "说\"再见\"退出；语音失败自动降级文字。[/dim]\n"
            )
        else:
            self.console.print("Type 'quit' or 'exit' to end.\n")

        self.config_reloader.start()

        for worker in self.workers:
            worker.start()

        session_id = (
            await Agent(self.agent_def, self.context).new_session(CliEventSource())
        ).session_id

        try:
            while True:
                if voice_on and self.voice_io is not None:
                    # 语音输入：回车录音 → ASR 转写（失败静默降级为再试一轮）
                    user_input = await self.voice_io.listen()
                    if not user_input:
                        self.console.print("[dim]没有听清，请再说一遍。[/dim]")
                        continue
                    self.console.print(Text(f"你: {user_input}", style="cyan"))
                else:
                    user_input = await asyncio.to_thread(self.get_user_input)

                if user_input.lower() in ("quit", "exit", "q") or (
                    voice_on and "再见" in user_input
                ):
                    if voice_on and self.voice_io is not None:
                        try:
                            await self.voice_io.speak("再见，我在这儿，会替您记着的。")
                        except Exception:
                            pass
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
                    response = await asyncio.wait_for(
                        self.response_queue.get(),
                        timeout=60.0
                    )
                    self.display_agent_response(response.content)
                    if voice_on and self.voice_io is not None:
                        await self.voice_io.speak(response.content)
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


def chat_command(
    ctx: typer.Context,
    agent_id: str | None = None,
    voice_mode: bool | None = None,
) -> None:
    """Start interactive chat session."""
    config = ctx.obj.get("config")
    setup_logging(config, console_output=False)

    chat_loop = ChatLoop(config, agent_id=agent_id, voice_mode=voice_mode)
    asyncio.run(chat_loop.run())
