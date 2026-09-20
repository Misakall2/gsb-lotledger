from datetime import date, datetime
from decimal import Decimal
from unittest import TestCase
from zoneinfo import ZoneInfo

from lotledger import (
    Ledger,
    LedgerError,
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


class MultiPortfolioTestCase(TestCase):
    def make_ledger(self):
        ledger = Ledger()
        ledger.add_account("CASH_USD")
        return ledger

    def test_same_symbol_portfolios_never_share_lots(self):
        ledger = self.make_ledger()
        ledger.buy(
            "B-A", ts(2026, 8, 3), "AAPL", "100", "USD", "10", "7", "CASH_USD",
            portfolio="P-A",
        )
        ledger.buy(
            "B-B", ts(2026, 8, 4), "AAPL", "100", "USD", "12", "7", "CASH_USD",
            portfolio="P-B",
        )

        sale = ledger.sell(
            "S-B", ts(2026, 8, 10), "AAPL", "100", "USD", "13", "7", "CASH_USD",
            portfolio="P-B",
        )
        self.assertEqual(sale.action["allocations"][0]["lot_id"], "LOT-000002")
        self.assertEqual(sale.action["cost_base"], "8400.00")
        self.assertEqual(sale.action["realized_pnl"], "700.00")

        grouped = ledger.positions_by_portfolio(date(2026, 8, 10))
        self.assertEqual(
            grouped["P-A"]["AAPL"],
            {"quantity": Decimal("100.00000000"), "cost_base": Decimal("7000.00")},
        )
        self.assertNotIn("P-B", grouped)

        # P-B is flat; P-A still holds 100 shares, but they cannot be stolen.
        with self.assertRaises(OversoldError):
            ledger.sell(
                "S-STEAL", ts(2026, 8, 11), "AAPL", "1", "USD", "13", "7",
                "CASH_USD", portfolio="P-B",
            )

    def test_positions_at_can_filter_by_portfolio(self):
        ledger = self.make_ledger()
        ledger.buy(
            "B-A", ts(2026, 8, 3), "AAPL", "100", "USD", "10", "7", "CASH_USD",
            portfolio="P-A",
        )
        ledger.buy(
            "B-B", ts(2026, 8, 4), "AAPL", "50", "USD", "12", "7", "CASH_USD",
            portfolio="P-B",
        )

        combined = ledger.positions_at(date(2026, 8, 4))
        self.assertEqual(combined["AAPL"]["quantity"], Decimal("150.00000000"))
        self.assertEqual(combined["AAPL"]["cost_base"], Decimal("11200.00"))
        only_a = ledger.positions_at(date(2026, 8, 4), portfolio="P-A")
        self.assertEqual(only_a["AAPL"]["quantity"], Decimal("100.00000000"))
        self.assertEqual(only_a["AAPL"]["cost_base"], Decimal("7000.00"))


class CostMethodTestCase(TestCase):
    def make_ledger(self):
        ledger = Ledger()
        ledger.add_account("CASH_USD")
        return ledger

    def test_lifo_consumes_newest_lot_first(self):
        ledger = self.make_ledger()
        ledger.buy(
            "B1", ts(2026, 8, 3), "AAPL", "100", "USD", "10", "7", "CASH_USD",
            method="LIFO",
        )
        ledger.buy(
            "B2", ts(2026, 8, 4), "AAPL", "100", "USD", "12", "7", "CASH_USD",
            method="LIFO",
        )
        sale = ledger.sell(
            "S1", ts(2026, 8, 10), "AAPL", "150", "USD", "11", "7", "CASH_USD"
        )

        self.assertEqual(sale.action["allocations"][0]["lot_id"], "LOT-000002")
        self.assertEqual(sale.action["allocations"][0]["quantity"], "100.00000000")
        self.assertEqual(sale.action["allocations"][0]["cost_base"], "8400.00")
        self.assertEqual(sale.action["allocations"][1]["lot_id"], "LOT-000001")
        self.assertEqual(sale.action["allocations"][1]["quantity"], "50.00000000")
        self.assertEqual(sale.action["allocations"][1]["cost_base"], "3500.00")
        self.assertEqual(sale.action["realized_pnl"], "-350.00")
        self.assertEqual(
            ledger.lot_remaining("LOT-000001").remaining_qty, Decimal("50.00000000")
        )
        self.assertTrue(ledger.lot_remaining("LOT-000002").closed)

    def test_method_is_locked_once_the_position_is_opened(self):
        ledger = self.make_ledger()
        ledger.buy(
            "B1", ts(2026, 8, 3), "AAPL", "100", "USD", "10", "7", "CASH_USD",
            method="FIFO",
        )
        with self.assertRaises(LedgerError):
            ledger.buy(
                "B2", ts(2026, 8, 4), "AAPL", "100", "USD", "12", "7", "CASH_USD",
                method="LIFO",
            )
        # Omitting the method inherits the locked one.
        ledger.buy("B3", ts(2026, 8, 4), "AAPL", "100", "USD", "12", "7", "CASH_USD")
        self.assertEqual(ledger.get_entry("B3").action["method"], "FIFO")

        # A different portfolio of the same symbol chooses independently.
        ledger.buy(
            "B4", ts(2026, 8, 5), "AAPL", "10", "USD", "10", "7", "CASH_USD",
            portfolio="P-B", method="LIFO",
        )
        with self.assertRaises(LedgerError):
            ledger.buy(
                "B5", ts(2026, 8, 6), "AAPL", "10", "USD", "10", "7", "CASH_USD",
                portfolio="P-B", method="FIFO",
            )
        with self.assertRaises(LedgerError):
            ledger.buy(
                "B6", ts(2026, 8, 6), "AAPL", "10", "USD", "10", "7", "CASH_USD",
                portfolio="P-C", method="WEIGHTED",
            )


class RightsIssueTestCase(TestCase):
    def make_ledger(self):
        ledger = Ledger()
        ledger.add_account("CASH_USD")
        return ledger

    def test_rights_issue_scales_lots_and_posts_balanced_entry(self):
        ledger = self.make_ledger()
        ledger.buy("B1", ts(2026, 8, 1), "AAPL", "100", "USD", "10", "7", "CASH_USD")

        entry = ledger.rights_issue(
            "RI1", ts(2026, 8, 15), "AAPL", new_shares=1, old_shares=1
        )
        self.assertEqual(entry.kind, "rights_issue")
        debit_base = sum(leg.amount_base for leg in entry.legs if leg.debit)
        credit_base = sum(leg.amount_base for leg in entry.legs if leg.credit)
        self.assertEqual(debit_base, credit_base)
        self.assertEqual(debit_base, Decimal("7000.00"))

        lot = ledger.lot_remaining("LOT-000001", date(2026, 8, 15))
        self.assertEqual(lot.remaining_qty, Decimal("200.00000000"))
        self.assertEqual(lot.remaining_cost, Decimal("7000.00"))
        self.assertEqual(lot.unit_cost, Decimal("35.00"))

        # Selling after the issue uses the diluted unit cost.
        sale = ledger.sell(
            "S1", ts(2026, 8, 20), "AAPL", "200", "USD", "8", "7", "CASH_USD"
        )
        self.assertEqual(sale.action["cost_base"], "7000.00")
        self.assertEqual(sale.action["realized_pnl"], "4200.00")
        self.assertNotIn("AAPL", ledger.positions_at(date(2026, 8, 20)))

    def test_rights_issue_applies_to_every_portfolio_of_the_symbol(self):
        ledger = self.make_ledger()
        ledger.buy(
            "B-A", ts(2026, 8, 1), "AAPL", "100", "USD", "10", "7", "CASH_USD",
            portfolio="P-A",
        )
        ledger.buy(
            "B-B", ts(2026, 8, 2), "AAPL", "50", "USD", "12", "7", "CASH_USD",
            portfolio="P-B",
        )
        ledger.rights_issue("RI1", ts(2026, 8, 15), "AAPL", new_shares=1, old_shares=2)

        grouped = ledger.positions_by_portfolio(date(2026, 8, 15))
        self.assertEqual(grouped["P-A"]["AAPL"]["quantity"], Decimal("150.00000000"))
        self.assertEqual(grouped["P-A"]["AAPL"]["cost_base"], Decimal("7000.00"))
        self.assertEqual(grouped["P-B"]["AAPL"]["quantity"], Decimal("75.00000000"))
        self.assertEqual(grouped["P-B"]["AAPL"]["cost_base"], Decimal("4200.00"))

    def test_rights_issue_cannot_touch_a_locked_period(self):
        ledger = self.make_ledger()
        ledger.buy("B1", ts(2026, 8, 1), "AAPL", "100", "USD", "10", "7", "CASH_USD")
        ledger.lock_month(2026, 8)

        with self.assertRaises(LockedPeriodError):
            ledger.rights_issue(
                "RI-AUG", ts(2026, 8, 31), "AAPL", new_shares=1, old_shares=1
            )
        # History is untouched; the corporate action is booked next period.
        self.assertEqual(
            ledger.lot_remaining("LOT-000001", date(2026, 8, 31)).remaining_qty,
            Decimal("100.00000000"),
        )
        ledger.rights_issue("RI-SEP", ts(2026, 9, 1), "AAPL", new_shares=1, old_shares=1)
        self.assertEqual(
            ledger.lot_remaining("LOT-000001").remaining_qty, Decimal("200.00000000")
        )

    def test_rights_issue_requires_open_lots(self):
        ledger = self.make_ledger()
        with self.assertRaises(LedgerError):
            ledger.rights_issue(
                "RI1", ts(2026, 8, 15), "AAPL", new_shares=1, old_shares=1
            )
