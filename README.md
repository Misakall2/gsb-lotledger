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

Trades create or consume FIFO lots. The stable FIFO key is
`(opening timestamp, append sequence, lot id)`. Same-timestamp events are
therefore consumed in append order, regardless of entry id. A stock split
multiplies open quantities and divides unit cost while preserving total CNY
cost.

Locked months reject new, modified, or deleted events in that month. A locked
entry is corrected in a later period with `reverse_entry`, which references
the original id and posts opposite legs. Date-bounded queries replay events up
to their cutoff, so they still show the locked point in time.

Amounts use `Decimal`; money is rounded to `0.01`, quantities to eight
fractional shares, and FX rates to eight decimal places.
