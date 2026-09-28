from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from app.config import Settings
from app.db.seed import seed
from app.db.session import Database

FIXED_NOW = datetime(2026, 6, 15, 4, 30, tzinfo=UTC)  # 10:00 IST


def make_settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "app_env": "test",
        "database_url": "sqlite://",
        "llm_provider": "fake",
        "embedding_backend": "hash",
        "admin_password": "test-admin-pw",
        "seed_on_startup": True,
        "_env_file": None,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture
def db() -> Iterator[Database]:
    database = Database("sqlite://")
    database.create_all()
    seed(database, FIXED_NOW)
    yield database
    database.dispose()
