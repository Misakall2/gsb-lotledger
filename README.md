# gsb-lotledger

Python 3 standard library only. No command line, web framework, or third-party
package is provided.

Run the tests:

```bash
python3 -m unittest discover -s . -v
```

## Ledger Rules

- Base currency is fixed to `CNY`.
- Book timezone is `Asia/Shanghai`; day-end is `23:59:59.999999`.
- Event order on the same day is `(date, entry sequence, id)` and never depends
  on dictionary order.
- Manual and generated entries must have at least two distinct accounts and at
  least two legs. Each leg records its own currency and `rate_to_base`.
- Debits and credits must balance after conversion to CNY. A residual of at
  most one cent is posted to the `FX_VARIANCE` account; larger differences
  raise `ValidationError`.
- `record_buy` opens a lot. `record_sell` consumes open lots in FIFO order and
  records base-currency realized P/L in the supplied account.
- `record_stock_split` scales quantities and unit costs on open lots while
  preserving each lot's original and base total costs.
- `record_cash_dividend` posts cash and dividend income without changing
  quantity.
- `lock_month` rejects creates, amends, and deletes in that month. A locked
  entry is corrected with `reverse_entry` dated in a later month, followed by a
  fresh correct entry. Reversals set `reversal_of` and negate all legs.
- `positions_at`, `lot`, and `realized_pnl` replay append-only events, so a
  query at a prior date excludes later corrections and restores that point in
  time.

## Import Example

```python
from datetime import date
from lotledger import Ledger

book = Ledger()
book.add_account("CASH_USD", "USD cash")
book.add_account("INVEST", "Investments")
book.add_account("PNL", "Realized P/L")

book.record_buy(
    "buy-1", date(2024, 1, 10), "AAPL", 10, "100.00",
    cash_account="CASH_USD", inventory_account="INVEST",
    currency="USD", rate_to_base="7.10",
)
book.record_sell(
    "sell-1", date(2024, 1, 20), "AAPL", 4, "110.00",
    cash_account="CASH_USD", inventory_account="INVEST",
    pnl_account="PNL", rate_to_base="7.20",
)

positions = book.positions_at(date(2024, 1, 20))
remaining_lot = book.lot("buy-1", date(2024, 1, 20))
pnl = book.realized_pnl(date(2024, 1, 1), date(2024, 1, 31))
```
