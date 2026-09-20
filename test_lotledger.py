import unittest
from datetime import date
from decimal import Decimal

from lotledger import (
    AccountError,
    Ledger,
    Leg,
    LockedPeriodError,
    LotError,
    ValidationError,
)
from lotledger.ledger import DAY_END, LEDGER_TIMEZONE


class LotLedgerTest(unittest.TestCase):
    def setUp(self):
        self.ledger = Ledger()
        for code, name in [
            ("CASH_USD", "USD cash"),
            ("INVEST", "Equity investments"),
            ("PNL", "Realized trading P/L"),
            ("DIVIDEND", "Dividend income"),
            ("CASH_CNY", "CNY cash"),
            ("EXPENSE", "Expense"),
        ]:
            self.ledger.add_account(code, name)

    def test_two_buys_one_sell_uses_fifo_lots(self):
        self.ledger.record_buy(
            "B1", date(2024, 3, 1), "AAPL", 10, "100.00",
            "CASH_USD", "INVEST", "USD", "7.00",
        )
        self.ledger.record_buy(
            "B2", date(2024, 3, 1), "AAPL", 5, "110.00",
            "CASH_USD", "INVEST", "USD", "7.10",
        )
        self.ledger.record_sell(
            "S1", date(2024, 3, 2), "AAPL", 12, "120.00",
            "CASH_USD", "INVEST", "PNL", "7.20",
        )

        pnl = self.ledger.realized_pnl(date(2024, 3, 2), date(2024, 3, 2))
        self.assertEqual(len(pnl), 1)
        self.assertEqual(pnl[0].proceeds, Decimal("1440.00"))
        self.assertEqual(pnl[0].base_proceeds, Decimal("10368.00"))
        self.assertEqual(pnl[0].cost, Decimal("1220.00"))
        self.assertEqual(pnl[0].base_cost, Decimal("8562.00"))
        self.assertEqual(pnl[0].base_realized_pnl, Decimal("1806.00"))
        self.assertEqual(
            [(m.lot_id, m.quantity, m.cost, m.base_cost) for m in pnl[0].matches],
            [
                ("B1", Decimal("10"), Decimal("1000.00"), Decimal("7000.00")),
                ("B2", Decimal("2"), Decimal("220.00"), Decimal("1562.00")),
            ],
        )

        positions = self.ledger.positions_at(date(2024, 3, 2))
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0].symbol, "AAPL")
        self.assertEqual(positions[0].quantity, Decimal("3.00000000"))
        self.assertEqual(positions[0].cost, Decimal("330.00"))
        self.assertEqual(positions[0].base_cost, Decimal("2343.00"))
        self.assertEqual(positions[0].open_lot_ids, ("B2",))

    def test_manual_cross_currency_rounding_is_booked_to_fx_account(self):
        entry = self.ledger.add_entry(
            "FX1",
            date(2024, 4, 1),
            [
                {"account": "CASH_USD", "debit": "100.00", "currency": "USD",
                 "rate_to_base": "7.00"},
                {"account": "CASH_CNY", "credit": "700.01"},
            ],
        )
        self.assertEqual(
            [(leg.account, leg.debit, leg.credit) for leg in entry.legs],
            [
                ("CASH_USD", Decimal("100.00"), Decimal("0.00")),
                ("CASH_CNY", Decimal("0.00"), Decimal("700.01")),
                ("FX_VARIANCE", Decimal("0.01"), Decimal("0.00")),
            ],
        )

        opposite = self.ledger.add_entry(
            "FX2",
            date(2024, 4, 1),
            [
                {"account": "CASH_USD", "debit": "10.00", "currency": "USD",
                 "rate_to_base": "7.001"},
                {"account": "EXPENSE", "credit": "70.00"},
            ],
        )
        fx = opposite.legs[-1]
        self.assertEqual(fx.account, "FX_VARIANCE")
        self.assertEqual((fx.debit, fx.credit), (Decimal("0.00"), Decimal("0.01")))

    def test_invalid_account_and_unbalanced_entry_raise(self):
        with self.assertRaises(AccountError):
            self.ledger.add_entry(
                "BAD1", date(2024, 4, 2),
                [Leg("CASH_CNY", debit=1), Leg("MISSING", credit=1)],
            )
        with self.assertRaises(ValidationError):
            self.ledger.add_entry(
                "BAD2", date(2024, 4, 2),
                [Leg("CASH_CNY", debit="10.00"), Leg("EXPENSE", credit="10.02")],
            )
        with self.assertRaises(ValidationError):
            self.ledger.add_entry(
                "BAD3", date(2024, 4, 2),
                [
                    Leg("CASH_USD", debit=100, currency="USD", rate_to_base=7),
                    Leg("EXPENSE", credit=100),
                ],
            )

    def test_two_for_one_split_changes_shares_and_unit_cost_only(self):
        self.ledger.record_buy(
            "SPLIT-BUY", date(2024, 5, 1), "MSFT", 10, "100.00",
            "CASH_CNY", "INVEST", "CNY", 1,
        )
        self.ledger.record_stock_split(
            "SPLIT2", date(2024, 5, 2), "MSFT", "2", "2-for-1"
        )

        lot = self.ledger.lot("SPLIT-BUY", date(2024, 5, 2))
        self.assertEqual(lot.remaining_quantity, Decimal("20.00000000"))
        self.assertEqual(lot.unit_cost, Decimal("50.00000000"))
        self.assertEqual(lot.remaining_cost, Decimal("1000.00"))
        self.assertEqual(lot.remaining_base_cost, Decimal("1000.00"))

    def test_selling_more_than_open_lots_raises(self):
        self.ledger.record_buy(
            "OVER-BUY", date(2024, 6, 1), "TSLA", 2, "100",
            "CASH_USD", "INVEST", "USD", "7",
        )
        with self.assertRaises(LotError):
            self.ledger.record_sell(
                "OVER-SELL", date(2024, 6, 2), "TSLA", 3, "110",
                "CASH_USD", "INVEST", "PNL", "7",
            )

    def test_cash_dividend_does_not_change_quantity(self):
        self.ledger.record_buy(
            "DIV-BUY", date(2024, 7, 1), "NVDA", 4, "100",
            "CASH_USD", "INVEST", "USD", "7",
        )
        entry = self.ledger.record_cash_dividend(
            "DIV1", date(2024, 7, 2), "NVDA", "4.00",
            "CASH_USD", "DIVIDEND", "USD", "7.10",
        )
        self.assertEqual(
            [(leg.account, leg.debit, leg.credit) for leg in entry.legs],
            [
                ("CASH_USD", Decimal("4.00"), Decimal("0.00")),
                ("DIVIDEND", Decimal("0.00"), Decimal("4.00")),
            ],
        )
        position = self.ledger.positions_at(date(2024, 7, 2))[0]
        self.assertEqual(position.quantity, Decimal("4.00000000"))

    def test_locked_month_rejects_amend_and_delete(self):
        self.ledger.add_account("ADJ", "Adjustment")
        self.ledger.add_entry(
            "LOCKED-MANUAL",
            date(2024, 1, 15),
            [Leg("CASH_CNY", debit=10), Leg("ADJ", credit=10)],
        )
        self.ledger.lock_month(2024, 1)

        with self.assertRaises(LockedPeriodError):
            self.ledger.update_entry(
                "LOCKED-MANUAL",
                [Leg("CASH_CNY", debit=11), Leg("ADJ", credit=11)],
            )
        with self.assertRaises(LockedPeriodError):
            self.ledger.delete_entry("LOCKED-MANUAL")

    def test_locked_entry_is_reversed_next_month_and_point_in_time_is_stable(self):
        self.ledger.record_buy(
            "OLD-BUY", date(2024, 1, 10), "AAPL", 5, "100",
            "CASH_CNY", "INVEST", "CNY", 1,
        )
        self.ledger.record_sell(
            "OLD-SELL", date(2024, 1, 20), "AAPL", 5, "110",
            "CASH_CNY", "INVEST", "PNL", 1,
        )
        self.ledger.lock_month(2024, 1)

        reversal = self.ledger.reverse_entry(
            "OLD-SELL", date(2024, 2, 1), "OLD-SELL-REV"
        )
        self.assertEqual(reversal.reversal_of, "OLD-SELL")
        self.ledger.add_entry(
            "CORRECTION",
            date(2024, 2, 1),
            [Leg("CASH_CNY", debit=1), Leg("EXPENSE", credit=1)],
            memo="Correcting entry in the next open period",
        )

        self.assertEqual(self.ledger.positions_at(date(2024, 1, 31)), ())
        reopened = self.ledger.positions_at(date(2024, 2, 1))[0]
        self.assertEqual((reopened.symbol, reopened.quantity, reopened.cost),
                         ("AAPL", Decimal("5.00000000"), Decimal("500.00")))
        january_pnl = self.ledger.realized_pnl(date(2024, 1, 1), date(2024, 1, 31))
        february_pnl = self.ledger.realized_pnl(date(2024, 2, 1), date(2024, 2, 28))
        self.assertEqual(january_pnl[0].base_realized_pnl, Decimal("50.00"))
        self.assertEqual(february_pnl[0].base_realized_pnl, Decimal("-50.00"))

        with self.assertRaises(LockedPeriodError):
            self.ledger.reverse_entry("OLD-SELL", date(2024, 1, 31), "BAD-REV")

    def test_same_day_order_is_stable_by_entry_sequence(self):
        self.ledger.record_buy(
            "LOT-A", date(2024, 8, 1), "IBM", 1, "100",
            "CASH_CNY", "INVEST", "CNY", 1,
        )
        self.ledger.record_buy(
            "LOT-B", date(2024, 8, 1), "IBM", 1, "120",
            "CASH_CNY", "INVEST", "CNY", 1,
        )
        self.ledger.record_sell(
            "SAME-DAY-SELL", date(2024, 8, 1), "IBM", 1, "130",
            "CASH_CNY", "INVEST", "PNL", 1,
        )
        pnl = self.ledger.realized_pnl()[0]
        self.assertEqual(pnl.matches[0].lot_id, "LOT-A")
        self.assertEqual(pnl.base_realized_pnl, Decimal("30.00"))

    def test_day_end_cutoff_uses_fixed_ledger_timezone(self):
        self.ledger.record_buy(
            "CUTOFF-BUY", date(2024, 9, 10), "GE", 1, "10",
            "CASH_CNY", "INVEST", "CNY", 1,
        )
        cutoff = self.ledger.day_end_datetime(date(2024, 9, 10))
        self.assertEqual(cutoff.timetz().replace(tzinfo=None), DAY_END)
        self.assertEqual(cutoff.tzinfo, LEDGER_TIMEZONE)
        self.assertEqual(self.ledger.positions_at(date(2024, 9, 9)), ())
        self.assertEqual(len(self.ledger.positions_at(date(2024, 9, 10))), 1)


if __name__ == "__main__":
    unittest.main()
