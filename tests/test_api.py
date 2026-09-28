"""API integration tests: chat (JSON + SSE), rate limits, admin auth and endpoints."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.db.session import Database
from app.main import create_app
from app.services import Services
from tests.conftest import make_settings
from tests.llm_stub import ScriptedLLM, text, tool

AUTH = ("admin", "test-admin-pw")
PRIYA = {"email": "priya.sharma@example.in", "order_number": "AO-10001", "pin_code": "560034"}


def make_client(steps, **setting_overrides) -> tuple[TestClient, ScriptedLLM]:
    settings = make_settings(**setting_overrides)
    llm = ScriptedLLM(steps)
    services = Services(settings=settings, db=Database("sqlite://"), llm=llm)
    return TestClient(create_app(settings, services=services)), llm


@pytest.fixture
def client() -> Iterator[TestClient]:
    c, _ = make_client(
        [
            tool("verify_customer", **PRIYA),
            text("Thanks Priya, you're verified. My number is +91 98450 12345."),
            text("AO-10001 was delivered."),
            text("Hello again!"),
        ]
    )
    with c:
        yield c


def test_index_and_static_served_with_security_headers(client: TestClient) -> None:
    res = client.get("/")
    assert res.status_code == 200 and "Acme Outdoors Support" in res.text
    assert "default-src 'self'" in res.headers["content-security-policy"]
    assert res.headers["x-frame-options"] == "DENY"
    assert client.get("/static/css/app.css").status_code == 200
    assert client.get("/static/js/chat.js").status_code == 200


def test_chat_creates_and_continues_session(client: TestClient) -> None:
    first = client.post("/api/chat", json={"message": "priya.sharma@example.in AO-10001 560034"})
    assert first.status_code == 200
    body = first.json()
    assert body["status"] == "ok" and len(body["session_id"]) == 32
    second = client.post(
        "/api/chat", json={"session_id": body["session_id"], "message": "where is AO-10001?"}
    )
    assert second.json()["turn"] == 2
    hist = client.get(f"/api/chat/{body['session_id']}/history").json()
    assert [m["role"] for m in hist["messages"]] == ["user", "assistant", "user", "assistant"]


@pytest.mark.parametrize(
    ("payload", "status"),
    [
        ({"message": ""}, 422),
        ({"message": "   "}, 422),
        ({"message": "x" * 2001}, 413),
        ({"message": "hi", "session_id": "bad id!"}, 422),
        ({}, 422),
    ],
)
def test_chat_validation(client: TestClient, payload: dict, status: int) -> None:
    assert client.post("/api/chat", json=payload).status_code == status


def test_history_rejects_bad_session(client: TestClient) -> None:
    assert client.get("/api/chat/bad!id/history").status_code in (404, 422)


def test_session_rate_limit() -> None:
    c, _ = make_client([text("a"), text("b"), text("c")], rate_limit_session_per_min=2)
    with c:
        sid = "session-rate-test"
        for _ in range(2):
            assert c.post("/api/chat", json={"session_id": sid, "message": "hi"}).status_code == 200
        res = c.post("/api/chat", json={"session_id": sid, "message": "hi"})
        assert res.status_code == 429 and int(res.headers["retry-after"]) >= 1
        assert "conversation" in res.json()["detail"]


def test_ip_rate_limit_uses_forwarded_for() -> None:
    c, _ = make_client([text("a")] * 3, rate_limit_ip_per_min=1)
    with c:
        headers = {"x-forwarded-for": "203.0.113.7, 10.0.0.1"}
        assert c.post("/api/chat", json={"message": "hi"}, headers=headers).status_code == 200
        assert c.post("/api/chat", json={"message": "hi"}, headers=headers).status_code == 429
        other = {"x-forwarded-for": "198.51.100.1"}
        assert c.post("/api/chat", json={"message": "hi"}, headers=other).status_code == 200


def test_sse_stream_emits_progress_then_reply(client: TestClient) -> None:
    with client.stream(
        "POST",
        "/api/chat/stream",
        json={"message": "verify me: priya.sharma@example.in AO-10001 560034"},
    ) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        body = "".join(r.iter_text())
    events = [
        line.removeprefix("event: ") for line in body.splitlines() if line.startswith("event: ")
    ]
    assert events[0] == "session" and events[-2:] == ["reply", "done"]
    assert "progress" in events
    assert "Verifying your details" in body


def test_budget_cap_returns_demo_limit() -> None:
    c, llm = make_client([], daily_budget_inr=0)
    with c:
        body = c.post("/api/chat", json={"message": "hi"}).json()
        assert body["status"] == "demo_limit" and not llm.calls


def test_demo_info(client: TestClient) -> None:
    info = client.get("/api/demo-info").json()
    assert len(info["credentials"]) == 3 and info["sample_prompts"]
    assert info["model"] == "stub:stub" and info["llm_ready"]


def test_health_and_ready(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").json()["checks"] == {"database": "ok", "knowledge_base": "ok"}


# --- admin -----------------------------------------------------------------------------


def test_admin_requires_auth(client: TestClient) -> None:
    assert client.get("/admin").status_code == 401
    assert client.get("/api/admin/stats").status_code == 401
    assert client.get("/api/admin/stats", auth=("admin", "wrong")).status_code == 401
    assert client.get("/api/admin/stats", auth=("root", "test-admin-pw")).status_code == 401
    assert client.get("/admin", auth=AUTH).status_code == 200


def test_admin_disabled_without_password() -> None:
    c, _ = make_client([], admin_password="")
    with c:
        assert c.get("/api/admin/stats", auth=AUTH).status_code == 503


def test_admin_conversation_views_are_masked(client: TestClient) -> None:
    sid = client.post(
        "/api/chat",
        json={"message": "priya.sharma@example.in, AO-10001, PIN 560034, call 9845012345"},
    ).json()["session_id"]
    listing = client.get("/api/admin/conversations", auth=AUTH).json()
    assert listing["total"] == 1 and listing["items"][0]["verified"]
    detail = client.get(f"/api/admin/conversations/{sid}", auth=AUTH).json()
    dump = str(detail)
    for secret in ("priya.sharma@example.in", "9845012345", "98450 12345", "Priya Sharma"):
        assert secret not in dump, secret
    kinds = {e["kind"] for e in detail["trace"]}
    assert {"llm_call", "tool_call", "turn", "router"} <= kinds
    assert all(e["cost"].startswith("₹") for e in detail["trace"])
    assert client.get("/api/admin/conversations/nope", auth=AUTH).status_code == 404


def test_admin_stats_in_rupees(client: TestClient) -> None:
    client.post("/api/chat", json={"message": "priya.sharma@example.in AO-10001 560034"})
    s = client.get("/api/admin/stats", auth=AUTH).json()
    assert s["conversations"] == 1 and s["llm_calls"] == 2
    assert s["cost_today"] == "₹0.18"  # 2 calls x $0.001 x 85 = 17 paise -> ceil per call 9+9
    assert s["daily_budget"] == "₹300"
    assert s["turn_latency_ms"]["p95"] >= 0


def test_admin_tickets_and_resolve() -> None:
    c, _ = make_client([text("A specialist will contact you.")])
    with c:
        c.post("/api/chat", json={"message": "I want to speak to a human"})
        tickets = c.get("/api/admin/tickets", auth=AUTH).json()["items"]
        assert len(tickets) == 1 and tickets[0]["reason_code"] == "CUSTOMER_REQUESTED_HUMAN"
        assert "suggested_next_step" in tickets[0]["summary"]
        resolved = c.post(f"/api/admin/tickets/{tickets[0]['id']}/resolve", auth=AUTH).json()
        assert resolved["status"] == "resolved"
        assert c.get("/api/admin/tickets", auth=AUTH).json()["items"] == []
        assert c.post("/api/admin/tickets/999/resolve", auth=AUTH).status_code == 404


def test_admin_eval_and_reset(client: TestClient) -> None:
    res = client.get("/api/admin/eval", auth=AUTH)
    assert res.status_code == 200 and set(res.json()) == {"markdown", "summary"}
    assert client.post("/api/admin/reset-demo", auth=AUTH).status_code == 400
    client.post("/api/chat", json={"message": "hello"})
    assert client.post("/api/admin/reset-demo?confirm=RESET", auth=AUTH).json() == {
        "status": "reseeded"
    }
    assert client.get("/api/admin/conversations", auth=AUTH).json()["total"] == 0
