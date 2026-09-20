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

Trades create or consume lots per `(symbol, portfolio)`. `buy`/`sell` accept
a `portfolio` name (default `"DEFAULT"`); lots in one portfolio can never
cover a sale from another, and `positions_by_portfolio_at(date)` summarizes
end-of-day quantity and CNY cost per portfolio while `positions_at(date)`
still aggregates across portfolios.

Each `(symbol, portfolio)` position opens as `FIFO` (default) or `LIFO` via
the `method` argument of `buy`. The method is fixed while any lot of that
position is open; a conflicting `buy` raises `LedgerError`. Once the
position is fully closed the next `buy` may choose again. The stable
consumption key is `(opening timestamp, append sequence, lot id)`, taken in
reverse for LIFO. Same-timestamp events are therefore consumed in append
order, regardless of entry id. A stock split multiplies open quantities and
divides unit cost while preserving total CNY cost.

`allot_shares(id, ts, symbol, new_shares, old_shares)` records a 配股: every
`old_shares` held earn `new_shares` more, so open quantities rise by
`(old + new) / old`, unit cost is diluted, and total CNY cost is unchanged.
Unlike a split, an allotment also posts a balanced journal entry (debit and
credit `SECURITIES` for the affected cost) and is reversed with
`reverse_entry`, subject to the same locked-period rules as trades.

Locked months reject new, modified, or deleted events in that month. A locked
entry is corrected in a later period with `reverse_entry`, which references
the original id and posts opposite legs. Date-bounded queries replay events up
to their cutoff, so they still show the locked point in time.

Amounts use `Decimal`; money is rounded to `0.01`, quantities to eight
fractional shares, and FX rates to eight decimal places.
