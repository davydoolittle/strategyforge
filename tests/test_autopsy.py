"""
Tests for the forward-trade autopsy (app/autopsy.py): display only, never an
input to a candidate, the same forward trades as health.py, and its fields.
Run from the repo root:   python -m unittest discover -s tests -v
"""

import os
import re
import sqlite3
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import autopsy as AU  # noqa: E402
from test_market_context import TRADE_CALL  # noqa: E402

APP = os.path.join(os.path.dirname(__file__), "..", "app")


class TestDisplayOnly(unittest.TestCase):
    def test_no_module_but_web_imports_it(self):
        for f in os.listdir(APP):
            if f.endswith(".py") and f not in ("web.py", "autopsy.py"):
                with open(os.path.join(APP, f)) as fh:
                    self.assertIsNone(re.search(r"^\s*(import autopsy|from autopsy)", fh.read(), re.M), f)

    def test_strategies_and_note(self):
        self.assertEqual(AU.STRATEGIES, ("smrt-v41-eth-3214", "core-btc-4027"))
        self.assertIn("Not an input to any candidate", AU.NOTE)
        self.assertIsNone(TRADE_CALL.search(AU.NOTE))


class TestRows(unittest.TestCase):
    def test_fields_from_health_forward_trades(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE candles (pair TEXT, ts INTEGER, close REAL)")
        conn.execute("INSERT INTO candles VALUES ('BTC-USD', 1763000000, 1)")
        conn.execute("CREATE TABLE equity_regime (date TEXT, band TEXT)")
        conn.execute("INSERT INTO equity_regime VALUES ('2025-11-10', '>90')")
        trade = {"signal_ts": 1762956000, "entry_ts": 1762959600, "exit_ts": 1762970400, "dir": "long",
                 "reason": "stop"}                                   # Wed 2025-11-12 14:00 UTC
        strat = {"pair": "BTC-USD"}
        tagger = mock.Mock()
        tagger.at.return_value = "chop"
        with mock.patch.object(AU.paper, "strategy_by_name", return_value=strat), \
                mock.patch.object(AU.health, "forward_trades", return_value=("engine", [], [(trade, -1.25)])), \
                mock.patch.object(AU.features, "RegimeTagger", return_value=tagger):
            r = AU.rows_for(conn, "core-btc-4027")
        row = r["trades"][0]
        self.assertEqual((row["regime"], row["vix_band"], row["weekday"], row["hold_h"]), ("chop", ">90", "Wed", 3.0))
        self.assertEqual((row["ret_pct"], row["outcome"], row["reason"]), (-1.25, "loss", "stop"))
        self.assertEqual(r["source"], "engine")

    def test_missing_strategy(self):
        with mock.patch.object(AU.paper, "strategy_by_name", return_value=None):
            r = AU.rows_for(sqlite3.connect(":memory:"), "x")
        self.assertTrue(r["missing"])


if __name__ == "__main__":
    unittest.main()
