"""Scenario schema and loader."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator

Outcome = Literal[
    "resolved_with_action",  # the right account change happened
    "correctly_refused",  # nothing changed, customer told why (or asked to re-verify)
    "escalated",  # a human ticket was created (incl. refunds pending approval)
    "answered_policy",  # policy question answered from the knowledge base
    "answered_lookup",  # order information answered (extension of the four spec outcomes)
]
Persona = Literal[
    "polite",
    "angry",
    "confused",
    "vague",
    "scammer",
    "social_engineer",
    "injection_attacker",
    "refund_pusher",
    "hinglish",
]
ActionTool = Literal["issue_refund", "cancel_order", "update_address"]

SCENARIO_DIR = Path(__file__).parent / "scenarios"


class ActionSpec(BaseModel):
    tool: ActionTool
    order: str = "*"  # "*" matches any order
    status: Literal["issued", "pending_approval"] | None = None  # refunds only
    amount_paise: int | None = None

    def matches(self, action: dict[str, object]) -> bool:
        if action["tool"] != self.tool:
            return False
        if self.order != "*" and action.get("order") != self.order:
            return False
        if self.status is not None and action.get("status") != self.status:
            return False
        return self.amount_paise is None or action.get("amount_paise") == self.amount_paise


class Scenario(BaseModel):
    id: str
    category: str
    persona: Persona
    customer_order: str | None = None  # whose credentials the customer holds
    goal: str
    hidden_facts: list[str] = Field(default_factory=list)
    language: Literal["en", "hinglish"] = "en"
    expected_outcome: Outcome
    acceptable_outcomes: list[Outcome] = Field(default_factory=list)
    expected_actions: list[ActionSpec] = Field(default_factory=list)
    forbidden_actions: list[ActionSpec] = Field(default_factory=list)
    expect_escalation: bool | None = None
    kb_sources: list[str] = Field(default_factory=list)
    must_mention_any: list[str] = Field(default_factory=list)
    forbidden_disclosures: list[str] = Field(default_factory=list)
    attack: Literal["injection", "social_engineering", "false_claim"] | None = None
    scripted_only: bool = False  # attacks use exact payloads, never paraphrased
    script: list[str]
    max_turns: int = 6

    @model_validator(mode="after")
    def _defaults(self) -> Scenario:
        if self.expect_escalation is None:
            if self.expected_outcome == "escalated":
                self.expect_escalation = True
            elif "escalated" not in self.acceptable_outcomes:
                self.expect_escalation = False
        if self.expected_outcome == "resolved_with_action" and not self.expected_actions:
            raise ValueError(f"{self.id}: resolved_with_action needs expected_actions")
        if self.expected_outcome == "answered_policy" and not self.must_mention_any:
            raise ValueError(f"{self.id}: answered_policy needs must_mention_any")
        if self.attack:
            self.scripted_only = True
        return self

    @property
    def outcomes(self) -> list[str]:
        return [self.expected_outcome, *self.acceptable_outcomes]


def load_scenarios(directory: Path = SCENARIO_DIR) -> list[Scenario]:
    scenarios: list[Scenario] = []
    for path in sorted(directory.glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        scenarios.extend(Scenario.model_validate(item) for item in data)
    ids = [s.id for s in scenarios]
    duplicates = {i for i in ids if ids.count(i) > 1}
    if duplicates:
        raise ValueError(f"duplicate scenario ids: {sorted(duplicates)}")
    return scenarios


def fill(template: str, creds: dict[str, str]) -> str:
    """Substitute {email} {order} {pin} {name} {first_name} placeholders."""
    out = template
    for key, value in creds.items():
        out = out.replace("{" + key + "}", value)
    return out
