from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from .entries import EntryValidator, inverse_legs
from .models import (
    Account,
    Entry,
    Led,
    LedgerError,
    LockedPeriodError,
    Lot,
    OversoldError,
    Split,
)
from .periods import PeriodLocks
from .queries import (
    day_cutoff,
    lot_remaining as get_lot_remaining,
    positions_at as query_positions_at,
    positions_by_portfolio as query_positions_by_portfolio,
    realized_pnl as query_realized_pnl,
    replay,
)
from .values import (
    COST_METHODS,
    DEFAULT_PORTFOLIO,
    as_decimal,
    fx_rate,
    money,
    qty,
    to_base,
)


class Ledger:
    """Append-only CNY ledger with FIFO lots and month locks."""

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
        self.locks = PeriodLocks(timezone)
        self.timezone = self.locks.timezone
        self.fx_account = fx_account
        self.investment_account = investment_account
        self.realized_pnl_account = realized_pnl_account
        self.dividend_income_account = dividend_income_account
        self.accounts: dict[str, Account] = {}
        self.entries: list[Entry] = []
        self.splits: list[Split] = []
        self._event_seq = 0
        self._lot_seq = 0
        self.validator = EntryValidator(
            self.accounts, self.base_currency, self.fx_account
        )
        for code in {
            fx_account,
            investment_account,
            realized_pnl_account,
            dividend_income_account,
        }:
            self.add_account(Account(code))

    @property
    def locked_months(self):
        return self.locks.locked_months

    def add_account(self, account: Account | str) -> Account:
        if isinstance(account, str):
            account = Account(account)
        if account.code in self.accounts:
            raise LedgerError(f"account {account.code} already exists")
        self.accounts[account.code] = account
        return account

    def localize(self, value: datetime) -> datetime:
        return self.locks.localize(value)

    def lock_month(self, year: int, month: int) -> None:
        self.locks.lock_month(year, month)

    def is_locked(self, timestamp: datetime) -> bool:
        return self.locks.is_locked(timestamp)

    def _require_account(self, code: str) -> None:
        self.validator.require_account(code)

    def _check_timestamp(self, timestamp: datetime) -> datetime:
        latest = [event.timestamp for event in self.entries]
        latest.extend(event.timestamp for event in self.splits)
        return self.locks.check_timestamp(timestamp, latest)

    def _balanced_legs(self, raw_legs: list[dict]) -> tuple[Led, ...]:
        return self.validator.balanced_legs(raw_legs)

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

    def post(self, entry_id, timestamp, legs, *, memo: str = "") -> Entry:
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
        return self._append(entry_id, timestamp, legs, memo=memo, kind="buy", action=action)

    def _resolve_method(self, portfolio: str, symbol: str, method: str | None) -> str:
        """Pick the cost method, locking it on first open for a key."""
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
        amount_base = to_base(cash, ratio)
        legs = [
            {
                "account": cash_account,
                "debit": cash,
                "currency": currency,
                "fx_rate": ratio,
                "amount_base": amount_base,
            },
            {
                "account": self.dividend_income_account,
                "credit": cash,
                "currency": currency,
                "fx_rate": ratio,
                "amount_base": amount_base,
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
        """Bonus-style allotment: more shares, same total cost."""
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
        ts = self.locks.ensure_reversal_later_than_locked_original(
            timestamp, original.timestamp, "entry"
        )
        action = (
            {"type": f"reverse_{original.kind}", "original_entry_id": original_id}
            if original.kind in {"buy", "sell"}
            else {}
        )
        return self._append(
            reversal_id,
            ts,
            inverse_legs(original),
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
        ts = self.locks.ensure_reversal_later_than_locked_original(
            timestamp, original.timestamp, "split"
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

    def _replay(self, cutoff: datetime | None = None):
        localized_cutoff = self.localize(cutoff) if cutoff is not None else None
        return replay(self.entries, self.splits, localized_cutoff)

    def _day_cutoff(self, date_value) -> datetime:
        return day_cutoff(date_value, self.timezone)

    def positions_at(self, date_value, portfolio: str | None = None):
        """Return quantity and CNY cost per symbol at ledger end-of-day."""
        cutoff = self._day_cutoff(date_value)
        return query_positions_at(
            self.entries, self.splits, cutoff, portfolio=portfolio
        )

    def positions_by_portfolio(self, date_value):
        """Return end-of-day position summary grouped by portfolio, then symbol."""
        cutoff = self._day_cutoff(date_value)
        return query_positions_by_portfolio(self.entries, self.splits, cutoff)

    def lot_remaining(self, lot_id: str, date_value=None) -> Lot:
        cutoff = self._day_cutoff(date_value) if date_value is not None else None
        return get_lot_remaining(self.entries, self.splits, lot_id, cutoff)

    def realized_pnl(self, start_date, end_date) -> Decimal:
        """Sum realized P/L for entries in the inclusive date range."""
        start = self._day_cutoff(start_date).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        end = self._day_cutoff(end_date)
        return query_realized_pnl(self.entries, self.get_entry, start, end)
