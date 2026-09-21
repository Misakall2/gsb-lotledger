from __future__ import annotations

from decimal import Decimal
from typing import Mapping

from .amounts import MAX_PLUG, money
from .errors import (
    LedgerError,
    UnbalancedEntryError,
    UnknownAccountError,
)
from .fx import normalize_fx_rate, to_base
from .models import Account, Led


class AccountDirectory:
    def __init__(self):
        self.accounts: dict[str, Account] = {}

    def add(self, account: Account | str) -> Account:
        if isinstance(account, str):
            account = Account(account)
        if account.code in self.accounts:
            raise LedgerError(f"account {account.code} already exists")
        self.accounts[account.code] = account
        return account

    def require(self, code: str) -> None:
        if code not in self.accounts:
            raise UnknownAccountError(f"unknown account {code}")


class EntryValidator:
    def __init__(
        self,
        accounts: AccountDirectory,
        *,
        base_currency: str,
        fx_account: str,
    ):
        self.accounts = accounts
        self.base_currency = base_currency
        self.fx_account = fx_account

    def leg(self, raw: Mapping) -> Led:
        account = raw["account"]
        self.accounts.require(account)
        currency = raw.get("currency", self.base_currency)
        ratio = normalize_fx_rate(raw.get("fx_rate", Decimal(1)))
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
            money(raw["amount_base"]) if "amount_base" in raw else to_base(amount, ratio)
        )
        if amount_base <= 0:
            raise LedgerError("base amount must be positive")
        return Led(account, debit, credit, currency, ratio, amount_base)

    def balanced_legs(
        self, raw_legs: list[Mapping]
    ) -> tuple[Led, ...]:
        if len(raw_legs) < 2:
            raise UnbalancedEntryError("an entry needs at least two legs")
        legs = tuple(self.leg(raw) for raw in raw_legs)
        debit_base = sum(
            (leg.amount_base for leg in legs if leg.debit), Decimal("0")
        )
        credit_base = sum(
            (leg.amount_base for leg in legs if leg.credit), Decimal("0")
        )
        difference = money(debit_base - credit_base)
        is_cross_currency = any(leg.currency != self.base_currency for leg in legs)
        if difference and not is_cross_currency:
            raise UnbalancedEntryError(
                f"entry is unbalanced by {difference} {self.base_currency}"
            )
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

    @staticmethod
    def inverse_legs(entry) -> list[dict]:
        return [
            {
                "account": leg.account,
                "debit": leg.credit if leg.credit else 0,
                "credit": leg.debit if leg.debit else 0,
                "currency": leg.currency,
                "fx_rate": leg.fx_rate,
                "amount_base": leg.amount_base,
            }
            for leg in entry.legs
        ]
