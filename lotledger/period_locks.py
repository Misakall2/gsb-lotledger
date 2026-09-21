from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from .errors import LedgerError, LockedPeriodError


class PeriodLock:
    def __init__(self, timezone: str):
        self.timezone = ZoneInfo(timezone)
        self.locked_months: set[tuple[int, int]] = set()

    def localize(self, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=self.timezone)
        return value.astimezone(self.timezone)

    def lock_month(self, year: int, month: int) -> None:
        self.locked_months.add((year, month))

    def is_locked(self, timestamp: datetime) -> bool:
        localized = self.localize(timestamp)
        return (localized.year, localized.month) in self.locked_months

    def require_open(self, timestamp: datetime) -> datetime:
        localized = self.localize(timestamp)
        if self.is_locked(localized):
            raise LockedPeriodError(
                f"{localized.year}-{localized.month:02d} is locked"
            )
        return localized

    def require_chronological(self, timestamp: datetime, latest) -> datetime:
        localized = self.localize(timestamp)
        if latest is not None and localized < latest:
            raise LedgerError("events must be appended in chronological order")
        if self.is_locked(localized):
            raise LockedPeriodError(
                f"{localized.year}-{localized.month:02d} is locked"
            )
        return localized

    def require_reversal_period(self, reversal_ts, original_ts) -> None:
        localized = self.localize(reversal_ts)
        if self.is_locked(original_ts) and (
            (localized.year, localized.month)
            <= (original_ts.year, original_ts.month)
        ):
            raise LockedPeriodError(
                "a locked-period event must be reversed in a later period"
            )
