"""Local small-language-model implementation of `LLMClient` via Ollama's native chat API.

The native API (not the OpenAI shim) is used because it lets us switch off
"thinking" for hybrid-reasoning models and set the context window explicitly;
Ollama's default context is too small for the system prompt + tool schemas.
Local inference has no per-token bill, so calls are priced at ₹0.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import httpx

from app.llm.types import ChatMessage, LLMError, LLMResponse, ToolCall, ToolSchema, Usage


def _to_api_messages(system: str, context: str, messages: list[ChatMessage]) -> list[dict]:
    # Static prompt first (cacheable with the tool block the template renders after it);
    # the per-turn context goes last so it never invalidates the prompt-prefix cache.
    out: list[dict[str, Any]] = [{"role": "system", "content": system}]
    names_by_id: dict[str, str] = {}
    for msg in messages:
        if msg.role == "assistant":
            entry: dict[str, Any] = {"role": "assistant", "content": msg.content or ""}
            if msg.tool_calls:
                entry["tool_calls"] = [
                    {"function": {"name": c.name, "arguments": c.arguments}} for c in msg.tool_calls
                ]
                names_by_id.update({c.id: c.name for c in msg.tool_calls})
            out.append(entry)
        elif msg.role == "tool":
            out.append(
                {
                    "role": "tool",
                    "content": msg.content,
                    "tool_name": names_by_id.get(msg.tool_call_id or "", ""),
                }
            )
        else:
            out.append({"role": "user", "content": msg.content})
    if context:
        out.append({"role": "system", "content": context})
    return out


class OllamaClient:
    provider = "ollama"

    def __init__(
        self,
        model: str,
        *,
        base_url: str = "http://localhost:11434",
        timeout_s: float = 180.0,
        num_ctx: int = 8192,
        http: httpx.Client | None = None,
    ) -> None:
        self.model = model
        self._base = base_url.rstrip("/")
        self._num_ctx = num_ctx
        self._http = http or httpx.Client(timeout=timeout_s)

    def complete(
        self,
        *,
        system: str,
        context: str,
        messages: list[ChatMessage],
        tools: list[ToolSchema],
        max_tokens: int,
    ) -> LLMResponse:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": _to_api_messages(system, context, messages),
            "stream": False,
            "think": False,
            "keep_alive": "30m",
            "options": {"temperature": 0.2, "num_ctx": self._num_ctx, "num_predict": max_tokens},
        }
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.input_schema,
                    },
                }
                for t in tools
            ]
        try:
            resp = self._http.post(f"{self._base}/api/chat", json=body)
        except httpx.TimeoutException as exc:
            raise LLMError("timeout", retryable=True, code="timeout") from exc
        except httpx.TransportError as exc:
            raise LLMError(
                f"Ollama unreachable at {self._base}: {exc}", retryable=True, code="connection"
            ) from exc
        if resp.status_code == 404:
            raise LLMError(
                f"model {self.model} not pulled in Ollama", retryable=False, code="model_missing"
            )
        if resp.status_code >= 400:
            raise LLMError(
                f"status {resp.status_code}: {resp.text[:300]}",
                retryable=resp.status_code >= 500,
                code=f"http_{resp.status_code}",
            )
        return self._parse(resp.json())

    def _parse(self, data: dict[str, Any]) -> LLMResponse:
        message = data.get("message") or {}
        calls = []
        for tc in message.get("tool_calls") or []:
            fn = tc.get("function") or {}
            args = fn.get("arguments")
            if not isinstance(args, dict):
                args = {"__unparseable__": str(args)[:500]}
            calls.append(
                ToolCall(tc.get("id") or f"call_{uuid.uuid4().hex[:12]}", fn.get("name", ""), args)
            )
        usage = Usage(
            input_tokens=int(data.get("prompt_eval_count") or 0),
            output_tokens=int(data.get("eval_count") or 0),
        )
        done = data.get("done_reason") or "stop"
        stop = "tool_use" if calls else ("max_tokens" if done == "length" else "end_turn")
        return LLMResponse(
            text=(message.get("content") or "").strip(),
            tool_calls=calls,
            stop_reason=stop,
            usage=usage,
            model=data.get("model") or self.model,
            cost_usd=Decimal(0),
            provider="ollama",
        )
