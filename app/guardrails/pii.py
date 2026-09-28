"""PII masking for logs, traces and the admin panel.

Order matters: broader/longer patterns run first so that, for example, a +91
number is not half-eaten by the Aadhaar pattern. Over-masking is preferred to
under-masking; this text is never shown back to customers.
"""

from __future__ import annotations

import re
from typing import Any

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("[EMAIL]", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")),
    # UPI VPA: handle@bank with no dot in the bank part (emails were consumed above).
    ("[UPI_ID]", re.compile(r"\b[A-Za-z0-9._-]{2,64}@[A-Za-z][A-Za-z0-9]{1,63}\b")),
    ("[PHONE]", re.compile(r"(?:\+|\b)91[\s-]?[6-9]\d{4}[\s-]?\d{5}(?!\d)")),
    ("[CARD]", re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")),
    ("[AADHAAR]", re.compile(r"(?<!\d)[2-9]\d{3}[ -]?\d{4}[ -]?\d{4}(?!\d)")),
    ("[PHONE]", re.compile(r"(?<![\d₹])0?[6-9]\d{4}[\s-]?\d{5}(?!\d)")),
    ("[PAN]", re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b", re.IGNORECASE)),
    # Street address ending in a PIN code: up to 6 comma-separated segments + PIN.
    (
        "[ADDRESS]",
        re.compile(
            r"(?:[^\n,;:.!?]{1,60},\s*){1,6}[^\n,;:.!?]{0,40}?(?<![\d₹,.])"
            r"\b[1-9]\d{5}\b(?![\d,])"
        ),
    ),
    ("[PIN]", re.compile(r"(?<![\d₹,.])\b[1-9]\d{5}\b(?![\d,])")),
]

# Structured payload keys whose values are always fully redacted.
_SENSITIVE_KEYS = {
    "email",
    "phone",
    "ship_phone",
    "name",
    "ship_name",
    "line1",
    "line2",
    "ship_line1",
    "ship_line2",
    "address",
    "pin_code",
    "ship_pin",
    "upi_id",
    "card_number",
    "pan",
    "aadhaar",
    "bank_account",
    "shipping_address",
    "new_address",
    "verified_name",
    "verified_email",
    "customer_name",
    "customer_first_name",
}


def mask_pii(text: str) -> str:
    for label, pattern in _PATTERNS:
        text = pattern.sub(label, text)
    return text


def mask_structured(value: Any, _key: str | None = None) -> Any:
    """Recursively mask a JSON-like structure (dicts, lists, strings)."""
    if _key is not None and _key.lower() in _SENSITIVE_KEYS and value not in (None, "", {}):
        return f"[{_key.upper()}]"
    if isinstance(value, str):
        return mask_pii(value)
    if isinstance(value, dict):
        return {k: mask_structured(v, str(k)) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [mask_structured(v) for v in value]
    return value
