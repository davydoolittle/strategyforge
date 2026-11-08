"""
P7.6 market context: parsers, derived SPY series, the regime tag, the panel,
and the explanation file. No network.
"""
import json
import os
import re
import sqlite3
import sys
import unittest
from datetime import date, datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app"))

import market_context as M  # noqa: E402
import regime_equity as R  # noqa: E402
import xls  # noqa: E402

EXPLAIN = os.path.join(HERE, "..", "app", "static", "explain.json")
INDEX = os.path.join(HERE, "..", "app", "static", "index.html")
# "buy" or "sell" (any form) followed by a ticker-like word: SPY, $QQQ, the GLD
# (the verb in any case; the ticker must be upper case, so "buys of $100K" passes)
TRADE_CALL = re.compile(r"\b(?i:buy|buys|buying|bought|sell|sells|selling|sold)\s+(?:(?i:the)\s+)?\$?[A-Z]{1,5}\b")


def db():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE context (ts INTEGER NOT NULL, metric TEXT NOT NULL, pair TEXT NOT NULL,"
                 " value REAL, PRIMARY KEY (ts, metric, pair))")
    conn.execute("CREATE TABLE candles (pair TEXT, ts INTEGER, open REAL, high REAL, low REAL, close REAL,"
                 " volume REAL, session TEXT, PRIMARY KEY (pair, ts))")
    return conn


def weekdays(n, start=date(2020, 1, 6)):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def load_spy(conn, closes, start=date(2020, 1, 6)):
    for d, c in zip(weekdays(len(closes), start), closes):
        conn.execute("INSERT INTO candles VALUES ('SPY', ?, ?, ?, ?, ?, 1, 'us_equity')",
                     (M._ts(d), c, c, c, c))


class TestParsers(unittest.TestCase):
    def test_vix(self):
        self.assertEqual(M.parse_vix("DATE,OPEN,HIGH,LOW,CLOSE\n03/16/2020,1,2,1,82.69\n"),
                         [(date(2020, 3, 16), 82.69)])

    def test_fred_skips_missing_days(self):
        p = {"observations": [{"date": "2024-01-02", "value": "0.5"}, {"date": "2024-01-03", "value": "."}]}
        self.assertEqual(M.parse_fred(p), [(date(2024, 1, 2), 0.5)])

    def test_putcall_archive_skips_the_disclaimer(self):
        t = ("Cboe data is compiled for convenience, without responsibility, 1, 2\nDATE,CALL,PUT,TOTAL,P/C Ratio\n"
             "10/04/2019, 916877, 598296, 1515173, 0.65\n")
        self.assertEqual(M.parse_pc_archive(t), [(date(2019, 10, 4), 0.65)])

    def test_putcall_daily_takes_the_equity_ratio(self):
        p = {"ratios": [{"name": "TOTAL PUT/CALL RATIO", "value": "0.95"},
                        {"name": "EQUITY PUT/CALL RATIO", "value": "0.70"}]}
        self.assertEqual(M.parse_pc_daily(p), 0.70)
        self.assertIsNone(M.parse_pc_daily({"ratios": []}))

    def test_shiller_link(self):
        page = '<a href="//img1.wsimg.com/blobby/x/downloads/abc/ie_data.xls?ver=17">Data</a>'
        self.assertEqual(M.shiller_xls_url(page), "https://img1.wsimg.com/blobby/x/downloads/abc/ie_data.xls?ver=17")
        with self.assertRaises(RuntimeError):
            M.shiller_xls_url("<html></html>")

    def test_xls_refuses_a_non_xls_file(self):
        with self.assertRaises(xls.XlsError):
            xls.read_sheet(b"PK\x03\x04 not an xls", "Data")

    def test_rk_numbers(self):
        self.assertEqual(xls._rk((5 << 2) | 2), 5.0)              # integer
        self.assertEqual(xls._rk((-7 << 2 & 0xFFFFFFFF) | 2), -7.0)
        self.assertEqual(xls._rk((1234 << 2) | 3), 12.34)          # integer / 100

    def test_real_shiller_file_if_cached(self):
        path = os.path.join(HERE, "..", "data", "cache", "ie_data.xls")
        if not os.path.exists(path):
            self.skipTest("no cached ie_data.xls (written by the market_context job)")
        cape = dict(M.parse_cape(open(path, "rb").read()))
        self.assertAlmostEqual(cape[date(1999, 12, 1)], 44.2, places=1)   # the famous peak
        self.assertAlmostEqual(cape[date(2009, 3, 1)], 13.3, places=1)
        self.assertIn(date(2000, 10, 1), cape)                             # October is written .1


class TestPutCall(unittest.TestCase):
    def test_archive_once_then_daily_files_resume(self):
        conn = db()
        load_spy(conn, [100.0] * 6, start=date(2019, 10, 7))
        calls = []

        def get(url):
            calls.append(url)
            if url == M.PC_ARCHIVE:
                return b"DATE,CALL,PUT,TOTAL,P/C Ratio\n10/04/2019, 1, 1, 2, 0.65\n"
            return json.dumps({"ratios": [{"name": M.PC_NAME, "value": "0.70"}]}).encode()
        added, missing = M.refresh_putcall(conn, get, lambda s: None, today=date(2019, 10, 12))
        self.assertEqual((added, missing), (5, []))
        calls.clear()
        self.assertEqual(M.refresh_putcall(conn, get, lambda s: None, today=date(2019, 10, 12)), (0, []))
        self.assertEqual(calls, [])


class TestSpy(unittest.TestCase):
    def test_sma_and_cross(self):
        closes = [100.0 + i * 0.1 for i in range(250)] + [80.0] * 60 + [120.0] * 80
        d = M.derive_spy(list(zip(weekdays(len(closes)), closes)))
        self.assertEqual([v for _, v in d["spy_cross"]], [-1.0, 1.0])
        self.assertEqual(d["spy_above_sma200"][-1][1], 1.0)
        self.assertAlmostEqual(M.sma([1.0, 2.0, 3.0], 2)[2], 2.5)

    def test_weekly_rsi_of_a_steady_rise_is_100(self):
        closes = [100.0 + i for i in range(200)]
        d = M.derive_spy(list(zip(weekdays(200), closes)))
        self.assertEqual(d["spy_rsi_weekly"][-1][1], 100.0)
        self.assertTrue(all(day.weekday() == 4 for day, _ in d["spy_rsi_weekly"][:-1]))


class TestRegime(unittest.TestCase):
    def test_states_and_bands(self):
        days = weekdays(600)
        up = [(d, 100.0 + i * 0.1) for i, d in enumerate(days)]
        vix = {d: 20.0 + (i % 10) for i, d in enumerate(days)}
        rows = R.tag(up, vix)
        self.assertEqual({r[8] for r in rows} <= set(R.STATES), True)
        self.assertTrue(all(r[4] == "up" for r in rows))
        down = [(d, 200.0 - i * 0.1) for i, d in enumerate(days)]
        self.assertTrue(all(r[4] == "down" for r in R.tag(down, vix)))
        calm = {d: 30.0 - i * 0.01 for i, d in enumerate(days)}           # always the year's lowest
        self.assertTrue(all(r[8] == "trend_up_calm" for r in R.tag(up, calm)))

    def test_percentile_and_band(self):
        self.assertEqual(R.percentile([1, 2, 3, 4], 4), 75.0)
        self.assertEqual((R.band(49.9), R.band(50), R.band(90), R.band(90.1)), ("<50", "50-90", "50-90", ">90"))

    def test_sizing_lines_quote_the_trigger(self):
        s = M.sizing_line(31.5, 95.0)
        self.assertEqual(s["text"], "new positions 50% of normal, no adds on dips until VIX < 90th pct")
        self.assertIn("VIX 31.50", s["trigger"])
        self.assertIn("95th percentile", s["trigger"])
        self.assertEqual(M.sizing_line(15, 10)["text"], "normal size")
        self.assertEqual(M.sizing_line(22, 70)["text"], "new positions 75% of normal, widen stops")


class TestPanel(unittest.TestCase):
    def test_statuses_follow_fixed_rules(self):
        rules = {k: r for k, _, _, _, r, _ in M.INDICATORS}
        h = [(date(2026, 1, 1), 0.0)]
        self.assertEqual([rules["vix"](v, h) for v in (15, 25, 35)], ["calm", "elevated", "high"])
        self.assertEqual([rules["t10y2y"](v, h) for v in (-0.1, 0.4)], ["inverted", "normal"])
        self.assertEqual(rules["sahm"](0.53, h), "triggered")
        fed = [(date(2026, 1, 1), 5.0), (date(2026, 9, 1), 4.0)]
        self.assertEqual(rules["fedfunds"](4.0, fed), "falling")

    def test_panel_with_data(self):
        conn = db()
        closes = [100.0 + i * 0.1 for i in range(600)]
        load_spy(conn, closes)
        days = weekdays(600)
        M.replace_series(conn, "vix", [(d, 15.0 + (i % 7)) for i, d in enumerate(days)])
        for k, v in M.derive_spy(list(zip(days, closes))).items():
            M.replace_series(conn, k, v)
        R.run(conn)
        p = M.panel(conn)
        vix = next(r for r in p["indicators"] if r["key"] == "vix")
        self.assertEqual(vix["status"], "calm")
        self.assertEqual(len(vix["spark"]), 252)
        self.assertIn(p["regime"]["state"], R.STATES)
        self.assertIn(p["sizing"]["text"], dict(M.SIZING).values())
        self.assertEqual(p["footer"], M.FOOTER)

    def test_a_db_without_the_tables_shows_no_data(self):
        p = M.panel(sqlite3.connect(":memory:"))
        self.assertEqual({r["status"] for r in p["indicators"]}, {"no data"})
        self.assertIsNone(p["sizing"])

    def test_empty_db(self):
        p = M.panel(db())
        self.assertEqual({r["status"] for r in p["indicators"]}, {"no data"})
        self.assertIsNone(p["regime"])


class TestExplain(unittest.TestCase):
    def setUp(self):
        self.e = json.load(open(EXPLAIN))

    def test_an_entry_for_every_indicator_state_and_the_sizing_line(self):
        need = {k for k, *_ in M.INDICATORS} | {"spy_sma200", "spy_cross", "sizing_context"} | set(R.STATES)
        self.assertEqual(need - set(self.e), set())

    def test_four_short_fields(self):
        for k, v in self.e.items():
            self.assertEqual(set(v), {"what_it_is", "traditional_reading", "what_the_record_shows", "source"}, k)
            for f in ("what_it_is", "traditional_reading", "what_the_record_shows"):
                self.assertLess(len(v[f].split()), 60, f"{k}.{f}")
            self.assertTrue(v["source"].startswith(("http://", "https://")), k)
            self.assertRegex(v["what_the_record_shows"], r"\b(19|20)\d\d\b", f"{k} has no dated example")


class TestNoTradeCalls(unittest.TestCase):
    """No string the dashboard shows may say buy or sell followed by a ticker."""

    def test_the_pattern_catches_one(self):
        self.assertTrue(TRADE_CALL.search("consider buying SPY now"))
        self.assertTrue(TRADE_CALL.search("Sell $QQQ"))
        self.assertFalse(TRADE_CALL.search("open-market buys of $100K+"))

    def test_page_explanations_and_panel_text(self):
        texts = [open(INDEX).read()]
        for v in json.load(open(EXPLAIN)).values():
            texts += list(v.values())
        texts += [M.FOOTER] + [t for _, t in M.SIZING] + [r[5] for r in M.INDICATORS]
        for t in texts:
            m = TRADE_CALL.search(t)
            self.assertIsNone(m, f"trade call in dashboard text: {m.group(0) if m else ''!r}")


if __name__ == "__main__":
    unittest.main()
