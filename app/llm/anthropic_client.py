"""Anthropic implementation of `LLMClient` (Messages API, manual tool loop driven by the agent)."""

from __future__ import annotations

import json
from typing import Any

import anthropic

from app.llm.pricing import ANTHROPIC_PRICES, Price, cost_usd
from app.llm.types import ChatMessage, LLMError, LLMResponse, ToolCall, ToolSchema, Usage

# Server-side refusal fallback (re-runs a declined request on a fallback model).
FALLBACK_BETA = "server-side-fallback-2026-07-01"
FALLBACK_MODELS = {"claude-opus-5", "claude-opus-5-5", "claude-fable-5", "claude-fable-5-1"}
NO_EFFORT_MODELS = {"claude-haiku-4-5"}


def _to_api_messages(messages: list[ChatMessage]) -> list[dict[str, Any]]:
    """Convert neutral messages; consecutive tool results become one user message."""
    out: list[dict[str, Any]] = []
    pending_results: list[dict[str, Any]] = []

    def flush() -> None:
        if pending_results:
            out.append({"role": "user", "content": list(pending_results)})
            pending_results.clear()

    for msg in messages:
        if msg.role == "tool":
            pending_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": msg.tool_call_id,
                    "content": msg.content,
                    "is_error": msg.is_error,
                }
            )
            continue
        flush()
        if msg.role == "assistant":
            if msg.raw is not None:
                out.append({"role": "assistant", "content": msg.raw})
            else:
                blocks: list[dict[str, Any]] = []
                if msg.content:
                    blocks.append({"type": "text", "text": msg.content})
                blocks.extend(
                    {"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments}
                    for c in msg.tool_calls
                )
                out.append({"role": "assistant", "content": blocks or msg.content})
        else:
            out.append({"role": "user", "content": msg.content})
    flush()
    return out


class AnthropicClient:
    provider = "anthropic"

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        effort: str = "medium",
        timeout_s: float = 60.0,
        price: Price | None = None,
    ) -> None:
        # Retries are owned by the agent loop (with tracing), so the SDK does not retry.
        self._client = anthropic.Anthropic(api_key=api_key, timeout=timeout_s, max_retries=0)
        self.model = model
        self.effort = effort
        self.price = price or ANTHROPIC_PRICES.get(model)

    def complete(
        self,
        *,
        system: str,
        context: str,
        messages: list[ChatMessage],
        tools: list[ToolSchema],
        max_tokens: int,
    ) -> LLMResponse:
        tool_defs: list[dict[str, Any]] = [
            {"name": t.name, "description": t.description, "input_schema": t.input_schema}
            for t in tools
        ]
        if tool_defs:
            tool_defs[-1]["cache_control"] = {"type": "ephemeral"}
        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            # Static prompt is cached; the per-turn context sits after the breakpoint.
            "system": [
                {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": context},
            ],
            "messages": _to_api_messages(messages),
            "tools": tool_defs,
        }
        if self.model not in NO_EFFORT_MODELS:
            params["output_config"] = {"effort": self.effort}
        if self.model in FALLBACK_MODELS:
            params["betas"] = [FALLBACK_BETA]
            params["fallbacks"] = "default"
        try:
            if "betas" in params:
                response = self._client.beta.messages.create(**params)
            else:
                response = self._client.messages.create(**params)
        except anthropic.RateLimitError as exc:
            raise LLMError(f"rate limited: {exc}", retryable=True, code="rate_limited") from exc
        except anthropic.APITimeoutError as exc:
            raise LLMError("timeout", retryable=True, code="timeout") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError("connection error", retryable=True, code="connection") from exc
        except anthropic.APIStatusError as exc:
            retryable = exc.status_code >= 500 or exc.status_code in (408, 409, 529)
            raise LLMError(
                f"status {exc.status_code}: {exc.message}",
                retryable=retryable,
                code=f"http_{exc.status_code}",
            ) from exc
        return self._parse(response)

    def _parse(self, response: Any) -> LLMResponse:
        usage = Usage(
            input_tokens=response.usage.input_tokens or 0,
            output_tokens=response.usage.output_tokens or 0,
            cache_read_tokens=getattr(response.usage, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(response.usage, "cache_creation_input_tokens", 0) or 0,
        )
        if response.stop_reason == "refusal":
            return LLMResponse(
                "",
                [],
                "refusal",
                usage,
                response.model,
                raw=None,
                cost_usd=self._cost(usage),
                provider=self.provider,
            )
        texts, calls = [], []
        for block in response.content:
            if block.type == "text":
                texts.append(block.text)
            elif block.type == "tool_use":
                args = block.input if isinstance(block.input, dict) else json.loads(block.input)
                calls.append(ToolCall(block.id, block.name, args))
        stop = "tool_use" if calls else (response.stop_reason or "end_turn")
        return LLMResponse(
            text="\n".join(t for t in texts if t).strip(),
            tool_calls=calls,
            stop_reason=stop,
            usage=usage,
            model=response.model,
            raw=response.content,  # replayed verbatim (keeps thinking blocks valid)
            cost_usd=self._cost(usage),
            provider=self.provider,
        )

    def _cost(self, usage: Usage) -> Any:
        return cost_usd(usage, self.price) if self.price else None
