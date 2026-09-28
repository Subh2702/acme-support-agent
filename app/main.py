"""FastAPI application: routes, static files, startup (tables, seed, KB index)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import health
from app.config import Settings, get_settings
from app.logging_setup import configure_logging, get_logger
from app.services import Services, build_services

log = get_logger(__name__)


def create_app(settings: Settings | None = None, services: Services | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(settings.log_level)
        svc = services or build_services(settings)
        svc.startup()
        app.state.services = svc
        log.info("startup complete", extra={"llm_provider": svc.llm_provider_name})
        yield
        svc.shutdown()

    app = FastAPI(title="Acme Outdoors Support Agent", version="1.0.0", lifespan=lifespan)
    app.include_router(health.router)
    return app


app = create_app()
