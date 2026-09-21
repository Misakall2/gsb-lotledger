from __future__ import annotations

from datetime import datetime

from .lots import LotReplay
from .models import LedgerError, Position, Split
from decimal import Decimal

from .values import money, qty


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


def events_until(entries, splits, cutoff):
    events = [("entry", entry) for entry in entries]
    events.extend(("split", split) for split in splits)
    events.sort(key=lambda item: item[1].sequence)
    for kind, event in events:
        if cutoff is not None and event.timestamp > cutoff:
            break
        yield kind, event


def replay(entries, splits, cutoff=None) -> LotReplay:
    state = LotReplay()
    for kind, event in events_until(entries, splits, cutoff):
        if kind == "split":
            if event.reversal_of:
                original = state.splits_by_id[event.reversal_of]
                state.apply_split(
                    Split(
                        event.id,
                        event.timestamp,
                        event.symbol,
                        original.old_shares,
                        original.new_shares,
                        event.memo,
                    )
                )
            else:
                state.apply_split(event)
            state.splits_by_id[event.id] = event
            continue

        state.entries_by_id[event.id] = event
        event_type = event.action.get("type")
        if event_type == "buy":
            state.open_lot(event, event.action)
        elif event_type == "sell":
            state.sell(event.action)
        elif event_type == "reverse_buy":
            state.reverse_buy(event.action)
        elif event_type == "reverse_sell":
            state.reverse_sell(event.action)
    return state


def positions_at(entries, splits, cutoff, portfolio=None):
    totals: dict[str, Position] = {}
    for (item_portfolio, symbol), pos in replay(
        entries, splits, cutoff
    ).positions_by_portfolio().items():
        if portfolio is not None and item_portfolio != portfolio:
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


def positions_by_portfolio(entries, splits, cutoff):
    result = {}
    for (portfolio, symbol), pos in replay(
        entries, splits, cutoff
    ).positions_by_portfolio().items():
        result.setdefault(portfolio, {})[symbol] = {
            "quantity": pos.quantity,
            "cost_base": pos.cost_base,
        }
    return result


def lot_remaining(entries, splits, lot_id: str, cutoff=None):
    state = replay(entries, splits, cutoff)
    if lot_id not in state.lots:
        raise LedgerError(f"lot {lot_id} does not exist")
    return state.lots[lot_id].lot


def realized_pnl(entries, get_entry, start, end):
    total = Decimal("0.00")
    for entry in entries:
        if (
            entry.kind in {"sell", "reverse_sell"}
            and start <= entry.timestamp <= end
        ):
            if entry.kind == "sell":
                total += Decimal(entry.action["realized_pnl"])
            else:
                original = get_entry(entry.reversal_of)
                total -= Decimal(original.action["realized_pnl"])
    return money(total)
