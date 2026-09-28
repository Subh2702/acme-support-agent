"""FastAPI application: routes, static files, startup (tables, seed, KB index, LLM)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api import admin, chat, health
from app.config import ROOT_DIR, Settings, get_settings
from app.logging_setup import configure_logging, get_logger
from app.services import Services, build_services

log = get_logger(__name__)
WEB_DIR = ROOT_DIR / "app" / "web"

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' https://fonts.googleapis.com; "
    "font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)


def create_app(settings: Settings | None = None, services: Services | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(settings.log_level)
        svc = services or build_services(settings)
        svc.startup()
        app.state.services = svc
        log.info("startup complete", extra={"llm": svc.llm_provider_name})
        yield
        svc.shutdown()

    app = FastAPI(
        title="Acme Outdoors Support Agent",
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
    )

    @app.middleware("http")
    async def security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        if not request.url.path.startswith("/api/docs"):  # Swagger UI loads from a CDN
            response.headers.setdefault("Content-Security-Policy", CSP)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Frame-Options", "DENY")
        return response

    app.include_router(health.router)
    app.include_router(chat.router)
    app.include_router(admin.router)
    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(WEB_DIR / "chat.html")

    return app


app = create_app()
