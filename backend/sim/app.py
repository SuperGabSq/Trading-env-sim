"""FastAPI application: REST API, WebSocket and the web UI."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .config import settings
from .data.store import store
from .hub import hub
from .session import manager

log = logging.getLogger("tsim")

LOOPBACK = {"127.0.0.1", "::1", "localhost", "testclient"}


def ensure_sample_data() -> None:
    if not store().symbols():
        from .data.synthetic import generate_into_store
        log.warning("No market data found - generating 1 year of synthetic MES/MNQ sample data...")
        generate_into_store(["MES", "MNQ"], days=365)


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_sample_data()
    from .api import live as live_api
    await live_api.restore_on_startup()
    yield
    if manager.current:
        await manager.stop()


def create_app() -> FastAPI:
    app = FastAPI(title="Trading Env Sim", version=__version__, lifespan=lifespan)

    @app.middleware("http")
    async def api_key_guard(request: Request, call_next):
        # Local clients are trusted; remote clients need X-API-Key when TSIM_API_KEY is set.
        if settings.api_key and request.url.path.startswith("/api"):
            host = request.client.host if request.client else ""
            if host not in LOOPBACK and request.headers.get("x-api-key") != settings.api_key:
                return JSONResponse({"detail": "invalid or missing X-API-Key"}, status_code=401)
        return await call_next(request)

    from .api import backtest, bots, data, live, trading
    app.include_router(data.router)
    app.include_router(trading.router)
    app.include_router(backtest.router)
    app.include_router(live.router)
    app.include_router(bots.router)

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        await hub.handle(websocket)

    @app.websocket("/ws/bot")
    async def ws_bot(websocket: WebSocket):
        await bots.handle_bot_ws(websocket)

    dist = settings.frontend_dist
    if (dist / "index.html").exists():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        async def spa(path: str):
            f = dist / path
            if path and f.is_file() and dist in f.resolve().parents:
                return FileResponse(f)
            return FileResponse(dist / "index.html")
    else:
        @app.get("/", include_in_schema=False)
        async def no_ui():
            return HTMLResponse("<h2>Trading Env Sim API is running</h2><p>The web UI is not built. "
                                "Run <code>npm install</code> and <code>npm run build</code> in "
                                "<code>frontend/</code>, or open <a href='/docs'>/docs</a>.</p>")
    return app


app = create_app()
