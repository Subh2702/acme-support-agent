"""Service container: everything a request handler needs, built once at startup."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.config import Settings
from app.db.seed import seed
from app.db.session import Database
from app.logging_setup import get_logger

log = get_logger(__name__)


@dataclass
class Services:
    settings: Settings
    db: Database
    kb_ready: bool = False
    llm_provider_name: str = "none"
    extras: dict[str, object] = field(default_factory=dict)

    def startup(self) -> None:
        self.db.create_all()
        if self.settings.seed_on_startup and seed(self.db):
            log.info("database seeded with demo data")
        self.kb_ready = True

    def shutdown(self) -> None:
        self.db.dispose()


def build_services(settings: Settings) -> Services:
    return Services(settings=settings, db=Database(settings.database_url))
