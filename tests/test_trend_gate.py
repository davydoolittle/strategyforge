"""
Tests for Phase 16.1's trend gate in engine.backtest (cfg["trend_gate"]): longs
only in Bull, shorts only in Bear, nothing in Flat or warmup, the trend read
from the last completed UTC day, and no effect when the key is absent.
Run from the repo root:   python -m unittest discover -s tests -v
"""

import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import engine  # noqa: E402
import market_trend as MT  # noqa: E402
import lineage  # noqa: E402
import trend_gate as TG  # noqa: E402
from test_squeeze import H, N_BARS, T0, candles_db, iso, loose  # noqa: E402

DAY = 86400


def add_gate_market(conn, daily_values, start_ts):
    """A crypto 'market' GATE-USD: one hourly candle a day at 23:00 is enough,
    since a day's close is its last hourly close."""
    for k, v in enumerate(daily_values):
        conn.execute("INSERT INTO candles VALUES ('GATE-USD', ?, ?, ?, ?, ?, 1)",
                     (start_ts + k * DAY + 23 * 3600, v, v, v, v))
    conn.commit()


class TestPieces(unittest.TestCase):
    def test_gated(self):
        self.assertEqual(engine.gated(True, False, "Bull"), (True, False))
        self.assertEqual(engine.gated(True, False, "Bear"), (False, False))
        self.assertEqual(engine.gated(False, True, "Bear"), (False, True))
        self.assertEqual(engine.gated(False, True, "Bull"), (False, False))
        self.assertEqual(engine.gated(True, True, "Flat"), (False, False))
        self.assertEqual(engine.gated(True, True, None), (False, False))

    def test_trend_is_the_previous_completed_day(self):
        days, states = ["2026-01-01", "2026-01-02", "2026-01-03"], ["Bull", "Flat", "Bear"]
        t = int(datetime(2026, 1, 3, 15, tzinfo=timezone.utc).timestamp())
        self.assertEqual(engine.trend_before(days, states, t), "Flat")     # not the 3rd's own Bear
        t0 = int(datetime(2026, 1, 1, 0, tzinfo=timezone.utc).timestamp())
        self.assertIsNone(engine.trend_before(days, states, t0))
        late = int(datetime(2026, 2, 1, tzinfo=timezone.utc).timestamp())
        self.assertEqual(engine.trend_before(days, states, late), "Bear")


class TestBacktest(unittest.TestCase):
    def setUp(self):
        self.conn = candles_db()
        self.addCleanup(self.conn.close)
        self.start, self.end = iso(T0 + 2500 * H), iso(T0 + (N_BARS - 1) * H)
        self.base = engine.backtest(self.conn, "BTC-USD", loose(), self.start, self.end)
        self.assertTrue({t["dir"] for t in self.base["trades"]} >= {"long", "short"})
        self.gate_start = T0 - 300 * DAY

    def run_gate(self, values):
        add_gate_market(self.conn, values, self.gate_start)
        return engine.backtest(self.conn, "BTC-USD", {**loose(), "trend_gate": {"market": "GATE-USD"}},
                               self.start, self.end)

    def test_bull_keeps_only_longs(self):
        r = self.run_gate([100 * 1.002 ** k for k in range(500)])
        self.assertGreater(r["n_trades"], 0)
        self.assertEqual({t["dir"] for t in r["trades"]}, {"long"})

    def test_bear_keeps_only_shorts(self):
        r = self.run_gate([100 * 0.998 ** k for k in range(500)])
        self.assertEqual({t["dir"] for t in r["trades"]}, {"short"})

    def test_flat_or_warmup_blocks_everything(self):
        self.assertEqual(self.run_gate([100.0] * 500)["n_trades"], 0)         # flat average: Flat
        conn2 = candles_db()
        self.addCleanup(conn2.close)
        add_gate_market(conn2, [100 * 1.002 ** k for k in range(50)], T0)       # < 220 days: no trend yet
        r = engine.backtest(conn2, "BTC-USD", {**loose(), "trend_gate": {"market": "GATE-USD"}}, self.start, self.end)
        self.assertEqual(r["n_trades"], 0)

    def test_no_gate_no_change(self):
        again = engine.backtest(self.conn, "BTC-USD", loose(), self.start, self.end)
        self.assertEqual(again["trades"], self.base["trades"])

    def test_bad_gate_is_refused(self):
        with self.assertRaises(ValueError):
            engine.backtest(self.conn, "BTC-USD", {**loose(), "trend_gate": {"pair": "X"}}, self.start, self.end)

    def test_dashboard_rule_is_the_gate_rule(self):
        add_gate_market(self.conn, [100 * 1.002 ** k for k in range(500)], self.gate_start)
        st = MT.states(MT.daily_closes(self.conn, "GATE-USD", "crypto"))
        self.assertEqual(st[-1][4], "Bull")


class TestRunner(unittest.TestCase):
    def test_child_is_parent_plus_the_registered_gate(self):
        self.assertEqual(TG.PARENTS, (3214, 4027, 5833))
        parent = loose()
        child = TG.child_cfg(parent)
        self.assertEqual(child, {**parent, "trend_gate": {"market": "BTC-USD"}})
        self.assertNotIn("trend_gate", parent)
        self.assertNotEqual(lineage.dna_of(child), lineage.dna_of(parent))   # a new strategy, not the parent


if __name__ == "__main__":
    unittest.main()
