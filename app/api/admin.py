"""Admin API (HTTP basic auth): conversations, traces, escalation queue, stats, eval results."""

from __future__ import annotations

import json
import secrets
from datetime import timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy import func, select

from app.config import ROOT_DIR
from app.db.models import Conversation, Message, Ticket, TicketStatus, TraceEvent
from app.db.seed import reset_and_seed
from app.guardrails.pii import mask_pii, mask_structured
from app.money import format_inr, inr_to_paise
from app.services import Services
from app.timeutil import format_ist, start_of_ist_day, utcnow

security = HTTPBasic(auto_error=False)
WEB_DIR = ROOT_DIR / "app" / "web"
EVAL_RESULTS_DIR = ROOT_DIR / "eval" / "results"


def require_admin(
    request: Request,
    credentials: Annotated[HTTPBasicCredentials | None, Depends(security)],
) -> str:
    settings = request.app.state.services.settings
    if not settings.admin_password:
        raise HTTPException(503, "Admin panel disabled: set ADMIN_PASSWORD")
    challenge = {"WWW-Authenticate": 'Basic realm="acme-admin"'}
    if credentials is None:
        raise HTTPException(401, "Authentication required", headers=challenge)
    user_ok = secrets.compare_digest(credentials.username.encode(), settings.admin_user.encode())
    pass_ok = secrets.compare_digest(
        credentials.password.encode(), settings.admin_password.encode()
    )
    if not (user_ok and pass_ok):
        raise HTTPException(401, "Invalid credentials", headers=challenge)
    return credentials.username


Admin = Annotated[str, Depends(require_admin)]
router = APIRouter()


def _services(request: Request) -> Services:
    return request.app.state.services


def _percentile(values: list[int], pct: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round(pct / 100 * (len(ordered) - 1))))
    return ordered[idx]


@router.get("/admin", include_in_schema=False)
def admin_page(_user: Admin) -> FileResponse:
    return FileResponse(WEB_DIR / "admin.html")


@router.get("/api/admin/conversations")
def conversations(
    request: Request,
    _user: Admin,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    escalated: bool | None = None,
) -> dict[str, Any]:
    db = _services(request).db
    with db.session() as session:
        stmt = select(Conversation).order_by(Conversation.updated_at.desc())
        if escalated is not None:
            stmt = stmt.where(Conversation.escalated == escalated)
        convs = session.scalars(stmt.limit(limit).offset(offset)).all()
        ids = [c.id for c in convs]
        costs = (
            dict(
                session.execute(
                    select(TraceEvent.conversation_id, func.sum(TraceEvent.cost_paise))
                    .where(TraceEvent.conversation_id.in_(ids))
                    .group_by(TraceEvent.conversation_id)
                ).all()
            )
            if ids
            else {}
        )
        items = []
        for conv in convs:
            last = session.scalars(
                select(Message)
                .where(Message.conversation_id == conv.id)
                .order_by(Message.id.desc())
                .limit(1)
            ).first()
            state = conv.state or {}
            items.append(
                {
                    "id": conv.id,
                    "updated": format_ist(conv.updated_at),
                    "turns": state.get("turn_count", 0),
                    "verified": state.get("verified_customer_id") is not None,
                    "escalated": conv.escalated,
                    "language": state.get("language", "en"),
                    "last_intent": state.get("last_intent"),
                    "cost": format_inr(int(costs.get(conv.id) or 0)),
                    "preview": mask_pii(last.content)[:140] if last else "",
                }
            )
        total = session.scalar(select(func.count()).select_from(Conversation)) or 0
    return {"total": total, "items": items}


@router.get("/api/admin/conversations/{conversation_id}")
def conversation_detail(request: Request, conversation_id: str, _user: Admin) -> dict[str, Any]:
    db = _services(request).db
    with db.session() as session:
        conv = session.get(Conversation, conversation_id)
        if conv is None:
            raise HTTPException(404, "Conversation not found")
        messages = session.scalars(
            select(Message).where(Message.conversation_id == conv.id).order_by(Message.id)
        ).all()
        events = session.scalars(
            select(TraceEvent).where(TraceEvent.conversation_id == conv.id).order_by(TraceEvent.id)
        ).all()
        tickets = session.scalars(select(Ticket).where(Ticket.conversation_id == conv.id)).all()
        return {
            "id": conv.id,
            "escalated": conv.escalated,
            "state": mask_structured(
                {k: v for k, v in (conv.state or {}).items() if k not in ("verified_email",)}
            ),
            "messages": [
                {"role": m.role, "content": mask_pii(m.content), "at": format_ist(m.created_at)}
                for m in messages
            ],
            "trace": [
                {
                    "turn": e.turn_index,
                    "kind": e.kind,
                    "name": e.name,
                    "payload": e.payload,
                    "input_tokens": e.input_tokens,
                    "output_tokens": e.output_tokens,
                    "cost": format_inr(e.cost_paise),
                    "latency_ms": e.latency_ms,
                    "at": format_ist(e.created_at),
                }
                for e in events
            ],
            "tickets": [_ticket(t) for t in tickets],
        }


def _ticket(t: Ticket) -> dict[str, Any]:
    return {
        "id": t.id,
        "conversation_id": t.conversation_id,
        "reason_code": t.reason_code,
        "urgency": t.urgency,
        "status": t.status,
        "order_number": t.order_number,
        "summary": t.summary,
        "created": format_ist(t.created_at),
        "resolved": format_ist(t.resolved_at),
    }


@router.get("/api/admin/tickets")
def tickets(request: Request, _user: Admin, status: str = TicketStatus.OPEN) -> dict[str, Any]:
    urgency_order = {"high": 0, "normal": 1, "low": 2}
    with _services(request).db.session() as session:
        rows = session.scalars(
            select(Ticket)
            .where(Ticket.status == status)
            .order_by(Ticket.created_at.desc())
            .limit(200)
        ).all()
        items = sorted((_ticket(t) for t in rows), key=lambda t: urgency_order.get(t["urgency"], 1))
    return {"items": items}


@router.post("/api/admin/tickets/{ticket_id}/resolve")
def resolve_ticket(request: Request, ticket_id: int, _user: Admin) -> dict[str, Any]:
    with _services(request).db.transaction() as session:
        ticket = session.get(Ticket, ticket_id)
        if ticket is None:
            raise HTTPException(404, "Ticket not found")
        ticket.status = TicketStatus.RESOLVED
        ticket.resolved_at = utcnow()
        return _ticket(ticket)


@router.get("/api/admin/stats")
def stats(request: Request, _user: Admin) -> dict[str, Any]:
    services = _services(request)
    now = utcnow()
    today = start_of_ist_day(now)
    week = now - timedelta(days=7)
    with services.db.session() as session:

        def cost_since(since: Any) -> int:
            return int(
                session.scalar(
                    select(func.coalesce(func.sum(TraceEvent.cost_paise), 0)).where(
                        TraceEvent.created_at >= since
                    )
                )
                or 0
            )

        total_convs = session.scalar(select(func.count()).select_from(Conversation)) or 0
        escalated = (
            session.scalar(
                select(func.count())
                .select_from(Conversation)
                .where(Conversation.escalated.is_(True))
            )
            or 0
        )
        total_cost = int(
            session.scalar(select(func.coalesce(func.sum(TraceEvent.cost_paise), 0))) or 0
        )
        llm = session.execute(
            select(
                func.count(),
                func.coalesce(func.sum(TraceEvent.input_tokens), 0),
                func.coalesce(func.sum(TraceEvent.output_tokens), 0),
            ).where(TraceEvent.kind == "llm_call")
        ).one()
        turn_latencies = list(
            session.scalars(
                select(TraceEvent.latency_ms)
                .where(
                    TraceEvent.kind == "turn",
                    TraceEvent.name == "agent_reply",
                    TraceEvent.created_at >= week,
                )
                .order_by(TraceEvent.id.desc())
                .limit(5000)
            )
        )
        llm_latencies = list(
            session.scalars(
                select(TraceEvent.latency_ms)
                .where(TraceEvent.kind == "llm_call", TraceEvent.created_at >= week)
                .order_by(TraceEvent.id.desc())
                .limit(5000)
            )
        )
        guardrails = dict(
            session.execute(
                select(TraceEvent.name, func.count())
                .where(TraceEvent.kind == "guardrail")
                .group_by(TraceEvent.name)
            ).all()
        )
        intents = dict(
            session.execute(
                select(TraceEvent.name, func.count())
                .where(TraceEvent.kind == "router")
                .group_by(TraceEvent.name)
            ).all()
        )
        open_tickets = (
            session.scalar(
                select(func.count()).select_from(Ticket).where(Ticket.status == TicketStatus.OPEN)
            )
            or 0
        )
        spent_today = cost_since(today)
    budget = inr_to_paise(services.settings.daily_budget_inr)
    return {
        "model": services.llm_provider_name,
        "conversations": total_convs,
        "escalation_rate": round(escalated / total_convs, 3) if total_convs else 0.0,
        "open_tickets": open_tickets,
        "llm_calls": llm[0],
        "input_tokens": int(llm[1]),
        "output_tokens": int(llm[2]),
        "cost_today": format_inr(spent_today),
        "cost_total": format_inr(total_cost),
        "cost_per_conversation": format_inr(total_cost // total_convs)
        if total_convs
        else format_inr(0),
        "daily_budget": format_inr(budget),
        "budget_used_pct": round(100 * spent_today / budget, 1) if budget else 0.0,
        "turn_latency_ms": {
            "p50": _percentile(turn_latencies, 50),
            "p95": _percentile(turn_latencies, 95),
        },
        "llm_latency_ms": {
            "p50": _percentile(llm_latencies, 50),
            "p95": _percentile(llm_latencies, 95),
        },
        "guardrail_hits": guardrails,
        "intents": intents,
        "usd_inr_rate": services.settings.usd_inr_rate,
    }


@router.get("/api/admin/eval")
def eval_results(_user: Admin) -> dict[str, Any]:
    md = EVAL_RESULTS_DIR / "latest.md"
    js = EVAL_RESULTS_DIR / "latest.json"
    return {
        "markdown": md.read_text(encoding="utf-8") if md.exists() else None,
        "summary": json.loads(js.read_text(encoding="utf-8")) if js.exists() else None,
    }


@router.post("/api/admin/reset-demo")
def reset_demo(
    request: Request, _user: Admin, confirm: Annotated[str, Query()] = ""
) -> dict[str, str]:
    if confirm != "RESET":
        raise HTTPException(400, "Pass confirm=RESET to wipe and reseed the demo data")
    reset_and_seed(_services(request).db)
    return {"status": "reseeded"}
