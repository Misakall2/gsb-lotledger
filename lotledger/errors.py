
class LedgerError(Exception):
    """Base error for all ledger failures."""


class AccountError(LedgerError):
    """An account does not exist or is invalid."""


class ValidationError(LedgerError):
    """An entry or event violates ledger rules."""


class DuplicateIdError(LedgerError):
    """An entry, account, or event id is already used."""


class LockedPeriodError(LedgerError):
    """An operation changes a locked accounting period."""


class LotError(LedgerError):
    """A FIFO lot operation is impossible, such as selling too many shares."""
