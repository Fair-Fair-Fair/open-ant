"""FastAPI application with Websocket support"""
import re
from pathlib import Path

from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse

from ant.core.context import SharedContext

_URL_RE = re.compile(r"https?://[^\s)\]]+")

_STATIC_DIR = Path(__file__).parent / "static"


def create_app(context: SharedContext) -> FastAPI:
    """Create and configure FastAPI application"""
    app = FastAPI(
        title="Ant WebSocket Server",
        description="WebSocket server for real-time agent communication",
        version="0.1.0"
    )
    app.state.context = context

    # Enable CORS for web clients
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/", response_class=HTMLResponse)
    async def serve_web_ui():
        """Serve the web chat interface"""
        html_path = _STATIC_DIR / "index.html"
        if not html_path.exists():
            return HTMLResponse("<h1>Web UI not found</h1>", status_code=404)
        return HTMLResponse(html_path.read_text(encoding="utf-8"))

    @app.get("/api/agents")
    async def list_agents():
        """List all available agents"""
        try:
            agents = context.agent_loader.discover_agents()
            return JSONResponse([
                {
                    "id": a.id,
                    "name": a.name,
                    "description": a.description,
                }
                for a in agents
            ])
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.get("/api/config")
    async def get_config():
        """Get public config for the web UI"""
        return JSONResponse({
            "default_agent": context.config.default_agent,
        })

    @app.get("/api/sessions/messages")
    async def get_session_messages(source: str = ""):
        """Return historical messages for a given source (e.g. platform-webSocket:web-xxx).

        The frontend calls this on page load to restore the conversation UI
        after a refresh. Tool results include a resolved ``tool_name`` so the
        frontend can render them as collapsible source cards instead of raw text.
        """
        if not source:
            return JSONResponse({"error": "source query parameter is required"}, status_code=400)

        # Look up session_id from the runtime source→session cache
        source_session = context.config.sources.get(source)
        if not source_session:
            return JSONResponse({"messages": []})

        session_id = source_session.session_id
        history_msgs = context.history_store.get_messages(session_id)

        # Build a tool_call_id → name mapping from assistant messages
        tool_names: dict[str, str] = {}
        for m in history_msgs:
            if m.role == "assistant" and m.tool_calls:
                for tc in m.tool_calls:
                    tc_id = tc.get("id", "")
                    tc_name = tc.get("function", {}).get("name", "unknown")
                    if tc_id:
                        tool_names[tc_id] = tc_name

        enriched = []
        for m in history_msgs:
            entry: dict = {"role": m.role, "content": m.content}
            if m.role == "tool" and m.tool_call_id:
                entry["tool_call_id"] = m.tool_call_id
                entry["tool_name"] = tool_names.get(m.tool_call_id, "unknown")
                # Extract URLs for source link display
                urls = _URL_RE.findall(m.content)
                entry["urls"] = urls[:3]  # top 3 URLs at most
                # First line preview (title) for the card header
                first_line = m.content.split("\n")[0].strip() if m.content else ""
                preview = first_line[:100] if first_line else m.content[:100]
                entry["preview"] = preview + ("…" if len(m.content) > 100 else "")
            if m.role == "assistant" and m.tool_calls:
                entry["tool_calls"] = [
                    {"id": tc.get("id"), "name": tc.get("function", {}).get("name", "")}
                    for tc in m.tool_calls if tc.get("id")
                ]
            enriched.append(entry)

        return JSONResponse({
            "session_id": session_id,
            "messages": enriched,
        })

    # Websocket endpoint
    @app.websocket("/web_socket")
    async def websocket_endpoint(web_socket: WebSocket):
        """WebSocket endpoint for real-time event streaming and chat
        正式完成 WebSocket 的握手（HTTP 状态码 101 Switching Protocols）"""
        await web_socket.accept()

        # Check if websocket worker is available
        if context.websocket_worker is None:
            await web_socket.close(code=1013,
                                   reason="WebSocket not available")
            return

        # Hand off to worker
        await context.websocket_worker.handle_connection(web_socket)

    return app
