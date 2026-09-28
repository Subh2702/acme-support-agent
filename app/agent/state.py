"""Per-conversation state, persisted as JSON on the conversation row."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

ActionOutcome = Literal["done", "pending_approval", "denied", "failed"]


class ActionRecord(BaseModel):
    tool: str
    order_number: str | None = None
    outcome: ActionOutcome
    reason_code: str
    detail: str = ""
    amount_paise: int | None = None
    ticket_id: int | None = None


class ConversationState(BaseModel):
    verified_customer_id: int | None = None
    verified_name: str | None = None
    verified_email: str | None = None
    failed_verifications: int = 0
    verification_locked: bool = False
    action_count: int = 0
    actions: list[ActionRecord] = Field(default_factory=list)
    escalated: bool = False
    ticket_ids: list[int] = Field(default_factory=list)
    turn_count: int = 0
    negative_turns: int = 0
    unresolved_turns: int = 0
    language: Literal["en", "hinglish"] = "en"
    last_intent: str | None = None

    @property
    def is_verified(self) -> bool:
        return self.verified_customer_id is not None

    def record_action(self, record: ActionRecord) -> None:
        self.actions.append(record)

    @property
    def actions_done(self) -> list[ActionRecord]:
        return [a for a in self.actions if a.outcome in ("done", "pending_approval")]

    @property
    def actions_denied(self) -> list[ActionRecord]:
        return [a for a in self.actions if a.outcome in ("denied", "failed")]
