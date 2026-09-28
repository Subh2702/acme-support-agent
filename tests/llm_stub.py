"""Scripted LLM test double. Lives in tests/ only: unit tests must not hit the network."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import Any

from app.llm.types import ChatMessage, LLMError, LLMResponse, ToolCall, ToolSchema, Usage


def text(reply: str) -> LLMResponse:
    return LLMResponse(reply, [], "end_turn", Usage(1000, 50), "stub", cost_usd=Decimal("0.001"))


def tool(name: str, call_id: str = "", **arguments: Any) -> LLMResponse:
    call = ToolCall(call_id or f"call_{name}", name, arguments)
    return LLMResponse("", [call], "tool_use", Usage(1000, 30), "stub", cost_usd=Decimal("0.001"))


Step = LLMResponse | LLMError | Callable[[list[ChatMessage]], LLMResponse]


class ScriptedLLM:
    provider = "stub"
    model = "stub"

    def __init__(self, steps: list[Step]) -> None:
        self.steps = list(steps)
        self.calls: list[dict[str, Any]] = []

    def complete(
        self,
        *,
        system: str,
        context: str,
        messages: list[ChatMessage],
        tools: list[ToolSchema],
        max_tokens: int,
    ) -> LLMResponse:
        self.calls.append(
            {
                "system": system,
                "context": context,
                "messages": list(messages),
                "tools": [t.name for t in tools],
            }
        )
        if not self.steps:
            raise AssertionError("ScriptedLLM ran out of steps")
        step = self.steps.pop(0)
        if isinstance(step, LLMError):
            raise step
        if callable(step):
            return step(messages)
        return step
