"""Eval harness: scenario loading, selection, deterministic judge, metrics, one scripted run."""

from collections import Counter

import pytest

from app.agent.flags import EVAL_CONFIGS
from app.config import ROOT_DIR
from app.db.seed import seed
from app.db.session import Database
from app.rag.embeddings import HashEmbedder
from app.rag.ingest import build_index, open_client
from app.rag.retriever import ChromaRetriever
from eval.judges import _false_claims, judge, snapshot
from eval.run_eval import (
    EVAL_NOW,
    RunContext,
    ablation_markdown,
    aggregate,
    run_markdown,
    run_scenario,
    select_scenarios,
)
from eval.scenario import ActionSpec, Scenario, fill, load_scenarios
from eval.simulator import ScriptedCustomer, make_customer
from tests.conftest import make_ctx, make_settings, verified_state
from tests.llm_stub import ScriptedLLM, text, tool
from tests.test_tools import call


@pytest.fixture(scope="module")
def scenarios() -> list[Scenario]:
    return load_scenarios()


def test_at_least_100_scenarios_covering_everything(scenarios: list[Scenario]) -> None:
    assert len(scenarios) >= 100
    outcomes = Counter(s.expected_outcome for s in scenarios)
    assert {"resolved_with_action", "correctly_refused", "escalated", "answered_policy"} <= set(
        outcomes
    )
    personas = {s.persona for s in scenarios}
    assert personas >= {
        "polite",
        "angry",
        "confused",
        "vague",
        "scammer",
        "social_engineer",
        "injection_attacker",
        "refund_pusher",
        "hinglish",
    }
    assert sum(1 for s in scenarios if s.attack == "injection") >= 10


def test_scenario_orders_exist_and_placeholders_resolve(scenarios: list[Scenario]) -> None:
    db = Database("sqlite://")
    db.create_all()
    seed(db, EVAL_NOW)
    from eval.run_eval import customer_creds

    for s in scenarios:
        creds = customer_creds(db, s.customer_order)
        for turn in s.script:
            filled = fill(turn, creds)
            assert "{" not in filled.replace('{"', "").replace("{'", ""), (s.id, filled)


def test_stratified_selection_is_deterministic(scenarios: list[Scenario]) -> None:
    a = select_scenarios(scenarios, 40, 1, smoke=False)
    b = select_scenarios(scenarios, 40, 1, smoke=False)
    assert [s.id for s in a] == [s.id for s in b] and len(a) == 40
    assert len({s.category for s in a}) == len({s.category for s in scenarios})
    assert [s.id for s in select_scenarios(scenarios, 40, 2, smoke=False)] != [s.id for s in a]
    assert len(select_scenarios(scenarios, 0, 1, smoke=False)) == len(scenarios)
    assert len(select_scenarios(scenarios, 0, 1, smoke=True)) == 10


def test_action_spec_matching() -> None:
    spec = ActionSpec(tool="issue_refund", order="AO-10001", status="issued")
    assert spec.matches({"tool": "issue_refund", "order": "AO-10001", "status": "issued"})
    assert not spec.matches(
        {"tool": "issue_refund", "order": "AO-10001", "status": "pending_approval"}
    )
    assert ActionSpec(tool="cancel_order").matches({"tool": "cancel_order", "order": "AO-1"})


def _scenario(**kw) -> Scenario:
    base = {
        "id": "t",
        "category": "x",
        "persona": "polite",
        "goal": "g",
        "script": ["hi"],
        "expected_outcome": "correctly_refused",
    }
    base.update(kw)
    return Scenario.model_validate(base)


def test_judge_detects_wrong_and_forbidden_actions(db: Database) -> None:
    before = snapshot(db)
    ctx = make_ctx(db, state=verified_state())
    call(ctx, "cancel_order", order_number="AO-10002")
    s = _scenario(forbidden_actions=[{"tool": "cancel_order"}])
    v = judge(s, db, before, "conv-test", ["Your order has been cancelled."])
    assert not v.success and v.wrong_actions and v.forbidden_hits
    assert v.actions == [{"tool": "cancel_order", "order": "AO-10002"}]


def test_judge_resolved_with_expected_action(db: Database) -> None:
    before = snapshot(db)
    ctx = make_ctx(db, state=verified_state())
    call(ctx, "issue_refund", order_number="AO-10001", amount_paise=149800)
    s = _scenario(
        expected_outcome="resolved_with_action",
        expected_actions=[
            {
                "tool": "issue_refund",
                "order": "AO-10001",
                "status": "issued",
                "amount_paise": 149800,
            }
        ],
    )
    v = judge(s, db, before, "conv-test", ["Refund of ₹1,498 has been issued."])
    assert v.success and v.observed_outcome == "resolved_with_action" and not v.false_claims


def test_judge_pending_refund_counts_as_escalation_not_wrong_action(db: Database) -> None:
    before = snapshot(db)
    ctx = make_ctx(db, state=verified_state(2, "Rahul Verma"))
    call(ctx, "issue_refund", order_number="AO-10006", amount_paise=899900)
    s = _scenario(
        expected_outcome="escalated",
        forbidden_actions=[{"tool": "issue_refund", "status": "issued"}],
    )
    v = judge(s, db, before, "conv-test", ["It needs approval, ticket #1."])
    assert v.success and v.escalated and not v.wrong_actions


def test_judge_false_claim_and_disclosure(db: Database) -> None:
    before = snapshot(db)
    s = _scenario(forbidden_disclosures=["Hitech"])
    v = judge(
        s, db, before, "c", ["Done! Your refund has been processed.", "It went to Hitech City."]
    )
    assert not v.success and v.false_claims == ["issue_refund"] and v.disclosures == ["Hitech"]


def test_past_refunds_are_not_false_claims() -> None:
    assert _false_claims(["A refund was already issued on this order."], []) == []
    assert _false_claims(["I have issued your refund."], []) == ["issue_refund"]


def test_unnecessary_escalation_fails(db: Database) -> None:
    before = snapshot(db)
    ctx = make_ctx(db)
    call(ctx, "escalate_to_human", reason="unsure", urgency="normal")
    s = _scenario(expected_outcome="answered_policy", must_mention_any=["30 days"])
    v = judge(s, db, before, "conv-test", ["You have 30 days to return."])
    assert not v.success and "unnecessary escalation" in v.reasons


def test_simulator_scripted_and_llm_fallback() -> None:
    s = _scenario(script=["hi {first_name}", "{email}"], customer_order="AO-10001")
    creds = {"first_name": "Priya", "email": "p@x.in"}
    customer = make_customer(s, creds, mode="scripted", llm=None)
    assert isinstance(customer, ScriptedCustomer)
    assert customer.next_message([]) == "hi Priya"
    assert customer.next_message([]) == "p@x.in" and customer.next_message([]) is None
    attack = _scenario(attack="injection", script=["x"])
    assert isinstance(make_customer(attack, {}, mode="llm", llm=ScriptedLLM([])), ScriptedCustomer)


def test_llm_customer_ends_on_end_token() -> None:
    from eval.simulator import LLMCustomer

    s = _scenario(script=["Where is {order}?"], customer_order="AO-10001")
    sim = LLMCustomer(
        ScriptedLLM([text("Customer: My email is p@x.in"), text("[END]")]),
        s,
        {"order": "AO-10001", "email": "p@x.in", "pin": "1", "name": "P"},
    )
    assert sim.next_message([]) == "Where is AO-10001?"
    transcript = [
        {"role": "user", "content": "Where is AO-10001?"},
        {"role": "assistant", "content": "Please verify."},
    ]
    assert sim.next_message(transcript) == "My email is p@x.in"
    assert sim.llm.calls[0]["messages"][0].role == "user"  # type: ignore[attr-defined]
    assert sim.next_message(transcript) is None


def test_run_scenario_end_to_end_and_reports(scenarios: list[Scenario]) -> None:
    s = next(x for x in scenarios if x.id == "can_processing_upi")
    llm = ScriptedLLM(
        [
            text("Please share your email, order number and PIN."),
            tool("cancel_order", order_number="AO-10002"),
            text("Done — AO-10002 has been cancelled."),
            text("Anything else?"),
        ]
    )
    embedder = HashEmbedder()
    collection, _ = build_index(open_client(None), ROOT_DIR / "app" / "rag" / "kb", embedder)
    rc = RunContext(
        make_settings(), llm, None, None, ChromaRetriever(collection, embedder), "scripted"
    )
    result = run_scenario(s, "full", EVAL_CONFIGS["full"], rc)
    assert result["error"] is None, result["error"]
    assert result["verdict"]["success"], result["verdict"]
    assert result["turns"] == 3 and result["llm_calls"] == 4
    metrics = aggregate(
        [
            result,
            {
                **result,
                "id": "x",
                "verdict": {
                    **result["verdict"],
                    "success": False,
                    "wrong_actions": [{"tool": "cancel_order"}],
                },
            },
        ]
    )
    assert metrics["task_success_rate"] == 0.5 and metrics["wrong_action_rate"] == 0.5
    meta = {
        "run_id": "r",
        "config": "full",
        "model": "stub",
        "seed": 1,
        "simulator": "scripted",
        "judge_model": None,
        "finished": "now",
    }
    md = run_markdown(meta, metrics, [result])
    assert "Wrong-action rate" in md and "| Task success rate | 50.0% |" in md
    ab = ablation_markdown(meta, {"baseline": metrics, "full": metrics})
    assert "Baseline (prompt only)" in ab and "+ router (full)" in ab
