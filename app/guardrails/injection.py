"""Prompt-injection heuristics (layer 1 of 4).

This layer only *flags*. On a flagged turn the agent loop withholds action tools,
so even a successful jailbreak of the model cannot move money; the policy engine
inside each tool is the final, strongest layer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_I = re.IGNORECASE
SUSPECT_THRESHOLD = 0.4
BLOCK_THRESHOLD = 1.0

_RULES: list[tuple[str, float, re.Pattern[str]]] = [
    (
        "override_instructions",
        0.6,
        re.compile(
            r"\b(?:ignore|disregard|forget|override|bypass)\b[^.\n]{0,40}\b(?:instructions?|rules|"
            r"prompt|guidelines|polic(?:y|ies)|restrictions|guardrails)\b",
            _I,
        ),
    ),
    (
        "role_hijack",
        0.4,
        re.compile(
            r"\b(?:you are now|from now on,? you|act as (?:an?|the)|pretend (?:to be|you are)|"
            r"roleplay as|new persona|you are no longer)\b",
            _I,
        ),
    ),
    (
        "prompt_extraction",
        0.5,
        re.compile(
            r"\b(?:system prompt|initial prompt|hidden (?:prompt|instructions)|(?:reveal|show|print|"
            r"repeat|output|dump)\b[^.\n]{0,30}\b(?:prompt|instructions|rules|configuration|"
            r"guidelines))\b",
            _I,
        ),
    ),
    (
        "jailbreak_keyword",
        0.6,
        re.compile(r"\b(?:developer mode|jailbreak|DAN|do anything now|god mode|sudo mode)\b"),
    ),
    (
        "fake_role_marker",
        0.5,
        re.compile(
            r"(?:^|\n)\s*(?:system|assistant|developer|tool)\s*:|<\/?(?:system|assistant|tool)>|"
            r"\[(?:system|admin)\]|BEGIN (?:SYSTEM|ADMIN)|<\|im_start\|>|###\s*(?:system|instruction)",
            _I,
        ),
    ),
    (
        "authority_claim",
        0.4,
        re.compile(
            r"\b(?:i am|i'm|this is)\s+(?:the|an?|your)?\s*(?:admin|administrator|developer|ceo|"
            r"acme (?:employee|staff|manager)|support (?:lead|staff)|security team|system)\b|"
            r"\b(?:admin override|override code|authori[sz]ed by (?:acme|management)|internal test)",
            _I,
        ),
    ),
    (
        "policy_bypass",
        0.4,
        re.compile(
            r"\b(?:skip|without|no need (?:for|to))\b[^.\n]{0,20}\bverif\w*|\bapprove (?:it|this) "
            r"anyway\b|\bpolic(?:y|ies) (?:does not|doesn't|don't) apply\b|\bspecial exception\b",
            _I,
        ),
    ),
    (
        "tool_syntax",
        0.4,
        re.compile(
            r"\b(?:issue_refund|cancel_order|update_address|verify_customer)\s*\(|\"amount_paise\"|"
            r"\bcall the (?:tool|function)\b|\btool_use\b|\bfunction_call\b",
            _I,
        ),
    ),
    (
        "hinglish_override",
        0.6,
        re.compile(
            r"\b(?:pichle|pehle|saare|sab)\b[^.\n]{0,25}\b(?:instructions?|rules|niyam)\b[^.\n]{0,20}"
            r"\b(?:ignore|bhool|bhul|chhod)\w*|\brules?\b[^.\n]{0,10}\b(?:ignore|bhool\w*|chhod\w*)\s+"
            r"(?:karo|kar do|jao|do)\b|\bverification\b[^.\n]{0,15}\b(?:skip|mat|rehne do)\b",
            _I,
        ),
    ),
    ("encoded_blob", 0.3, re.compile(r"[A-Za-z0-9+/]{80,}={0,2}")),
]


@dataclass
class InjectionResult:
    score: float
    labels: list[str] = field(default_factory=list)

    @property
    def suspected(self) -> bool:
        return self.score >= SUSPECT_THRESHOLD

    @property
    def block(self) -> bool:
        return self.score >= BLOCK_THRESHOLD


def detect_injection(text: str) -> InjectionResult:
    labels = [label for label, _w, rx in _RULES if rx.search(text)]
    weights = {label: w for label, w, _rx in _RULES}
    score = min(1.0, round(sum(weights[label] for label in labels), 2))
    return InjectionResult(score, labels)
