"""LLM client adapters, tested offline against mocked HTTP transports."""

import json
from decimal import Decimal

import httpx
import pytest

from app.llm import build_llm
from app.llm.anthropic_client import _to_api_messages as anthropic_messages
from app.llm.hf_client import HuggingFaceClient
from app.llm.ollama_client import OllamaClient
from app.llm.pricing import ANTHROPIC_PRICES, Price, cost_paise, cost_usd
from app.llm.types import ChatMessage, LLMError, ToolCall, ToolSchema, Usage
from tests.conftest import make_settings

TOOLS = [ToolSchema("get_order", "Get an order", {"type": "object", "properties": {}})]
HISTORY = [
    ChatMessage("user", "where is AO-10001?"),
    ChatMessage(
        "assistant", "", tool_calls=[ToolCall("c1", "get_order", {"order_number": "AO-10001"})]
    ),
    ChatMessage("tool", '{"ok": true}', tool_call_id="c1"),
]


def _client(handler, cls, **kw):
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return cls(http=http, **kw)


def test_ollama_request_and_tool_call_parsing() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": "qwen3:4b-instruct",
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "function": {
                                "name": "get_order",
                                "arguments": {"order_number": "AO-10001"},
                            }
                        }
                    ],
                },
                "done_reason": "stop",
                "prompt_eval_count": 1800,
                "eval_count": 20,
            },
        )

    client = _client(handler, OllamaClient, model="qwen3:4b-instruct", num_ctx=8192)
    resp = client.complete(
        system="SYS", context="CTX", messages=HISTORY, tools=TOOLS, max_tokens=300
    )
    assert resp.stop_reason == "tool_use"
    assert resp.tool_calls[0].arguments == {"order_number": "AO-10001"}
    assert resp.cost_usd == Decimal(0) and resp.usage.input_tokens == 1800
    assert seen["options"]["num_ctx"] == 8192 and seen["think"] is False
    assert seen["messages"][0] == {"role": "system", "content": "SYS"}
    assert seen["messages"][-1] == {"role": "system", "content": "CTX"}  # keeps prefix cacheable
    assert seen["messages"][-2]["tool_name"] == "get_order"


def test_ollama_errors() -> None:
    missing = _client(
        lambda r: httpx.Response(404, text="model not found"), OllamaClient, model="x"
    )
    with pytest.raises(LLMError) as exc:
        missing.complete(system="", context="", messages=HISTORY, tools=[], max_tokens=5)
    assert exc.value.code == "model_missing" and not exc.value.retryable

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(LLMError) as exc:
        _client(down, OllamaClient, model="x").complete(
            system="", context="", messages=HISTORY, tools=[], max_tokens=5
        )
    assert exc.value.retryable


def test_hf_prices_calls_from_router_listing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "Qwen/Qwen3-235B-A22B-Instruct-2507",
                            "providers": [
                                {
                                    "provider": "deepinfra",
                                    "pricing": {"input": 0.09, "output": 0.55},
                                }
                            ],
                        }
                    ]
                },
            )
        body = json.loads(request.content)
        assert body["tool_choice"] == "auto" and body["tools"][0]["function"]["name"] == "get_order"
        return httpx.Response(
            200,
            headers={"x-inference-provider": "deepinfra"},
            json={
                "model": "qwen3-235b",
                "choices": [{"message": {"content": "Hi!"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000},
            },
        )

    client = _client(
        handler, HuggingFaceClient, token="t", model="Qwen/Qwen3-235B-A22B-Instruct-2507:cheapest"
    )
    resp = client.complete(system="S", context="C", messages=HISTORY, tools=TOOLS, max_tokens=5)
    assert resp.text == "Hi!" and resp.provider == "huggingface/deepinfra"
    assert resp.cost_usd == Decimal("0.64")


def test_hf_quota_exhausted_is_not_retryable() -> None:
    client = _client(
        lambda r: httpx.Response(402, json={"error": "credits"}),
        HuggingFaceClient,
        token="t",
        model="m",
    )
    with pytest.raises(LLMError) as exc:
        client.complete(system="", context="", messages=HISTORY, tools=[], max_tokens=5)
    assert exc.value.code == "quota_exhausted" and not exc.value.retryable


def test_hf_bad_tool_arguments_become_structured() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": []})
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "x",
                                    "function": {"name": "get_order", "arguments": "{not json"},
                                }
                            ],
                        }
                    }
                ]
            },
        )

    resp = _client(handler, HuggingFaceClient, token="t", model="m").complete(
        system="", context="", messages=HISTORY, tools=TOOLS, max_tokens=5
    )
    assert "__unparseable__" in resp.tool_calls[0].arguments and resp.cost_usd is None


def test_anthropic_groups_tool_results_into_one_user_message() -> None:
    msgs = [
        *HISTORY[:2],
        ChatMessage("tool", "a", tool_call_id="c1"),
        ChatMessage("tool", "b", tool_call_id="c2", is_error=True),
    ]
    out = anthropic_messages(msgs)
    assert [m["role"] for m in out] == ["user", "assistant", "user"]
    assert [b["tool_use_id"] for b in out[-1]["content"]] == ["c1", "c2"]
    assert out[-1]["content"][1]["is_error"] is True
    assert out[1]["content"][0]["type"] == "tool_use"


def test_pricing_in_paise() -> None:
    usage = Usage(input_tokens=2000, output_tokens=300, cache_read_tokens=10_000)
    usd = cost_usd(usage, ANTHROPIC_PRICES["claude-opus-5"])
    # 2000*5 + 10000*5*0.1 + 300*25 = 22500 -> $0.0225 -> ₹1.9125 -> 192 paise (rounded up)
    assert usd == Decimal("0.0225") and cost_paise(usd, 85) == 192
    assert cost_usd(usage, Price(Decimal(0), Decimal(0))) == 0


def test_factory_selects_provider() -> None:
    assert build_llm(make_settings(llm_provider="ollama")).provider == "ollama"
    assert build_llm(make_settings(llm_provider="huggingface", hf_token="t")).provider == (
        "huggingface"
    )
    assert build_llm(make_settings(llm_provider="anthropic", llm_api_key="k")).provider == (
        "anthropic"
    )
    with pytest.raises(LLMError):
        build_llm(make_settings(llm_provider="huggingface", hf_token=""))
    assert make_settings(llm_provider="auto", hf_token="").resolved_llm_provider == "ollama"
