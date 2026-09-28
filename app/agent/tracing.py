"""Trace logging: every LLM call, tool call, policy decision and guardrail hit.

Payloads are PII-masked before they reach the database. Events are also kept in
memory for the current turn so the output checks can verify claims against them,
and optionally forwarded to a listener (the SSE stream shows live progress).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.db.models import TraceEvent
from app.db.session import Database
from app.guardrails.pii import mask_structured
from app.logging_setup import get_logger

log = get_logger(__name__)

EventListener = Callable[[str, str, dict[str, Any]], None]


@dataclass
class TraceRecord:
    kind: str
    name: str
    payload: dict[str, Any]
    input_tokens: int = 0
    output_tokens: int = 0
    cost_paise: int = 0
    latency_ms: int = 0


@dataclass
class Tracer:
    db: Database
    conversation_id: str
    turn_index: int = 0
    listener: EventListener | None = None
    events: list[TraceRecord] = field(default_factory=list)

    def record(
        self,
        kind: str,
        name: str,
        payload: dict[str, Any] | None = None,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_paise: int = 0,
        latency_ms: int = 0,
    ) -> None:
        raw = payload or {}
        masked = mask_structured(raw)
        rec = TraceRecord(kind, name, raw, input_tokens, output_tokens, cost_paise, latency_ms)
        self.events.append(rec)
        try:
            with self.db.transaction() as session:
                session.add(
                    TraceEvent(
                        conversation_id=self.conversation_id,
                        turn_index=self.turn_index,
                        kind=kind,
                        name=name,
                        payload=masked,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        cost_paise=cost_paise,
                        latency_ms=latency_ms,
                    )
                )
        except Exception:  # tracing must never break a customer conversation
            log.exception("failed to persist trace event", extra={"kind": kind, "name": name})
        if self.listener is not None:
            try:
                self.listener(kind, name, masked)
            except Exception:
                log.exception("trace listener failed")

    @property
    def total_cost_paise(self) -> int:
        return sum(e.cost_paise for e in self.events)

    def successful_tools(self) -> list[TraceRecord]:
        return [e for e in self.events if e.kind == "tool_call" and e.payload.get("ok")]
