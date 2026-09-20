# gsb-lotledger

Python 3 stdlib only. `python3 -m unittest discover -s . -v`

## Model

`lotledger.Ledger` is an import-only library. The base currency is fixed to
`CNY`, timestamps are interpreted in `Asia/Shanghai`, and day queries include
events through `23:59:59.999999` in that timezone.

Every entry stores the transaction currency, the entry-date FX rate to CNY,
and the CNY amount on each leg. Base-currency debit and credit totals must
balance. A residual difference of at most `0.02 CNY` is rounded to the `FX`
account; a larger difference raises `UnbalancedEntryError`.

Trades create or consume lots per `(portfolio, symbol)`. The cost method is
chosen on the first buy of a key with `method="FIFO"` (default) or
`method="LIFO"` and is locked from then on; a conflicting later method raises
`LedgerError`, while omitting it inherits the locked method. The stable
consumption key is `(opening timestamp, append sequence, lot id)`, ascending
for FIFO and descending for LIFO. Same-timestamp events are therefore
consumed in append order, regardless of entry id. A stock split multiplies
open quantities and divides unit cost while preserving total CNY cost.

Portfolios keep the same symbol's lots fully separate: `buy`/`sell` take a
`portfolio` argument (default `"DEFAULT"`), a sell only consumes lots of its
own portfolio, and overselling raises `OversoldError` even when another
portfolio still holds the symbol. `positions_at(date, portfolio=...)` filters
one portfolio and `positions_by_portfolio(date)` returns the end-of-day
summary grouped by portfolio, then symbol.

`rights_issue` books a bonus-style allotment: for every `old_shares` held,
`new_shares` bonus shares are issued. Open lots of the symbol across all
portfolios gain quantity, unit cost falls, and total CNY cost is unchanged.
It also posts a self-balanced memo entry (debit and credit the investment
account for the affected cost basis) so the corporate action is visible in
the journal.

Locked months reject new, modified, or deleted events in that month. A locked
entry is corrected in a later period with `reverse_entry`, which references
the original id and posts opposite legs. Date-bounded queries replay events up
to their cutoff, so they still show the locked point in time.

Amounts use `Decimal`; money is rounded to `0.01`, quantities to eight
fractional shares, and FX rates to eight decimal places.
