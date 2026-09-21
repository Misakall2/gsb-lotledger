from __future__ import annotations

from decimal import Decimal
from typing import Mapping

from .models import (
    Entry,
    LedgerError,
    Lot,
    LotState,
    OversoldError,
    Position,
    Split,
)
from .values import DEFAULT_PORTFOLIO, as_decimal, money, qty


class LotReplay:
    def __init__(self):
        self.lots: dict[str, LotState] = {}
        self.entries_by_id: dict[str, Entry] = {}
        self.splits_by_id: dict[str, Split] = {}
        self.methods: dict[tuple[str, str], str] = {}
        self.realized = Decimal("0.00")

    def open_lots_for(
        self, symbol: str, portfolio: str, method: str = "FIFO"
    ) -> list[LotState]:
        result = [
            state
            for state in self.lots.values()
            if not state.lot.closed
            and state.lot.symbol == symbol
            and state.lot.portfolio == portfolio
        ]
        ordered = sorted(result, key=self._lot_order_key)
        if method == "LIFO":
            ordered.reverse()
        return ordered

    @staticmethod
    def _lot_order_key(state: LotState):
        return (state.lot.opened_at, state.sequence, state.lot.id)

    def open_lot(self, entry: Entry, action: Mapping) -> None:
        lot_id = action["lot_id"]
        portfolio = action.get("portfolio", DEFAULT_PORTFOLIO)
        method = action.get("method", "FIFO")
        key = (portfolio, action["symbol"])
        existing = self.methods.get(key)
        if existing is not None and existing != method:
            raise LedgerError(
                f"{action['symbol']} in portfolio {portfolio} already uses "
                f"{existing}; cannot switch to {method}"
            )
        self.methods[key] = method
        lot = Lot(
            id=lot_id,
            symbol=action["symbol"],
            opened_at=entry.timestamp,
            original_qty=as_decimal(action["quantity"]),
            remaining_qty=as_decimal(action["quantity"]),
            original_cost=as_decimal(action["cost_base"]),
            remaining_cost=as_decimal(action["cost_base"]),
            unit_cost=as_decimal(action["cost_base"])
            / as_decimal(action["quantity"]),
            entry_id=entry.id,
            portfolio=portfolio,
        )
        self.lots[lot_id] = LotState(lot=lot, sequence=int(action["sequence"]))

    def set_closed(self, lot_id: str) -> None:
        state = self.lots[lot_id]
        lot = state.lot
        state.lot = Lot(
            id=lot.id,
            symbol=lot.symbol,
            opened_at=lot.opened_at,
            original_qty=lot.original_qty,
            remaining_qty=Decimal("0"),
            original_cost=lot.original_cost,
            remaining_cost=Decimal("0"),
            unit_cost=lot.unit_cost,
            entry_id=lot.entry_id,
            closed=True,
            portfolio=lot.portfolio,
        )

    def sell(self, action: Mapping) -> None:
        symbol = action["symbol"]
        portfolio = action.get("portfolio", DEFAULT_PORTFOLIO)
        method = self.methods.get((portfolio, symbol), "FIFO")
        wanted = as_decimal(action["quantity"])
        if wanted <= 0:
            raise OversoldError(
                f"cannot sell {wanted} {symbol} in portfolio {portfolio}"
            )
        available = sum(
            (
                state.lot.remaining_qty
                for state in self.open_lots_for(symbol, portfolio, method)
            ),
            Decimal("0"),
        )
        if wanted > available:
            raise OversoldError(
                f"cannot sell {wanted} {symbol} in portfolio {portfolio}; "
                f"{available} remain"
            )

        allocations = []
        for state in self.open_lots_for(symbol, portfolio, method):
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
            state.lot = Lot(
                id=lot.id,
                symbol=lot.symbol,
                opened_at=lot.opened_at,
                original_qty=lot.original_qty,
                remaining_qty=new_qty,
                original_cost=lot.original_cost,
                remaining_cost=new_cost,
                unit_cost=lot.unit_cost,
                entry_id=lot.entry_id,
                closed=new_qty == 0,
                portfolio=lot.portfolio,
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
        self.realized += as_decimal(action["realized_pnl"])

    def reverse_sell(self, action: Mapping) -> None:
        original = self.entries_by_id[action["original_entry_id"]]
        for allocation in original.action["allocations"]:
            lot_id = allocation["lot_id"]
            if lot_id not in self.lots:
                raise LedgerError(f"lot {lot_id} is unavailable for reversal")
            state = self.lots[lot_id]
            current_scale = state.scale
            old_scale = as_decimal(allocation["scale"])
            returned_qty = qty(
                as_decimal(allocation["quantity"]) * current_scale / old_scale
            )
            returned_cost = money(allocation["cost_base"])
            lot = state.lot
            new_qty = qty(lot.remaining_qty + returned_qty)
            new_cost = money(lot.remaining_cost + returned_cost)
            state.lot = Lot(
                id=lot.id,
                symbol=lot.symbol,
                opened_at=lot.opened_at,
                original_qty=lot.original_qty,
                remaining_qty=new_qty,
                original_cost=lot.original_cost,
                remaining_cost=new_cost,
                unit_cost=new_cost / new_qty,
                entry_id=lot.entry_id,
                closed=False,
                portfolio=lot.portfolio,
            )
        self.realized -= as_decimal(original.action["realized_pnl"])

    def reverse_buy(self, action: Mapping) -> None:
        original = self.entries_by_id[action["original_entry_id"]]
        lot_id = original.action["lot_id"]
        if lot_id not in self.lots:
            raise LedgerError(f"lot {lot_id} cannot be reversed")
        state = self.lots[lot_id]
        lot = state.lot
        expected = qty(as_decimal(original.action["quantity"]) * state.scale)
        if lot.closed or lot.remaining_qty != expected:
            raise LedgerError(f"buy {original.id} has later consumption")
        self.set_closed(lot_id)

    def apply_split(self, split: Split) -> None:
        for state in self.lots.values():
            lot = state.lot
            if lot.symbol != split.symbol:
                continue
            # Scale tracks corporate actions even for closed lots, so a sale
            # reversal after a split restores the current share count.
            state.scale *= split.ratio
            if lot.closed:
                continue
            new_quantity = qty(lot.remaining_qty * split.ratio)
            state.lot = Lot(
                id=lot.id,
                symbol=lot.symbol,
                opened_at=lot.opened_at,
                original_qty=lot.original_qty,
                remaining_qty=new_quantity,
                original_cost=lot.original_cost,
                remaining_cost=lot.remaining_cost,
                unit_cost=lot.remaining_cost / new_quantity,
                entry_id=lot.entry_id,
                closed=False,
                portfolio=lot.portfolio,
            )

    def positions_by_portfolio(self) -> dict[tuple[str, str], Position]:
        result: dict[tuple[str, str], Position] = {}
        for state in self.lots.values():
            lot = state.lot
            if lot.closed:
                continue
            key = (lot.portfolio, lot.symbol)
            previous = result.get(key, Position(Decimal("0"), Decimal("0")))
            result[key] = Position(
                qty(previous.quantity + lot.remaining_qty),
                money(previous.cost_base + lot.remaining_cost),
            )
        return result
