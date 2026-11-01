"""
P9.3 wiring: the frozen snapshot, the declared budget, seeds, and ETFs in A2
and A7. A small random market, so the run itself proves nothing about edge.
"""
import json
import os
import random
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import adversary as ADV  # noqa: E402
import engine  # noqa: E402
import equities as EQ  # noqa: E402
import lineage  # noqa: E402
import optimizer as OPT  # noqa: E402
import validation as V  # noqa: E402

SNAP = "test-snap"


def market(conn, symbol, seed, n=1000):
    rng = random.Random(seed)
    px, d, rows = 100.0, datetime(2015, 1, 5, tzinfo=timezone.utc), []
    while len(rows) < n:
        if d.weekday() < 5:
            o = px * (1 + rng.gauss(0, 0.002))
            c = o * (1 + rng.gauss(0.0004, 0.012))
            rows.append((symbol, int(d.timestamp()), o, max(o, c) * 1.004, min(o, c) * 0.996, c,
                         rng.uniform(500, 2000), "us_equity"))
            px = c
        d += timedelta(days=1)
    conn.executemany("INSERT INTO candles (pair, ts, open, high, low, close, volume, session)"
                     " VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)


class Base(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("""CREATE TABLE candles (pair TEXT NOT NULL, ts INTEGER NOT NULL, open REAL,
                             high REAL, low REAL, close REAL, volume REAL, PRIMARY KEY (pair, ts))""")
        EQ.ensure_tables(self.conn)
        EQ.seed_universe(self.conn)
        OPT.ensure_tables(self.conn)
        for i, s in enumerate(("SPY", "TLT")):
            market(self.conn, s, i + 1)
            self.conn.execute("UPDATE instruments SET source='tiingo' WHERE symbol=?", (s,))
        self.conn.commit()
        EQ.snapshot(self.conn, SNAP)

    def tearDown(self):
        self.conn.close()

    def run_pair(self, pair="SPY", trials=3, seed=9101):
        return OPT.run_pair(self.conn, pair, "1d", trials, seed, "core", SNAP)


class TestSnapshot(Base):
    def test_a_snapshot_is_immutable_and_detects_a_changed_name(self):
        n = self.conn.execute("SELECT COUNT(*) FROM bar_snapshots").fetchone()[0]
        EQ.snapshot(self.conn, SNAP)     # same bars: a no-op
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM bar_snapshots").fetchone()[0], n)
        self.conn.execute("UPDATE candles SET close=close*1.01 WHERE pair='SPY'")
        with self.assertRaises(ValueError):
            EQ.snapshot(self.conn, SNAP)

    def test_a_killed_snapshot_resumes(self):
        self.conn.execute("DELETE FROM snapshot_meta")
        self.conn.execute("DELETE FROM bar_snapshots WHERE pair='TLT'")
        EQ.snapshot(self.conn, SNAP)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM bar_snapshots").fetchone()[0], 2000)

    def test_a_candidate_reproduces_after_prices_are_rebased(self):
        c = {**engine.default_cfg("core"), "bars_snapshot": SNAP, "adx_threshold": 15.0,
             "min_rel_vol": 0.8, "max_dist_ema": 4.0}
        a = engine.backtest(self.conn, "SPY", c, "2015-01-05", "2018-10-01", 86400)
        self.assertGreater(a["n_trades"], 0)
        self.conn.execute("UPDATE candles SET open=open*0.9, high=high*0.9, low=low*0.9,"
                          " close=close*0.9 WHERE pair='SPY'")
        b = engine.backtest(self.conn, "SPY", c, "2015-01-05", "2018-10-01", 86400)
        self.assertEqual(a["net"], b["net"])

    def test_an_unknown_snapshot_fails(self):
        c = {**engine.default_cfg("core"), "bars_snapshot": "nope"}
        with self.assertRaises(RuntimeError):
            engine.backtest(self.conn, "SPY", c, "2015-01-05", "2018-10-01", 86400)

    def test_the_snapshot_is_not_dna(self):
        c = engine.default_cfg("core")
        self.assertEqual(lineage.dna_of(c), lineage.dna_of({**c, "bars_snapshot": SNAP}))


class TestBudget(Base):
    def test_over_budget_is_refused(self):
        with self.assertRaises(SystemExit):
            self.run_pair(trials=101)

    def test_a_second_seed_is_refused(self):
        self.run_pair(trials=2)
        with self.assertRaises(SystemExit):
            self.run_pair(trials=2, seed=9102)

    def test_a_rerun_of_the_same_seed_resumes_without_new_rows(self):
        self.run_pair(trials=3)
        self.run_pair(trials=3)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0], 3)

    def test_an_etf_run_needs_a_snapshot(self):
        with self.assertRaises(SystemExit):
            OPT.run_pair(self.conn, "SPY", "1d", 1, 9101, "core", None)


class TestRows(Base):
    def test_seed_tf_family_snapshot_and_windows_are_stored(self):
        self.run_pair(pair="TLT", trials=1)
        seed, tf, fam, pj, s = self.conn.execute(
            "SELECT seed, tf, family, params_json, split_ts FROM candidates").fetchone()
        self.assertEqual((seed, tf, fam), (9101 * 1000 + list(EQ.UNIVERSE).index("TLT"), "1d", "core"))
        self.assertEqual(json.loads(pj)["bars_snapshot"], SNAP)
        self.assertIsNotNone(s)

    def test_seeds_differ_per_etf_and_universe_order_is_fixed(self):
        self.assertEqual(list(EQ.UNIVERSE),
                         "SPY QQQ IWM EFA EEM TLT IEF GLD DBC UUP".split())


class TestAdversaryScope(Base):
    def test_a2_counts_every_etf_and_a7_tests_the_others(self):
        self.run_pair("SPY", 4)
        self.run_pair("TLT", 4)
        cid = self.conn.execute("SELECT id FROM candidates WHERE pair='SPY' ORDER BY id LIMIT 1"
                                ).fetchone()[0]
        self.assertEqual(V.sharpe_variance_from_candidates(self.conn, None, "1d", "core")[2], 8)
        rep = ADV.attack(self.conn, cid, verbose=False)
        a2, a7 = rep["attacks"]["A2"], rep["attacks"]["A7"]
        self.assertEqual((a2["n_trials_pair_tf"], a2["n_trials_home_pair"], a2["trial_scope"]),
                         (8, 4, "family_tf"))
        self.assertEqual(a7["excluded_unverified_tick"], [])
        self.assertEqual([r["pair"] for r in a7["results"]], ["TLT"])


if __name__ == "__main__":
    unittest.main()
