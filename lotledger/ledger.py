from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, getcontext
from typing import Mapping
from zoneinfo import ZoneInfo

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


class LedgerError(Exception):
    """Base class for ledger validation failures."""


class UnknownAccountError(LedgerError):
    pass


class UnbalancedEntryError(LedgerError):
    pass


class OversoldError(LedgerError):
    pass


class LockedPeriodError(LedgerError):
    pass


@dataclass(frozen=True)
class Account:
    code: str
    name: str = ""


@dataclass(frozen=True)
class Led:
    """A single journal-entry leg in the transaction currency."""

    account: str
    debit: Decimal
    credit: Decimal
    currency: str
    fx_rate: Decimal
    amount_base: Decimal


@dataclass(frozen=True)
class Entry:
    id: str
    timestamp: datetime
    legs: tuple[Led, ...]
    memo: str
    kind: str
    reversal_of: str | None = None
    action: Mapping = field(default_factory=dict)
    sequence: int = 0


@dataclass(frozen=True)
class Split:
    id: str
    timestamp: datetime
    symbol: str
    new_shares: Decimal
    old_shares: Decimal
    memo: str
    reversal_of: str | None = None
    sequence: int = 0

    @property
    def ratio(self) -> Decimal:
        return self.new_shares / self.old_shares


@dataclass(frozen=True)
class Lot:
    id: str
    symbol: str
    opened_at: datetime
    original_qty: Decimal
    remaining_qty: Decimal
    original_cost: Decimal
    remaining_cost: Decimal
    unit_cost: Decimal
    entry_id: str
    closed: bool = False
    portfolio: str = DEFAULT_PORTFOLIO


@dataclass
class _LotState:
    lot: Lot
    scale: Decimal = Decimal(1)
    sequence: int = 0


@dataclass(frozen=True)
class _Position:
    quantity: Decimal
    cost_base: Decimal


class _ReplayState:
    def __init__(self):
        self.lots: dict[str, _LotState] = {}
        self.entries_by_id: dict[str, Entry] = {}
        self.splits_by_id: dict[str, Split] = {}
        self.methods: dict[tuple[str, str], str] = {}
        self.realized = Decimal("0.00")

    def open_lots_for(
        self, symbol: str, portfolio: str, method: str = "FIFO"
    ) -> list[_LotState]:
        result = [
            state
            for state in self.lots.values()
            if not state.lot.closed
            and state.lot.symbol == symbol
            and state.lot.portfolio == portfolio
        ]
        ordered = sorted(result, key=lambda s: (s.lot.opened_at, s.sequence, s.lot.id))
        if method == "LIFO":
            ordered.reverse()
        return ordered

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
            unit_cost=as_decimal(action["cost_base"]) / as_decimal(action["quantity"]),
            entry_id=entry.id,
            portfolio=portfolio,
        )
        self.lots[lot_id] = _LotState(lot=lot, sequence=int(action["sequence"]))

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
        available = sum(
            (s.lot.remaining_qty for s in self.open_lots_for(symbol, portfolio, method)),
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
            # Scale tracks every corporate action since the lot opened, even
            # for closed lots, so a later reversal returns the current
            # (post-split) number of shares.
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

    def positions_by_portfolio(self) -> dict[tuple[str, str], _Position]:
        result: dict[tuple[str, str], _Position] = {}
        for state in self.lots.values():
            lot = state.lot
            if lot.closed:
                continue
            key = (lot.portfolio, lot.symbol)
            previous = result.get(key, _Position(Decimal("0"), Decimal("0")))
            result[key] = _Position(
                qty(previous.quantity + lot.remaining_qty),
                money(previous.cost_base + lot.remaining_cost),
            )
        return result


class Ledger:
    """Append-only CNY ledger with FIFO lots and month locks.

    Events must be appended in non-decreasing ledger-time order. Events with
    the same timestamp are ordered by the sequence assigned at append time.
    """

    def __init__(
        self,
        *,
        base_currency: str = "CNY",
        timezone: str = "Asia/Shanghai",
        fx_account: str = "FX",
        investment_account: str = "SECURITIES",
        realized_pnl_account: str = "REALIZED_PNL",
        dividend_income_account: str = "DIVIDEND_INCOME",
    ):
        self.base_currency = base_currency
        self.timezone = ZoneInfo(timezone)
        self.fx_account = fx_account
        self.investment_account = investment_account
        self.realized_pnl_account = realized_pnl_account
        self.dividend_income_account = dividend_income_account
        self.accounts: dict[str, Account] = {}
        self.entries: list[Entry] = []
        self.splits: list[Split] = []
        self.locked_months: set[tuple[int, int]] = set()
        self._event_seq = 0
        self._lot_seq = 0
        for code in {
            fx_account,
            investment_account,
            realized_pnl_account,
            dividend_income_account,
        }:
            self.add_account(Account(code))

    def add_account(self, account: Account | str) -> Account:
        if isinstance(account, str):
            account = Account(account)
        if account.code in self.accounts:
            raise LedgerError(f"account {account.code} already exists")
        self.accounts[account.code] = account
        return account

    def localize(self, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=self.timezone)
        return value.astimezone(self.timezone)

    def lock_month(self, year: int, month: int) -> None:
        self.locked_months.add((year, month))

    def is_locked(self, timestamp: datetime) -> bool:
        ts = self.localize(timestamp)
        return (ts.year, ts.month) in self.locked_months

    def _require_account(self, code: str) -> None:
        if code not in self.accounts:
            raise UnknownAccountError(f"unknown account {code}")

    def _check_timestamp(self, timestamp: datetime) -> datetime:
        ts = self.localize(timestamp)
        latest_events = [event.timestamp for event in self.entries]
        latest_events.extend(event.timestamp for event in self.splits)
        if latest_events and ts < max(latest_events):
            raise LedgerError("events must be appended in chronological order")
        if self.is_locked(ts):
            raise LockedPeriodError(f"{ts.year}-{ts.month:02d} is locked")
        return ts

    def _leg(self, raw: Mapping) -> Led:
        account = raw["account"]
        self._require_account(account)
        currency = raw.get("currency", self.base_currency)
        ratio = fx_rate(raw.get("fx_rate", Decimal(1)))
        if ratio <= 0:
            raise LedgerError("fx_rate must be positive")
        if currency == self.base_currency and ratio != 1:
            raise LedgerError("base-currency legs must use fx_rate 1")

        debit = money(raw.get("debit", 0))
        credit = money(raw.get("credit", 0))
        if (debit > 0) == (credit > 0) or debit < 0 or credit < 0:
            raise LedgerError("each leg must be exactly one debit or one credit")
        amount = debit if debit else credit
        amount_base = (
            money(raw["amount_base"]) if "amount_base" in raw else money(amount * ratio)
        )
        if amount_base <= 0:
            raise LedgerError("base amount must be positive")
        return Led(account, debit, credit, currency, ratio, amount_base)

    def _balanced_legs(self, raw_legs: list[Mapping]) -> tuple[Led, ...]:
        if len(raw_legs) < 2:
            raise UnbalancedEntryError("an entry needs at least two legs")
        legs = tuple(self._leg(raw) for raw in raw_legs)
        debit_base = sum((leg.amount_base for leg in legs if leg.debit), Decimal("0"))
        credit_base = sum((leg.amount_base for leg in legs if leg.credit), Decimal("0"))
        difference = money(debit_base - credit_base)
        if abs(difference) > MAX_PLUG:
            raise UnbalancedEntryError(
                f"entry is unbalanced by {difference} {self.base_currency}"
            )
        if difference:
            plug = Led(
                self.fx_account,
                abs(difference) if difference < 0 else Decimal("0"),
                difference if difference > 0 else Decimal("0"),
                self.base_currency,
                Decimal(1),
                abs(difference),
            )
            legs += (plug,)
        return legs

    def _append(
        self,
        entry_id: str,
        timestamp: datetime,
        legs: list[Mapping],
        *,
        memo: str,
        kind: str,
        reversal_of: str | None = None,
        action: Mapping | None = None,
    ) -> Entry:
        existing_ids = {entry.id for entry in self.entries}
        if entry_id in existing_ids:
            raise LedgerError(f"entry {entry_id} already exists")
        if reversal_of and reversal_of not in existing_ids:
            raise LedgerError(f"reversal target {reversal_of} does not exist")
        ts = self._check_timestamp(timestamp)
        balanced = self._balanced_legs(legs)
        entry = Entry(
            entry_id,
            ts,
            balanced,
            memo,
            kind,
            reversal_of,
            dict(action or {}),
            self._event_seq,
        )
        self._event_seq += 1
        self.entries.append(entry)
        self._replay()
        return entry

    def post(
        self,
        entry_id: str,
        timestamp: datetime,
        legs: list[Mapping],
        *,
        memo: str = "",
    ) -> Entry:
        """Post a manual multi-currency voucher."""
        return self._append(
            entry_id, timestamp, legs, memo=memo, kind="manual", action={}
        )

    def buy(
        self,
        entry_id: str,
        timestamp: datetime,
        symbol: str,
        quantity,
        currency: str,
        unit_price,
        rate_value,
        cash_account: str,
        *,
        portfolio: str = DEFAULT_PORTFOLIO,
        method: str | None = None,
        memo: str = "",
    ) -> Entry:
        self._require_account(cash_account)
        shares = qty(quantity)
        price = as_decimal(unit_price)
        ratio = fx_rate(rate_value)
        if shares <= 0 or price <= 0:
            raise LedgerError("buy quantity and price must be positive")
        chosen_method = self._resolve_method(portfolio, symbol, method)
        gross = money(shares * price)
        cost_base = money(gross * ratio)
        self._lot_seq += 1
        lot_id = f"LOT-{self._lot_seq:06d}"
        legs = [
            {
                "account": self.investment_account,
                "debit": cost_base,
                "currency": self.base_currency,
                "fx_rate": 1,
            },
            {
                "account": cash_account,
                "credit": gross,
                "currency": currency,
                "fx_rate": ratio,
            },
        ]
        action = {
            "type": "buy",
            "symbol": symbol,
            "quantity": str(shares),
            "cost_base": str(cost_base),
            "lot_id": lot_id,
            "sequence": self._lot_seq,
            "portfolio": portfolio,
            "method": chosen_method,
        }
        return self._append(
            entry_id, timestamp, legs, memo=memo, kind="buy", action=action
        )

    def _resolve_method(self, portfolio: str, symbol: str, method: str | None) -> str:
        """Pick the cost method for a (portfolio, symbol), locking it on first open."""
        chosen = None
        if method is not None:
            chosen = str(method).upper()
            if chosen not in COST_METHODS:
                raise LedgerError(
                    f"method must be one of {COST_METHODS}, got {method!r}"
                )
        existing = self._replay().methods.get((portfolio, symbol))
        if existing is not None:
            if chosen is not None and chosen != existing:
                raise LedgerError(
                    f"{symbol} in portfolio {portfolio} already uses {existing}; "
                    f"cannot switch to {chosen}"
                )
            return existing
        return chosen or "FIFO"

    def sell(
        self,
        entry_id: str,
        timestamp: datetime,
        symbol: str,
        quantity,
        currency: str,
        unit_price,
        rate_value,
        cash_account: str,
        *,
        portfolio: str = DEFAULT_PORTFOLIO,
        memo: str = "",
    ) -> Entry:
        self._require_account(cash_account)
        shares = qty(quantity)
        price = as_decimal(unit_price)
        ratio = fx_rate(rate_value)
        if shares <= 0 or price <= 0:
            raise LedgerError("sell quantity and price must be positive")
        proceeds_native = money(shares * price)
        proceeds_base = money(proceeds_native * ratio)

        trial = self._replay()
        action = {
            "type": "sell",
            "symbol": symbol,
            "quantity": str(shares),
            "proceeds_base": str(proceeds_base),
            "realized_pnl": "0.00",
            "portfolio": portfolio,
        }
        trial.sell(action)
        cost_base = money(
            sum(
                (as_decimal(item["cost_base"]) for item in action["allocations"]),
                Decimal("0"),
            )
        )
        pnl = money(proceeds_base - cost_base)
        action["cost_base"] = str(cost_base)
        action["realized_pnl"] = str(pnl)

        legs = [
            {
                "account": cash_account,
                "debit": proceeds_native,
                "currency": currency,
                "fx_rate": ratio,
            },
            {
                "account": self.investment_account,
                "credit": cost_base,
                "currency": self.base_currency,
                "fx_rate": 1,
            },
        ]
        if pnl:
            legs.append(
                {
                    "account": self.realized_pnl_account,
                    "debit": abs(pnl) if pnl < 0 else 0,
                    "credit": pnl if pnl > 0 else 0,
                    "currency": self.base_currency,
                    "fx_rate": 1,
                }
            )
        return self._append(
            entry_id, timestamp, legs, memo=memo, kind="sell", action=action
        )

    def dividend(
        self,
        entry_id: str,
        timestamp: datetime,
        symbol: str,
        amount,
        currency: str,
        rate_value,
        cash_account: str,
        *,
        memo: str = "",
    ) -> Entry:
        self._require_account(cash_account)
        cash = money(amount)
        if cash <= 0:
            raise LedgerError("dividend must be positive")
        ratio = fx_rate(rate_value)
        legs = [
            {
                "account": cash_account,
                "debit": cash,
                "currency": currency,
                "fx_rate": ratio,
            },
            {
                "account": self.dividend_income_account,
                "credit": cash,
                "currency": currency,
                "fx_rate": ratio,
            },
        ]
        action = {"type": "dividend", "symbol": symbol}
        return self._append(
            entry_id, timestamp, legs, memo=memo, kind="dividend", action=action
        )

    def split_shares(
        self,
        split_id: str,
        timestamp: datetime,
        symbol: str,
        new_shares,
        old_shares,
        *,
        memo: str = "",
    ) -> Split:
        if split_id in {split.id for split in self.splits}:
            raise LedgerError(f"split {split_id} already exists")
        ts = self._check_timestamp(timestamp)
        new = as_decimal(new_shares)
        old = as_decimal(old_shares)
        if new <= 0 or old <= 0:
            raise LedgerError("split ratio parts must be positive")
        split = Split(split_id, ts, symbol, new, old, memo, sequence=self._event_seq)
        self._event_seq += 1
        self.splits.append(split)
        self._replay()
        return split

    def rights_issue(
        self,
        entry_id: str,
        timestamp: datetime,
        symbol: str,
        new_shares,
        old_shares,
        *,
        memo: str = "",
    ) -> Entry:
        """Bonus-style rights issue: more shares, same total cost, lower unit cost.

        ``new_shares``/``old_shares`` is the allotment ratio: for every
        ``old_shares`` held, ``new_shares`` bonus shares are issued, so open
        quantities are multiplied by ``(old + new) / old``.
        Every open lot of the symbol (across all portfolios) is scaled by
        that factor. A self-balancing memo entry on the investment
        account records the affected cost basis, so the books stay balanced
        while total CNY cost is unchanged. Like any event, it cannot be dated
        inside a locked month; book it in the next open period instead.
        """
        new = as_decimal(new_shares)
        old = as_decimal(old_shares)
        if new <= 0 or old <= 0:
            raise LedgerError("rights issue ratio parts must be positive")
        state = self._replay()
        affected = money(
            sum(
                (
                    lot_state.lot.remaining_cost
                    for lot_state in state.lots.values()
                    if not lot_state.lot.closed and lot_state.lot.symbol == symbol
                ),
                Decimal("0"),
            )
        )
        if affected <= 0:
            raise LedgerError(f"no open lots for {symbol}; nothing to adjust")
        legs = [
            {
                "account": self.investment_account,
                "debit": affected,
                "currency": self.base_currency,
                "fx_rate": 1,
            },
            {
                "account": self.investment_account,
                "credit": affected,
                "currency": self.base_currency,
                "fx_rate": 1,
            },
        ]
        action = {
            "type": "rights_issue",
            "symbol": symbol,
            "new_shares": str(new),
            "old_shares": str(old),
        }
        text = memo or f"rights issue {symbol} {old} for {new}"
        entry = self._append(
            entry_id, timestamp, legs, memo=text, kind="rights_issue", action=action
        )
        split_id = f"{entry_id}-SPLIT"
        if split_id in {split.id for split in self.splits}:
            raise LedgerError(f"split {split_id} already exists")
        ts = self._check_timestamp(entry.timestamp)
        split = Split(
            split_id, ts, symbol, new + old, old, text, sequence=self._event_seq
        )
        self._event_seq += 1
        self.splits.append(split)
        self._replay()
        return entry

    def reverse_entry(
        self,
        reversal_id: str,
        timestamp: datetime,
        original_id: str,
        *,
        memo: str = "red-letter reversal",
    ) -> Entry:
        original = self.get_entry(original_id)
        ts = self.localize(timestamp)
        if self.is_locked(original.timestamp) and (
            (ts.year, ts.month) <= (original.timestamp.year, original.timestamp.month)
        ):
            raise LockedPeriodError(
                "a locked-period entry must be reversed in a later period"
            )
        inverse_legs = [
            {
                "account": leg.account,
                "debit": leg.credit if leg.credit else 0,
                "credit": leg.debit if leg.debit else 0,
                "currency": leg.currency,
                "fx_rate": leg.fx_rate,
                "amount_base": leg.amount_base,
            }
            for leg in original.legs
        ]
        action = (
            {"type": f"reverse_{original.kind}", "original_entry_id": original_id}
            if original.kind in {"buy", "sell"}
            else {}
        )
        return self._append(
            reversal_id,
            ts,
            inverse_legs,
            memo=memo,
            kind=f"reverse_{original.kind}",
            reversal_of=original_id,
            action=action,
        )

    def reverse_split(
        self,
        reversal_id: str,
        timestamp: datetime,
        original_id: str,
        *,
        memo: str = "red-letter split reversal",
    ) -> Split:
        original = next(
            (split for split in self.splits if split.id == original_id), None
        )
        if original is None:
            raise LedgerError(f"split {original_id} does not exist")
        ts = self.localize(timestamp)
        if self.is_locked(original.timestamp) and (
            (ts.year, ts.month) <= (original.timestamp.year, original.timestamp.month)
        ):
            raise LockedPeriodError(
                "a locked-period split must be reversed in a later period"
            )
        if any(split.reversal_of == original_id for split in self.splits):
            raise LedgerError(f"split {original_id} already has a reversal")
        self._check_timestamp(ts)
        reversal = Split(
            reversal_id,
            ts,
            original.symbol,
            original.old_shares,
            original.new_shares,
            memo,
            original_id,
            self._event_seq,
        )
        self._event_seq += 1
        self.splits.append(reversal)
        self._replay()
        return reversal

    def delete_manual_entry(self, entry_id: str) -> None:
        entry = self.get_entry(entry_id)
        if entry.kind != "manual":
            raise LedgerError("only manual entries may be deleted; reverse trades")
        if any(existing.reversal_of == entry_id for existing in self.entries):
            raise LedgerError("entry is referenced by a reversal")
        if self.is_locked(entry.timestamp):
            raise LockedPeriodError("cannot delete an entry in a locked period")
        self.entries.remove(entry)

    def get_entry(self, entry_id: str) -> Entry:
        for entry in self.entries:
            if entry.id == entry_id:
                return entry
        raise LedgerError(f"entry {entry_id} does not exist")

    def _events_until(self, cutoff: datetime | None):
        events = [("entry", entry) for entry in self.entries]
        events.extend(
            ("split", split) for split in self.splits
        )
        events.sort(key=lambda item: item[1].sequence)
        for kind, event in events:
            if cutoff is not None and event.timestamp > cutoff:
                break
            yield kind, event

    def _replay(self, cutoff: datetime | None = None) -> _ReplayState:
        cutoff = self.localize(cutoff) if cutoff is not None else None
        state = _ReplayState()
        for kind, event in self._events_until(cutoff):
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
            action = event.action
            event_type = action.get("type")
            if event_type == "buy":
                state.open_lot(event, action)
            elif event_type == "sell":
                state.sell(action)
            elif event_type == "reverse_buy":
                state.reverse_buy(action)
            elif event_type == "reverse_sell":
                state.reverse_sell(action)
        return state

    def _day_cutoff(self, date_value) -> datetime:
        return datetime(
            date_value.year,
            date_value.month,
            date_value.day,
            23,
            59,
            59,
            999999,
            tzinfo=self.timezone,
        )

    def positions_at(
        self, date_value, portfolio: str | None = None
    ) -> dict[str, dict[str, Decimal]]:
        """Return quantity and CNY cost per symbol at ledger end-of-day.

        With ``portfolio`` set, only lots of that portfolio are aggregated.
        """
        cutoff = self._day_cutoff(date_value)
        totals: dict[str, _Position] = {}
        for (pf, symbol), pos in (
            self._replay(cutoff).positions_by_portfolio().items()
        ):
            if portfolio is not None and pf != portfolio:
                continue
            previous = totals.get(symbol, _Position(Decimal("0"), Decimal("0")))
            totals[symbol] = _Position(
                qty(previous.quantity + pos.quantity),
                money(previous.cost_base + pos.cost_base),
            )
        return {
            symbol: {"quantity": pos.quantity, "cost_base": pos.cost_base}
            for symbol, pos in totals.items()
        }

    def positions_by_portfolio(
        self, date_value
    ) -> dict[str, dict[str, dict[str, Decimal]]]:
        """Return end-of-day quantity and CNY cost grouped by portfolio, then symbol."""
        cutoff = self._day_cutoff(date_value)
        result: dict[str, dict[str, dict[str, Decimal]]] = {}
        for (pf, symbol), pos in (
            self._replay(cutoff).positions_by_portfolio().items()
        ):
            result.setdefault(pf, {})[symbol] = {
                "quantity": pos.quantity,
                "cost_base": pos.cost_base,
            }
        return result

    def lot_remaining(self, lot_id: str, date_value=None) -> Lot:
        cutoff = self._day_cutoff(date_value) if date_value is not None else None
        state = self._replay(cutoff)
        if lot_id not in state.lots:
            raise LedgerError(f"lot {lot_id} does not exist")
        return state.lots[lot_id].lot

    def realized_pnl(self, start_date, end_date) -> Decimal:
        """Sum realized P/L for entries in the inclusive ledger-date range."""
        start = self._day_cutoff(start_date)
        end = self._day_cutoff(end_date)
        start = start.replace(hour=0, minute=0, second=0, microsecond=0)
        total = Decimal("0.00")
        for entry in self.entries:
            if entry.kind in {"sell", "reverse_sell"} and start <= entry.timestamp <= end:
                if entry.kind == "sell":
                    total += as_decimal(entry.action["realized_pnl"])
                else:
                    original = self.get_entry(entry.reversal_of)
                    total -= as_decimal(original.action["realized_pnl"])
        return money(total)
