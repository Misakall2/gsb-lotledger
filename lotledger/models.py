from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Mapping

from .values import DEFAULT_PORTFOLIO


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
class LotState:
    lot: Lot
    scale: Decimal = Decimal(1)
    sequence: int = 0


@dataclass(frozen=True)
class Position:
    quantity: Decimal
    cost_base: Decimal
