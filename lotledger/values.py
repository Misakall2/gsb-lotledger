from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, getcontext

getcontext().prec = 40

MONEY = Decimal("0.01")
QTY = Decimal("0.00000001")
RATE = Decimal("0.00000001")
MAX_PLUG = Decimal("0.02")
DEFAULT_PORTFOLIO = "DEFAULT"
COST_METHODS = ("FIFO", "LIFO")


def as_decimal(value) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def money(value) -> Decimal:
    return as_decimal(value).quantize(MONEY, rounding=ROUND_HALF_UP)


def qty(value) -> Decimal:
    return as_decimal(value).quantize(QTY, rounding=ROUND_HALF_UP)


def fx_rate(value) -> Decimal:
    return as_decimal(value).quantize(RATE, rounding=ROUND_HALF_UP)


def to_base(amount, rate_value) -> Decimal:
    """Convert a transaction-currency amount using the entry-date FX rate."""
    return money(as_decimal(amount) * fx_rate(rate_value))
