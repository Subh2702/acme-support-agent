"""Money helpers. All money is integer paise; conversion to text happens only at the edges.

Why paise: binary floats cannot represent 0.10 exactly, so float arithmetic on
rupees drifts (0.1 + 0.2 != 0.3). Integer paise make every comparison exact,
which matters for policy boundaries such as "refund <= ₹2,000".
"""

from __future__ import annotations

import re
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal, InvalidOperation

PAISE_PER_RUPEE = 100
_AMOUNT_RE = re.compile(r"(?:₹|rs\.?|inr)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)", re.IGNORECASE)


def rupees_to_paise(value: str | int | Decimal) -> int:
    """Convert a rupee amount to integer paise. Floats are rejected on purpose."""
    if isinstance(value, (bool, float)):
        raise TypeError("Use str, int or Decimal for money, never float")
    if isinstance(value, int):
        return value * PAISE_PER_RUPEE
    try:
        amount = Decimal(str(value).replace(",", "").replace("₹", "").strip())
    except InvalidOperation as exc:
        raise ValueError(f"Not a valid rupee amount: {value!r}") from exc
    paise = amount * PAISE_PER_RUPEE
    if paise != paise.to_integral_value():
        raise ValueError(f"Amount has more than 2 decimal places: {value!r}")
    return int(paise)


def paise_to_rupees(paise: int) -> Decimal:
    return (Decimal(paise) / PAISE_PER_RUPEE).quantize(Decimal("0.01"))


def _group_indian(digits: str) -> str:
    """Group an integer string the Indian way: last 3 digits, then pairs (1,25,000)."""
    if len(digits) <= 3:
        return digits
    head, tail = digits[:-3], digits[-3:]
    pairs = []
    while len(head) > 2:
        pairs.insert(0, head[-2:])
        head = head[:-2]
    if head:
        pairs.insert(0, head)
    return ",".join([*pairs, tail])


def format_inr(paise: int, *, always_show_paise: bool = False) -> str:
    """Format paise as ₹ with Indian digit grouping, e.g. 12500050 -> '₹1,25,000.50'.

    Whole-rupee amounts omit '.00' unless `always_show_paise` is set.
    """
    if not isinstance(paise, int) or isinstance(paise, bool):
        raise TypeError("format_inr expects integer paise")
    sign = "-" if paise < 0 else ""
    rupees, rem = divmod(abs(paise), PAISE_PER_RUPEE)
    text = f"{sign}₹{_group_indian(str(rupees))}"
    if rem or always_show_paise:
        text += f".{rem:02d}"
    return text


def parse_inr(text: str) -> int | None:
    """Find the first explicit rupee amount ('₹1,999.50', 'Rs 500', 'INR 2000') in text."""
    match = _AMOUNT_RE.search(text)
    if not match:
        return None
    try:
        return rupees_to_paise(match.group(1))
    except ValueError:
        return None


def usd_to_paise(usd: Decimal, usd_inr_rate: Decimal | float | str) -> int:
    """Convert a USD cost to paise, rounding *up* so budget checks never under-count."""
    rate = Decimal(str(usd_inr_rate))
    paise = usd * rate * PAISE_PER_RUPEE
    return int(paise.to_integral_value(rounding=ROUND_CEILING))


def token_cost_usd(
    input_tokens: int,
    output_tokens: int,
    input_usd_per_mtok: Decimal,
    output_usd_per_mtok: Decimal,
) -> Decimal:
    million = Decimal(1_000_000)
    return (
        Decimal(input_tokens) * input_usd_per_mtok + Decimal(output_tokens) * output_usd_per_mtok
    ) / million


def inr_to_paise(rupees: float | int | str | Decimal) -> int:
    """For configuration values (e.g. DAILY_BUDGET_INR) that arrive as numbers from env."""
    amount = Decimal(str(rupees)) * PAISE_PER_RUPEE
    return int(amount.quantize(Decimal(1), rounding=ROUND_HALF_UP))
