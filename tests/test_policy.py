"""Policy engine: every rule and every boundary."""

from datetime import timedelta

import pytest

from app.policy.engine import (
    Actor,
    Decision,
    OrderFacts,
    PolicyConfig,
    Reason,
    Urgency,
    check_account_access,
    check_address_change,
    check_cancel,
    check_order_access,
    check_refund,
)
from tests.conftest import FIXED_NOW

ME = Actor(verified_customer_id=7)
CFG = PolicyConfig()


def order(**kw: object) -> OrderFacts:
    base: dict[str, object] = {
        "customer_id": 7,
        "status": "delivered",
        "payment_method": "upi",
        "total_paise": 5_000_000,  # ₹50,000 so amount limits are what's under test
        "delivered_at": FIXED_NOW - timedelta(days=5),
        "has_refund": False,
    }
    base.update(kw)
    return OrderFacts(**base)  # type: ignore[arg-type]


def refund(amount: int, *, actor: Actor = ME, now=FIXED_NOW, **kw: object):
    return check_refund(actor, order(**kw), amount, now, CFG)


# --- verification & ownership ---------------------------------------------------------


def test_unverified_actor_denied_everywhere() -> None:
    anon = Actor(verified_customer_id=None)
    assert check_account_access(anon).reason is Reason.NOT_VERIFIED
    assert check_order_access(anon, order()).decision is Decision.DENY
    assert refund(100, actor=anon).reason is Reason.NOT_VERIFIED
    assert check_cancel(anon, order(status="processing")).reason is Reason.NOT_VERIFIED
    assert check_address_change(anon, order(status="processing")).reason is Reason.NOT_VERIFIED


def test_other_customers_order_denied_everywhere() -> None:
    theirs = {"customer_id": 99}
    assert check_order_access(ME, order(**theirs)).reason is Reason.NOT_OWNER
    assert refund(100, **theirs).reason is Reason.NOT_OWNER
    assert check_cancel(ME, order(status="processing", **theirs)).reason is Reason.NOT_OWNER
    assert check_address_change(ME, order(status="processing", **theirs)).reason is Reason.NOT_OWNER


def test_not_owner_message_does_not_confirm_existence() -> None:
    result = check_order_access(ME, order(customer_id=99))
    assert "No order with that number" in result.explanation


def test_own_order_allowed() -> None:
    assert check_order_access(ME, order()).decision is Decision.ALLOW


# --- refund: status, window, uniqueness, amount --------------------------------------------


@pytest.mark.parametrize("status", ["processing", "shipped", "cancelled"])
def test_refund_only_for_delivered(status: str) -> None:
    assert refund(100, status=status, delivered_at=None).reason is Reason.REFUND_NOT_DELIVERED


def test_refund_status_refunded_is_already_refunded() -> None:
    assert refund(100, status="refunded").reason is Reason.REFUND_ALREADY_EXISTS


def test_refund_at_most_once() -> None:
    assert refund(100, has_refund=True).reason is Reason.REFUND_ALREADY_EXISTS


@pytest.mark.parametrize(
    ("age", "decision"),
    [
        (timedelta(days=29), Decision.ALLOW),
        (timedelta(days=29, hours=23, minutes=59), Decision.ALLOW),
        (timedelta(days=30), Decision.ALLOW),  # inclusive boundary
        (timedelta(days=30, seconds=1), Decision.DENY),
        (timedelta(days=31), Decision.DENY),
        (timedelta(days=0), Decision.ALLOW),
    ],
)
def test_refund_window_boundaries(age: timedelta, decision: Decision) -> None:
    result = refund(100, delivered_at=FIXED_NOW - age)
    assert result.decision is decision
    if decision is Decision.DENY:
        assert result.reason is Reason.REFUND_WINDOW_EXPIRED


@pytest.mark.parametrize("amount", [0, -1, -200_000])
def test_refund_amount_must_be_positive(amount: int) -> None:
    assert refund(amount).reason is Reason.REFUND_INVALID_AMOUNT


def test_refund_amount_cannot_exceed_total() -> None:
    assert refund(150_001, total_paise=150_000).reason is Reason.REFUND_EXCEEDS_TOTAL
    assert refund(150_000, total_paise=150_000).decision is Decision.ALLOW


# --- refund: thresholds ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("amount", "decision", "reason", "urgency"),
    [
        (1, Decision.ALLOW, Reason.OK, None),
        (199_999, Decision.ALLOW, Reason.OK, None),
        (200_000, Decision.ALLOW, Reason.OK, None),  # exactly ₹2,000
        (
            200_001,
            Decision.NEEDS_APPROVAL,
            Reason.REFUND_NEEDS_APPROVAL,
            Urgency.NORMAL,
        ),  # ₹2,000.01
        (999_999, Decision.NEEDS_APPROVAL, Reason.REFUND_NEEDS_APPROVAL, Urgency.NORMAL),
        (
            1_000_000,
            Decision.NEEDS_APPROVAL,
            Reason.REFUND_NEEDS_APPROVAL,
            Urgency.NORMAL,
        ),  # ₹10,000
        (1_000_001, Decision.NEEDS_APPROVAL, Reason.REFUND_HIGH_VALUE, Urgency.HIGH),  # ₹10,000.01
        (4_500_000, Decision.NEEDS_APPROVAL, Reason.REFUND_HIGH_VALUE, Urgency.HIGH),
    ],
)
def test_refund_thresholds_prepaid(amount, decision, reason, urgency) -> None:
    result = refund(amount)
    assert (result.decision, result.reason, result.urgency) == (decision, reason, urgency)


@pytest.mark.parametrize("method", ["upi", "credit_card", "debit_card", "net_banking"])
def test_small_prepaid_refunds_allowed_for_all_prepaid_methods(method: str) -> None:
    assert refund(50_000, payment_method=method).decision is Decision.ALLOW


@pytest.mark.parametrize(
    ("amount", "urgency"),
    [
        (100, Urgency.NORMAL),
        (200_000, Urgency.NORMAL),
        (1_000_000, Urgency.NORMAL),
        (1_000_001, Urgency.HIGH),
    ],
)
def test_cod_refund_always_needs_human(amount: int, urgency: Urgency) -> None:
    result = refund(amount, payment_method="cod")
    assert result.decision is Decision.NEEDS_APPROVAL
    assert result.reason is Reason.REFUND_COD_NEEDS_HUMAN
    assert result.urgency is urgency


def test_cod_refund_still_subject_to_window_and_status() -> None:
    assert (
        refund(100, payment_method="cod", delivered_at=FIXED_NOW - timedelta(days=31)).reason
        is Reason.REFUND_WINDOW_EXPIRED
    )
    assert refund(100, payment_method="cod", status="shipped").reason is Reason.REFUND_NOT_DELIVERED


def test_explanations_never_leak_thresholds() -> None:
    for amount in (200_001, 1_000_001):
        text = refund(amount).explanation
        for leak in ("2,000", "2000", "10,000", "10000", "₹"):
            assert leak not in text


# --- cancel & address -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "decision"),
    [
        ("processing", Decision.ALLOW),
        ("shipped", Decision.DENY),
        ("delivered", Decision.DENY),
        ("cancelled", Decision.DENY),
        ("refunded", Decision.DENY),
    ],
)
def test_cancel_only_processing(status: str, decision: Decision) -> None:
    result = check_cancel(ME, order(status=status))
    assert result.decision is decision
    if decision is Decision.DENY:
        assert result.reason is Reason.CANCEL_NOT_PROCESSING


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        ("processing", Reason.OK),
        ("shipped", Reason.ADDRESS_ALREADY_SHIPPED),
        ("delivered", Reason.ADDRESS_ALREADY_SHIPPED),
        ("cancelled", Reason.ADDRESS_ORDER_CLOSED),
        ("refunded", Reason.ADDRESS_ORDER_CLOSED),
    ],
)
def test_address_change_only_before_shipping(status: str, reason: Reason) -> None:
    assert check_address_change(ME, order(status=status)).reason is reason


# --- action cap -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("count", "decision"), [(0, Decision.ALLOW), (2, Decision.ALLOW), (3, Decision.NEEDS_APPROVAL)]
)
def test_action_cap_applies_to_all_actions(count: int, decision: Decision) -> None:
    actor = Actor(verified_customer_id=7, action_count=count)
    assert check_cancel(actor, order(status="processing")).decision is decision
    assert check_address_change(actor, order(status="processing")).decision is decision
    assert check_refund(actor, order(), 100, FIXED_NOW, CFG).decision is decision


def test_action_cap_does_not_mask_real_denials() -> None:
    actor = Actor(verified_customer_id=7, action_count=10)
    assert check_cancel(actor, order(status="shipped")).reason is Reason.CANCEL_NOT_PROCESSING
    result = check_refund(actor, order(), 100, FIXED_NOW, CFG)
    assert result.reason is Reason.ACTION_LIMIT_REACHED


def test_thresholds_are_configurable() -> None:
    cfg = PolicyConfig(
        refund_window_days=7,
        auto_refund_max_paise=50_000,
        high_value_refund_paise=100_000,
        max_actions_per_conversation=1,
    )
    o = order(delivered_at=FIXED_NOW - timedelta(days=8))
    assert check_refund(ME, o, 100, FIXED_NOW, cfg).reason is Reason.REFUND_WINDOW_EXPIRED
    assert check_refund(ME, order(), 50_001, FIXED_NOW, cfg).reason is Reason.REFUND_NEEDS_APPROVAL
    assert check_refund(ME, order(), 100_001, FIXED_NOW, cfg).reason is Reason.REFUND_HIGH_VALUE
    busy = Actor(verified_customer_id=7, action_count=1)
    assert check_cancel(busy, order(status="processing"), cfg).reason is Reason.ACTION_LIMIT_REACHED
