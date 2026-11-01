"""
Phase 10: the pre-registered portfolio rule, on hand-made prices whose answers
can be worked out by eye.
"""
import math
import os
import random
import sqlite3
import sys
import unittest
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import equities as EQ  # noqa: E402
import etf_portfolio as P  # noqa: E402


def calendar(n, start=date(2019, 1, 1)):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp()))
        d += timedelta(days=1)
    return out


def walk(n, seed, drift, vol=0.01):
    rng = random.Random(seed)
    px, out = 100.0, []
    for _ in range(n):
        px *= 1 + rng.gauss(drift, vol)
        out.append(px)
    return out


class TestSignals(unittest.TestCase):
    def test_month_ends(self):
        cal = calendar(70)
        ends = P.month_ends(cal)
        self.assertEqual([datetime.fromtimestamp(cal[i], tz=timezone.utc).date().isoformat()
                          for i in ends[:2]], ["2019-01-31", "2019-02-28"])
        self.assertEqual(ends[-1], 69)

    def test_rising_is_held_and_falling_is_cash(self):
        c = {"UP": walk(300, 1, 0.002), "DOWN": walk(300, 2, -0.002)}
        t = P.book_targets(c, 299, 40)
        self.assertEqual(list(t), ["UP"])

    def test_size_targets_ten_percent_vol_in_a_tenth_of_equity(self):
        c = {str(i): walk(300, i, 0.003, 0.0126) for i in range(10)}    # ~20% vol a year
        t = P.book_targets(c, 299, 40)
        for s, w in t.items():
            self.assertAlmostEqual(w, P.TARGET_VOL / P.realised_vol(c[s], 299) / 10)
            self.assertLess(w, 0.1)

    def test_sleeve_cap_limits_total_notional_to_two(self):
        c = {str(i): walk(300, i, 0.003, 0.0005) for i in range(10)}
        t = P.book_targets(c, 299, 40)
        self.assertEqual(len(t), 10)
        self.assertAlmostEqual(sum(t.values()), 2.0)

    def test_a_signal_uses_nothing_after_its_day(self):
        c = {"A": walk(300, 3, 0.002)}
        before = P.book_targets(c, 200, 40)
        c["A"][201:] = [1.0] * 99
        self.assertEqual(before, P.book_targets(c, 200, 40))


class TestSimulate(unittest.TestCase):
    def market(self, n=400, drift=0.001):
        cal = calendar(n)
        closes = {"A": walk(n, 4, drift), "B": walk(n, 5, drift)}
        opens = {s: [c[0]] + c[:-1] for s, c in closes.items()}   # open = prior close
        return cal, opens, closes, {"A": 0.0, "B": 0.0}

    def test_fills_at_the_open_after_the_signal(self):
        cal, o, c, r = self.market()
        sim = P.simulate(cal, o, c, r, P.bench_targets)
        self.assertEqual(sim["days"][0], P.month_ends(cal)[12] + 1)

    def test_a_delay_moves_the_first_fill(self):
        cal, o, c, r = self.market()
        a = P.simulate(cal, o, c, r, P.bench_targets)
        b = P.simulate(cal, o, c, r, P.bench_targets, delay=1)
        self.assertEqual(b["days"][0], a["days"][0] + 1)

    def test_a_fully_invested_single_asset_tracks_its_price(self):
        cal = calendar(400)
        c = {"A": walk(400, 6, 0.001)}
        o = {"A": [c["A"][0]] + c["A"][:-1]}
        sim = P.simulate(cal, o, c, {"A": 0.0}, lambda cl, s, p: {"A": 1.0})
        d0 = sim["days"][0]
        self.assertAlmostEqual(sim["equity"][-1], c["A"][-1] / o["A"][d0])
        self.assertAlmostEqual(sum(sim["sleeve_pnl"].values()), sim["equity"][-1] - 1)

    def test_all_cash_stays_at_one(self):
        cal, o, c, r = self.market()
        sim = P.simulate(cal, o, c, r, lambda cl, s, p: {})
        self.assertEqual(set(sim["equity"]), {1.0})

    def test_costs_are_charged_on_what_is_traded(self):
        cal, o, c, _ = self.market()
        target = lambda cl, s, p: {"A": 1.0}   # noqa: E731
        free = P.simulate(cal, o, c, {"A": 0.0, "B": 0.0}, target)
        paid = P.simulate(cal, o, c, {"A": 0.01, "B": 0.0}, target)
        twice = P.simulate(cal, o, c, {"A": 0.01, "B": 0.0}, target, cost_x=2.0)
        self.assertLess(paid["equity"][-1], free["equity"][-1])
        self.assertLess(twice["equity"][-1], paid["equity"][-1])

    def test_borrowing_only_costs_above_one_x(self):
        cal, o, c, r = self.market()
        one = lambda cl, s, p: {"A": 0.5, "B": 0.5}   # noqa: E731
        two = lambda cl, s, p: {"A": 1.0, "B": 1.0}   # noqa: E731
        self.assertAlmostEqual(P.simulate(cal, o, c, r, one, borrow_rate=0.05)["equity"][-1],
                               P.simulate(cal, o, c, r, one)["equity"][-1], places=6)
        self.assertLess(P.simulate(cal, o, c, r, two, borrow_rate=0.05)["equity"][-1],
                        P.simulate(cal, o, c, r, two)["equity"][-1])


class TestVerdict(unittest.TestCase):
    def test_psr_of_a_steady_gain_passes_and_a_loss_does_not(self):
        rng = random.Random(7)
        up = [0.01 + rng.gauss(0, 0.01) for _ in range(200)]
        self.assertGreater(P.psr(up)[0], 0.99)
        self.assertLess(P.psr([-x for x in up])[0], 0.01)

    def test_psr_at_zero_mean_is_about_a_half(self):
        x = [0.01, -0.01] * 100
        self.assertAlmostEqual(P.psr(x)[0], 0.5, places=6)


class TestLoad(unittest.TestCase):
    def db(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE candles (pair TEXT, ts INTEGER)")
        EQ.ensure_tables(conn)
        EQ.seed_universe(conn)
        for s in EQ.UNIVERSE:
            for t in calendar(5, date(2007, 3, 1)):
                conn.execute("INSERT INTO bar_snapshots VALUES ('s', ?, ?, 1, 1, 1, 1, 1)", (s, t))
        return conn

    def test_loads_a_full_calendar(self):
        cal, o, c, rate = P.load(self.db(), "s")
        self.assertEqual((len(cal), len(c)), (5, 10))
        self.assertAlmostEqual(rate["SPY"], 2.0 * 0.5 / 10000)

    def test_a_missing_bar_is_refused(self):
        conn = self.db()
        conn.execute("DELETE FROM bar_snapshots WHERE pair='TLT' AND ts=(SELECT MAX(ts) FROM bar_snapshots)")
        with self.assertRaises(ValueError):
            P.load(conn, "s")


if __name__ == "__main__":
    unittest.main()
