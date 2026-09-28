"""Hugging Face Inference Providers implementation of `LLMClient`.

Talks to the OpenAI-compatible router (https://router.huggingface.co/v1). The router
reports which provider served each call (`x-inference-provider`) and publishes
per-provider USD prices in its model listing, so every call is priced from the
provider's real list price rather than an estimate.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from decimal import Decimal
from typing import Any

import httpx

from app.llm.pricing import Price, cost_usd
from app.llm.types import ChatMessage, LLMError, LLMResponse, ToolCall, ToolSchema, Usage

ROUTER_URL = "https://router.huggingface.co/v1"
PRICE_TTL_S = 6 * 3600


def _to_api_messages(system: str, context: str, messages: list[ChatMessage]) -> list[dict]:
    out: list[dict[str, Any]] = [{"role": "system", "content": f"{system}\n\n{context}"}]
    for msg in messages:
        if msg.role == "tool":
            out.append({"role": "tool", "tool_call_id": msg.tool_call_id, "content": msg.content})
        elif msg.role == "assistant":
            entry: dict[str, Any] = {"role": "assistant", "content": msg.content or ""}
            if msg.tool_calls:
                entry["tool_calls"] = [
                    {
                        "id": c.id,
                        "type": "function",
                        "function": {"name": c.name, "arguments": json.dumps(c.arguments)},
                    }
                    for c in msg.tool_calls
                ]
            out.append(entry)
        else:
            out.append({"role": "user", "content": msg.content})
    return out


def _parse_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "{}")
    except (json.JSONDecodeError, TypeError):
        return {"__unparseable__": str(raw)[:500]}
    return parsed if isinstance(parsed, dict) else {"__unparseable__": str(raw)[:500]}


class HuggingFaceClient:
    provider = "huggingface"

    def __init__(
        self,
        token: str,
        model: str,
        *,
        timeout_s: float = 60.0,
        base_url: str = ROUTER_URL,
        price_override: Price | None = None,
        http: httpx.Client | None = None,
    ) -> None:
        self.model = model
        self._base = base_url.rstrip("/")
        self._http = http or httpx.Client(
            timeout=timeout_s, headers={"Authorization": f"Bearer {token}"}
        )
        self._price_override = price_override
        self._prices: dict[str, Price] = {}
        self._prices_at = 0.0
        self._lock = threading.Lock()

    # --- pricing -----------------------------------------------------------------------
    def _base_model_id(self) -> str:
        return self.model.split(":", 1)[0]

    def _refresh_prices(self) -> None:
        with self._lock:
            if self._prices and time.time() - self._prices_at < PRICE_TTL_S:
                return
            try:
                data = self._http.get(f"{self._base}/models").json().get("data", [])
            except (httpx.HTTPError, ValueError):
                return
            for model in data:
                if model.get("id") != self._base_model_id():
                    continue
                for p in model.get("providers", []):
                    pricing = p.get("pricing")
                    if pricing and p.get("provider"):
                        self._prices[p["provider"]] = Price(
                            Decimal(str(pricing["input"])),
                            Decimal(str(pricing["output"])),
                            cache_read_multiplier=Decimal(1),
                            cache_write_multiplier=Decimal(1),
                        )
            self._prices_at = time.time()

    def price_for(self, provider: str | None) -> Price | None:
        if self._price_override is not None:
            return self._price_override
        self._refresh_prices()
        return self._prices.get(provider or "")

    # --- completion -----------------------------------------------------------------------
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
            "max_tokens": max_tokens,
            "temperature": 0.2,
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
            body["tool_choice"] = "auto"
        try:
            resp = self._http.post(f"{self._base}/chat/completions", json=body)
        except httpx.TimeoutException as exc:
            raise LLMError("timeout", retryable=True, code="timeout") from exc
        except httpx.TransportError as exc:
            raise LLMError(f"connection error: {exc}", retryable=True, code="connection") from exc
        if resp.status_code == 402:
            raise LLMError(
                "Hugging Face credits exhausted", retryable=False, code="quota_exhausted"
            )
        if resp.status_code >= 400:
            retryable = resp.status_code in (408, 409, 429) or resp.status_code >= 500
            raise LLMError(
                f"status {resp.status_code}: {resp.text[:300]}",
                retryable=retryable,
                code=f"http_{resp.status_code}",
            )
        return self._parse(resp.json(), resp.headers.get("x-inference-provider"))

    def _parse(self, data: dict[str, Any], provider: str | None) -> LLMResponse:
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        calls = [
            ToolCall(
                id=tc.get("id") or f"call_{uuid.uuid4().hex[:12]}",
                name=(tc.get("function") or {}).get("name", ""),
                arguments=_parse_arguments((tc.get("function") or {}).get("arguments")),
            )
            for tc in message.get("tool_calls") or []
        ]
        usage_raw = data.get("usage") or {}
        usage = Usage(
            input_tokens=int(usage_raw.get("prompt_tokens") or 0),
            output_tokens=int(usage_raw.get("completion_tokens") or 0),
        )
        finish = choice.get("finish_reason") or "stop"
        stop = "tool_use" if calls else ("max_tokens" if finish == "length" else "end_turn")
        price = self.price_for(provider)
        return LLMResponse(
            text=(message.get("content") or "").strip(),
            tool_calls=calls,
            stop_reason=stop,
            usage=usage,
            model=data.get("model") or self.model,
            raw=None,
            cost_usd=cost_usd(usage, price) if price else None,
            provider=f"huggingface/{provider}" if provider else "huggingface",
        )
