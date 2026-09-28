"""Checks on the agent's final reply before the customer sees it.

1. Unbacked action claims: the reply must not say something was cancelled,
   refunded or changed unless a tool call actually did it.
2. Leaks: the system prompt (canary / verbatim fragments) or internal thresholds.
3. Other customers' data: order numbers or emails that belong to someone else.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.agent.prompts import CANARY, STATIC_SYSTEM_PROMPT

_I = re.IGNORECASE

CLAIM_PATTERNS: dict[str, list[re.Pattern[str]]] = {
    "issue_refund": [
        re.compile(
            r"\b(?:i|we)(?:'ve| have)?\s+(?:just\s+|now\s+|successfully\s+)?"
            r"(?:issued|processed|initiated|credited|approved)\b[^.\n]{0,40}\brefund",
            _I,
        ),
        re.compile(
            r"\brefund\b[^.\n]{0,40}\b(?:has been|have been|is now|was|is being)\s+"
            r"(?:successfully\s+)?(?:issued|processed|initiated|credited|approved|"
            r"completed)\b",
            _I,
        ),
        re.compile(r"\b(?:i|we)(?:'ve| have)\s+refunded\b", _I),
        re.compile(
            r"\b(?:refund|paisa|paise|amount)\b[^.\n]{0,40}\b(?:kar diya|ho gaya|"
            r"bhej diya|process kar diya|initiate kar diya)\b",
            _I,
        ),
    ],
    "cancel_order": [
        re.compile(
            r"\b(?:order|it)\b[^.\n]{0,30}\b(?:has been|is now|was|is)\s+"
            r"(?:successfully\s+)?cancell?ed\b",
            _I,
        ),
        re.compile(r"\b(?:i|we)(?:'ve| have)\s+(?:successfully\s+)?cancell?ed\b", _I),
        re.compile(r"\bcancel\b[^.\n]{0,20}\b(?:ho gaya|kar diya|ho chuka)\b", _I),
    ],
    "update_address": [
        re.compile(
            r"\baddress\b[^.\n]{0,30}\b(?:has been|is now|was)\s+(?:successfully\s+)?"
            r"(?:updated|changed)\b",
            _I,
        ),
        re.compile(r"\b(?:i|we)(?:'ve| have)\s+(?:updated|changed)\b[^.\n]{0,20}\baddress\b", _I),
        re.compile(
            r"\baddress\b[^.\n]{0,25}\b(?:update|change|badal)\s+(?:ho gaya|kar diya)\b", _I
        ),
    ],
}

THRESHOLD_LEAK_RE = re.compile(
    r"(?:(?:₹|rs\.?\s?|inr\s?)?\b(?:2,000|2000|10,000|10000)\b[^.\n]{0,50}\b(?:approv\w*|"
    r"auto\w*|threshold|limit|manual review|review)\b|\b(?:approv\w*|auto\w*|threshold|limit)"
    r"\b[^.\n]{0,50}(?:₹|rs\.?\s?|inr\s?)?\b(?:2,000|2000|10,000|10000)\b)",
    _I,
)
ORDER_RE = re.compile(r"\bAO-\d{5}\b", _I)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")


def _shingles(text: str, n: int = 9) -> set[str]:
    words = re.findall(r"[a-z0-9']+", text.lower())
    return {" ".join(words[i : i + n]) for i in range(len(words) - n + 1)}


_PROMPT_SHINGLES = _shingles(STATIC_SYSTEM_PROMPT)


@dataclass
class ReplyFacts:
    """What actually happened, from the trace and state (not from the model)."""

    done_tools: set[str] = field(default_factory=set)  # action tools that succeeded
    observed: set[str] = field(default_factory=set)  # e.g. refund_on_record, order_cancelled
    allowed_orders: set[str] = field(default_factory=set)
    known_orders: set[str] | None = None  # real order numbers; None = treat all as real
    allowed_emails: set[str] = field(default_factory=set)


@dataclass
class OutputCheckResult:
    reply: str
    unbacked_claims: list[str] = field(default_factory=list)
    leaks: list[str] = field(default_factory=list)
    redactions: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (self.unbacked_claims or self.leaks or self.redactions)


BACKING_FACTS = {
    "issue_refund": "refund_on_record",
    "cancel_order": "order_cancelled",
    "update_address": "address_on_record",
}


def find_unbacked_claims(reply: str, facts: ReplyFacts) -> list[str]:
    claims = []
    for tool, patterns in CLAIM_PATTERNS.items():
        if any(p.search(reply) for p in patterns):
            if tool in facts.done_tools or BACKING_FACTS[tool] in facts.observed:
                continue
            claims.append(tool)
    return claims


def find_leaks(reply: str) -> list[str]:
    leaks = []
    if CANARY.lower() in reply.lower():
        leaks.append("canary")
    if _shingles(reply) & _PROMPT_SHINGLES:
        leaks.append("system_prompt_fragment")
    if THRESHOLD_LEAK_RE.search(reply):
        leaks.append("internal_threshold")
    return leaks


def redact_foreign_data(reply: str, facts: ReplyFacts) -> tuple[str, list[str]]:
    redactions: list[str] = []
    allowed_orders = {o.upper() for o in facts.allowed_orders}
    allowed_emails = {e.lower() for e in facts.allowed_emails}

    known = {o.upper() for o in facts.known_orders} if facts.known_orders is not None else None

    def order_sub(m: re.Match[str]) -> str:
        number = m.group(0).upper()
        if number in allowed_orders or (known is not None and number not in known):
            return m.group(0)  # the customer's own order, or a made-up example
        redactions.append(f"order:{m.group(0).upper()}")
        return "[order withheld]"

    def email_sub(m: re.Match[str]) -> str:
        if m.group(0).lower() in allowed_emails:
            return m.group(0)
        redactions.append("email")
        return "[email withheld]"

    reply = ORDER_RE.sub(order_sub, reply)
    reply = EMAIL_RE.sub(email_sub, reply)
    return reply, redactions


def check_reply(reply: str, facts: ReplyFacts) -> OutputCheckResult:
    redacted, redactions = redact_foreign_data(reply, facts)
    return OutputCheckResult(
        reply=redacted,
        unbacked_claims=find_unbacked_claims(reply, facts),
        leaks=find_leaks(reply),
        redactions=redactions,
    )
