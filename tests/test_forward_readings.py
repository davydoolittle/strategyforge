"""
Tests for the quarterly forward-test readings (app/forward_readings.py): the
strategy lines, the P10.5 lines that never carry a verdict before month 72, the
footer, no trade-call wording, and the job.
Run from the repo root:   python -m unittest discover -s tests -v
"""

import os
import sqlite3
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import forward_readings as FR  # noqa: E402
import main  # noqa: E402
from test_market_context import TRADE_CALL  # noqa: E402


def db():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE strategies (id INTEGER, name TEXT, forward_test INTEGER)")
    conn.executemany("INSERT INTO strategies VALUES (?, ?, ?)", [(1, "a-1", 1), (2, "b-2", 1), (3, "c-3", 0)])
    conn.execute("CREATE TABLE health (strategy_id INTEGER, day TEXT, n_forward INTEGER, cusum REAL, alert INTEGER,"
                 " source TEXT)")
    conn.executemany("INSERT INTO health VALUES (?, ?, ?, ?, ?, ?)", [
        (1, "2026-11-26", 1, 0.0, 0, "paper"), (1, "2026-11-27", 2, -0.5, 0, "paper"),
        (2, "2026-11-27", 9, -3.2, 1, "engine"), (3, "2026-11-27", 5, 0.0, 0, "engine")])
    return conn


class TestReading(unittest.TestCase):
    def test_strategy_lines(self):
        lines = FR.strategy_lines(db())
        self.assertEqual(lines[0], "a-1: 2 forward trades (paper), CUSUM -0.50 (as of 2026-11-27)")
        self.assertIn("DECAY ALERT", lines[1])
        self.assertEqual(len(lines), 2)                      # c-3 is not a forward test

    def test_treasury_lines_never_judge_early(self):
        def fake(conn, cy, sym):
            return {"months": 30, "verdict": None, "ann_sr": 0.4} if sym == "IEF" else {"months": 0, "verdict": None}
        with mock.patch.object(FR.TM, "forward", side_effect=fake), mock.patch.object(FR.TS, "cash_yield", return_value=([], [])):
            lines = FR.treasury_lines(None)
        self.assertEqual(lines[0], "P10.5 IEF: month 30 of 72, excess Sharpe so far +0.40")
        self.assertIn("no full forward month yet", lines[1])
        for t in lines:
            self.assertNotIn("PASS", t)
            self.assertNotIn("FAIL", t)

    def test_text_and_job(self):
        with mock.patch.object(FR, "treasury_lines", return_value=["P10.5 IEF: month 1 of 72"]):
            text = FR.reading(db())
        self.assertTrue(text.endswith(FR.FOOT))
        self.assertIsNone(TRADE_CALL.search(text))
        jobs = {n: (s, c) for n, s, c, _ in main.JOB_SPECS}
        self.assertEqual(jobs["forward_readings"], ("91d", "forward_readings.py --notify"))


if __name__ == "__main__":
    unittest.main()
