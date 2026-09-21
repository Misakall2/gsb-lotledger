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
