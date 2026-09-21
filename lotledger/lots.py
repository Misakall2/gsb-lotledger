from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping

from .amounts import (
    DEFAULT_PORTFOLIO,
    as_decimal,
    money,
    qty,
)
from .errors import LedgerError, OversoldError
from .models import Entry, Lot, Split


@dataclass
class LotState:
    lot: Lot
    scale: Decimal = Decimal(1)
    sequence: int = 0


def lot_order_key(state: LotState):
    return (state.lot.opened_at, state.sequence, state.lot.id)


def open_lots_for(
    lot_states: dict[str, LotState],
    symbol: str,
    portfolio: str,
    method: str = "FIFO",
) -> list[LotState]:
    result = [
        state
        for state in lot_states.values()
        if not state.lot.closed
        and state.lot.symbol == symbol
        and state.lot.portfolio == portfolio
    ]
    ordered = sorted(result, key=lot_order_key)
    if method == "LIFO":
        ordered.reverse()
    return ordered


def open_lot(
    lot_states: dict[str, LotState],
    methods: dict[tuple[str, str], str],
    entry: Entry,
    action: Mapping,
) -> None:
    lot_id = action["lot_id"]
    portfolio = action.get("portfolio", DEFAULT_PORTFOLIO)
    method = action.get("method", "FIFO")
    key = (portfolio, action["symbol"])
    existing = methods.get(key)
    if existing is not None and existing != method:
        raise LedgerError(
            f"{action['symbol']} in portfolio {portfolio} already uses "
            f"{existing}; cannot switch to {method}"
        )
    methods[key] = method
    quantity = as_decimal(action["quantity"])
    cost_base = as_decimal(action["cost_base"])
    lot = Lot(
        id=lot_id,
        symbol=action["symbol"],
        opened_at=entry.timestamp,
        original_qty=quantity,
        remaining_qty=quantity,
        original_cost=cost_base,
        remaining_cost=cost_base,
        unit_cost=cost_base / quantity,
        entry_id=entry.id,
        portfolio=portfolio,
    )
    lot_states[lot_id] = LotState(lot=lot, sequence=int(action["sequence"]))


def set_closed(lot_states: dict[str, LotState], lot_id: str) -> None:
    state = lot_states[lot_id]
    lot = state.lot
    state.lot = replace_lot(
        lot,
        remaining_qty=Decimal("0"),
        remaining_cost=Decimal("0"),
        closed=True,
    )


def consume_sell(
    lot_states: dict[str, LotState],
    methods: dict[tuple[str, str], str],
    action: Mapping,
) -> Decimal:
    symbol = action["symbol"]
    portfolio = action.get("portfolio", DEFAULT_PORTFOLIO)
    method = methods.get((portfolio, symbol), "FIFO")
    wanted = as_decimal(action["quantity"])
    if wanted <= 0:
        raise OversoldError(
            f"cannot sell {wanted} {symbol} in portfolio {portfolio}"
        )
    available = sum(
        (
            state.lot.remaining_qty
            for state in open_lots_for(lot_states, symbol, portfolio, method)
        ),
        Decimal("0"),
    )
    if wanted > available:
        raise OversoldError(
            f"cannot sell {wanted} {symbol} in portfolio {portfolio}; "
            f"{available} remain"
        )

    allocations = []
    for state in open_lots_for(lot_states, symbol, portfolio, method):
        if wanted <= 0:
            break
        lot = state.lot
        take = min(wanted, lot.remaining_qty)
        cost = (
            lot.remaining_cost
            if take == lot.remaining_qty
            else min(money(take * lot.unit_cost), lot.remaining_cost)
        )
        new_qty = qty(lot.remaining_qty - take)
        new_cost = money(lot.remaining_cost - cost)
        state.lot = replace_lot(
            lot,
            remaining_qty=new_qty,
            remaining_cost=new_cost,
            closed=new_qty == 0,
        )
        allocations.append(
            {
                "lot_id": lot.id,
                "quantity": str(qty(take)),
                "cost_base": str(cost),
                "scale": str(state.scale),
            }
        )
        wanted -= take
    action["allocations"] = allocations
    return as_decimal(action.get("realized_pnl", Decimal("0")))


def restore_sell(
    lot_states: dict[str, LotState],
    original: Entry,
) -> Decimal:
    for allocation in original.action["allocations"]:
        lot_id = allocation["lot_id"]
        if lot_id not in lot_states:
            raise LedgerError(f"lot {lot_id} is unavailable for reversal")
        state = lot_states[lot_id]
        current_scale = state.scale
        old_scale = as_decimal(allocation["scale"])
        # Corporate-action scales determine the number of current shares.
        new_qty = qty(
            as_decimal(allocation["quantity"]) * current_scale / old_scale
        )
        returned_cost = money(allocation["cost_base"])
        lot = state.lot
        new_qty = qty(lot.remaining_qty + new_qty)
        new_cost = money(lot.remaining_cost + returned_cost)
        state.lot = replace_lot(
            lot,
            remaining_qty=new_qty,
            remaining_cost=new_cost,
            unit_cost=new_cost / new_qty,
            closed=False,
        )
    return as_decimal(original.action["realized_pnl"])


def reverse_buy(
    lot_states: dict[str, LotState],
    original: Entry,
) -> None:
    lot_id = original.action["lot_id"]
    if lot_id not in lot_states:
        raise LedgerError(f"lot {lot_id} cannot be reversed")
    state = lot_states[lot_id]
    lot = state.lot
    expected = qty(as_decimal(original.action["quantity"]) * state.scale)
    if lot.closed or lot.remaining_qty != expected:
        raise LedgerError(f"buy {original.id} has later consumption")
    set_closed(lot_states, lot_id)


def apply_split(lot_states: dict[str, LotState], split: Split) -> None:
    for state in lot_states.values():
        lot = state.lot
        if lot.symbol != split.symbol:
            continue
        # Scale tracks every corporate action since the lot opened, even for
        # closed lots, so a later reversal returns current share counts.
        state.scale *= split.ratio
        if lot.closed:
            continue
        new_quantity = qty(lot.remaining_qty * split.ratio)
        state.lot = replace_lot(
            lot,
            remaining_qty=new_quantity,
            unit_cost=lot.remaining_cost / new_quantity,
            closed=False,
        )


def replace_lot(
    lot: Lot,
    *,
    remaining_qty: Decimal,
    remaining_cost: Decimal | None = None,
    unit_cost: Decimal | None = None,
    closed: bool,
) -> Lot:
    return Lot(
        id=lot.id,
        symbol=lot.symbol,
        opened_at=lot.opened_at,
        original_qty=lot.original_qty,
        remaining_qty=remaining_qty,
        original_cost=lot.original_cost,
        remaining_cost=(
            lot.remaining_cost if remaining_cost is None else remaining_cost
        ),
        unit_cost=lot.unit_cost if unit_cost is None else unit_cost,
        entry_id=lot.entry_id,
        closed=closed,
        portfolio=lot.portfolio,
    )
