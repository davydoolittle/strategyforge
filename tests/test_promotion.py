"""
Tests for P4.3: paper.evidence() and `lineage promote --to paper_validated`.
Reuses test_paper's synthetic forge db (one strategy, c1-btc-1h) and builds
alerts from the engine's own entries.
Run from the repo root:   python -m unittest discover -s tests -v
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import lineage  # noqa: E402
import paper as P  # noqa: E402
from test_paper import H, N_BARS, T0, alert, forge_db, signals_db  # noqa: E402

D = 86400
END = T0 + (N_BARS - 1) * H


class Base(unittest.TestCase):
    def setUp(self):
        self.conn = forge_db()
        self.addCleanup(self.conn.close)
        self.conn.execute("UPDATE strategies SET status='oos_validated' WHERE id=1")
        self.conn.commit()
        strat = P.strategy_by_name(self.conn, "SF c1-btc-1h")
        self.ent = sorted(P.engine_entries(self.conn, strat, END).values(), key=lambda e: e["ts"])

    def feed(self, entries):
        sconn = signals_db([alert(e["ts"], e["side"], e["close"]) for e in entries])
        self.addCleanup(sconn.close)
        P.reconcile(self.conn, sconn)

    def last_days(self, days):
        return [e for e in self.ent if e["ts"] >= END - days * D]

    def status(self):
        return self.conn.execute("SELECT status FROM strategies WHERE id=1").fetchone()[0]


class TestEvidence(Base):
    def test_full_agreement_passes(self):
        tail = self.last_days(75)
        self.assertGreaterEqual(len(tail), 30)
        self.feed(tail)
        ev = P.evidence(self.conn, 1)
        self.assertEqual(ev["n_reconciled"], len(tail))
        self.assertEqual(ev["engine_only"], 0)
        self.assertAlmostEqual(ev["agreement"], 1.0)
        self.assertGreaterEqual(ev["span_days"], 60)
        self.assertGreater(ev["rolling_30d_trades"], 0)
        self.assertTrue(ev["passed"], ev["failed"])

    def test_missed_alerts_count_against_agreement(self):
        tail = self.last_days(75)
        kept = [e for i, e in enumerate(tail) if i % 5 != 2]       # drop every 5th: ~80%
        self.feed(kept)
        ev = P.evidence(self.conn, 1)
        self.assertEqual(ev["engine_only"], len(tail) - len(kept))
        self.assertAlmostEqual(ev["agreement"], len(kept) / len(tail))
        self.assertFalse(ev["passed"])
        self.assertTrue(any("agreement" in f for f in ev["failed"]))

    def test_too_few_and_too_short(self):
        self.feed(self.last_days(20))
        ev = P.evidence(self.conn, 1)
        self.assertFalse(ev["passed"])
        self.assertTrue(any("reconciled alerts <" in f for f in ev["failed"]))
        self.assertTrue(any("days <" in f for f in ev["failed"]))

    def test_low_rolling_pf_fails(self):
        self.feed(self.last_days(75))
        with mock.patch.object(P, "PROMOTE_MIN_PF", 1e9):
            ev = P.evidence(self.conn, 1)
        self.assertFalse(ev["passed"])
        self.assertTrue(any("30-day paper PF" in f for f in ev["failed"]))

    def test_no_alerts(self):
        ev = P.evidence(self.conn, 1)
        self.assertFalse(ev["passed"])
        self.assertEqual(ev["n_reconciled"], 0)


class TestPromote(Base):
    def test_promotes_with_measured_evidence(self):
        self.feed(self.last_days(75))
        lineage.promote(self.conn, 1, "paper_validated", "paper:1")
        self.assertEqual(self.status(), "paper_validated")
        ev = self.conn.execute("SELECT evidence FROM promotions WHERE to_status="
                               "'paper_validated'").fetchone()[0]
        self.assertTrue(ev.startswith("paper:1 n="))
        self.assertIn("agree=100%", ev)

    def test_refuses_insufficient_evidence(self):
        self.feed(self.last_days(20))
        with self.assertRaises(SystemExit) as cm:
            lineage.promote(self.conn, 1, "paper_validated", "paper:1")
        self.assertIn("paper evidence insufficient", str(cm.exception))
        self.assertEqual(self.status(), "oos_validated")

    def test_refuses_another_strategys_log(self):
        self.feed(self.last_days(75))
        with self.assertRaises(SystemExit) as cm:
            lineage.promote(self.conn, 1, "paper_validated", "paper:2")
        self.assertIn("another strategy", str(cm.exception))
        with self.assertRaises(SystemExit):
            lineage.promote(self.conn, 1, "paper_validated", "paper:abc")
        with self.assertRaises(SystemExit):
            lineage.promote(self.conn, 1, "paper_validated", "report:1")
        self.assertEqual(self.status(), "oos_validated")


if __name__ == "__main__":
    unittest.main()
