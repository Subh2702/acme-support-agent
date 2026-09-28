"""Token prices (USD per million tokens) and conversion of call cost to paise.

Anthropic prices are first-party list prices. Hugging Face router prices are
read live from the router's model listing for the provider that served the call
(see `HuggingFaceClient`), so they are not hard-coded here.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.llm.types import Usage
from app.money import usd_to_paise


@dataclass(frozen=True)
class Price:
    input_usd_per_mtok: Decimal
    output_usd_per_mtok: Decimal
    cache_read_multiplier: Decimal = Decimal("0.1")
    cache_write_multiplier: Decimal = Decimal("1.25")


ANTHROPIC_PRICES: dict[str, Price] = {
    "claude-fable-5-1": Price(Decimal(10), Decimal(50)),
    "claude-opus-5-5": Price(Decimal(4), Decimal(20)),
    "claude-opus-5": Price(Decimal(5), Decimal(25)),
    "claude-opus-4-8": Price(Decimal(5), Decimal(25)),
    "claude-sonnet-5": Price(Decimal(2), Decimal(10)),
    "claude-sonnet-4-6": Price(Decimal(3), Decimal(15)),
    "claude-haiku-4-5": Price(Decimal(1), Decimal(5)),
}

FREE = Price(Decimal(0), Decimal(0))


def cost_usd(usage: Usage, price: Price) -> Decimal:
    million = Decimal(1_000_000)
    return (
        Decimal(usage.input_tokens) * price.input_usd_per_mtok
        + Decimal(usage.cache_read_tokens) * price.input_usd_per_mtok * price.cache_read_multiplier
        + Decimal(usage.cache_write_tokens)
        * price.input_usd_per_mtok
        * price.cache_write_multiplier
        + Decimal(usage.output_tokens) * price.output_usd_per_mtok
    ) / million


def cost_paise(cost: Decimal, usd_inr_rate: float) -> int:
    return usd_to_paise(cost, usd_inr_rate)


def override_price(input_usd: float | None, output_usd: float | None) -> Price | None:
    if input_usd is None or output_usd is None:
        return None
    return Price(Decimal(str(input_usd)), Decimal(str(output_usd)))
