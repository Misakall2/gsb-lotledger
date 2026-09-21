from datetime import date, datetime
from decimal import Decimal
from unittest import TestCase
from zoneinfo import ZoneInfo

from lotledger.fx import to_base


SH = ZoneInfo("Asia/Shanghai")


def ts(day):
    return datetime(2026, 8, day, 12, tzinfo=SH)


class FxContractTestCase(TestCase):
    def test_base_conversion_is_the_single_rounding_point(self):
        self.assertEqual(to_base("100.005", "7.12345678"), Decimal("712.38"))


class TradeSplitContractTestCase(TestCase):
    def test_buys_sells_and_split_match_refactor_baseline(self):
        from lotledger import Ledger

        ledger = Ledger()
        ledger.add_account("CASH_USD")
        ledger.buy(
            "B1", ts(1), "AAPL", "100", "USD", "10", "7", "CASH_USD",
            portfolio="P1",
        )
        ledger.buy(
            "B2", ts(2), "AAPL", "50", "USD", "12", "7", "CASH_USD",
            portfolio="P1",
        )
        ledger.sell(
            "S1", ts(5), "AAPL", "120", "USD", "11", "7.2", "CASH_USD",
            portfolio="P1",
        )
        ledger.split_shares("X1", ts(10), "AAPL", 2, 1)
        ledger.sell(
            "S2", ts(15), "AAPL", "40", "USD", "8", "7.1", "CASH_USD",
            portfolio="P1",
        )

        self.assertEqual(
            ledger.positions_by_portfolio(date(2026, 8, 10)),
            {
                "P1": {
                    "AAPL": {
                        "quantity": Decimal("60.00000000"),
                        "cost_base": Decimal("2520.00"),
                    }
                }
            },
        )
        self.assertEqual(
            ledger.positions_by_portfolio(date(2026, 8, 15)),
            {
                "P1": {
                    "AAPL": {
                        "quantity": Decimal("20.00000000"),
                        "cost_base": Decimal("840.00"),
                    }
                }
            },
        )
        self.assertEqual(
            ledger.realized_pnl(date(2026, 8, 1), date(2026, 8, 15)),
            Decimal("1416.00"),
        )
        remaining = ledger.lot_remaining("LOT-000002")
        self.assertEqual(remaining.remaining_qty, Decimal("20.00000000"))
        self.assertEqual(remaining.remaining_cost, Decimal("840.00"))
