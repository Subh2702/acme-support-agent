"""Escalation tickets and the structured handoff summary a human agent reads first.

The summary is built deterministically from conversation state and the trace
(not by an LLM): it is cheap, cannot hallucinate actions, and is unit-testable.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.state import ConversationState
from app.db.models import Conversation, Ticket, TicketStatus
from app.guardrails.pii import mask_pii
from app.money import format_inr

URGENCY_RANK = {"low": 0, "normal": 1, "high": 2}

NEXT_STEPS: dict[str, str] = {
    "REFUND_NEEDS_APPROVAL": "Review the pending refund{amount}{order} and approve or reject it "
    "in the refunds console; the customer was told to expect an update within 24-48 hours.",
    "REFUND_HIGH_VALUE": "High-value refund{amount}{order}: verify the return/QC status, then "
    "approve or reject. Prioritise: customer expects a reply within 24 hours.",
    "REFUND_COD_NEEDS_HUMAN": "COD refund{amount}{order}: send the secure bank-details link to "
    "the registered email/phone (never collect bank details in chat), then pay by NEFT/IMPS.",
    "ACTION_LIMIT_REACHED": "The per-conversation action limit was reached. Review the actions "
    "below and complete any outstanding request manually.",
    "VERIFICATION_FAILED": "Identity verification failed repeatedly. Treat as a possible "
    "account-takeover attempt: verify via a callback to the registered phone before sharing "
    "any order information.",
    "CUSTOMER_REQUESTED_HUMAN": "Customer asked for a human agent. Pick up the conversation and "
    "resolve the issue described below.",
    "LEGAL_THREAT": "Customer mentioned legal action / consumer forum / helpline. Route to the "
    "escalations lead within 2 hours; do not admit liability in writing.",
    "NEGATIVE_SENTIMENT": "Customer is frustrated after several unresolved turns. Call back "
    "with an owner-level resolution.",
    "LOW_CONFIDENCE": "The agent could not resolve this confidently. Review the transcript and "
    "reply to the customer.",
    "TOOL_FAILURE": "A system error left an action's outcome unknown. Check the order state "
    "before replying to the customer.",
    "AGENT_ESCALATION": "The agent escalated this conversation. Review the reason and transcript.",
}


def mask_email(email: str | None) -> str | None:
    if not email or "@" not in email:
        return email
    local, domain = email.split("@", 1)
    shown = local[0] + "***" + (local[-1] if len(local) > 1 else "")
    return f"{shown}@{domain}"


def build_handoff_summary(
    *,
    state: ConversationState,
    reason_code: str,
    reason: str,
    urgency: str,
    order_number: str | None,
    transcript: list[dict[str, str]],
    amount_paise: int | None = None,
) -> dict[str, Any]:
    customer_msgs = [m["content"] for m in transcript if m["role"] == "user"]
    first = customer_msgs[0] if customer_msgs else ""
    latest = customer_msgs[-1] if customer_msgs else ""
    issue = reason.strip()
    if first:
        issue += f' | Customer opened with: "{mask_pii(first)[:240]}"'
    if latest and latest != first:
        issue += f' | Latest message: "{mask_pii(latest)[:240]}"'
    template = NEXT_STEPS.get(reason_code, NEXT_STEPS["AGENT_ESCALATION"])
    next_step = template.format(
        amount=f" of {format_inr(amount_paise)}" if amount_paise else "",
        order=f" on order {order_number}" if order_number else "",
    )
    return {
        "issue": issue,
        "reason_code": reason_code,
        "urgency": urgency,
        "order_number": order_number,
        "verified_identity": {
            "verified": state.is_verified,
            "customer_id": state.verified_customer_id,
            "name": state.verified_name,
            "email": mask_email(state.verified_email),
            "failed_verification_attempts": state.failed_verifications,
        },
        "actions_taken": [
            {
                "tool": a.tool,
                "order_number": a.order_number,
                "outcome": a.outcome,
                "detail": a.detail,
            }
            for a in state.actions_done
        ],
        "actions_denied": [
            {
                "tool": a.tool,
                "order_number": a.order_number,
                "reason_code": a.reason_code,
                "why": a.detail,
            }
            for a in state.actions_denied
        ],
        "customer_language": state.language,
        "suggested_next_step": next_step,
        "transcript_excerpt": [
            {"role": m["role"], "content": mask_pii(m["content"])[:400]} for m in transcript[-6:]
        ],
    }


def open_ticket(
    session: Session,
    *,
    conversation_id: str,
    state: ConversationState,
    reason_code: str,
    reason: str,
    urgency: str,
    order_number: str | None,
    transcript: list[dict[str, str]],
    amount_paise: int | None = None,
    force_new: bool = False,
) -> Ticket:
    """Create a ticket, or reuse this conversation's open generic ticket.

    Approval tickets (refunds) are always new because each one is a separate
    decision for the refunds team; generic escalations reuse the open ticket and
    only ever raise its urgency.
    """
    summary = build_handoff_summary(
        state=state,
        reason_code=reason_code,
        reason=reason,
        urgency=urgency,
        order_number=order_number,
        transcript=transcript,
        amount_paise=amount_paise,
    )
    existing = None
    if not force_new:
        existing = session.scalars(
            select(Ticket)
            .where(Ticket.conversation_id == conversation_id, Ticket.status == TicketStatus.OPEN)
            .order_by(Ticket.id.desc())
        ).first()
    if existing is not None:
        if URGENCY_RANK.get(urgency, 1) > URGENCY_RANK.get(existing.urgency, 1):
            existing.urgency = urgency
            summary["urgency"] = urgency
        else:
            summary["urgency"] = existing.urgency
        existing.summary = summary
        ticket = existing
    else:
        ticket = Ticket(
            conversation_id=conversation_id,
            reason_code=reason_code,
            reason=reason[:1000],
            urgency=urgency,
            order_number=order_number,
            summary=summary,
        )
        session.add(ticket)
    session.flush()
    conv = session.get(Conversation, conversation_id)
    if conv is not None:
        conv.escalated = True
    state.escalated = True
    if ticket.id not in state.ticket_ids:
        state.ticket_ids.append(ticket.id)
    return ticket
