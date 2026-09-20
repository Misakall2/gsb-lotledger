from datetime import date, datetime
from decimal import Decimal
from unittest import TestCase
from zoneinfo import ZoneInfo

from lotledger import (
    Ledger,
    LockedPeriodError,
    OversoldError,
    UnbalancedEntryError,
    UnknownAccountError,
)


SH = ZoneInfo("Asia/Shanghai")


def ts(year, month, day, hour=12):
    return datetime(year, month, day, hour, tzinfo=SH)


class LedgerTestCase(TestCase):
    def make_ledger(self):
        ledger = Ledger()
        ledger.add_account("CASH_USD")
        ledger.add_account("CASH_HKD")
        return ledger

    def test_two_buys_one_sell_uses_fifo_and_realizes_pnl(self):
        ledger = self.make_ledger()
        ledger.buy("B1", ts(2026, 8, 3), "AAPL", "100", "USD", "10", "7.00", "CASH_USD")
        ledger.buy("B2", ts(2026, 8, 4), "AAPL", "100", "USD", "12", "7.00", "CASH_USD")
        sale = ledger.sell(
            "S1", ts(2026, 8, 10), "AAPL", "150", "USD", "11", "7.20", "CASH_USD"
        )

        self.assertEqual(sale.action["allocations"][0]["lot_id"], "LOT-000001")
        self.assertEqual(sale.action["allocations"][0]["quantity"], "100.00000000")
        self.assertEqual(sale.action["allocations"][0]["cost_base"], "7000.00")
        self.assertEqual(sale.action["allocations"][1]["lot_id"], "LOT-000002")
        self.assertEqual(sale.action["allocations"][1]["quantity"], "50.00000000")
        self.assertEqual(sale.action["allocations"][1]["cost_base"], "4200.00")
        self.assertEqual(sale.action["realized_pnl"], "680.00")

        positions = ledger.positions_at(date(2026, 8, 10))
        self.assertEqual(positions["AAPL"]["quantity"], Decimal("50.00000000"))
        self.assertEqual(positions["AAPL"]["cost_base"], Decimal("4200.00"))
        self.assertTrue(ledger.lot_remaining("LOT-000001").closed)
        self.assertEqual(
            ledger.lot_remaining("LOT-000002").remaining_qty, Decimal("50.00000000")
        )
        self.assertEqual(
            ledger.realized_pnl(date(2026, 8, 1), date(2026, 8, 31)),
            Decimal("680.00"),
        )

    def test_manual_cross_currency_entry_keeps_base_balance_with_fx_leg(self):
        ledger = self.make_ledger()
        entry = ledger.post(
            "FX1",
            ts(2026, 8, 2),
            [
                {"account": "SECURITIES", "debit": "100.00", "currency": "CNY"},
                {
                    "account": "CASH_USD",
                    "credit": "14.00",
                    "currency": "USD",
                    "fx_rate": "7.14285714",
                    "amount_base": "99.99",
                },
            ],
        )

        plug = entry.legs[-1]
        self.assertEqual(plug.account, "FX")
        self.assertEqual(plug.credit, Decimal("0.01"))
        self.assertEqual(plug.amount_base, Decimal("0.01"))
        debit_base = sum(leg.amount_base for leg in entry.legs if leg.debit)
        credit_base = sum(leg.amount_base for leg in entry.legs if leg.credit)
        self.assertEqual(debit_base, credit_base)

    def test_cross_currency_buy_records_native_amounts_and_cny_cost(self):
        ledger = self.make_ledger()
        entry = ledger.buy(
            "B-HKD",
            ts(2026, 8, 5),
            "0700",
            "100",
            "HKD",
            "300",
            "0.92",
            "CASH_HKD",
        )

        cash_leg = entry.legs[1]
        asset_leg = entry.legs[0]
        self.assertEqual(cash_leg.currency, "HKD")
        self.assertEqual(cash_leg.credit, Decimal("30000.00"))
        self.assertEqual(cash_leg.fx_rate, Decimal("0.92000000"))
        self.assertEqual(asset_leg.debit, Decimal("27600.00"))
        self.assertEqual(
            ledger.positions_at(date(2026, 8, 5))["0700"]["cost_base"],
            Decimal("27600.00"),
        )

    def test_two_for_one_split_changes_quantity_and_unit_cost_not_total(self):
        ledger = self.make_ledger()
        ledger.buy("B1", ts(2026, 8, 1), "AAPL", "100", "USD", "10", "7", "CASH_USD")
        ledger.split_shares(
            "SPLIT1", ts(2026, 8, 15), "AAPL", new_shares=2, old_shares=1
        )

        lot = ledger.lot_remaining("LOT-000001", date(2026, 8, 15))
        self.assertEqual(lot.remaining_qty, Decimal("200.00000000"))
        self.assertEqual(lot.remaining_cost, Decimal("7000.00"))
        self.assertEqual(lot.unit_cost, Decimal("35.00"))
        self.assertEqual(
            ledger.positions_at(date(2026, 8, 15))["AAPL"]["quantity"],
            Decimal("200.00000000"),
        )

    def test_locked_period_rejects_change_and_next_period_red_letter_reversal_works(self):
        ledger = self.make_ledger()
        ledger.buy("B1", ts(2026, 8, 1), "AAPL", "100", "USD", "10", "7", "CASH_USD")
        sale = ledger.sell(
            "S1", ts(2026, 8, 2), "AAPL", "100", "USD", "11", "7", "CASH_USD"
        )
        ledger.lock_month(2026, 8)

        with self.assertRaises(LockedPeriodError):
            ledger.post(
                "BACKDATED",
                ts(2026, 8, 3),
                [
                    {"account": "SECURITIES", "debit": "1"},
                    {"account": "CASH_USD", "credit": "1"},
                ],
            )
        with self.assertRaises(LockedPeriodError):
            ledger.reverse_entry("BAD-REV", ts(2026, 8, 31), "S1")

        reversal = ledger.reverse_entry("REV-S1", ts(2026, 9, 1), "S1")
        self.assertEqual(reversal.reversal_of, "S1")
        self.assertEqual(reversal.kind, "reverse_sell")
        self.assertEqual(
            ledger.lot_remaining("LOT-000001").remaining_qty, Decimal("100.00000000")
        )
        self.assertEqual(
            ledger.realized_pnl(date(2026, 9, 1), date(2026, 9, 30)),
            -Decimal(sale.action["realized_pnl"]),
        )
        locked_point = ledger.positions_at(date(2026, 8, 31))
        self.assertNotIn("AAPL", locked_point)

    def test_reversal_after_split_restores_current_share_count(self):
        ledger = self.make_ledger()
        ledger.buy("B1", ts(2026, 8, 1), "AAPL", "100", "USD", "10", "7", "CASH_USD")
        ledger.sell("S1", ts(2026, 8, 2), "AAPL", "100", "USD", "11", "7", "CASH_USD")
        ledger.split_shares(
            "SPLIT1", ts(2026, 8, 15), "AAPL", new_shares=2, old_shares=1
        )
        ledger.reverse_entry("REV1", ts(2026, 9, 1), "S1")

        lot = ledger.lot_remaining("LOT-000001")
        self.assertEqual(lot.remaining_qty, Decimal("200.00000000"))
        self.assertEqual(lot.remaining_cost, Decimal("7000.00"))

    def test_same_timestamp_entries_are_stable_by_append_sequence(self):
        ledger = self.make_ledger()
        when = ts(2026, 8, 6, 10)
        ledger.buy("B2", when, "AAPL", "100", "USD", "12", "7", "CASH_USD")
        ledger.buy("B1", when, "AAPL", "100", "USD", "10", "7", "CASH_USD")
        sale = ledger.sell(
            "S1", when, "AAPL", "100", "USD", "11", "7", "CASH_USD"
        )

        self.assertEqual(sale.action["allocations"][0]["lot_id"], "LOT-000001")
        self.assertEqual(sale.action["cost_base"], "8400.00")

    def test_dividend_posts_without_changing_position_quantity(self):
        ledger = self.make_ledger()
        ledger.buy("B1", ts(2026, 8, 1), "AAPL", "10", "USD", "10", "7", "CASH_USD")
        dividend = ledger.dividend(
            "D1", ts(2026, 8, 20), "AAPL", "5", "USD", "7.10", "CASH_USD"
        )

        self.assertEqual(dividend.legs[0].debit, Decimal("5.00"))
        self.assertEqual(dividend.legs[1].credit, Decimal("5.00"))
        self.assertEqual(
            ledger.positions_at(date(2026, 8, 20))["AAPL"]["quantity"],
            Decimal("10.00000000"),
        )

    def test_end_of_day_cutoff_uses_ledger_timezone(self):
        ledger = self.make_ledger()
        ledger.buy(
            "B1",
            datetime(2026, 8, 1, 23, 59, 59, 999999, tzinfo=SH),
            "AAPL",
            "1",
            "USD",
            "10",
            "7",
            "CASH_USD",
        )
        self.assertIn("AAPL", ledger.positions_at(date(2026, 8, 1)))
        self.assertNotIn("AAPL", ledger.positions_at(date(2026, 7, 31)))

    def test_validation_errors_are_explicit(self):
        ledger = self.make_ledger()
        with self.assertRaises(UnknownAccountError):
            ledger.post(
                "BAD-ACCOUNT",
                ts(2026, 8, 1),
                [
                    {"account": "NOPE", "debit": "1"},
                    {"account": "FX", "credit": "1"},
                ],
            )
        with self.assertRaises(UnbalancedEntryError):
            ledger.post(
                "BAD-BALANCE",
                ts(2026, 8, 1),
                [
                    {"account": "SECURITIES", "debit": "10.00"},
                    {"account": "FX", "credit": "1.00"},
                ],
            )
        with self.assertRaises(OversoldError):
            ledger.sell(
                "TOO-MANY",
                ts(2026, 8, 1),
                "AAPL",
                "1",
                "USD",
                "10",
                "7",
                "CASH_USD",
            )
