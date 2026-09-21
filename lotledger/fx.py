from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from .amounts import RATE, as_decimal, money


def normalize_fx_rate(value) -> Decimal:
    return as_decimal(value).quantize(RATE, rounding=ROUND_HALF_UP)


def to_base(native_amount, rate) -> Decimal:
    return money(as_decimal(native_amount) * as_decimal(rate))
