from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from .amounts import (
    COST_METHODS,
    DEFAULT_PORTFOLIO,
    MAX_PLUG,
    MONEY,
    QTY,
    RATE,
    as_decimal,
    money,
    qty,
)
from . import lots, queries
from .entry_validation import AccountDirectory, EntryValidator
from .errors import (
    LedgerError,
    LockedPeriodError,
    OversoldError,
    UnbalancedEntryError,
    UnknownAccountError,
)
from .fx import normalize_fx_rate as fx_rate, to_base
from .models import Account, Entry, Led, Lot, Split
from .period_locks import PeriodLock
from .replay import replay


class Ledger:
    """Append-only CNY ledger with FIFO/LIFO lots and month locks.

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
        self.locks = PeriodLock(timezone)
        self.timezone = self.locks.timezone
        self.fx_account = fx_account
        self.investment_account = investment_account
        self.realized_pnl_account = realized_pnl_account
        self.dividend_income_account = dividend_income_account
        self.account_directory = AccountDirectory()
        self.accounts = self.account_directory.accounts
        self.entry_validator = EntryValidator(
            self.account_directory,
            base_currency=base_currency,
            fx_account=fx_account,
        )
        self.entries: list[Entry] = []
        self.splits: list[Split] = []
        self.locked_months = self.locks.locked_months
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
        return self.account_directory.add(account)

    def localize(self, value: datetime) -> datetime:
        return self.locks.localize(value)

    def lock_month(self, year: int, month: int) -> None:
        self.locks.lock_month(year, month)

    def is_locked(self, timestamp: datetime) -> bool:
        return self.locks.is_locked(timestamp)

    def _require_account(self, code: str) -> None:
        self.account_directory.require(code)

    def _latest_timestamp(self):
        timestamps = [event.timestamp for event in self.entries]
        timestamps.extend(event.timestamp for event in self.splits)
        return max(timestamps, default=None)

    def _check_timestamp(self, timestamp: datetime) -> datetime:
        return self.locks.require_chronological(timestamp, self._latest_timestamp())

    def _balanced_legs(self, raw_legs: list[dict]) -> tuple[Led, ...]:
        return self.entry_validator.balanced_legs(raw_legs)

    def _append(
        self,
        entry_id: str,
        timestamp: datetime,
        legs: list[dict],
        *,
        memo: str,
        kind: str,
        reversal_of: str | None = None,
        action: dict | None = None,
    ) -> Entry:
        existing_ids = {entry.id for entry in self.entries}
        if entry_id in existing_ids:
            raise LedgerError(f"entry {entry_id} already exists")
        if reversal_of and reversal_of not in existing_ids:
            raise LedgerError(f"reversal target {reversal_of} does not exist")
        if reversal_of and any(
            existing.reversal_of == reversal_of for existing in self.entries
        ):
            raise LedgerError(f"entry {reversal_of} already has a reversal")
        localized = self._check_timestamp(timestamp)
        balanced = self._balanced_legs(legs)
        entry = Entry(
            entry_id,
            localized,
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
        legs: list[dict],
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
        cost_base = to_base(gross, ratio)
        self._lot_seq += 1
        lot_id = f"LOT-{self._lot_seq:06d}"
        ledger_legs = [
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
            entry_id, timestamp, ledger_legs, memo=memo, kind="buy", action=action
        )

    def _resolve_method(self, portfolio: str, symbol: str, method: str | None) -> str:
        """Pick a method, preserving the first choice for this holding."""
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
        if shares <= 0:
            raise OversoldError("sell quantity must be positive")
        if price <= 0:
            raise LedgerError("sell price must be positive")
        proceeds_native = money(shares * price)
        proceeds_base = to_base(proceeds_native, ratio)

        trial = self._replay()
        action = {
            "type": "sell",
            "symbol": symbol,
            "quantity": str(shares),
            "proceeds_base": str(proceeds_base),
            "realized_pnl": "0.00",
            "portfolio": portfolio,
        }
        lots.consume_sell(trial.lot_states, trial.methods, action)
        cost_base = money(
            sum(
                (as_decimal(item["cost_base"]) for item in action["allocations"]),
                Decimal("0"),
            )
        )
        pnl = money(proceeds_base - cost_base)
        action["cost_base"] = str(cost_base)
        action["realized_pnl"] = str(pnl)

        ledger_legs = [
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
            ledger_legs.append(
                {
                    "account": self.realized_pnl_account,
                    "debit": abs(pnl) if pnl < 0 else 0,
                    "credit": pnl if pnl > 0 else 0,
                    "currency": self.base_currency,
                    "fx_rate": 1,
                }
            )
        return self._append(
            entry_id, timestamp, ledger_legs, memo=memo, kind="sell", action=action
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
        ledger_legs = [
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
            entry_id,
            timestamp,
            ledger_legs,
            memo=memo,
            kind="dividend",
            action=action,
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
        localized = self._check_timestamp(timestamp)
        new = as_decimal(new_shares)
        old = as_decimal(old_shares)
        if new <= 0 or old <= 0:
            raise LedgerError("split ratio parts must be positive")
        split = Split(
            split_id, localized, symbol, new, old, memo, sequence=self._event_seq
        )
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
        """Bonus issue: more shares, unchanged total CNY cost, lower unit cost."""
        new = as_decimal(new_shares)
        old = as_decimal(old_shares)
        if new <= 0 or old <= 0:
            raise LedgerError("rights issue ratio parts must be positive")
        state = self._replay()
        affected = money(
            sum(
                (
                    lot_state.lot.remaining_cost
                    for lot_state in state.lot_states.values()
                    if not lot_state.lot.closed and lot_state.lot.symbol == symbol
                ),
                Decimal("0"),
            )
        )
        if affected <= 0:
            raise LedgerError(f"no open lots for {symbol}; nothing to adjust")
        ledger_legs = [
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
            entry_id,
            timestamp,
            ledger_legs,
            memo=text,
            kind="rights_issue",
            action=action,
        )
        split_id = f"{entry_id}-SPLIT"
        if split_id in {split.id for split in self.splits}:
            raise LedgerError(f"split {split_id} already exists")
        localized = self._check_timestamp(entry.timestamp)
        split = Split(
            split_id,
            localized,
            symbol,
            new + old,
            old,
            text,
            sequence=self._event_seq,
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
        localized = self.localize(timestamp)
        self.locks.require_reversal_period(localized, original.timestamp)
        inverse_legs = self.entry_validator.inverse_legs(original)
        action = (
            {"type": f"reverse_{original.kind}", "original_entry_id": original_id}
            if original.kind in {"buy", "sell"}
            else {}
        )
        return self._append(
            reversal_id,
            localized,
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
        localized = self.localize(timestamp)
        self.locks.require_reversal_period(localized, original.timestamp)
        if any(split.reversal_of == original_id for split in self.splits):
            raise LedgerError(f"split {original_id} already has a reversal")
        checked = self._check_timestamp(localized)
        reversal = Split(
            reversal_id,
            checked,
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

    def _day_cutoff(self, date_value) -> datetime:
        return queries.day_cutoff(date_value, self.timezone)

    def _replay(self, cutoff: datetime | None = None):
        localized = self.localize(cutoff) if cutoff is not None else None
        return replay(self.entries, self.splits, localized)

    def positions_at(
        self, date_value, portfolio: str | None = None
    ) -> dict[str, dict[str, Decimal]]:
        """Return quantity and CNY cost per symbol at ledger end-of-day."""
        cutoff = self._day_cutoff(date_value)
        return queries.positions(self.entries, self.splits, cutoff, portfolio)

    def positions_by_portfolio(
        self, date_value
    ) -> dict[str, dict[str, dict[str, Decimal]]]:
        """Return end-of-day quantity and CNY cost by portfolio, then symbol."""
        cutoff = self._day_cutoff(date_value)
        result: dict[str, dict[str, dict[str, Decimal]]] = {}
        for (pf, symbol), pos in queries.positions_by_portfolio(
            self.entries, self.splits, cutoff
        ).items():
            result.setdefault(pf, {})[symbol] = {
                "quantity": pos.quantity,
                "cost_base": pos.cost_base,
            }
        return result

    def lot_remaining(self, lot_id: str, date_value=None) -> Lot:
        cutoff = self._day_cutoff(date_value) if date_value is not None else None
        return queries.lot_remaining(self.entries, self.splits, lot_id, cutoff)

    def realized_pnl(self, start_date, end_date) -> Decimal:
        """Sum realized P/L for entries in the inclusive ledger-date range."""
        start = self._day_cutoff(start_date).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        end = self._day_cutoff(end_date)
        return queries.realized_pnl(self.entries, start, end, self.get_entry)
