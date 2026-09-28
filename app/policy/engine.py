"""Deterministic business rules. No LLM, no I/O: plain data in, a decision out.

Every action tool calls these functions *inside* the tool, so no phrasing of a
customer message (or of a model's tool arguments) can skip them.

Customer-facing explanations deliberately never mention internal thresholds.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum


class Decision(StrEnum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    NEEDS_APPROVAL = "NEEDS_APPROVAL"


class Urgency(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"


class Reason(StrEnum):
    OK = "OK"
    NOT_VERIFIED = "NOT_VERIFIED"
    NOT_OWNER = "NOT_OWNER"
    ACTION_LIMIT_REACHED = "ACTION_LIMIT_REACHED"
    REFUND_NOT_DELIVERED = "REFUND_NOT_DELIVERED"
    REFUND_WINDOW_EXPIRED = "REFUND_WINDOW_EXPIRED"
    REFUND_ALREADY_EXISTS = "REFUND_ALREADY_EXISTS"
    REFUND_INVALID_AMOUNT = "REFUND_INVALID_AMOUNT"
    REFUND_EXCEEDS_TOTAL = "REFUND_EXCEEDS_TOTAL"
    REFUND_NEEDS_APPROVAL = "REFUND_NEEDS_APPROVAL"
    REFUND_HIGH_VALUE = "REFUND_HIGH_VALUE"
    REFUND_COD_NEEDS_HUMAN = "REFUND_COD_NEEDS_HUMAN"
    CANCEL_NOT_PROCESSING = "CANCEL_NOT_PROCESSING"
    ADDRESS_ALREADY_SHIPPED = "ADDRESS_ALREADY_SHIPPED"
    ADDRESS_ORDER_CLOSED = "ADDRESS_ORDER_CLOSED"


@dataclass(frozen=True)
class PolicyConfig:
    """The single place where business thresholds live. Money is in paise."""

    refund_window_days: int = 30
    auto_refund_max_paise: int = 200_000  # ₹2,000
    high_value_refund_paise: int = 1_000_000  # ₹10,000
    max_actions_per_conversation: int = 3
    max_verification_attempts: int = 3


DEFAULT_POLICY = PolicyConfig()


@dataclass(frozen=True)
class Actor:
    """Who is asking: the identity verified in this conversation (if any)."""

    verified_customer_id: int | None
    action_count: int = 0  # action tool calls already made in this conversation


@dataclass(frozen=True)
class OrderFacts:
    customer_id: int
    status: str
    payment_method: str
    total_paise: int
    delivered_at: datetime | None
    has_refund: bool


@dataclass(frozen=True)
class PolicyResult:
    decision: Decision
    reason: Reason
    explanation: str  # safe to relay to the customer
    urgency: Urgency | None = None

    @property
    def allowed(self) -> bool:
        return self.decision is Decision.ALLOW


def _allow() -> PolicyResult:
    return PolicyResult(Decision.ALLOW, Reason.OK, "Allowed.")


def _deny(reason: Reason, explanation: str) -> PolicyResult:
    return PolicyResult(Decision.DENY, reason, explanation)


def _approval(reason: Reason, explanation: str, urgency: Urgency) -> PolicyResult:
    return PolicyResult(Decision.NEEDS_APPROVAL, reason, explanation, urgency)


def check_account_access(actor: Actor) -> PolicyResult:
    if actor.verified_customer_id is None:
        return _deny(
            Reason.NOT_VERIFIED,
            "The customer must verify their identity (email, order number and delivery "
            "PIN code) before any account or order details can be shared or changed.",
        )
    return _allow()


def check_order_access(actor: Actor, order: OrderFacts) -> PolicyResult:
    access = check_account_access(actor)
    if not access.allowed:
        return access
    if order.customer_id != actor.verified_customer_id:
        # Same wording as "not found" so order numbers cannot be probed.
        return _deny(Reason.NOT_OWNER, "No order with that number exists on the verified account.")
    return _allow()


def _check_action_budget(actor: Actor, cfg: PolicyConfig) -> PolicyResult | None:
    if actor.action_count >= cfg.max_actions_per_conversation:
        return _approval(
            Reason.ACTION_LIMIT_REACHED,
            "This conversation has reached the number of account changes that can be made "
            "without a human review. A support specialist will take it from here.",
            Urgency.NORMAL,
        )
    return None


def check_refund(
    actor: Actor,
    order: OrderFacts,
    amount_paise: int,
    now: datetime,
    cfg: PolicyConfig = DEFAULT_POLICY,
) -> PolicyResult:
    access = check_order_access(actor, order)
    if not access.allowed:
        return access
    if order.has_refund or order.status == "refunded":
        return _deny(
            Reason.REFUND_ALREADY_EXISTS,
            "This order has already been refunded "
            "(or a refund is awaiting approval). Only one refund is possible per order.",
        )
    if order.status != "delivered" or order.delivered_at is None:
        return _deny(
            Reason.REFUND_NOT_DELIVERED,
            f"Refunds are only possible for delivered orders; this order is '{order.status}'. "
            "Processing orders can be cancelled instead.",
        )
    if now - order.delivered_at > timedelta(days=cfg.refund_window_days):
        return _deny(
            Reason.REFUND_WINDOW_EXPIRED,
            f"The {cfg.refund_window_days}-day refund window for this order has closed.",
        )
    if amount_paise <= 0:
        return _deny(Reason.REFUND_INVALID_AMOUNT, "The refund amount must be greater than zero.")
    if amount_paise > order.total_paise:
        return _deny(Reason.REFUND_EXCEEDS_TOTAL, "A refund cannot exceed the order total.")
    if (limited := _check_action_budget(actor, cfg)) is not None:
        return limited
    high = amount_paise > cfg.high_value_refund_paise
    if order.payment_method == "cod":
        return _approval(
            Reason.REFUND_COD_NEEDS_HUMAN,
            "Cash-on-delivery refunds are paid by bank transfer, so a specialist will collect "
            "bank details through a secure channel. Bank details must not be shared in chat.",
            Urgency.HIGH if high else Urgency.NORMAL,
        )
    if high:
        return _approval(
            Reason.REFUND_HIGH_VALUE,
            "Refunds of this value are reviewed by our refunds team before they are issued.",
            Urgency.HIGH,
        )
    if amount_paise > cfg.auto_refund_max_paise:
        return _approval(
            Reason.REFUND_NEEDS_APPROVAL,
            "This refund needs a quick approval from our refunds team before it is issued.",
            Urgency.NORMAL,
        )
    return _allow()


def check_cancel(
    actor: Actor, order: OrderFacts, cfg: PolicyConfig = DEFAULT_POLICY
) -> PolicyResult:
    access = check_order_access(actor, order)
    if not access.allowed:
        return access
    if order.status != "processing":
        hint = {
            "shipped": "It has already shipped; once delivered, a return/refund can be requested.",
            "delivered": "It has already been delivered; a return/refund may be possible instead.",
            "cancelled": "It is already cancelled.",
            "refunded": "It has already been refunded.",
        }.get(order.status, "")
        return _deny(
            Reason.CANCEL_NOT_PROCESSING,
            f"Only orders that are still processing can be cancelled. {hint}".strip(),
        )
    if (limited := _check_action_budget(actor, cfg)) is not None:
        return limited
    return _allow()


def check_address_change(
    actor: Actor, order: OrderFacts, cfg: PolicyConfig = DEFAULT_POLICY
) -> PolicyResult:
    access = check_order_access(actor, order)
    if not access.allowed:
        return access
    if order.status in ("cancelled", "refunded"):
        return _deny(
            Reason.ADDRESS_ORDER_CLOSED, "This order is closed, so its address can't change."
        )
    if order.status != "processing":
        return _deny(
            Reason.ADDRESS_ALREADY_SHIPPED,
            "The delivery address can only be changed before the order ships; this order has "
            "already shipped. The courier's own redirect options may still help.",
        )
    if (limited := _check_action_budget(actor, cfg)) is not None:
        return limited
    return _allow()
