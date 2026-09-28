from decimal import Decimal

import pytest

from app.money import (
    format_inr,
    inr_to_paise,
    paise_to_rupees,
    parse_inr,
    rupees_to_paise,
    token_cost_usd,
    usd_to_paise,
)


@pytest.mark.parametrize(
    ("paise", "expected"),
    [
        (0, "₹0"),
        (5, "₹0.05"),
        (29900, "₹299"),
        (100000, "₹1,000"),
        (200000, "₹2,000"),
        (200001, "₹2,000.01"),
        (1000000, "₹10,000"),
        (4499900, "₹44,999"),
        (12500050, "₹1,25,000.50"),
        (1234567800, "₹1,23,45,678"),
        (10000000000, "₹10,00,00,000"),
        (-150050, "-₹1,500.50"),
    ],
)
def test_format_inr_indian_grouping(paise: int, expected: str) -> None:
    assert format_inr(paise) == expected


def test_format_inr_always_show_paise() -> None:
    assert format_inr(200000, always_show_paise=True) == "₹2,000.00"


def test_format_inr_rejects_non_int() -> None:
    with pytest.raises(TypeError):
        format_inr(10.5)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        format_inr(True)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2000", 200000),
        ("2,000.01", 200001),
        ("₹1,25,000.50", 12500050),
        (Decimal("0.1"), 10),
        (45000, 4500000),
        ("0.07", 7),
    ],
)
def test_rupees_to_paise(value: object, expected: int) -> None:
    assert rupees_to_paise(value) == expected  # type: ignore[arg-type]


def test_rupees_to_paise_rejects_floats_and_sub_paise() -> None:
    with pytest.raises(TypeError):
        rupees_to_paise(19.99)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        rupees_to_paise("1.005")
    with pytest.raises(ValueError):
        rupees_to_paise("abc")


def test_no_float_rounding_errors() -> None:
    # 0.1 + 0.2 != 0.3 in floats; in paise it is exact.
    assert rupees_to_paise("0.1") + rupees_to_paise("0.2") == rupees_to_paise("0.3")
    total = sum(rupees_to_paise("19.99") for _ in range(100))
    assert total == 199900
    assert paise_to_rupees(199900) == Decimal("1999.00")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("refund ₹1,999.50 please", 199950),
        ("Rs. 500", 50000),
        ("INR 2000", 200000),
        ("rs 45,000", 4500000),
        ("no amount here", None),
    ],
)
def test_parse_inr(text: str, expected: int | None) -> None:
    assert parse_inr(text) == expected


def test_usd_to_paise_conversion_rounds_up() -> None:
    assert usd_to_paise(Decimal("1"), 85) == 8500
    assert usd_to_paise(Decimal("0.01"), 85) == 85
    # 0.0001 USD * 85 = 0.85 paise -> rounds up to 1 so budgets never under-count
    assert usd_to_paise(Decimal("0.0001"), 85) == 1
    assert usd_to_paise(Decimal("0"), 85) == 0
    assert usd_to_paise(Decimal("2"), "83.5") == 16700


def test_token_cost_usd() -> None:
    cost = token_cost_usd(1_000_000, 500_000, Decimal("5"), Decimal("25"))
    assert cost == Decimal("17.5")
    assert usd_to_paise(token_cost_usd(3000, 400, Decimal("5"), Decimal("25")), 85) == 213


def test_inr_to_paise_from_config() -> None:
    assert inr_to_paise(300) == 30000
    assert inr_to_paise("250.5") == 25050
    assert inr_to_paise(0.1) == 10
