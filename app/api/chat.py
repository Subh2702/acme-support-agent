"""Public chat API: JSON and Server-Sent Events."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from app.agent.loop import AgentReply, summarize_reply
from app.db.models import Conversation, Message
from app.db.seed import demo_credentials
from app.services import Services

router = APIRouter(prefix="/api")
SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

PROGRESS_LABELS = {
    "verify_customer": "Verifying your details",
    "get_order": "Looking up your order",
    "list_orders": "Looking up your orders",
    "search_help_center": "Checking the help center",
    "cancel_order": "Working on the cancellation",
    "issue_refund": "Working on the refund request",
    "update_address": "Updating the address",
    "escalate_to_human": "Connecting you to a specialist",
}

SAMPLE_PROMPTS = [
    "What is your return policy?",
    "Where is my order? (then share the demo email, order number and PIN)",
    "Please cancel order AO-10002",
    "I want a refund for AO-10001, the beanie didn't fit",
    "Change the delivery address of AO-10002 to 22, 12th Main, Indiranagar, Bengaluru 560038",
    "Mera COD refund kab milega? Order AO-10010",
    "I want to talk to a human",
]


class ChatRequest(BaseModel):
    session_id: str | None = Field(default=None, description="Omit to start a new conversation")
    message: str = Field(min_length=1)

    @field_validator("session_id")
    @classmethod
    def _session(cls, value: str | None) -> str | None:
        if value is not None and not SESSION_RE.match(value):
            raise ValueError("invalid session_id")
        return value

    @field_validator("message")
    @classmethod
    def _message(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("message is empty")
        return value


def _services(request: Request) -> Services:
    return request.app.state.services


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _hash_ip(ip: str, salt: str) -> str:
    return hashlib.sha256(f"{salt}:{ip}".encode()).hexdigest()[:32]


def _prepare(request: Request, body: ChatRequest) -> tuple[Services, str, str]:
    services = _services(request)
    settings = services.settings
    if len(body.message) > settings.max_message_chars:
        raise HTTPException(413, f"Message too long (max {settings.max_message_chars} chars)")
    session_id = body.session_id or uuid.uuid4().hex
    ip = client_ip(request)
    assert services.ip_limiter and services.session_limiter  # noqa: S101 - set at startup
    for limiter, key, scope in (
        (services.ip_limiter, ip, "IP address"),
        (services.session_limiter, session_id, "conversation"),
    ):
        allowed, retry = limiter.check(key)
        if not allowed:
            raise HTTPException(
                429,
                f"Too many messages from this {scope}. Please wait {retry}s.",
                headers={"Retry-After": str(retry)},
            )
    return services, session_id, _hash_ip(ip, settings.ip_hash_salt)


@router.post("/chat")
async def chat(request: Request, body: ChatRequest) -> JSONResponse:
    services, session_id, ip_hash = _prepare(request, body)
    assert services.agent is not None  # noqa: S101
    reply: AgentReply = await run_in_threadpool(
        services.agent.handle, session_id, body.message, client_ip_hash=ip_hash
    )
    return JSONResponse(summarize_reply(reply))


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.post("/chat/stream")
async def chat_stream(request: Request, body: ChatRequest) -> StreamingResponse:
    """Streams progress events (tool steps) and then the final, output-checked reply.

    Tokens are not streamed on purpose: the reply is only released after the output
    checks have verified it against the trace.
    """
    services, session_id, ip_hash = _prepare(request, body)
    assert services.agent is not None  # noqa: S101
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue()

    def listener(kind: str, name: str, payload: dict[str, Any]) -> None:
        labels: list[str] = []
        if kind == "llm_call" and payload.get("tool_calls"):
            labels = [PROGRESS_LABELS.get(t, "Working on it") for t in payload["tool_calls"]]
        elif kind == "escalation":
            labels = ["Connecting you to a specialist"]
        for label in labels:
            loop.call_soon_threadsafe(queue.put_nowait, ("progress", {"label": label}))

    async def run() -> None:
        try:
            reply = await run_in_threadpool(
                services.agent.handle,  # type: ignore[union-attr]
                session_id,
                body.message,
                listener=listener,
                client_ip_hash=ip_hash,
            )
            await queue.put(("reply", summarize_reply(reply)))
        except Exception:  # the stream must always terminate cleanly
            await queue.put(("error", {"detail": "Something went wrong. Please try again."}))
        await queue.put(("done", {}))

    async def events() -> AsyncIterator[str]:
        task = asyncio.create_task(run())
        yield _sse("session", {"session_id": session_id})
        try:
            while True:
                event, data = await queue.get()
                yield _sse(event, data)
                if event == "done":
                    break
        finally:
            await task

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/chat/{session_id}/history")
def history(request: Request, session_id: str) -> dict[str, Any]:
    if not SESSION_RE.match(session_id):
        raise HTTPException(422, "invalid session_id")
    services = _services(request)
    with services.db.session() as session:
        rows = session.scalars(
            select(Message).where(Message.conversation_id == session_id).order_by(Message.id)
        ).all()
        conv = session.get(Conversation, session_id)
        return {
            "session_id": session_id,
            "escalated": bool(conv and conv.escalated),
            "messages": [{"role": m.role, "content": m.content} for m in rows],
        }


@router.get("/demo-info")
def demo_info(request: Request) -> dict[str, Any]:
    services = _services(request)
    return {
        "credentials": demo_credentials(),
        "sample_prompts": SAMPLE_PROMPTS,
        "model": services.llm_provider_name,
        "llm_ready": services.llm is not None,
    }
