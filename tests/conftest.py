from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from app.config import Settings
from app.db.seed import seed
from app.db.session import Database

if TYPE_CHECKING:
    from app.agent.flags import AgentFlags
    from app.agent.state import ConversationState
    from app.tools import ToolContext

FIXED_NOW = datetime(2026, 6, 15, 4, 30, tzinfo=UTC)  # 10:00 IST


def make_settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "app_env": "test",
        "database_url": "sqlite://",
        "llm_provider": "huggingface",
        "hf_token": "",
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


def make_ctx(
    database: Database,
    *,
    state: ConversationState | None = None,
    flags: AgentFlags | None = None,
    now: datetime = FIXED_NOW,
    conversation_id: str = "conv-test",
    retriever: object | None = None,
) -> ToolContext:
    from app.agent.flags import AgentFlags
    from app.agent.state import ConversationState
    from app.agent.tracing import Tracer
    from app.db.models import Conversation
    from app.tools import ToolContext

    with database.transaction() as s:
        if s.get(Conversation, conversation_id) is None:
            s.add(Conversation(id=conversation_id, state={}))
    return ToolContext(
        db=database,
        state=state or ConversationState(),
        conversation_id=conversation_id,
        now=now,
        tracer=Tracer(database, conversation_id),
        flags=flags or AgentFlags.production(),
        retriever=retriever,  # type: ignore[arg-type]
        transcript=[{"role": "user", "content": "hi, I need help with my order"}],
    )


def verified_state(customer_id: int = 1, name: str = "Priya Sharma") -> ConversationState:
    from app.agent.state import ConversationState

    return ConversationState(
        verified_customer_id=customer_id,
        verified_name=name,
        verified_email="priya.sharma@example.in",
    )
