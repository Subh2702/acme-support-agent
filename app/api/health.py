"""Liveness and readiness probes."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()


@router.get("/healthz")
def healthz() -> dict[str, str]:
    """Liveness: the process is up. Never touches dependencies."""
    return {"status": "ok"}


@router.get("/readyz")
def readyz(request: Request) -> JSONResponse:
    """Readiness: database reachable and knowledge-base index built."""
    services = request.app.state.services
    checks: dict[str, str] = {}
    try:
        services.db.ping()
        checks["database"] = "ok"
    except Exception as exc:  # readiness must report, not raise
        checks["database"] = f"error: {type(exc).__name__}"
    checks["knowledge_base"] = "ok" if services.kb_ready else "building"
    ready = all(v == "ok" for v in checks.values())
    return JSONResponse(
        {"status": "ready" if ready else "not_ready", "checks": checks},
        status_code=200 if ready else 503,
    )
