"""Action tools. Each one calls the policy engine internally before touching the database."""

from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.agent.handoff import open_ticket
from app.agent.state import ActionRecord
from app.db.models import Order, OrderStatus, PaymentMethod, Refund, RefundStatus
from app.money import format_inr
from app.policy.engine import (
    Decision,
    PolicyResult,
    check_address_change,
    check_cancel,
    check_refund,
)
from app.tools.account import actor_of, load_accessible_order, order_facts
from app.tools.base import OrderRef, ToolContext, ToolResult

PIN_RE = re.compile(r"^[1-9]\d{5}$")
PHONE_RE = re.compile(r"^(?:\+91[\s-]?)?[6-9]\d{4}[\s-]?\d{5}$")

REVERSAL_TIMELINE = {
    PaymentMethod.UPI: "UPI refunds usually reflect within 2-3 business days",
    PaymentMethod.CREDIT_CARD: "card refunds usually reflect within 5-7 business days",
    PaymentMethod.DEBIT_CARD: "card refunds usually reflect within 5-7 business days",
    PaymentMethod.NET_BANKING: "net-banking refunds usually reflect within 5-7 business days",
}


class CancelArgs(OrderRef):
    reason: str = Field(default="", max_length=500, description="Optional; never ask for it")


class RefundArgs(OrderRef):
    amount_paise: int = Field(
        description="Refund amount in integer paise (₹1 = 100 paise). Use the order total for a "
        "full refund."
    )
    reason: str = Field(default="", max_length=500, description="Optional; never ask for it")


class NewAddress(BaseModel):
    name: str = Field(min_length=2, max_length=120, description="Recipient name")
    line1: str = Field(min_length=3, max_length=200, description="House/flat, building, street")
    line2: str = Field(default="", max_length=200, description="Area / landmark (optional)")
    city: str = Field(min_length=2, max_length=80)
    state: str = Field(min_length=2, max_length=80)
    pin_code: str = Field(description="6-digit Indian PIN code")
    phone: str | None = Field(default=None, description="Optional +91 contact number")

    @field_validator("pin_code")
    @classmethod
    def _pin(cls, value: str) -> str:
        value = re.sub(r"\s", "", value)
        if not PIN_RE.match(value):
            raise ValueError("PIN codes are 6 digits and do not start with 0")
        return value

    @field_validator("phone")
    @classmethod
    def _phone(cls, value: str | None) -> str | None:
        if value and not PHONE_RE.match(value.strip()):
            raise ValueError("phone must be a valid Indian mobile number")
        return value.strip() if value else value


class AddressArgs(OrderRef):
    new_address: NewAddress


def _record_policy(
    ctx: ToolContext, tool: str, order_number: str, result: PolicyResult, **extra: object
) -> None:
    ctx.tracer.record(
        "policy_decision",
        tool,
        {
            "order_number": order_number,
            "decision": result.decision.value,
            "reason": result.reason.value,
            "urgency": result.urgency.value if result.urgency else None,
            **extra,
        },
    )


def _deny(
    ctx: ToolContext,
    tool: str,
    order_number: str,
    result: PolicyResult,
    amount_paise: int | None = None,
) -> ToolResult:
    ctx.state.record_action(
        ActionRecord(
            tool=tool,
            order_number=order_number,
            outcome="denied",
            reason_code=result.reason.value,
            detail=result.explanation,
            amount_paise=amount_paise,
        )
    )
    return ToolResult.failure(result.reason.value, f"Not allowed: {result.explanation}")


def _pre_action(
    ctx: ToolContext, session: Session, tool: str, order_number: str
) -> tuple[Order | None, ToolResult | None]:
    """Count the action attempt and load an order the customer may act on."""
    ctx.state.action_count += 1
    order, error = load_accessible_order(ctx, session, order_number, lock=True)
    if error is not None:
        ctx.state.record_action(
            ActionRecord(
                tool=tool,
                order_number=order_number,
                outcome="denied",
                reason_code=error.error_code or "DENIED",
                detail=error.message,
            )
        )
    return order, error


def _escalate_for_approval(
    ctx: ToolContext,
    *,
    session: Session,
    tool: str,
    order: Order,
    result: PolicyResult,
    amount_paise: int | None,
) -> int:
    ticket = open_ticket(
        session,
        conversation_id=ctx.conversation_id,
        state=ctx.state,
        reason_code=result.reason.value,
        reason=result.explanation,
        urgency=(result.urgency.value if result.urgency else "normal"),
        order_number=order.order_number,
        transcript=ctx.transcript,
        amount_paise=amount_paise,
        force_new=tool == "issue_refund",
    )
    ctx.tracer.record(
        "escalation",
        f"needs_approval:{tool}",
        {"ticket_id": ticket.id, "reason": result.reason.value},
    )
    return ticket.id


def cancel_order(ctx: ToolContext, args: CancelArgs) -> ToolResult:
    tool = "cancel_order"
    # The prior check-then-act runs in one transaction with the order row locked.
    with ctx.db.transaction() as session:
        order, error = _pre_action(ctx, session, tool, args.order_number)
        if error is not None or order is None:
            return error or ToolResult.failure("ORDER_NOT_FOUND", "Order not found.")
        if ctx.flags.policy:
            result = check_cancel(actor_of(ctx), order_facts(order), ctx.policy)
            _record_policy(ctx, tool, order.order_number, result)
            if result.decision is Decision.DENY:
                return _deny(ctx, tool, order.order_number, result)
            if result.decision is Decision.NEEDS_APPROVAL:
                ticket_id = _escalate_for_approval(
                    ctx, session=session, tool=tool, order=order, result=result, amount_paise=None
                )
                ctx.state.record_action(
                    ActionRecord(
                        tool=tool,
                        order_number=order.order_number,
                        outcome="pending_approval",
                        reason_code=result.reason.value,
                        detail=result.explanation,
                        ticket_id=ticket_id,
                    )
                )
                return ToolResult.failure(
                    result.reason.value,
                    f"Needs human review: {result.explanation} Ticket #{ticket_id}.",
                    {"ticket_id": ticket_id, "escalated": True},
                )
        order.status = OrderStatus.CANCELLED
        order.cancelled_at = ctx.now
        if order.payment_method == PaymentMethod.COD:
            money_note = "No payment was collected (cash on delivery), so nothing is due back."
        else:
            timeline = REVERSAL_TIMELINE.get(order.payment_method, "within 5-7 business days")
            money_note = (
                f"{format_inr(order.total_paise)} will be reversed to the original "
                f"payment method; {timeline}."
            )
        ctx.state.record_action(
            ActionRecord(
                tool=tool,
                order_number=order.order_number,
                outcome="done",
                reason_code="OK",
                detail=f"Cancelled. {money_note}",
            )
        )
        return ToolResult.success(
            f"Order {order.order_number} has been cancelled. {money_note}",
            {"order_number": order.order_number, "status": order.status},
        )


def issue_refund(ctx: ToolContext, args: RefundArgs) -> ToolResult:
    tool = "issue_refund"
    with ctx.db.transaction() as session:
        order, error = _pre_action(ctx, session, tool, args.order_number)
        if error is not None or order is None:
            return error or ToolResult.failure("ORDER_NOT_FOUND", "Order not found.")
        amount = args.amount_paise
        refund_status = RefundStatus.ISSUED
        ticket_id: int | None = None
        result: PolicyResult | None = None
        if ctx.flags.policy:
            result = check_refund(actor_of(ctx), order_facts(order), amount, ctx.now, ctx.policy)
            _record_policy(ctx, tool, order.order_number, result, amount_paise=amount)
            if result.decision is Decision.DENY:
                return _deny(ctx, tool, order.order_number, result, amount)
            if result.decision is Decision.NEEDS_APPROVAL:
                refund_status = RefundStatus.PENDING_APPROVAL
                ticket_id = _escalate_for_approval(
                    ctx, session=session, tool=tool, order=order, result=result, amount_paise=amount
                )
        refund = Refund(
            order=order,
            amount_paise=amount,
            status=refund_status,
            reason=args.reason,
            conversation_id=ctx.conversation_id,
            ticket_id=ticket_id,
            created_at=ctx.now,
        )
        session.add(refund)
        if refund_status == RefundStatus.PENDING_APPROVAL and result is not None:
            ctx.state.record_action(
                ActionRecord(
                    tool=tool,
                    order_number=order.order_number,
                    outcome="pending_approval",
                    reason_code=result.reason.value,
                    detail=result.explanation,
                    amount_paise=amount,
                    ticket_id=ticket_id,
                )
            )
            return ToolResult.failure(
                result.reason.value,
                f"Refund of {format_inr(amount)} NOT issued yet - it needs human approval: "
                f"{result.explanation} Ticket #{ticket_id} was created; the team replies within "
                "24-48 hours.",
                {
                    "order_number": order.order_number,
                    "refund_status": refund_status,
                    "amount": format_inr(amount),
                    "ticket_id": ticket_id,
                    "escalated": True,
                },
            )
        if amount >= order.total_paise:
            order.status = OrderStatus.REFUNDED
        timeline = REVERSAL_TIMELINE.get(order.payment_method, "within 5-7 business days")
        ctx.state.record_action(
            ActionRecord(
                tool=tool,
                order_number=order.order_number,
                outcome="done",
                reason_code="OK",
                detail=f"Refund of {format_inr(amount)} issued.",
                amount_paise=amount,
            )
        )
        return ToolResult.success(
            f"Refund of {format_inr(amount)} issued for order {order.order_number} to the "
            f"original payment method; {timeline}.",
            {
                "order_number": order.order_number,
                "refund_status": refund_status,
                "amount": format_inr(amount),
            },
        )


def update_address(ctx: ToolContext, args: AddressArgs) -> ToolResult:
    tool = "update_address"
    with ctx.db.transaction() as session:
        order, error = _pre_action(ctx, session, tool, args.order_number)
        if error is not None or order is None:
            return error or ToolResult.failure("ORDER_NOT_FOUND", "Order not found.")
        if ctx.flags.policy:
            result = check_address_change(actor_of(ctx), order_facts(order), ctx.policy)
            _record_policy(ctx, tool, order.order_number, result)
            if result.decision is Decision.DENY:
                return _deny(ctx, tool, order.order_number, result)
            if result.decision is Decision.NEEDS_APPROVAL:
                ticket_id = _escalate_for_approval(
                    ctx, session=session, tool=tool, order=order, result=result, amount_paise=None
                )
                ctx.state.record_action(
                    ActionRecord(
                        tool=tool,
                        order_number=order.order_number,
                        outcome="pending_approval",
                        reason_code=result.reason.value,
                        detail=result.explanation,
                        ticket_id=ticket_id,
                    )
                )
                return ToolResult.failure(
                    result.reason.value,
                    f"Needs human review: {result.explanation} Ticket #{ticket_id}.",
                    {"ticket_id": ticket_id, "escalated": True},
                )
        addr = args.new_address
        order.ship_name, order.ship_line1, order.ship_line2 = addr.name, addr.line1, addr.line2
        order.ship_city, order.ship_state, order.ship_pin = addr.city, addr.state, addr.pin_code
        if addr.phone:
            order.ship_phone = addr.phone
        order.address_updated_at = ctx.now
        ctx.state.record_action(
            ActionRecord(
                tool=tool,
                order_number=order.order_number,
                outcome="done",
                reason_code="OK",
                detail=f"Address changed to {addr.city} {addr.pin_code}.",
            )
        )
        return ToolResult.success(
            f"Delivery address for {order.order_number} updated to: {addr.line1}, "
            f"{addr.city}, {addr.state} {addr.pin_code}.",
            {"order_number": order.order_number},
        )
