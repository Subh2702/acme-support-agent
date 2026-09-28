"""Provider-neutral LLM types and the `LLMClient` interface."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

Role = Literal["user", "assistant", "tool"]


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ChatMessage:
    role: Role
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None  # for role == "tool"
    is_error: bool = False  # for role == "tool"
    raw: Any = None  # provider-native assistant content, replayed verbatim within a turn


@dataclass(frozen=True)
class ToolSchema:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass
class LLMResponse:
    text: str
    tool_calls: list[ToolCall]
    stop_reason: str  # end_turn | tool_use | max_tokens | refusal
    usage: Usage
    model: str
    raw: Any = None
    cost_usd: Any = None  # Decimal, when the client can price the call itself
    provider: str | None = None


class LLMError(Exception):
    """A provider error. `retryable` drives the loop's backoff."""

    def __init__(self, message: str, *, retryable: bool, code: str = "llm_error") -> None:
        super().__init__(message)
        self.retryable = retryable
        self.code = code


class LLMClient(Protocol):
    provider: str
    model: str

    def complete(
        self,
        *,
        system: str,
        context: str,
        messages: list[ChatMessage],
        tools: list[ToolSchema],
        max_tokens: int,
    ) -> LLMResponse:
        """One model call.

        `system` is the static, cacheable instruction block; `context` is the small
        per-turn block (identity state, notes) that follows it.
        """
        ...
