"""FastAPI application with Websocket support"""
from pathlib import Path

from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse

from ant.core.context import SharedContext

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

    # Websocket endpoint
    @app.websocket("/web_socket")
    async def websocket_endpoint(web_socket: WebSocket):
        """WebSocket endpoint for real-time event streaming and chat"""
        await web_socket.accept()

        # Check if websocket worker is available
        if context.websocket_worker is None:
            await web_socket.close(code=1013,
                                   reason="WebSocket not available")
            return

        # Hand off to worker
        await context.websocket_worker.handle_connection(web_socket)

    return app
