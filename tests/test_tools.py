"""Tools: validation, policy enforcement inside the tool, DB side effects, structured results."""

from sqlalchemy import select

from app.agent.flags import AgentFlags
from app.db.models import Order, Refund, RefundStatus, Ticket
from app.db.session import Database
from app.rag.base import Passage
from app.tools import REGISTRY, available_tools, run_tool
from app.tools.base import normalise_order_number
from tests.conftest import make_ctx, verified_state

PRIYA = {"email": "priya.sharma@example.in", "order_number": "AO-10001", "pin_code": "560034"}


def call(ctx, name: str, **args):
    return run_tool(REGISTRY[name], ctx, args)


def get(db: Database, number: str) -> Order:
    with db.session() as s:
        o = s.scalars(select(Order).where(Order.order_number == number)).one()
        _ = o.refunds
        return o


# --- schemas & validation -------------------------------------------------------------------


def test_every_tool_has_object_schema() -> None:
    for spec in REGISTRY.values():
        schema = spec.input_schema()
        assert schema["type"] == "object"
        assert "title" not in schema


def test_available_tools_respects_flags() -> None:
    names = {t.name for t in available_tools(AgentFlags(rag=False))}
    assert "search_help_center" not in names
    no_actions = {t.name for t in available_tools(AgentFlags(), allow_actions=False)}
    assert not {"cancel_order", "issue_refund", "update_address"} & no_actions
    assert "escalate_to_human" in no_actions


def test_order_number_normalisation() -> None:
    for raw in ("AO-10001", "ao 10001", "AO10001", "#10001", "10001"):
        assert normalise_order_number(raw) == "AO-10001"


def test_invalid_arguments_are_structured(db: Database) -> None:
    ctx = make_ctx(db)
    res = call(ctx, "verify_customer", email="x", order_number="zzz", pin_code="012345")
    assert not res.ok and res.error_code == "INVALID_ARGUMENTS"
    assert "order_number" in res.message and "pin_code" in res.message


# --- verification ---------------------------------------------------------------------------


def test_verify_success_sets_state(db: Database) -> None:
    ctx = make_ctx(db)
    res = call(ctx, "verify_customer", **{**PRIYA, "email": "PRIYA.Sharma@Example.in "})
    assert res.ok and ctx.state.verified_customer_id == 1
    assert ctx.state.verified_name == "Priya Sharma"


def test_verify_wrong_pin_does_not_reveal_which_field(db: Database) -> None:
    ctx = make_ctx(db)
    res = call(ctx, "verify_customer", **{**PRIYA, "pin_code": "560035"})
    assert not res.ok and res.error_code == "VERIFICATION_FAILED"
    assert "PIN code" in res.message and "Do not reveal" in res.message
    assert ctx.state.failed_verifications == 1 and not ctx.state.is_verified


def test_verify_with_other_customers_order_fails(db: Database) -> None:
    ctx = make_ctx(db)
    res = call(ctx, "verify_customer", **{**PRIYA, "order_number": "AO-10020"})
    assert not res.ok and not ctx.state.is_verified


def test_verify_lockout_after_three_failures_escalates(db: Database) -> None:
    ctx = make_ctx(db)
    for _ in range(3):
        res = call(ctx, "verify_customer", **{**PRIYA, "pin_code": "111111"})
    assert res.error_code == "VERIFICATION_LOCKED"
    assert ctx.state.verification_locked and ctx.state.escalated
    # even correct details are refused once locked
    assert call(ctx, "verify_customer", **PRIYA).error_code == "VERIFICATION_LOCKED"
    with db.session() as s:
        ticket = s.scalars(select(Ticket)).one()
        assert ticket.reason_code == "VERIFICATION_FAILED"


def test_cannot_switch_identity_mid_chat(db: Database) -> None:
    ctx = make_ctx(db)
    assert call(ctx, "verify_customer", **PRIYA).ok
    res = call(
        ctx,
        "verify_customer",
        email="sneha.reddy@example.in",
        order_number="AO-10020",
        pin_code="500081",
    )
    assert res.error_code == "ALREADY_VERIFIED_OTHER" and ctx.state.verified_customer_id == 1


# --- reads ----------------------------------------------------------------------------------


def test_reads_require_verification(db: Database) -> None:
    ctx = make_ctx(db)
    assert call(ctx, "get_order", order_number="AO-10001").error_code == "NOT_VERIFIED"
    assert call(ctx, "list_orders").error_code == "NOT_VERIFIED"


def test_list_and_get_own_orders(db: Database) -> None:
    ctx = make_ctx(db, state=verified_state())
    listed = call(ctx, "list_orders")
    assert listed.ok
    numbers = {o["order_number"] for o in listed.data["orders"]}
    assert {"AO-10001", "AO-10002", "AO-10003", "AO-10004", "AO-10005"} <= numbers
    res = call(ctx, "get_order", order_number="ao-10001")
    assert res.ok and res.data["status"] == "delivered"
    assert res.data["total"] == "₹1,498"
    assert res.data["payment_method"] == "UPI"
    assert "IST" in res.data["delivered_on"]


def test_other_customers_order_looks_like_not_found(db: Database) -> None:
    ctx = make_ctx(db, state=verified_state())
    theirs = call(ctx, "get_order", order_number="AO-10020")
    missing = call(ctx, "get_order", order_number="AO-99999")
    assert theirs.error_code == missing.error_code == "ORDER_NOT_FOUND"
    assert theirs.message == missing.message
    assert "Sneha" not in theirs.model_dump_json()


# --- cancel ---------------------------------------------------------------------------------


def test_cancel_processing_order(db: Database) -> None:
    ctx = make_ctx(db, state=verified_state())
    res = call(ctx, "cancel_order", order_number="AO-10002", reason="changed mind")
    assert res.ok and "₹6,499" in res.message
    assert get(db, "AO-10002").status == "cancelled"
    assert ctx.state.actions[-1].outcome == "done"


def test_cancel_shipped_order_denied_with_reason(db: Database) -> None:
    ctx = make_ctx(db, state=verified_state())
    res = call(ctx, "cancel_order", order_number="AO-10003")
    assert not res.ok and res.error_code == "CANCEL_NOT_PROCESSING"
    assert "shipped" in res.message
    assert get(db, "AO-10003").status == "shipped"
    assert ctx.state.actions_denied[-1].reason_code == "CANCEL_NOT_PROCESSING"


def test_cancel_cod_order_message(db: Database) -> None:
    ctx = make_ctx(db, state=verified_state(3, "Ananya Iyer"))
    res = call(ctx, "cancel_order", order_number="AO-10011")
    assert res.ok and "cash on delivery" in res.message


# --- refunds --------------------------------------------------------------------------------


def test_small_refund_issued(db: Database) -> None:
    ctx = make_ctx(db, state=verified_state())
    res = call(ctx, "issue_refund", order_number="AO-10001", amount_paise=149800, reason="fit")
    assert res.ok and res.data["refund_status"] == RefundStatus.ISSUED
    order = get(db, "AO-10001")
    assert order.status == "refunded" and order.refunds[0].amount_paise == 149800


def test_refund_day_29_allowed_day_31_denied(db: Database) -> None:
    ctx = make_ctx(db, state=verified_state())
    assert call(ctx, "issue_refund", order_number="AO-10004", amount_paise=189900).ok
    res = call(ctx, "issue_refund", order_number="AO-10005", amount_paise=119900)
    assert res.error_code == "REFUND_WINDOW_EXPIRED"
    assert not get(db, "AO-10005").refunds


def test_partial_refund_keeps_status(db: Database) -> None:
    ctx = make_ctx(db, state=verified_state(4, "Arjun Mehta"))
    assert call(ctx, "issue_refund", order_number="AO-10013", amount_paise=200000).ok
    assert get(db, "AO-10013").status == "delivered"
    again = call(ctx, "issue_refund", order_number="AO-10013", amount_paise=100)
    assert again.error_code == "REFUND_ALREADY_EXISTS"


def test_mid_refund_needs_approval_creates_ticket_and_pending_refund(db: Database) -> None:
    ctx = make_ctx(db, state=verified_state(2, "Rahul Verma"))
    res = call(ctx, "issue_refund", order_number="AO-10006", amount_paise=899900)
    assert not res.ok and res.error_code == "REFUND_NEEDS_APPROVAL"
    assert "NOT issued" in res.message and res.data["escalated"]
    order = get(db, "AO-10006")
    assert order.status == "delivered"
    assert order.refunds[0].status == RefundStatus.PENDING_APPROVAL
    with db.session() as s:
        t = s.get(Ticket, res.data["ticket_id"])
        assert t.urgency == "normal" and t.summary["verified_identity"]["customer_id"] == 2
        assert "₹8,999" in t.summary["suggested_next_step"]
    assert ctx.state.escalated


def test_high_value_refund_high_urgency(db: Database) -> None:
    ctx = make_ctx(db, state=verified_state(2, "Rahul Verma"))
    res = call(ctx, "issue_refund", order_number="AO-10007", amount_paise=3299900)
    assert res.error_code == "REFUND_HIGH_VALUE"
    with db.session() as s:
        assert s.get(Ticket, res.data["ticket_id"]).urgency == "high"


def test_cod_refund_escalates_never_asks_bank_details(db: Database) -> None:
    ctx = make_ctx(db, state=verified_state(3, "Ananya Iyer"))
    res = call(ctx, "issue_refund", order_number="AO-10010", amount_paise=10000)
    assert res.error_code == "REFUND_COD_NEEDS_HUMAN"
    assert "must not be shared in chat" in res.message
    with db.session() as s:
        refunds = s.scalars(select(Refund).where(Refund.conversation_id == "conv-test")).all()
        assert [r.status for r in refunds] == [RefundStatus.PENDING_APPROVAL]


def test_refund_other_customers_order_denied(db: Database) -> None:
    ctx = make_ctx(db, state=verified_state())
    res = call(ctx, "issue_refund", order_number="AO-10020", amount_paise=100)
    assert res.error_code == "ORDER_NOT_FOUND"
    assert not get(db, "AO-10020").refunds


def test_refund_already_refunded(db: Database) -> None:
    ctx = make_ctx(db, state=verified_state(2, "Rahul Verma"))
    assert call(ctx, "issue_refund", order_number="AO-10008", amount_paise=100).error_code == (
        "REFUND_ALREADY_EXISTS"
    )


def test_refund_more_than_total_denied(db: Database) -> None:
    ctx = make_ctx(db, state=verified_state())
    res = call(ctx, "issue_refund", order_number="AO-10001", amount_paise=149801)
    assert res.error_code == "REFUND_EXCEEDS_TOTAL"


def test_unverified_refund_denied(db: Database) -> None:
    ctx = make_ctx(db)
    res = call(ctx, "issue_refund", order_number="AO-10001", amount_paise=100)
    assert res.error_code == "NOT_VERIFIED"
    assert not get(db, "AO-10001").refunds


def test_action_cap_escalates_fourth_action(db: Database) -> None:
    ctx = make_ctx(db, state=verified_state())
    call(ctx, "cancel_order", order_number="AO-10003")  # denied, still counts
    call(ctx, "cancel_order", order_number="AO-10005")  # denied
    call(
        ctx,
        "update_address",
        order_number="AO-10003",
        new_address={
            "name": "Priya",
            "line1": "1 MG Road",
            "city": "Bengaluru",
            "state": "Karnataka",
            "pin_code": "560001",
        },
    )
    res = call(ctx, "cancel_order", order_number="AO-10002")
    assert res.error_code == "ACTION_LIMIT_REACHED" and res.data["escalated"]
    assert get(db, "AO-10002").status == "processing"


# --- address --------------------------------------------------------------------------------

NEW_ADDR = {
    "name": "Priya Sharma",
    "line1": "22, 12th Main, HAL 2nd Stage",
    "line2": "Indiranagar",
    "city": "Bengaluru",
    "state": "Karnataka",
    "pin_code": "560038",
    "phone": "+91 98450 12345",
}


def test_update_address_before_shipping(db: Database) -> None:
    ctx = make_ctx(db, state=verified_state())
    res = call(ctx, "update_address", order_number="AO-10002", new_address=NEW_ADDR)
    assert res.ok
    order = get(db, "AO-10002")
    assert order.ship_pin == "560038" and order.address_updated_at is not None


def test_update_address_after_shipping_denied(db: Database) -> None:
    ctx = make_ctx(db, state=verified_state())
    res = call(ctx, "update_address", order_number="AO-10003", new_address=NEW_ADDR)
    assert res.error_code == "ADDRESS_ALREADY_SHIPPED"
    assert get(db, "AO-10003").ship_pin == "560034"


def test_update_address_validates_pin(db: Database) -> None:
    ctx = make_ctx(db, state=verified_state())
    res = call(
        ctx,
        "update_address",
        order_number="AO-10002",
        new_address={**NEW_ADDR, "pin_code": "56003"},
    )
    assert res.error_code == "INVALID_ARGUMENTS"
    assert ctx.state.action_count == 0  # invalid arguments never reach the action


# --- baseline (policy disabled, eval-only) --------------------------------------------------


def test_baseline_without_policy_executes_blindly(db: Database) -> None:
    ctx = make_ctx(db, flags=AgentFlags(policy=False, guardrails=False, rag=False, router=False))
    res = call(ctx, "issue_refund", order_number="AO-10020", amount_paise=799900)
    assert res.ok  # this is exactly the failure mode the policy engine prevents
    assert get(db, "AO-10020").status == "refunded"


# --- support tools --------------------------------------------------------------------------


class StubRetriever:
    def search(self, query: str, k: int = 4) -> list[Passage]:
        return [Passage("returns#window", "Returns", "30 days from delivery", 0.9)][:k]


def test_search_help_center(db: Database) -> None:
    ctx = make_ctx(db, retriever=StubRetriever())
    res = call(ctx, "search_help_center", query="return window")
    assert res.ok and res.data["passages"][0]["source_id"] == "returns#window"
    assert call(make_ctx(db), "search_help_center", query="x y").error_code == "KB_UNAVAILABLE"


def test_escalate_is_idempotent_and_raises_urgency(db: Database) -> None:
    ctx = make_ctx(db, state=verified_state())
    first = call(ctx, "escalate_to_human", reason="wants a human", urgency="normal")
    second = call(ctx, "escalate_to_human", reason="now threatening court", urgency="high")
    assert first.data["ticket_id"] == second.data["ticket_id"]
    with db.session() as s:
        t = s.get(Ticket, first.data["ticket_id"])
        assert t.urgency == "high"
        assert t.summary["verified_identity"]["email"] == "p***a@example.in"
        assert "suggested_next_step" in t.summary


def test_policy_decisions_are_traced(db: Database) -> None:
    ctx = make_ctx(db, state=verified_state())
    call(ctx, "cancel_order", order_number="AO-10003")
    kinds = [(e.kind, e.payload.get("decision")) for e in ctx.tracer.events]
    assert ("policy_decision", "DENY") in kinds
