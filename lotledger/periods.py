from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from .models import LockedPeriodError


class PeriodLocks:
    def __init__(self, timezone: str = "Asia/Shanghai"):
        self.timezone = ZoneInfo(timezone)
        self.locked_months: set[tuple[int, int]] = set()

    def localize(self, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=self.timezone)
        return value.astimezone(self.timezone)

    def lock_month(self, year: int, month: int) -> None:
        self.locked_months.add((year, month))

    def is_locked(self, timestamp: datetime) -> bool:
        ts = self.localize(timestamp)
        return (ts.year, ts.month) in self.locked_months

    def ensure_unlocked(self, timestamp: datetime) -> datetime:
        ts = self.localize(timestamp)
        if self.is_locked(ts):
            raise LockedPeriodError(f"{ts.year}-{ts.month:02d} is locked")
        return ts

    def check_timestamp(
        self, timestamp: datetime, latest_timestamps
    ) -> datetime:
        ts = self.localize(timestamp)
        latest = max(latest_timestamps, default=None)
        if latest is not None and ts < latest:
            from .models import LedgerError

            raise LedgerError("events must be appended in chronological order")
        return self.ensure_unlocked(ts)

    def ensure_reversal_later_than_locked_original(
        self, reversal_timestamp: datetime, original_timestamp: datetime, noun: str
    ) -> datetime:
        ts = self.localize(reversal_timestamp)
        original = self.localize(original_timestamp)
        if self.is_locked(original) and (ts.year, ts.month) <= (
            original.year,
            original.month,
        ):
            raise LockedPeriodError(
                f"a locked-period {noun} must be reversed in a later period"
            )
        return ts
