"""Identity and read-only order tools."""

from __future__ import annotations

import re
from datetime import timedelta

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.handoff import open_ticket
from app.db.models import Customer, Order, PaymentMethod
from app.money import format_inr
from app.policy.engine import (
    Actor,
    Decision,
    OrderFacts,
    PolicyResult,
    check_account_access,
    check_address_change,
    check_cancel,
    check_order_access,
    check_refund,
)
from app.timeutil import format_ist
from app.tools.base import OrderRef, ToolContext, ToolResult, normalise_order_number

NOT_FOUND_MSG = "No order with that number exists on the verified account."
PIN_RE = re.compile(r"^[1-9]\d{5}$")


class VerifyArgs(BaseModel):
    email: str = Field(description="Email address on the customer's account")
    order_number: str = Field(description="Any order number of the customer, e.g. AO-10001")
    pin_code: str = Field(description="6-digit delivery PIN code of that order")

    @field_validator("email")
    @classmethod
    def _email(cls, value: str) -> str:
        value = value.strip().lower()
        if "@" not in value or len(value) > 200:
            raise ValueError("not a valid email address")
        return value

    @field_validator("order_number")
    @classmethod
    def _order(cls, value: str) -> str:
        return normalise_order_number(value)

    @field_validator("pin_code")
    @classmethod
    def _pin(cls, value: str) -> str:
        value = re.sub(r"\s", "", str(value))
        if not PIN_RE.match(value):
            raise ValueError("PIN codes are 6 digits and do not start with 0")
        return value


class NoArgs(BaseModel):
    pass


def actor_of(ctx: ToolContext) -> Actor:
    return Actor(ctx.state.verified_customer_id, ctx.state.action_count)


def order_facts(order: Order) -> OrderFacts:
    return OrderFacts(
        customer_id=order.customer_id,
        status=order.status,
        payment_method=order.payment_method,
        total_paise=order.total_paise,
        delivered_at=order.delivered_at,
        has_refund=bool(order.refunds),
    )


def find_order(session: Session, order_number: str, *, lock: bool = False) -> Order | None:
    stmt = select(Order).where(Order.order_number == order_number)
    if lock:
        stmt = stmt.with_for_update()
    return session.scalars(stmt).first()


def load_accessible_order(
    ctx: ToolContext, session: Session, order_number: str, *, lock: bool = False
) -> tuple[Order | None, ToolResult | None]:
    """Fetch an order the conversation may see; returns (order, None) or (None, error)."""
    order = find_order(session, order_number, lock=lock)
    if not ctx.flags.policy:
        if order is None:
            return None, ToolResult.failure("ORDER_NOT_FOUND", "No order with that number.")
        return order, None
    access = check_account_access(actor_of(ctx))
    if not access.allowed:
        ctx.tracer.record(
            "policy_decision",
            "order_access",
            {"decision": access.decision, "reason": access.reason},
        )
        return None, ToolResult.failure(access.reason.value, access.explanation)
    if order is None:
        return None, ToolResult.failure("ORDER_NOT_FOUND", NOT_FOUND_MSG)
    decision = check_order_access(actor_of(ctx), order_facts(order))
    if not decision.allowed:
        ctx.tracer.record(
            "policy_decision",
            "order_access",
            {
                "order_number": order_number,
                "decision": decision.decision,
                "reason": decision.reason,
            },
        )
        # Identical to "not found" so the model cannot confirm another customer's order exists.
        return None, ToolResult.failure("ORDER_NOT_FOUND", NOT_FOUND_MSG)
    return order, None


def verify_customer(ctx: ToolContext, args: VerifyArgs) -> ToolResult:
    state, cfg = ctx.state, ctx.policy
    if state.verification_locked:
        return ToolResult.failure(
            "VERIFICATION_LOCKED",
            "Verification is locked after too many failed attempts; a human agent will help.",
        )
    with ctx.db.transaction() as session:
        order = find_order(session, args.order_number)
        customer = order.customer if order else None
        matched = (
            order is not None
            and customer is not None
            and customer.email.lower() == args.email
            and order.ship_pin == args.pin_code
        )
        if matched and customer is not None:
            if state.is_verified and state.verified_customer_id != customer.id:
                return ToolResult.failure(
                    "ALREADY_VERIFIED_OTHER",
                    "This chat is already verified for a different account. Please start a "
                    "new chat to get help with another account.",
                )
            state.verified_customer_id = customer.id
            state.verified_name = customer.name
            state.verified_email = customer.email
            return ToolResult.success(
                "Verified successfully. Address the customer by their first name.",
                {"customer_name": customer.name, "customer_first_name": customer.name.split()[0]},
            )
        state.failed_verifications += 1
        remaining = cfg.max_verification_attempts - state.failed_verifications
        if remaining > 0:
            return ToolResult.failure(
                "VERIFICATION_FAILED",
                "Those details don't match our records. Ask the customer to re-check the email, "
                f"order number and delivery PIN code ({remaining} attempt(s) left). Do not "
                "reveal which detail was wrong.",
            )
        state.verification_locked = True
        ticket = open_ticket(
            session,
            conversation_id=ctx.conversation_id,
            state=state,
            reason_code="VERIFICATION_FAILED",
            reason="Customer failed identity verification repeatedly.",
            urgency="normal",
            order_number=args.order_number,
            transcript=ctx.transcript,
        )
        ctx.tracer.record("escalation", "auto:verification_failed", {"ticket_id": ticket.id})
        return ToolResult.failure(
            "VERIFICATION_LOCKED",
            "Too many failed verification attempts. The conversation has been handed to a "
            f"human agent (ticket #{ticket.id}), who will verify the customer another way.",
            {"ticket_id": ticket.id},
        )


def _order_summary(order: Order) -> dict[str, object]:
    return {
        "order_number": order.order_number,
        "status": order.status,
        "total": format_inr(order.total_paise),
        "placed_on": format_ist(order.created_at),
        "items": ", ".join(f"{i.name} x{i.quantity}" for i in order.items),
    }


def list_orders(ctx: ToolContext, _args: NoArgs) -> ToolResult:
    access = check_account_access(actor_of(ctx))
    if not access.allowed:
        return ToolResult.failure(access.reason.value, access.explanation)
    with ctx.db.transaction() as session:
        customer = session.get(Customer, ctx.state.verified_customer_id)
        if customer is None:
            return ToolResult.failure("NOT_VERIFIED", access.explanation)
        orders = sorted(customer.orders, key=lambda o: o.created_at, reverse=True)
        return ToolResult.success(
            f"{len(orders)} order(s) on this account.",
            {"orders": [_order_summary(o) for o in orders]},
        )


def order_details(order: Order, ctx: ToolContext) -> dict[str, object]:
    refund = order.refunds[-1] if order.refunds else None
    window_end = (
        order.delivered_at + timedelta(days=ctx.policy.refund_window_days)
        if order.delivered_at
        else None
    )
    return {
        **_order_summary(order),
        "total_paise": order.total_paise,
        "payment_method": PaymentMethod.LABELS.get(order.payment_method, order.payment_method),
        "items": [
            {"name": i.name, "quantity": i.quantity, "unit_price": format_inr(i.unit_price_paise)}
            for i in order.items
        ],
        "shipped_on": format_ist(order.shipped_at),
        "delivered_on": format_ist(order.delivered_at),
        "cancelled_on": format_ist(order.cancelled_at),
        "courier": order.courier,
        "tracking_number": order.tracking_number,
        "shipping_address": f"{order.ship_line1}, {order.ship_city}, {order.ship_state} "
        f"{order.ship_pin}",
        "refund": (
            {"amount": format_inr(refund.amount_paise), "status": refund.status} if refund else None
        ),
        "refund_window_ends": format_ist(window_end) if order.status == "delivered" else None,
        "can_cancel": order.status == "processing",
        "can_change_address": order.status == "processing",
        **({"eligibility": _eligibility(order, ctx)} if ctx.flags.policy else {}),
    }


def _eligibility(order: Order, ctx: ToolContext) -> dict[str, str]:
    """Policy-engine dry run so the model reads decisions instead of doing date math.

    Deliberately coarse: it never reveals approval thresholds, only whether an action
    is possible at all and, if not, why.
    """
    actor = Actor(ctx.state.verified_customer_id, 0)
    facts = order_facts(order)

    number = order.order_number

    def verdict(result: PolicyResult, call: str) -> str:
        if result.decision is Decision.DENY:
            return f"not possible: {result.explanation}"
        return f"possible — if the customer wants it, call {call} (the system decides the outcome)"

    return {
        "full_refund": verdict(
            check_refund(actor, facts, order.total_paise, ctx.now, ctx.policy),
            f'issue_refund(order_number="{number}", amount_paise={order.total_paise})',
        ),
        "cancel": verdict(
            check_cancel(actor, facts, ctx.policy), f'cancel_order(order_number="{number}")'
        ),
        "change_address": verdict(
            check_address_change(actor, facts, ctx.policy),
            f'update_address(order_number="{number}", new_address=...)',
        ),
    }


def get_order(ctx: ToolContext, args: OrderRef) -> ToolResult:
    with ctx.db.transaction() as session:
        order, error = load_accessible_order(ctx, session, args.order_number)
        if error is not None or order is None:
            return error or ToolResult.failure("ORDER_NOT_FOUND", NOT_FOUND_MSG)
        return ToolResult.success(
            f"Order {order.order_number} is {order.status}.", order_details(order, ctx)
        )
