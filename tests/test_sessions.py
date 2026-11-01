"""
P9.2: session-aware execution. Synthetic daily bars with forced signals, so
each test can state the exact fill it expects.
"""
import os
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import engine  # noqa: E402
import equities as EQ  # noqa: E402
import validation as V  # noqa: E402
import windows as W  # noqa: E402

DAY = 86400
RATE = 2.0 * 0.5 / 10000       # SPY: 2 bps spread, half of it per side


def trading_days(n, skip=()):
    out, d = [], datetime(2020, 1, 6, tzinfo=timezone.utc)
    while len(out) < n:
        if d.weekday() < 5 and d.date().isoformat() not in skip:
            out.append(int(d.timestamp()))
        d += timedelta(days=1)
    return out


def db(symbol="SPY", n=330, skip=(), overrides=None, session="us_equity"):
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE candles (pair TEXT NOT NULL, ts INTEGER NOT NULL, open REAL,
                    high REAL, low REAL, close REAL, volume REAL, PRIMARY KEY (pair, ts))""")
    EQ.ensure_tables(conn)
    EQ.seed_universe(conn)
    days = trading_days(n, skip)
    for i, t in enumerate(days):
        o, h, lo, c = (overrides or {}).get(i, (100.0, 101.0, 99.0, 100.0))
        conn.execute("INSERT INTO candles (pair, ts, open, high, low, close, volume, session)"
                     " VALUES (?, ?, ?, ?, ?, ?, 1000, ?)", (symbol, t, o, h, lo, c, session))
    conn.commit()
    return conn, days


def cfg(**kw):
    return {**engine.default_cfg("core"), "use_trailing": False, "stop_atr": 2.0, "rr_ratio": 50.0,
            "atr_len": 14, "use_risk_sizing": True, "risk_pct": 1.0, **kw}


def run(conn, days, signals, c=None, pair="SPY", tf=DAY):
    start = datetime.fromtimestamp(days[305], tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    end = datetime.fromtimestamp(days[-1], tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    return engine.backtest(conn, pair, c or cfg(), start, end, tf, signals=signals)


class TestGapThroughStop(unittest.TestCase):
    def test_long_stop_gapped_through_fills_at_the_open(self):
        # entry 100, stop about 96; opens at 90
        conn, days = db(overrides={310: (90.0, 91.0, 89.0, 90.5)})
        r = run(conn, days, {days[307]: "long"})
        t = r["trades"][0]
        self.assertEqual(t["reason"], "stop")
        self.assertEqual(t["exit_ts"], days[310])
        self.assertAlmostEqual(t["exit"], 90.0 * (1 - RATE))
        self.assertLess(t["exit"], t["entry"] - 2.0 * 2.0)   # worse than the stop price

    def test_short_stop_gapped_through_fills_at_the_open(self):
        conn, days = db(overrides={310: (110.0, 111.0, 109.0, 110.5)})
        t = run(conn, days, {days[307]: "short"})["trades"][0]
        self.assertEqual(t["reason"], "stop")
        self.assertAlmostEqual(t["exit"], 110.0 * (1 + RATE))

    def test_trailing_stop_gapped_through_fills_at_the_open(self):
        conn, days = db(overrides={310: (90.0, 91.0, 89.0, 90.5)})
        c = cfg(use_trailing=True, trail_atr=1.0)
        self.assertAlmostEqual(run(conn, days, {days[307]: "long"}, c)["trades"][0]["exit"],
                               90.0 * (1 - RATE))

    def test_a_stop_hit_intraday_still_fills_at_the_stop(self):
        conn, days = db(overrides={310: (100.0, 101.0, 90.0, 95.0)})
        t = run(conn, days, {days[307]: "long"})["trades"][0]
        self.assertGreater(t["exit"], 90.5)
        self.assertLess(t["exit"], 100.0)

    def test_crypto_gap_through_is_unchanged(self):
        conn, days = db(symbol="BTC-USD", overrides={310: (90.0, 91.0, 89.0, 90.5)}, session="crypto")
        c = cfg(slippage_ticks=5, tick_size=0.01)
        t = run(conn, days, {days[307]: "long"}, c, pair="BTC-USD")["trades"][0]
        self.assertAlmostEqual(t["exit"], 90.0 - 0.05)


class TestTradingCalendar(unittest.TestCase):
    def signal_day(self, days, weekday):
        return next(i for i in range(306, 320) if
                    datetime.fromtimestamp(days[i], tz=timezone.utc).weekday() == weekday)

    def test_friday_signal_fills_monday(self):
        conn, days = db()
        i = self.signal_day(days, 4)
        t = run(conn, days, {days[i]: "long"})["open_position"]
        self.assertEqual(t["entry_ts"], days[i + 1])
        self.assertEqual(datetime.fromtimestamp(t["entry_ts"], tz=timezone.utc).weekday(), 0)

    def test_holiday_monday_fills_tuesday(self):
        base, days = db()
        i = self.signal_day(days, 4)
        monday = datetime.fromtimestamp(days[i + 1], tz=timezone.utc).date().isoformat()
        conn, days = db(skip=(monday,))
        t = run(conn, days, {days[i]: "long"})["open_position"]
        entry = datetime.fromtimestamp(t["entry_ts"], tz=timezone.utc)
        self.assertEqual(entry.weekday(), 1)
        self.assertEqual(t["entry_ts"], days[i + 1])

    def test_every_fill_is_on_a_trading_day(self):
        conn, days = db(n=360, overrides={325: (90.0, 91.0, 89.0, 90.5)})
        sig = {days[i]: ("long" if i % 2 else "short") for i in range(306, 350, 3)}
        r = run(conn, days, sig)
        self.assertGreater(len(r["trades"]), 5)
        for t in r["trades"]:
            self.assertIn(t["entry_ts"], days)
            self.assertIn(t["exit_ts"], days)

    def test_a_weekend_bar_is_refused(self):
        conn, days = db()
        conn.execute("INSERT INTO candles (pair, ts, open, high, low, close, volume, session)"
                     " VALUES ('SPY', ?, 100, 101, 99, 100, 1, 'us_equity')",
                     (int(datetime(2020, 1, 11, tzinfo=timezone.utc).timestamp()),))
        with self.assertRaises(ValueError):
            run(conn, days, {})

    def test_a_partial_day_bar_is_refused(self):
        conn, days = db()
        d = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        conn.execute("INSERT INTO candles (pair, ts, open, high, low, close, volume, session)"
                     " VALUES ('SPY', ?, 100, 101, 99, 100, 1, 'us_equity')", (int(d.timestamp()),))
        with self.assertRaises(ValueError):
            engine.backtest(conn, "SPY", cfg(), "2020-11-02", d.strftime("%Y-%m-%dT%H:%M:%S"), DAY)

    def test_intraday_timeframes_are_refused(self):
        conn, days = db()
        for tf in (3600, 14400):
            with self.assertRaises(ValueError):
                run(conn, days, {}, tf=tf)


class TestCosts(unittest.TestCase):
    def entry(self, **kw):
        conn, days = db()
        return run(conn, days, {days[307]: "long"}, cfg(**kw))["open_position"]

    def test_instrument_costs_replace_the_cfgs(self):
        t = self.entry(tick_size=0.5, slippage_ticks=1000, commission_pct=5.0)
        self.assertAlmostEqual(t["entry"], 100.0 * (1 + RATE))

    def test_slippage_is_proportional_to_price(self):
        for px in (25.0, 400.0):
            ov = {i: (px, px * 1.01, px * 0.99, px) for i in range(330)}
            conn, days = db(overrides=ov)
            t = run(conn, days, {days[307]: "long"})["open_position"]
            self.assertAlmostEqual(t["entry"] / px - 1, RATE)

    def test_commission_is_zero(self):
        conn, days = db(overrides={310: (120.0, 121.0, 119.0, 120.0)})
        c = cfg(commission_pct=5.0, rr_ratio=1.0)
        t = run(conn, days, {days[307]: "long"}, c)["trades"][0]
        self.assertAlmostEqual(t["pnl"], (t["exit"] - t["entry"]) * t["qty"])

    def test_cost_stress_scales_instrument_costs(self):
        ov = {i: (100.0 + (i % 7), 108.0, 99.0, 100.0 + (i % 5)) for i in range(330)}
        conn, days = db(overrides=ov)
        start = datetime.fromtimestamp(days[305], tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        end = datetime.fromtimestamp(days[-1], tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        conn.execute("UPDATE instruments SET spread_bps=200 WHERE symbol='SPY'")
        c = cfg()
        sig = {days[i]: "long" for i in range(306, 325, 4)}
        one = engine.backtest(conn, "SPY", c, start, end, DAY, signals=sig)["open_position"]
        three = engine.backtest(conn, "SPY", {**c, "cost_x": 3.0}, start, end, DAY,
                                signals=sig)["open_position"]
        self.assertAlmostEqual((three["entry"] / one["entry"] - 1), 0.02 * (1.0 / 1.01), places=3)
        self.assertEqual(V.cost_stress(conn, "SPY", c, DAY, start, end, (1.0,))[0]["cost_x"], 1.0)

    def test_notional_is_capped_at_max_leverage(self):
        conn, days = db(overrides={i: (100.0, 100.05, 99.95, 100.0) for i in range(330)})
        c = cfg(stop_atr=0.5, risk_pct=5.0)
        t = run(conn, days, {days[307]: "long"}, c)["trades"][0]
        self.assertAlmostEqual(t["qty"] * t["entry"], c["initial_capital"] * 2.0)

    def test_a_row_without_costs_fails_rather_than_using_crypto_costs(self):
        conn, days = db()
        conn.execute("UPDATE instruments SET spread_bps=NULL WHERE symbol='SPY'")
        with self.assertRaises(ValueError):
            run(conn, days, {})

    def test_every_etf_has_a_spread(self):
        self.assertEqual(set(EQ.SPREAD_BPS), set(EQ.UNIVERSE))


class TestCrypto(unittest.TestCase):
    def test_no_instrument_row_means_crypto(self):
        conn, _ = db()
        self.assertIsNone(engine.instrument(conn, "BTC-USD"))
        self.assertIsNone(engine.instrument(sqlite3.connect(":memory:"), "BTC-USD"))

    def test_crypto_runs_on_a_db_without_the_cost_columns(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE instruments (symbol TEXT PRIMARY KEY, session TEXT, tick_size REAL)")
        conn.execute("INSERT INTO instruments VALUES ('SPY', 'us_equity', 0.01)")
        self.assertIsNone(engine.instrument(conn, "BTC-USD"))
        with self.assertRaises(ValueError):
            engine.instrument(conn, "SPY")

    def test_daily_timeframe_name(self):
        self.assertEqual(W.tf_seconds("1d"), DAY)
        self.assertEqual((W.tf_seconds("1h"), W.tf_seconds("4h")), (3600, 14400))


if __name__ == "__main__":
    unittest.main()
