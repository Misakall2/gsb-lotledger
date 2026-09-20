"""Double-entry brokerage ledger with FIFO tax lots.

Trades, corporate actions, manual postings, and reversals are stored as
append-only events. Lot and P/L queries replay events in the deterministic
order (date, sequence, id). Later corrections therefore do not change a
point-in-time query made before the correction date.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, Mapping, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from .errors import (
    AccountError,
    DuplicateIdError,
    LockedPeriodError,
    LotError,
    ValidationError,
)

BASE_CURRENCY = "CNY"
LEDGER_TIMEZONE = ZoneInfo("Asia/Shanghai")
DAY_END = time(23, 59, 59, 999999)
MONEY_Q = Decimal("0.01")
FINE_Q = Decimal("0.00000001")
MAX_ROUNDING_RESIDUAL = Decimal("0.01")


def D(value) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def money(value) -> Decimal:
    return D(value).quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def fine(value) -> Decimal:
    return D(value).quantize(FINE_Q, rounding=ROUND_HALF_UP)


def as_date(value) -> date:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.date()
        return value.astimezone(LEDGER_TIMEZONE).date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value)
    raise ValidationError("date must be date or ISO YYYY-MM-DD string")


def period_key(value: date) -> Tuple[int, int]:
    return value.year, value.month


def month_end(value: date) -> date:
    next_month = value.replace(day=28) + timedelta(days=4)
    return next_month - timedelta(days=next_month.day)


@dataclass(frozen=True)
class Account:
    code: str
    name: str


@dataclass(frozen=True)
class Leg:
    account: str
    debit: Decimal = Decimal("0")
    credit: Decimal = Decimal("0")
    currency: str = BASE_CURRENCY
    rate_to_base: Decimal = Decimal("1")

    def __post_init__(self):
        object.__setattr__(self, "debit", money(D(self.debit)))
        object.__setattr__(self, "credit", money(D(self.credit)))
        object.__setattr__(self, "rate_to_base", D(self.rate_to_base))
        object.__setattr__(self, "currency", self.currency.upper())

    @property
    def base_debit(self) -> Decimal:
        return money(self.debit * self.rate_to_base)

    @property
    def base_credit(self) -> Decimal:
        return money(self.credit * self.rate_to_base)

    def negated(self) -> "Leg":
        return Leg(
            self.account,
            -self.debit,
            -self.credit,
            self.currency,
            self.rate_to_base,
        )


@dataclass(frozen=True)
class Entry:
    id: str
    date: date
    legs: Tuple[Leg, ...]
    memo: str = ""
    reversal_of: Optional[str] = None
    sequence: int = 0


@dataclass
class Lot:
    id: str
    symbol: str
    currency: str
    rate_to_base: Decimal
    opened_date: date
    sequence: int
    original_quantity: Decimal
    remaining_quantity: Decimal
    unit_cost: Decimal
    base_unit_cost: Decimal
    total_cost: Decimal
    remaining_cost: Decimal
    total_base_cost: Decimal
    remaining_base_cost: Decimal
    closed: bool = False


@dataclass(frozen=True)
class LotMatch:
    lot_id: str
    quantity: Decimal
    cost: Decimal
    base_cost: Decimal
    currency: str
    rate_to_base: Decimal


@dataclass(frozen=True)
class Position:
    symbol: str
    currency: str
    quantity: Decimal
    cost: Decimal
    base_cost: Decimal
    open_lot_ids: Tuple[str, ...]


@dataclass(frozen=True)
class RealizedPnl:
    date: date
    sequence: int
    event_id: str
    symbol: str
    currency: str
    quantity: Decimal
    proceeds: Decimal
    base_proceeds: Decimal
    cost: Decimal
    base_cost: Decimal
    base_realized_pnl: Decimal
    account: str
    matches: Tuple[LotMatch, ...]


@dataclass
class _Event:
    id: str
    kind: str
    event_date: date
    sequence: int
    symbol: Optional[str]
    payload: dict


@dataclass
class _Sale:
    date: date
    event_id: str
    symbol: str
    currency: str
    quantity: Decimal
    proceeds: Decimal
    base_proceeds: Decimal
    pnl_account: str
    matches: Tuple[LotMatch, ...]
    total_cost: Decimal
    total_base_cost: Decimal


class _State:
    def __init__(self):
        self.lots: "OrderedDict[str, Lot]" = OrderedDict()
        self.realized: list[RealizedPnl] = []
        self.sales: Dict[str, _Sale] = {}


class Ledger:
    """Append-only in-memory brokerage ledger.

    Defaults deliberately fix the required base currency (CNY), timezone
    (Asia/Shanghai), and day-end cutoff (23:59:59.999999).
    """

    def __init__(
        self,
        fx_variance_account: str = "FX_VARIANCE",
    ):
        self.base_currency = BASE_CURRENCY
        self.timezone = LEDGER_TIMEZONE
        self.day_end_cutoff = DAY_END
        self.fx_variance_account = fx_variance_account
        self._accounts: Dict[str, Account] = {}
        self._entries: Dict[str, Entry] = {}
        self._events: list[_Event] = []
        self._locked: Dict[Tuple[int, int], date] = {}
        self._seq = 0
        self.add_account(fx_variance_account, "FX translation variance")

    def add_account(self, code: str, name: str = "") -> Account:
        if not isinstance(code, str) or not code:
            raise AccountError("account code must be a non-empty string")
        if code in self._accounts:
            raise DuplicateIdError(f"account already exists: {code}")
        account = Account(code, name)
        self._accounts[code] = account
        return account

    def account(self, code: str) -> Account:
        try:
            return self._accounts[code]
        except KeyError:
            raise AccountError(f"unknown account: {code}") from None

    def lock_month(self, year: int, month: int) -> None:
        if not 1 <= month <= 12:
            raise ValidationError("month must be between 1 and 12")
        first = date(year, month, 1)
        self._locked[period_key(first)] = month_end(first)

    def is_locked(self, value) -> bool:
        return period_key(as_date(value)) in self._locked

    def locked_periods(self) -> Tuple[Tuple[int, int], ...]:
        return tuple(sorted(self._locked))

    def day_end_datetime(self, value) -> datetime:
        return datetime.combine(as_date(value), self.day_end_cutoff, tzinfo=self.timezone)

    def locked_period_end_datetime(self, year: int, month: int) -> datetime:
        return datetime.combine(
            month_end(date(year, month, 1)), self.day_end_cutoff, tzinfo=self.timezone
        )

    def add_entry(
        self,
        entry_id: str,
        posting_date,
        legs: Sequence[Leg | Mapping],
        memo: str = "",
    ) -> Entry:
        d = as_date(posting_date)
        self._ensure_unlocked(d, "add")
        prepared = self._balance_manual_legs([self._prepare_leg(x) for x in legs])
        entry = self._store_entry(entry_id, d, prepared, memo, None)
        event = _Event(entry_id, "manual", d, entry.sequence, None, {})
        return self._commit(entry, event)

    def update_entry(
        self,
        entry_id: str,
        legs: Sequence[Leg | Mapping],
        memo: Optional[str] = None,
    ) -> Entry:
        old = self.entry(entry_id)
        event = self._event(entry_id)
        if event.kind != "manual":
            raise ValidationError("generated entries must be reversed, not amended")
        if self._has_reversal(entry_id):
            raise ValidationError("already reversed entries can only be corrected with new entries")
        self._ensure_unlocked(old.date, "amend")
        prepared = self._balance_manual_legs([self._prepare_leg(x) for x in legs])
        updated = Entry(
            entry_id,
            old.date,
            tuple(prepared),
            old.memo if memo is None else memo,
            old.reversal_of,
            old.sequence,
        )
        self._entries[entry_id] = updated
        self._validate_replay()
        return updated

    def delete_entry(self, entry_id: str) -> None:
        entry = self.entry(entry_id)
        event = self._event(entry_id)
        if event.kind != "manual":
            raise ValidationError("generated entries must be reversed, not deleted")
        if self._has_reversal(entry_id):
            raise ValidationError("already reversed entries cannot be deleted")
        self._ensure_unlocked(entry.date, "delete")
        self._events.remove(event)
        del self._entries[entry_id]

    def entry(self, entry_id: str) -> Entry:
        try:
            return self._entries[entry_id]
        except KeyError:
            raise ValidationError(f"unknown entry: {entry_id}") from None

    def entries(self, start=None, end=None, include_reversals: bool = True) -> Tuple[Entry, ...]:
        start_d = date.min if start is None else as_date(start)
        end_d = date.max if end is None else as_date(end)
        result = [
            entry
            for entry in self._entries.values()
            if start_d <= entry.date <= end_d
            and (include_reversals or entry.reversal_of is None)
        ]
        return tuple(sorted(result, key=lambda e: (e.date, e.sequence, e.id)))

    def reverse_entry(
        self,
        original_id: str,
        reversal_date,
        reversal_id: Optional[str] = None,
        memo: Optional[str] = None,
    ) -> Entry:
        original = self.entry(original_id)
        original_event = self._event(original_id)
        if original.reversal_of is not None:
            raise ValidationError("cannot reverse a reversal entry")
        if any(e.reversal_of == original_id for e in self._entries.values()):
            raise DuplicateIdError(f"entry already reversed: {original_id}")

        d = as_date(reversal_date)
        if d < original.date:
            raise ValidationError("reversal cannot be dated before the original entry")
        if period_key(original.date) in self._locked:
            allowed_from = self._locked[period_key(original.date)] + timedelta(days=1)
            if d < allowed_from:
                raise LockedPeriodError(
                    f"reversal of locked {original.date:%Y-%m} must be dated {allowed_from} or later"
                )
        self._ensure_unlocked(d, "add reversal")

        rid = reversal_id or f"{original_id}-REV"
        if rid in self._entries:
            raise DuplicateIdError(f"entry already exists: {rid}")

        kind_map = {
            "manual": "manual_reversal",
            "buy": "buy_reversal",
            "sell": "sell_reversal",
            "dividend": "dividend_reversal",
        }
        kind = kind_map.get(original_event.kind)
        if kind is None:
            raise ValidationError("this event type cannot be reversed as an accounting entry")

        reversed_entry = Entry(
            rid,
            d,
            tuple(leg.negated() for leg in original.legs),
            memo if memo is not None else f"Red-letter reversal of {original_id}",
            original_id,
            self._next_sequence(),
        )
        event = _Event(
            rid,
            kind,
            d,
            reversed_entry.sequence,
            original_event.symbol,
            {"original_event": original_id},
        )
        return self._commit(reversed_entry, event)

    def record_buy(
        self,
        trade_id: str,
        trade_date,
        symbol: str,
        quantity,
        unit_cost,
        cash_account: str,
        inventory_account: str,
        currency: str = BASE_CURRENCY,
        rate_to_base=1,
        memo: str = "",
    ) -> Entry:
        d = as_date(trade_date)
        self._ensure_unlocked(d, "add buy")
        self.account(cash_account)
        self.account(inventory_account)
        if cash_account == inventory_account:
            raise ValidationError("cash and inventory accounts must be distinct")
        if cash_account == self.fx_variance_account or inventory_account == self.fx_variance_account:
            raise ValidationError("trade economic accounts cannot use the FX variance account")
        self._validate_symbol(symbol)
        qty = self._positive_quantity(quantity)
        price = self._positive_money(unit_cost)
        rate = self._positive_rate(rate_to_base)
        currency = currency.upper()
        total_cost = money(qty * price)
        total_base_cost = money(total_cost * rate)

        legs = [
            Leg(inventory_account, total_cost, currency=currency, rate_to_base=rate),
            Leg(cash_account, credit=total_cost, currency=currency, rate_to_base=rate),
        ]
        entry = self._store_entry(
            trade_id,
            d,
            self._balance_generated_legs(legs),
            memo,
            None,
        )
        payload = {
            "symbol": symbol,
            "currency": currency,
            "quantity": qty,
            "unit_cost": price,
            "rate_to_base": rate,
            "total_cost": total_cost,
            "total_base_cost": total_base_cost,
        }
        event = _Event(trade_id, "buy", d, entry.sequence, symbol, payload)
        return self._commit(entry, event)

    def record_sell(
        self,
        trade_id: str,
        trade_date,
        symbol: str,
        quantity,
        unit_proceeds,
        cash_account: str,
        inventory_account: str,
        pnl_account: str,
        rate_to_base=1,
        memo: str = "",
    ) -> Entry:
        d = as_date(trade_date)
        self._ensure_unlocked(d, "add sell")
        self.account(cash_account)
        self.account(inventory_account)
        self.account(pnl_account)
        if len({cash_account, inventory_account, pnl_account}) != 3:
            raise ValidationError("cash, inventory, and P/L accounts must be distinct")
        if self.fx_variance_account in {cash_account, inventory_account, pnl_account}:
            raise ValidationError("trade economic accounts cannot use the FX variance account")
        self._validate_symbol(symbol)
        qty = self._positive_quantity(quantity)
        proceeds_price = self._positive_money(unit_proceeds)
        rate = self._positive_rate(rate_to_base)
        proceeds = money(qty * proceeds_price)
        base_proceeds = money(proceeds * rate)

        state = self._state_at(d, include_sequence=self._seq + 1)
        available = self._available_quantity(state, symbol)
        if qty > available:
            raise LotError(
                f"cannot sell {qty} shares of {symbol}; only {available} are open"
            )

        matches = self._fifo_matches(state, symbol, qty)
        cost = money(sum((m.cost for m in matches), Decimal("0")))
        base_cost = money(sum((m.base_cost for m in matches), Decimal("0")))
        currency = matches[0].currency
        base_pnl = money(base_proceeds - base_cost)

        pnl_debit = max(-base_pnl, Decimal("0"))
        pnl_credit = max(base_pnl, Decimal("0"))
        legs = [Leg(cash_account, proceeds, currency=currency, rate_to_base=rate)]
        for match in matches:
            legs.append(
                Leg(
                    inventory_account,
                    credit=match.cost,
                    currency=match.currency,
                    rate_to_base=match.rate_to_base,
                )
            )
        if pnl_credit:
            legs.append(Leg(pnl_account, credit=pnl_credit, currency=self.base_currency))
        if pnl_debit:
            legs.append(Leg(pnl_account, debit=pnl_debit, currency=self.base_currency))

        entry = self._store_entry(
            trade_id,
            d,
            self._balance_generated_legs(legs),
            memo,
            None,
        )
        payload = {
            "symbol": symbol,
            "currency": currency,
            "quantity": qty,
            "rate_to_base": rate,
            "proceeds": proceeds,
            "base_proceeds": base_proceeds,
            "pnl_account": pnl_account,
            "matches": matches,
        }
        event = _Event(trade_id, "sell", d, entry.sequence, symbol, payload)
        return self._commit(entry, event)

    def record_stock_split(
        self,
        event_id: str,
        split_date,
        symbol: str,
        factor,
        memo: str = "",
    ) -> _Event:
        d = as_date(split_date)
        self._ensure_unlocked(d, "add stock split")
        self._validate_symbol(symbol)
        split_factor = D(factor)
        if split_factor <= 0:
            raise ValidationError("stock split factor must be positive")
        if split_factor == 1:
            raise ValidationError("stock split factor must differ from 1")

        state = self._state_at(d, include_sequence=self._seq + 1)
        affected = tuple(
            lot_id
            for lot_id, lot in state.lots.items()
            if lot.symbol == symbol and lot.remaining_quantity > 0
        )
        if not affected:
            raise LotError(f"no open lots to split for {symbol}")
        if event_id in self._entries or any(event.id == event_id for event in self._events):
            raise DuplicateIdError(f"id already exists: {event_id}")

        event = _Event(
            event_id,
            "split",
            d,
            self._next_sequence(),
            symbol,
            {"factor": split_factor, "lot_ids": affected},
        )
        self._commit_event(event)
        return event

    def record_cash_dividend(
        self,
        dividend_id: str,
        dividend_date,
        symbol: str,
        amount,
        cash_account: str,
        dividend_income_account: str,
        currency: str = BASE_CURRENCY,
        rate_to_base=1,
        memo: str = "",
    ) -> Entry:
        d = as_date(dividend_date)
        self._ensure_unlocked(d, "add dividend")
        self.account(cash_account)
        self.account(dividend_income_account)
        if cash_account == dividend_income_account:
            raise ValidationError("cash and dividend income accounts must be distinct")
        if self.fx_variance_account in {cash_account, dividend_income_account}:
            raise ValidationError("dividend economic accounts cannot use the FX variance account")
        self._validate_symbol(symbol)
        dividend_amount = self._positive_money(amount)
        rate = self._positive_rate(rate_to_base)
        currency = currency.upper()

        legs = [
            Leg(cash_account, dividend_amount, currency=currency, rate_to_base=rate),
            Leg(
                dividend_income_account,
                credit=dividend_amount,
                currency=currency,
                rate_to_base=rate,
            ),
        ]
        entry = self._store_entry(
            dividend_id,
            d,
            self._balance_generated_legs(legs),
            memo,
            None,
        )
        payload = {
            "symbol": symbol,
            "currency": currency,
            "amount": dividend_amount,
            "rate_to_base": rate,
        }
        event = _Event(dividend_id, "dividend", d, entry.sequence, symbol, payload)
        return self._commit(entry, event)

    def positions_at(self, as_of=None) -> Tuple[Position, ...]:
        """Return open positions at ledger end-of-day on ``as_of``."""
        state = self._state_at(date.max if as_of is None else as_date(as_of))
        grouped: Dict[Tuple[str, str], dict] = {}
        for lot in state.lots.values():
            if lot.remaining_quantity <= 0:
                continue
            key = (lot.symbol, lot.currency)
            item = grouped.setdefault(
                key,
                {
                    "quantity": Decimal("0"),
                    "cost": Decimal("0"),
                    "base_cost": Decimal("0"),
                    "lots": [],
                },
            )
            item["quantity"] += lot.remaining_quantity
            item["cost"] += lot.remaining_cost
            item["base_cost"] += lot.remaining_base_cost
            item["lots"].append(lot.id)
        return tuple(
            Position(
                symbol,
                currency,
                fine(item["quantity"]),
                money(item["cost"]),
                money(item["base_cost"]),
                tuple(item["lots"]),
            )
            for (symbol, currency), item in sorted(grouped.items())
        )

    def lot(self, lot_id: str, as_of=None) -> Lot:
        state = self._state_at(date.max if as_of is None else as_date(as_of))
        try:
            return state.lots[lot_id]
        except KeyError:
            raise LotError(f"unknown lot: {lot_id}") from None

    def lots_for_symbol(self, symbol: str, as_of=None) -> Tuple[Lot, ...]:
        state = self._state_at(date.max if as_of is None else as_date(as_of))
        return tuple(lot for lot in state.lots.values() if lot.symbol == symbol)

    def realized_pnl(self, start=None, end=None, symbol: Optional[str] = None) -> Tuple[RealizedPnl, ...]:
        start_d = date.min if start is None else as_date(start)
        end_d = date.max if end is None else as_date(end)
        state = self._state_at(date.max)
        result = [
            item
            for item in state.realized
            if start_d <= item.date <= end_d and (symbol is None or item.symbol == symbol)
        ]
        return tuple(sorted(result, key=lambda x: (x.date, x.sequence, x.event_id)))

    def _store_entry(
        self,
        entry_id: str,
        d: date,
        legs: Sequence[Leg],
        memo: str,
        reversal_of: Optional[str],
    ) -> Entry:
        if not isinstance(entry_id, str) or not entry_id:
            raise ValidationError("entry id must be a non-empty string")
        if entry_id in self._entries:
            raise DuplicateIdError(f"entry already exists: {entry_id}")
        entry = Entry(
            entry_id,
            d,
            tuple(legs),
            memo,
            reversal_of,
            self._next_sequence(),
        )
        self._entries[entry_id] = entry
        return entry

    def _commit(self, entry: Entry, event: _Event) -> Entry:
        self._events.append(event)
        try:
            self._validate_replay()
        except Exception:
            self._events.remove(event)
            if self._entries.get(event.id) is entry:
                del self._entries[event.id]
            raise
        return entry

    def _commit_event(self, event: _Event) -> _Event:
        self._events.append(event)
        try:
            self._validate_replay()
        except Exception:
            self._events.remove(event)
            raise
        return event

    def _validate_replay(self) -> _State:
        return self._replay(self._ordered_events())

    def _ordered_events(self) -> Tuple[_Event, ...]:
        return tuple(sorted(self._events, key=lambda e: (e.event_date, e.sequence, e.id)))

    def _state_at(self, d: date, include_sequence: Optional[int] = None) -> _State:
        events = []
        for event in self._ordered_events():
            if event.event_date < d:
                events.append(event)
            elif event.event_date == d and (
                include_sequence is None or event.sequence < include_sequence
            ):
                events.append(event)
        return self._replay(tuple(events))

    def _next_sequence(self) -> int:
        self._seq += 1
        return self._seq

    def _event(self, event_id: str) -> _Event:
        for event in self._events:
            if event.id == event_id:
                return event
        raise ValidationError(f"unknown event: {event_id}")

    def _ensure_unlocked(self, value, operation: str) -> None:
        d = as_date(value)
        if period_key(d) in self._locked:
            raise LockedPeriodError(f"cannot {operation} in locked month {d:%Y-%m}")

    def _validate_symbol(self, symbol: str) -> None:
        if not isinstance(symbol, str) or not symbol:
            raise ValidationError("symbol must be a non-empty string")

    def _positive_quantity(self, value) -> Decimal:
        result = fine(D(value))
        if result <= 0:
            raise ValidationError("quantity must be positive")
        return result

    def _positive_money(self, value) -> Decimal:
        result = money(D(value))
        if result <= 0:
            raise ValidationError("amount must be positive")
        return result

    def _positive_rate(self, value) -> Decimal:
        result = D(value)
        if result <= 0:
            raise ValidationError("rate_to_base must be positive")
        return result

    def _has_reversal(self, entry_id: str) -> bool:
        return any(entry.reversal_of == entry_id for entry in self._entries.values())

    def _prepare_leg(self, value: Leg | Mapping) -> Leg:
        if isinstance(value, Mapping):
            try:
                leg = Leg(
                    account=value["account"],
                    debit=value.get("debit", 0),
                    credit=value.get("credit", 0),
                    currency=value.get("currency", self.base_currency),
                    rate_to_base=value.get("rate_to_base", 1),
                )
            except KeyError as exc:
                raise ValidationError(f"leg missing field: {exc.args[0]}") from None
        elif isinstance(value, Leg):
            # Rebuild through Leg so callers using the default CNY constant get
            # the current ledger's base currency validation.
            leg = Leg(
                value.account,
                value.debit,
                value.credit,
                value.currency,
                value.rate_to_base,
            )
        else:
            raise ValidationError("legs must be Leg objects or mappings")

        self.account(leg.account)
        if leg.debit < 0 or leg.credit < 0:
            raise ValidationError("negative legs are only allowed on generated reversals")
        if leg.debit == 0 and leg.credit == 0:
            raise ValidationError("a leg cannot be zero")
        if leg.debit > 0 and leg.credit > 0:
            raise ValidationError("a leg cannot be both debit and credit")
        if leg.rate_to_base <= 0:
            raise ValidationError("rate_to_base must be positive")
        if leg.currency == self.base_currency and leg.rate_to_base != 1:
            raise ValidationError(f"{self.base_currency} legs must use rate_to_base=1")
        return leg

    def _balance_manual_legs(self, legs: Sequence[Leg]) -> list[Leg]:
        return self._balance_legs(list(legs), reject_imbalance=True)

    def _balance_generated_legs(self, legs: Sequence[Leg]) -> list[Leg]:
        return self._balance_legs(list(legs), reject_imbalance=True)

    def _balance_legs(self, legs: list[Leg], reject_imbalance: bool) -> list[Leg]:
        del reject_imbalance
        accounts = [leg.account for leg in legs]
        if len(legs) < 2 or len(set(accounts)) < 2:
            raise ValidationError("every entry must have at least two distinct accounts")

        debit = money(sum((leg.base_debit for leg in legs), Decimal("0")))
        credit = money(sum((leg.base_credit for leg in legs), Decimal("0")))
        residual = money(credit - debit)
        if residual == 0:
            return legs
        if abs(residual) > MAX_ROUNDING_RESIDUAL:
            raise ValidationError(
                f"entry is not balanced in {self.base_currency}: "
                f"debit {debit}, credit {credit}"
            )
        if any(leg.account == self.fx_variance_account for leg in legs):
            raise ValidationError(
                f"entry using {self.fx_variance_account} must already balance exactly"
            )
        fx_leg = Leg(
            self.fx_variance_account,
            debit=abs(residual) if residual > 0 else Decimal("0"),
            credit=abs(residual) if residual < 0 else Decimal("0"),
            currency=self.base_currency,
        )
        return legs + [fx_leg]

    def _available_quantity(self, state: _State, symbol: str) -> Decimal:
        return fine(
            sum(
                (
                    lot.remaining_quantity
                    for lot in state.lots.values()
                    if lot.symbol == symbol and not lot.closed
                ),
                Decimal("0"),
            )
        )

    def _assert_single_lot_currency(self, state: _State, symbol: str, currency: str) -> None:
        currencies = {
            lot.currency
            for lot in state.lots.values()
            if lot.symbol == symbol and lot.remaining_quantity > 0
        }
        if currencies - {currency}:
            raise ValidationError(
                f"open lots of {symbol} use a different transaction currency"
            )

    def _fifo_matches(self, state: _State, symbol: str, quantity: Decimal) -> Tuple[LotMatch, ...]:
        target = fine(quantity)
        remaining_target = target
        matches: list[LotMatch] = []

        open_lots = [
            lot
            for lot in state.lots.values()
            if lot.symbol == symbol and lot.remaining_quantity > 0
        ]
        if not open_lots:
            raise LotError(f"no open lots for {symbol}")
        currency = open_lots[0].currency

        for lot in open_lots:
            if remaining_target == 0:
                break
            take = fine(min(remaining_target, lot.remaining_quantity))
            if take <= 0:
                continue
            ratio = take / lot.remaining_quantity
            take_cost = money(lot.remaining_cost * ratio)
            take_base_cost = money(lot.remaining_base_cost * ratio)
            if take == lot.remaining_quantity:
                take_cost = money(lot.remaining_cost)
                take_base_cost = money(lot.remaining_base_cost)
            matches.append(
                LotMatch(
                    lot.id,
                    take,
                    take_cost,
                    take_base_cost,
                    lot.currency,
                    lot.rate_to_base,
                )
            )
            remaining_target = fine(remaining_target - take)

        total = fine(sum((match.quantity for match in matches), Decimal("0")))
        if total != target:
            raise LotError(f"cannot sell {target} shares of {symbol}; only {total} are open")
        return tuple(matches)

    def _assert_same_matches(self, expected: Tuple[LotMatch, ...], stored: Tuple[LotMatch, ...]) -> None:
        normalized_expected = tuple(sorted((m.lot_id, m.quantity) for m in expected))
        normalized_stored = tuple(sorted((m.lot_id, m.quantity) for m in stored))
        if normalized_expected != normalized_stored:
            raise LotError("stored FIFO matches no longer agree with event replay")


    def _replay(self, events: Sequence[_Event]) -> _State:
        state = _State()
        for event in events:
            if event.kind == "buy":
                self._replay_buy(state, event)
            elif event.kind == "sell":
                self._replay_sell(state, event)
            elif event.kind == "split":
                self._replay_split(state, event)
            elif event.kind == "buy_reversal":
                self._replay_buy_reversal(state, event)
            elif event.kind == "sell_reversal":
                self._replay_sell_reversal(state, event)
            # Manual postings and dividend reversals have no lot impact.
        return state

    def _replay_buy(self, state: _State, event: _Event) -> None:
        p = event.payload
        if event.id in state.lots:
            raise DuplicateIdError(f"lot already exists: {event.id}")
        self._assert_single_lot_currency(state, p["symbol"], p["currency"])
        qty = fine(p["quantity"])
        total_cost = money(p["total_cost"])
        total_base_cost = money(p["total_base_cost"])
        state.lots[event.id] = Lot(
            event.id,
            p["symbol"],
            p["currency"],
            p["rate_to_base"],
            event.event_date,
            event.sequence,
            qty,
            qty,
            fine(total_cost / qty),
            fine(total_base_cost / qty),
            total_cost,
            total_cost,
            total_base_cost,
            total_base_cost,
        )

    def _replay_sell(self, state: _State, event: _Event) -> None:
        p = event.payload
        expected = self._fifo_matches(state, p["symbol"], p["quantity"])
        self._assert_same_matches(expected, tuple(p["matches"]))

        total_cost = Decimal("0")
        total_base_cost = Decimal("0")
        for match in expected:
            lot = state.lots[match.lot_id]
            if lot.remaining_quantity < match.quantity:
                raise LotError(f"lot {lot.id} does not contain {match.quantity} shares")
            lot.remaining_quantity = fine(lot.remaining_quantity - match.quantity)
            lot.remaining_cost = money(lot.remaining_cost - match.cost)
            lot.remaining_base_cost = money(lot.remaining_base_cost - match.base_cost)
            lot.closed = lot.remaining_quantity == 0
            total_cost += match.cost
            total_base_cost += match.base_cost

        total_cost = money(total_cost)
        total_base_cost = money(total_base_cost)
        base_pnl = money(p["base_proceeds"] - total_base_cost)
        state.realized.append(
            RealizedPnl(
                event.event_date,
                event.sequence,
                event.id,
                p["symbol"],
                p["currency"],
                fine(p["quantity"]),
                money(p["proceeds"]),
                money(p["base_proceeds"]),
                total_cost,
                total_base_cost,
                base_pnl,
                p["pnl_account"],
                expected,
            )
        )
        state.sales[event.id] = _Sale(
            event.event_date,
            event.id,
            p["symbol"],
            p["currency"],
            fine(p["quantity"]),
            money(p["proceeds"]),
            money(p["base_proceeds"]),
            p["pnl_account"],
            expected,
            total_cost,
            total_base_cost,
        )

    def _replay_split(self, state: _State, event: _Event) -> None:
        factor = D(event.payload["factor"])
        for lot_id in event.payload["lot_ids"]:
            try:
                lot = state.lots[lot_id]
            except KeyError:
                raise LotError(f"split references unknown lot: {lot_id}") from None
            if lot.symbol != event.symbol or lot.remaining_quantity <= 0:
                raise LotError(f"lot {lot_id} cannot be split")
            lot.original_quantity = fine(lot.original_quantity * factor)
            lot.remaining_quantity = fine(lot.remaining_quantity * factor)
            lot.unit_cost = fine(lot.total_cost / lot.original_quantity)
            lot.base_unit_cost = fine(lot.total_base_cost / lot.original_quantity)

    def _replay_buy_reversal(self, state: _State, event: _Event) -> None:
        original_id = event.payload["original_event"]
        try:
            lot = state.lots[original_id]
        except KeyError:
            raise LotError(f"buy reversal references unknown lot: {original_id}") from None
        if lot.remaining_cost != lot.total_cost or lot.remaining_base_cost != lot.total_base_cost:
            raise LotError(
                f"cannot fully reverse buy {original_id}; lot has FIFO consumption"
            )
        lot.remaining_quantity = Decimal("0")
        lot.remaining_cost = Decimal("0.00")
        lot.remaining_base_cost = Decimal("0.00")
        lot.closed = True

    def _replay_sell_reversal(self, state: _State, event: _Event) -> None:
        sale_id = event.payload["original_event"]
        try:
            sale = state.sales[sale_id]
        except KeyError:
            raise LotError(f"sell reversal references unknown sale: {sale_id}") from None

        for index, match in enumerate(sale.matches, start=1):
            lot_id = f"{sale_id}-REV-{index}"
            qty = fine(match.quantity)
            total_cost = money(match.cost)
            total_base_cost = money(match.base_cost)
            state.lots[lot_id] = Lot(
                lot_id,
                sale.symbol,
                match.currency,
                match.rate_to_base,
                event.event_date,
                event.sequence + index,
                qty,
                qty,
                fine(total_cost / qty),
                fine(total_base_cost / qty),
                total_cost,
                total_cost,
                total_base_cost,
                total_base_cost,
            )

        state.realized.append(
            RealizedPnl(
                event.event_date,
                event.sequence,
                event.id,
                sale.symbol,
                sale.currency,
                sale.quantity,
                -sale.proceeds,
                -sale.base_proceeds,
                sale.total_cost,
                sale.total_base_cost,
                money(-sale.base_proceeds + sale.total_base_cost),
                sale.pnl_account,
                sale.matches,
            )
        )
