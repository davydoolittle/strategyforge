"""
Tests for P4.4 health.py: returns, the lower tabular CUSUM, baseline from the
pinned validation window, engine vs paper forward source, the health table
and ntfy-once behaviour. Reuses test_paper's synthetic forge db.
Run from the repo root:   python -m unittest discover -s tests -v
"""

import os
import statistics
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import health  # noqa: E402
import paper as P  # noqa: E402
from test_paper import H, N_BARS, T0, alert, forge_db, signals_db  # noqa: E402

END = T0 + (N_BARS - 1) * H
SPLIT, OOS_END = T0 + 2000 * H, T0 + 2500 * H      # forge_db's pinned windows


class TestPure(unittest.TestCase):
    def test_returns_are_relative_to_running_equity(self):
        r = health.returns([{"pnl": 100.0}, {"pnl": -55.0}], 1000.0)
        self.assertAlmostEqual(r[0][1], 10.0)
        self.assertAlmostEqual(r[1][1], -5.0)

    def test_cusum(self):
        self.assertEqual(health.cusum([1, 2, 0.5]), (0.0, 0.0, None))       # never below 0
        s, lo, b = health.cusum([-1.5, -1.5, -1.5])                          # -1, -2, -3
        self.assertAlmostEqual(s, -3.0)
        self.assertEqual(b, 2)
        s, lo, b = health.cusum([-1.5, -1.5, -1.5, 5.0])                     # recovers
        self.assertEqual(s, 0.0)
        self.assertAlmostEqual(lo, -3.0)
        self.assertEqual(b, 2)
        self.assertIsNone(health.cusum([-0.4] * 100)[2])                     # inside the slack


class Base(unittest.TestCase):
    def setUp(self):
        self.conn = forge_db()
        self.addCleanup(self.conn.close)
        self.strat = P.strategy_by_name(self.conn, "SF c1-btc-1h")

    def watch(self):
        self.conn.execute("UPDATE strategies SET status='oos_validated' WHERE id=1")
        self.conn.commit()

    def rows(self):
        return self.conn.execute("SELECT day, alert, notified, source FROM health"
                                 " ORDER BY day").fetchall()


class TestMeasure(Base):
    def test_engine_source_baseline_and_forward(self):
        row = health.measure(self.conn, self.strat)
        r = health.engine.backtest(self.conn, "BTC-USD", P.W.pin_cfg(
            self.strat["cfg"], P._iso(T0 + 300 * H), 3600), P._iso(T0 + 300 * H), P._iso(END))
        rets = health.returns(r["trades"], self.strat["cfg"]["initial_capital"])
        base = [x for t, x in rets if SPLIT <= t["signal_ts"] < OOS_END]
        self.assertEqual(row["source"], "engine")
        self.assertGreaterEqual(len(base), health.MIN_BASELINE_TRADES)
        self.assertEqual(row["n_baseline"], len(base))
        self.assertAlmostEqual(row["mu"], statistics.mean(base))
        self.assertAlmostEqual(row["sigma"], statistics.stdev(base))
        self.assertEqual(row["n_forward"], sum(t["signal_ts"] >= OOS_END for t in r["trades"]))
        self.assertGreater(row["n_forward"], 5)
        self.assertLessEqual(row["cusum"], 0.0)

    def test_paper_source_when_alerts_exist(self):
        ent = sorted(P.engine_entries(self.conn, self.strat, END).values(), key=lambda e: e["ts"])
        sconn = signals_db([alert(e["ts"], e["side"], e["close"]) for e in ent[-6:]])
        self.addCleanup(sconn.close)
        P.reconcile(self.conn, sconn)
        row = health.measure(self.conn, self.strat)
        self.assertEqual(row["source"], "paper")
        r = P.simulate(self.conn, self.strat, P.own_alerts(self.conn, self.strat), END)
        self.assertEqual(row["n_forward"], r["n_trades"])

    def test_no_baseline(self):
        self.conn.execute("UPDATE candidates SET split_ts=?, oos_end=? WHERE id=1",
                          (P._iso(OOS_END - 10 * H), P._iso(OOS_END)))
        row = health.measure(self.conn, self.strat)
        self.assertIsNone(row["cusum"])
        self.assertEqual(row["alert"], 0)
        self.assertIn("no baseline", row["detail"])


class TestRun(Base):
    def test_only_watched_statuses(self):
        self.assertEqual(health.run(self.conn, day="2026-01-01", push=False), [])   # backtest_winner
        self.watch()
        self.assertEqual(len(health.run(self.conn, day="2026-01-01", push=False)), 1)
        health.run(self.conn, day="2026-01-01", push=False)                         # same day: replaced
        self.assertEqual(len(self.rows()), 1)
        self.conn.execute("UPDATE strategies SET status='retired' WHERE id=1")
        self.assertEqual(health.run(self.conn, day="2026-01-02", push=False), [])

    def test_breach_pushes_once(self):
        self.watch()
        with mock.patch.object(health, "CUSUM_H", 0.01), \
                mock.patch.object(health, "send_ntfy", return_value=True) as push:
            row = health.run(self.conn, day="2026-01-01", topic="t")[0]
            self.assertEqual(row["alert"], 1)
            self.assertIsNotNone(row["breach_ts"])
            self.assertEqual(push.call_count, 1)
            self.assertIn("decay CUSUM", push.call_args[0][2])
            health.run(self.conn, day="2026-01-01", topic="t")                      # same-day rerun
            health.run(self.conn, day="2026-01-02", topic="t")
            self.assertEqual(push.call_count, 1)
        self.assertEqual(self.rows(), [("2026-01-01", 1, 1, "engine"), ("2026-01-02", 1, 1, "engine")])

    def test_undelivered_alert_is_retried(self):
        self.watch()
        with mock.patch.object(health, "CUSUM_H", 0.01), \
                mock.patch.object(health, "send_ntfy", return_value=True) as push:
            health.run(self.conn, day="2026-01-01", topic="")                       # no topic
            self.assertEqual(push.call_count, 0)
            health.run(self.conn, day="2026-01-02", topic="t")
            self.assertEqual(push.call_count, 1)
        self.assertEqual([r[2] for r in self.rows()], [0, 1])

    def test_no_breach_no_push(self):
        self.watch()
        with mock.patch.object(health, "CUSUM_H", 1e9), \
                mock.patch.object(health, "send_ntfy", return_value=True) as push:
            row = health.run(self.conn, day="2026-01-01", topic="t")[0]
        self.assertEqual(row["alert"], 0)
        self.assertEqual(push.call_count, 0)

    def test_report_without_strategies(self):
        import io
        from contextlib import redirect_stdout
        out = io.StringIO()
        with redirect_stdout(out):
            health.print_report([])
        self.assertIn("nothing to watch", out.getvalue())


if __name__ == "__main__":
    unittest.main()
