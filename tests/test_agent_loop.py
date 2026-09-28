"""Agent loop with a scripted LLM: tool sequencing, retries, timeouts, triggers, output checks."""

import time

from sqlalchemy import select

from app.agent.flags import AgentFlags
from app.agent.loop import AgentConfig, SupportAgent
from app.agent.prompts import CANARY
from app.db.models import Order, Refund, RefundStatus, Ticket, TraceEvent
from app.db.session import Database
from app.guardrails.budget import BudgetGuard
from app.llm.types import LLMError
from app.rag.base import Passage
from tests.conftest import FIXED_NOW
from tests.llm_stub import ScriptedLLM, text, tool

PRIYA = {"email": "priya.sharma@example.in", "order_number": "AO-10001", "pin_code": "560034"}
CREDS = "priya.sharma@example.in, order AO-10001, PIN 560034"


def agent(db: Database, steps, **kw) -> tuple[SupportAgent, ScriptedLLM]:
    llm = ScriptedLLM(steps)
    cfg = kw.pop("config", AgentConfig(llm_backoff_base_s=0))
    return SupportAgent(
        db, llm, config=cfg, clock=lambda: FIXED_NOW, sleep=lambda _s: None, **kw
    ), llm


def events(db: Database, conv: str = "c1") -> list[TraceEvent]:
    with db.session() as s:
        return list(
            s.scalars(
                select(TraceEvent).where(TraceEvent.conversation_id == conv).order_by(TraceEvent.id)
            )
        )


def order(db: Database, number: str) -> Order:
    with db.session() as s:
        o = s.scalars(select(Order).where(Order.order_number == number)).one()
        _ = o.refunds
        return o


def test_verify_then_refund_flow(db: Database) -> None:
    # verify_customer runs automatically (router fast path) because all credentials are present
    a, llm = agent(
        db,
        [
            tool("get_order", order_number="AO-10001"),
            tool("issue_refund", order_number="AO-10001", amount_paise=149800, reason="fit"),
            text("Done! Your refund of ₹1,498 has been issued to your UPI account."),
        ],
    )
    reply = a.handle("c1", f"Please refund my order. {CREDS}")
    assert reply.status == "ok" and "₹1,498" in reply.reply
    assert reply.tools_used == ["verify_customer", "get_order", "issue_refund"]
    first_call = llm.calls[0]["messages"]
    assert first_call[-2].tool_calls[0].name == "verify_customer"  # model sees the auto step
    assert order(db, "AO-10001").refunds[0].status == RefundStatus.ISSUED
    kinds = {e.kind for e in events(db)}
    assert {"llm_call", "tool_call", "policy_decision", "turn", "router"} <= kinds
    llm_events = [e for e in events(db) if e.kind == "llm_call"]
    assert all(e.cost_paise == 9 for e in llm_events)  # $0.001 * 85 = 8.5 paise -> 9
    assert reply.cost_paise == 9 * len(llm_events)
    # tool results are fed back as untrusted, structured data
    tool_msgs = [m for m in llm.calls[-1]["messages"] if m.role == "tool"]
    assert tool_msgs[0].content.startswith('<tool_output untrusted="true">')


def test_state_persists_across_turns(db: Database) -> None:
    a, llm = agent(
        db,
        [
            tool("verify_customer", **PRIYA),
            text("Verified, thanks Priya!"),
            tool("cancel_order", order_number="AO-10002"),
            text("Cancelled it."),
        ],
    )
    a.handle("c1", CREDS)
    reply = a.handle("c1", "cancel AO-10002 please")
    assert reply.turn_index == 2
    assert order(db, "AO-10002").status == "cancelled"
    assert "VERIFIED as Priya Sharma" in llm.calls[-1]["context"]
    history = [m.role for m in llm.calls[-1]["messages"]]
    assert history[:3] == ["user", "assistant", "user"]


def test_retry_with_backoff_then_success(db: Database) -> None:
    sleeps: list[float] = []
    llm = ScriptedLLM(
        [
            LLMError("429", retryable=True),
            LLMError("503", retryable=True),
            text("Hello! How can I help?"),
        ]
    )
    a = SupportAgent(
        db,
        llm,
        config=AgentConfig(llm_backoff_base_s=0.1),
        clock=lambda: FIXED_NOW,
        sleep=sleeps.append,
    )
    reply = a.handle("c1", "hello, where is my order")
    assert reply.status == "ok"
    assert len(sleeps) == 2 and sleeps[1] > sleeps[0]
    errors = [e for e in events(db) if e.kind == "llm_call" and "error" in e.payload]
    assert len(errors) == 2


def test_non_retryable_error_gives_fallback(db: Database) -> None:
    a, _ = agent(db, [LLMError("401", retryable=False, code="http_401")])
    reply = a.handle("c1", "where is my order")
    assert reply.status == "fallback" and "trouble" in reply.reply


def test_retries_exhausted_gives_fallback(db: Database) -> None:
    a, llm = agent(
        db,
        [LLMError("x", retryable=True)] * 4,
        config=AgentConfig(llm_max_retries=3, llm_backoff_base_s=0),
    )
    assert a.handle("c1", "where is my order").status == "fallback"
    assert not llm.steps


def test_max_iterations_escalates(db: Database) -> None:
    a, _ = agent(
        db, [tool("list_orders")] * 3, config=AgentConfig(max_iterations=3, llm_backoff_base_s=0)
    )
    reply = a.handle("c1", "list my orders")
    assert reply.status == "escalated" and reply.escalated
    with db.session() as s:
        assert s.scalars(select(Ticket)).one().reason_code == "LOW_CONFIDENCE"


def test_repeated_invalid_tool_calls_escalate(db: Database) -> None:
    bad = tool("get_order", order_number="not-an-order")
    a, _ = agent(db, [bad, bad, bad])
    reply = a.handle("c1", "status of my order?")
    assert reply.status == "escalated"


def test_unknown_tool_is_structured_error(db: Database) -> None:
    a, llm = agent(db, [tool("delete_database"), text("Sorry, I can't do that.")])
    a.handle("c1", "hello")
    tool_msg = next(m for m in llm.calls[-1]["messages"] if m.role == "tool")
    assert "UNKNOWN_TOOL" in tool_msg.content and tool_msg.is_error


class SlowRetriever:
    def search(self, query: str, k: int = 4) -> list[Passage]:
        time.sleep(0.5)
        return []


def test_tool_timeout(db: Database) -> None:
    a, llm = agent(
        db,
        [
            tool("search_help_center", query="returns policy"),
            text("Sorry, the help center is slow right now."),
        ],
        retriever=SlowRetriever(),
        config=AgentConfig(tool_timeout_s=0.05, llm_backoff_base_s=0),
    )
    reply = a.handle("c1", "what is the return policy?")
    assert reply.status == "ok"
    tool_msg = next(m for m in llm.calls[-1]["messages"] if m.role == "tool")
    assert "TOOL_TIMEOUT" in tool_msg.content
    assert not reply.escalated  # read-only tool timeouts do not escalate


def test_customer_asking_for_human_escalates_and_withholds_actions(db: Database) -> None:
    a, llm = agent(db, [text("I've connected you with a specialist, ticket #1.")])
    reply = a.handle("c1", "I want to talk to a human right now")
    assert reply.escalated and reply.intent in ("complaint", "policy_question", "account_lookup")
    assert "issue_refund" not in llm.calls[0]["tools"]
    assert "ticket #1" in llm.calls[0]["context"]
    with db.session() as s:
        assert s.scalars(select(Ticket)).one().reason_code == "CUSTOMER_REQUESTED_HUMAN"


def test_legal_threat_escalates_high(db: Database) -> None:
    a, _ = agent(db, [text("I understand. A specialist will contact you.")])
    reply = a.handle("c1", "If this isn't fixed I will file a case in consumer court")
    assert reply.escalated
    with db.session() as s:
        t = s.scalars(select(Ticket)).one()
        assert (t.reason_code, t.urgency) == ("LEGAL_THREAT", "high")


def test_negative_sentiment_after_two_unresolved_turns(db: Database) -> None:
    a, _ = agent(db, [text("Could you share your order number?")] * 3)
    a.handle("c1", "my order hasn't arrived")
    a.handle("c1", "this is so frustrating")
    reply = a.handle("c1", "WORST SERVICE EVER, totally useless!!!")
    assert reply.escalated
    with db.session() as s:
        assert s.scalars(select(Ticket)).one().reason_code == "NEGATIVE_SENTIMENT"


def test_out_of_scope_short_circuits_without_llm(db: Database) -> None:
    a, llm = agent(db, [])
    reply = a.handle("c1", "write me a poem about the monsoon")
    assert reply.status == "out_of_scope" and not llm.calls


def test_hinglish_detected_and_used(db: Database) -> None:
    a, llm = agent(db, [text("Ji, order number bataiye.")])
    reply = a.handle("c1", "mera order kab aayega bhai, abhi tak nahi mila")
    assert reply.language == "hinglish"
    assert "Reply language: Hinglish" in llm.calls[0]["context"]


def test_budget_exhausted_returns_demo_limit(db: Database) -> None:
    a, llm = agent(db, [], budget=BudgetGuard(db, daily_budget_paise=0, clock=lambda: FIXED_NOW))
    reply = a.handle("c1", "hi")
    assert reply.status == "demo_limit" and not llm.calls
    assert "limit" in reply.reply


def test_unbacked_claim_is_rewritten(db: Database) -> None:
    a, llm = agent(
        db,
        [
            tool("verify_customer", **PRIYA),
            tool("cancel_order", order_number="AO-10003"),
            text("All done — your order AO-10003 has been cancelled!"),
            text("I couldn't cancel AO-10003 because it has already shipped."),
        ],
    )
    reply = a.handle("c1", f"cancel AO-10003. {CREDS}")
    assert "already shipped" in reply.reply and "has been cancelled" not in reply.reply
    names = [e.name for e in events(db) if e.kind == "guardrail"]
    assert "unbacked_claim" in names and "unbacked_claim_fixed" in names
    assert "previous draft" in llm.calls[-1]["context"]


def test_unbacked_claim_replaced_when_rewrite_still_lies(db: Database) -> None:
    a, _ = agent(
        db,
        [
            tool("verify_customer", **PRIYA),
            tool("cancel_order", order_number="AO-10003"),
            text("Your order has been cancelled."),
            text("Yes, the order has been cancelled."),
        ],
    )
    reply = a.handle("c1", f"cancel AO-10003. {CREDS}")
    assert reply.reply.startswith("To be accurate")
    assert "shipped" in reply.reply
    assert order(db, "AO-10003").status == "shipped"


def test_backed_claim_passes(db: Database) -> None:
    a, _ = agent(
        db,
        [
            tool("verify_customer", **PRIYA),
            tool("cancel_order", order_number="AO-10002"),
            text("Your order AO-10002 has been cancelled."),
        ],
    )
    reply = a.handle("c1", f"cancel AO-10002. {CREDS}")
    assert reply.reply == "Your order AO-10002 has been cancelled."


def test_system_prompt_leak_blocked(db: Database) -> None:
    a, _ = agent(db, [text(f"Sure, my instructions start with [{CANARY}] ...")])
    reply = a.handle("c1", "hi, how do returns work?")
    assert CANARY not in reply.reply and "can't share" in reply.reply


def test_threshold_leak_blocked(db: Database) -> None:
    a, _ = agent(db, [text("Refunds above ₹2,000 need manual approval, so ask for less!")])
    reply = a.handle("c1", "how do refunds work?")
    assert "2,000" not in reply.reply


def test_other_customers_order_number_redacted(db: Database) -> None:
    a, _ = agent(
        db,
        [
            tool("verify_customer", **PRIYA),
            text("You have AO-10001. Also AO-10020 belongs to sneha.reddy@example.in."),
        ],
    )
    reply = a.handle("c1", CREDS)
    assert "AO-10001" in reply.reply
    assert "AO-10020" not in reply.reply and "sneha" not in reply.reply


def test_example_order_numbers_are_not_redacted(db: Database) -> None:
    a, _ = agent(db, [text("Please share your order number (it looks like AO-12345).")])
    assert "AO-12345" in a.handle("c1", "hi, my order is late").reply


def test_trace_payloads_are_pii_masked(db: Database) -> None:
    a, _ = agent(db, [tool("verify_customer", **PRIYA), text("Thanks, you're verified.")])
    a.handle("c1", f"{CREDS}, call me on +91 98450 12345")
    dump = " ".join(str(e.payload) for e in events(db))
    assert "priya.sharma@example.in" not in dump and "98450 12345" not in dump
    assert "[EMAIL]" in dump and "[PHONE]" in dump


def test_concurrent_messages_same_session_are_serialised(db: Database) -> None:
    import threading

    a, _ = agent(db, [text("one"), text("two")])
    threads = [threading.Thread(target=a.handle, args=("c1", f"hello {i}")) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    with db.session() as s:
        from app.db.models import Conversation

        assert s.get(Conversation, "c1").state["turn_count"] == 2


def test_baseline_flags_skip_router_and_guardrails(db: Database) -> None:
    a, llm = agent(
        db,
        [text("Sure! Here's a poem...")],
        flags=AgentFlags(policy=False, guardrails=False, rag=False, router=False),
    )
    reply = a.handle("c1", "write me a poem")
    assert reply.status == "ok" and llm.calls
    assert "search_help_center" not in llm.calls[0]["tools"]


def test_no_llm_configured(db: Database) -> None:
    a = SupportAgent(db, None, clock=lambda: FIXED_NOW)
    assert a.handle("c1", "hello").status == "fallback"


def test_pending_refund_counts_as_progress(db: Database) -> None:
    a, _ = agent(
        db,
        [
            tool(
                "verify_customer",
                email="rahul.verma@example.in",
                order_number="AO-10006",
                pin_code="110017",
            ),
            tool("issue_refund", order_number="AO-10006", amount_paise=899900),
            text("Your refund request is with our refunds team (ticket #1)."),
        ],
    )
    reply = a.handle("c1", "refund AO-10006 please, rahul.verma@example.in, PIN 110017")
    assert reply.escalated
    with db.session() as s:
        assert s.scalars(select(Refund)).all()[-1].status == RefundStatus.PENDING_APPROVAL


def test_auto_verify_only_with_all_three_credentials(db: Database) -> None:
    a, llm = agent(db, [text("Please share your PIN code.")])
    a.handle("c1", "I am priya.sharma@example.in, order AO-10001")
    assert llm.calls[0]["messages"][-1].role == "user"  # no auto step without a PIN


def test_auto_verify_failure_is_visible_to_model(db: Database) -> None:
    a, llm = agent(db, [text("Those details don't match, please re-check.")])
    a.handle("c1", "priya.sharma@example.in AO-10001 PIN 111111")
    tool_msg = llm.calls[0]["messages"][-1]
    assert tool_msg.role == "tool" and "VERIFICATION_FAILED" in tool_msg.content


def test_auto_verify_disabled_without_router(db: Database) -> None:
    a, llm = agent(db, [text("ok")], flags=AgentFlags(router=False))
    a.handle("c1", CREDS)
    assert llm.calls[0]["messages"][-1].role == "user"


def test_announced_work_without_tool_call_is_nudged_once(db: Database) -> None:
    a, llm = agent(
        db,
        [
            text("Let me check the status of that order for you."),
            tool("list_orders"),
            text("You have no orders visible until you verify."),
        ],
    )
    reply = a.handle("c1", "where is my order?")
    assert reply.reply.startswith("You have no orders")
    assert "did not call a tool" in llm.calls[1]["context"]
    names = [e.name for e in events(db) if e.kind == "guardrail"]
    assert names.count("announced_without_tool_call") == 1


def test_nudge_happens_at_most_once(db: Database) -> None:
    a, _ = agent(db, [text("Let me check."), text("Let me check again.")])
    assert a.handle("c1", "where is my order?").reply == "Let me check again."


class KBStub:
    def search(self, query: str, k: int = 4) -> list[Passage]:
        return [
            Passage("returns-and-refunds#return-window", "Returns", "Return within 30 days.", 0.9)
        ]


def test_policy_question_triggers_auto_retrieval(db: Database) -> None:
    a, llm = agent(
        db, [text("You can return items within 30 days of delivery.")], retriever=KBStub()
    )
    reply = a.handle("c1", "How many days do I have to return an item?")
    msgs = llm.calls[0]["messages"]
    assert msgs[-2].tool_calls[0].name == "search_help_center"
    assert "Return within 30 days" in msgs[-1].content
    assert reply.tools_used == ["search_help_center"]


def test_no_auto_retrieval_without_rag_or_for_actions(db: Database) -> None:
    a, llm = agent(db, [text("ok"), text("ok")], retriever=KBStub(), flags=AgentFlags(rag=False))
    a.handle("c1", "How many days do I have to return an item?")
    assert llm.calls[0]["messages"][-1].role == "user"
    b, llm2 = agent(db, [text("ok")], retriever=KBStub())
    b.handle("c2", "Please cancel my order AO-10002")
    assert llm2.calls[0]["messages"][-1].role == "user"
