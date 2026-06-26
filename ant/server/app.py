"""FastAPI application with Websocket support"""
from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware

from ant.core.context import SharedContext


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

    # WebSocket endpoint
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
