from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from .amounts import money, qty
from .errors import LedgerError
from .models import Position
from .replay import replay


def day_cutoff(date_value, timezone) -> datetime:
    return datetime(
        date_value.year,
        date_value.month,
        date_value.day,
        23,
        59,
        59,
        999999,
        tzinfo=timezone,
    )


def positions_by_portfolio(
    entries, splits, cutoff
) -> dict[tuple[str, str], Position]:
    result: dict[tuple[str, str], Position] = {}
    state = replay(entries, splits, cutoff)
    for lot_state in state.lot_states.values():
        lot = lot_state.lot
        if lot.closed:
            continue
        key = (lot.portfolio, lot.symbol)
        previous = result.get(key, Position(Decimal("0"), Decimal("0")))
        result[key] = Position(
            qty(previous.quantity + lot.remaining_qty),
            money(previous.cost_base + lot.remaining_cost),
        )
    return result


def positions(entries, splits, cutoff, portfolio=None) -> dict[str, dict]:
    totals: dict[str, Position] = {}
    grouped = positions_by_portfolio(entries, splits, cutoff)
    for (pf, symbol), pos in grouped.items():
        if portfolio is not None and pf != portfolio:
            continue
        previous = totals.get(symbol, Position(Decimal("0"), Decimal("0")))
        totals[symbol] = Position(
            qty(previous.quantity + pos.quantity),
            money(previous.cost_base + pos.cost_base),
        )
    return {
        symbol: {"quantity": pos.quantity, "cost_base": pos.cost_base}
        for symbol, pos in totals.items()
    }


def lot_remaining(entries, splits, lot_id: str, cutoff=None):
    state = replay(entries, splits, cutoff)
    if lot_id not in state.lot_states:
        raise LedgerError(f"lot {lot_id} does not exist")
    return state.lot_states[lot_id].lot


def realized_pnl(entries, start, end, get_entry) -> Decimal:
    total = Decimal("0.00")
    for entry in entries:
        if entry.kind not in {"sell", "reverse_sell"}:
            continue
        if not (start <= entry.timestamp <= end):
            continue
        if entry.kind == "sell":
            total += Decimal(entry.action["realized_pnl"])
        else:
            original = get_entry(entry.reversal_of)
            total -= Decimal(original.action["realized_pnl"])
    return money(total)
