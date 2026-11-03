"""
Tests for the screen's ETF universe (P9.1c, app/etf_universe.py): the listing
filter, the leveraged/inverse/ETN exclusions, the dollar-volume ranking,
batching against Alpaca, stored runs with entered/left, and that the research
ten keep their own bars. No network.
Run from the repo root:   python -m unittest discover -s tests -v
"""

import io
import os
import sqlite3
import sys
import unittest
import zipfile
from datetime import datetime, timezone
from unittest import mock
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import equities as E  # noqa: E402
import etf_universe as U  # noqa: E402


def tickers_zip(rows):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("supported_tickers.csv", "ticker,exchange,assetType,priceCurrency,startDate,endDate\n" +
                   "\n".join(",".join(r) for r in rows) + "\n")
    return buf.getvalue()


def db():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE candles (pair TEXT NOT NULL, ts INTEGER NOT NULL, open REAL, high REAL,"
                 " low REAL, close REAL, volume REAL, PRIMARY KEY (pair, ts))")
    E.ensure_tables(conn)
    U.ensure_tables(conn)
    return conn


def vols(dv, n=20, start_day=1):
    return [(f"2026-10-{start_day + i:02d}", dv) for i in range(n)]


class TestExclusion(unittest.TestCase):
    def test_names(self):
        cases = {
            "ProShares UltraPro QQQ": "leveraged",
            "ProShares UltraShort S&P500": "leveraged",
            "Direxion Daily Semiconductor Bull 3X ETF": "leveraged",
            "GraniteShares 2x Long NVDA Daily ETF": "leveraged",
            "-1x Short VIX Futures ETF": "leveraged",
            "Defiance Daily Target 1.5X Long ETF": "leveraged",
            "ProShares Short S&P500": "inverse",
            "ProShares Short 20+ Year Treasury": "inverse",
            "AXS Inverse Something ETF": "inverse",
            "iPath Series B S&P 500 VIX Short-Term Futures ETN": "ETN",
            "MicroSectors FANG+ Index -3X Inverse Leveraged ETNs": "ETN",
            "State Street SPDR S&P 500 ETF Trust": None,
            "iShares Short Treasury Bond ETF": None,
            "Vanguard Short-Term Treasury ETF": None,
            "iShares Short Duration Bond ETF": None,
            "First Trust Long/Short Equity ETF": None,
            "Harbor Long-Short Equity ETF": None,
            "ProShares VIX Short-Term Futures ETF": None,     # 1x long VIX futures: kept
            "Invesco QQQ Trust, Series 1": None,
            "Bullish Something Fund": None,                   # 'bull' only as a word
            "": None,
        }
        for name, want in cases.items():
            self.assertEqual(U.exclusion(name), want, name)


class TestListing(unittest.TestCase):
    def test_us_usd_etfs_listed_in_the_last_week(self):
        z = tickers_zip([
            ("spy", "NYSE ARCA", "ETF", "USD", "1993-01-29", "2026-10-30"),
            ("QQQ", "NASDAQ", "ETF", "USD", "1999-03-10", "2026-10-30"),
            ("AAPL", "NASDAQ", "Stock", "USD", "1980-12-12", "2026-10-30"),
            ("510300", "SHG", "ETF", "CNY", "2012-05-28", "2026-10-30"),
            ("OTCX", "PINK", "ETF", "USD", "2010-01-01", "2026-10-30"),
            ("DEAD", "NYSE", "ETF", "USD", "2010-01-01", "2026-09-01"),
            ("WEEK", "BATS", "ETF", "USD", "2020-01-01", "2026-10-24"),
            ("NOEND", "NYSE", "ETF", "USD", "2020-01-01", ""),
        ])
        self.assertEqual(U.listed_etfs(z), {"SPY", "QQQ", "WEEK"})

    def test_empty_list_is_an_error(self):
        with self.assertRaises(ValueError):
            U.listed_etfs(tickers_zip([("AAPL", "NASDAQ", "Stock", "USD", "1980-12-12", "2026-10-30")]))


class TestRank(unittest.TestCase):
    def test_rank_filters_and_orders(self):
        listed = {"SPY", "QQQ", "TQQQ", "SH", "THIN", "GONE", "LOW", "TIE"}
        assets = {"SPY": ("SPDR S&P 500 ETF", "ARCA"), "QQQ": ("Invesco QQQ", "NASDAQ"),
                  "TQQQ": ("ProShares UltraPro QQQ", "NASDAQ"), "SH": ("ProShares Short S&P500", "ARCA"),
                  "THIN": ("Thin Fund ETF", "BATS"), "LOW": ("Low Fund ETF", "ARCA"),
                  "TIE": ("Tie Fund ETF", "ARCA"), "XTRA": ("Not listed by Tiingo", "ARCA")}
        volumes = {"SPY": vols(5e9), "QQQ": vols(4e9) + [("2026-10-31", 9e12)],   # median, not mean
                   "TQQQ": vols(9e9), "SH": vols(8e9), "THIN": vols(7e9, n=14),
                   "LOW": vols(1e6), "TIE": vols(1e6), "XTRA": vols(9e9)}
        members, stats = U.rank(listed, assets, volumes, top_n=3)
        self.assertEqual([m["symbol"] for m in members], ["SPY", "QQQ", "LOW"])  # tie broken by symbol
        self.assertEqual([m["rank"] for m in members], [1, 2, 3])
        self.assertEqual(members[1]["dollar_vol"], 4e9)
        self.assertEqual(stats["excluded"], {"TQQQ": "leveraged", "SH": "inverse"})
        self.assertEqual((stats["n_listed"], stats["n_tradable"], stats["n_ranked"]), (8, 7, 4))
        self.assertEqual(stats["data_end"], "2026-10-31")

    def test_uses_last_20_sessions(self):
        v = vols(1.0, n=10) + vols(100.0, n=20, start_day=11)
        members, _ = U.rank({"A"}, {"A": ("A ETF", "ARCA")}, {"A": v}, top_n=1)
        self.assertEqual((members[0]["dollar_vol"], members[0]["n_sessions"]), (100.0, 20))


class TestAlpacaBatches(unittest.TestCase):
    def test_batches_pages_and_end_lag(self):
        calls = []

        def get(url, headers):
            q = parse_qs(urlparse(url).query)
            calls.append(q)
            syms = q["symbols"][0].split(",")
            if "page_token" not in q and syms[0] == "S000":
                return {"bars": {syms[0]: [{"t": "2026-10-01T04:00:00Z", "c": 2.0, "v": 10}]},
                        "next_page_token": "p2"}
            return {"bars": {s: [{"t": "2026-10-02T04:00:00Z", "c": 1.0, "v": 5}] for s in syms},
                    "next_page_token": None}

        syms = {f"S{i:03d}" for i in range(450)}
        now = datetime(2026, 11, 1, 23, 0, tzinfo=timezone.utc)
        out = U.daily_dollar_volume(syms, {}, now=now, get=get, sleep=lambda s: None)
        self.assertEqual(len(calls), 4)                     # 200 + 200 + 50, first batch two pages
        self.assertEqual(max(len(c["symbols"][0].split(",")) for c in calls), U.BATCH_SYMBOLS)
        self.assertEqual(calls[0]["end"], ["2026-11-01T22:40:00Z"])
        self.assertEqual(calls[0]["adjustment"], ["raw"])
        self.assertEqual(out["S000"], [("2026-10-01", 20.0), ("2026-10-02", 5.0)])
        self.assertEqual(len(out), 450)


class TestStore(unittest.TestCase):
    def setUp(self):
        self.conn = db()
        self.addCleanup(self.conn.close)

    def put(self, run_date, syms):
        members = [{"rank": i, "symbol": s, "name": s + " ETF", "exchange": "ARCA",
                    "dollar_vol": 1e9 / i, "n_sessions": 20} for i, s in enumerate(syms, 1)]
        U.store(self.conn, members, {"n_listed": 9, "n_tradable": 8, "n_excluded": 1, "n_ranked": 7,
                                     "data_end": run_date}, run_date)

    def test_latest_entered_left(self):
        self.assertEqual(U.latest(self.conn), (None, [], []))
        self.put("2026-10-25", ["SPY", "QQQ", "OLD"])
        self.assertEqual(U.latest(self.conn), (["SPY", "QQQ", "OLD"], [], []))
        self.put("2026-11-01", ["QQQ", "SPY", "NEW"])
        self.assertEqual(U.latest(self.conn), (["QQQ", "SPY", "NEW"], ["NEW"], ["OLD"]))

    def test_rerun_same_day_replaces(self):
        self.put("2026-11-01", ["SPY", "QQQ"])
        self.put("2026-11-01", ["SPY", "IWM"])
        self.assertEqual([r[1] for r in U.members(self.conn, "2026-11-01")], ["SPY", "IWM"])
        self.assertEqual(U.runs(self.conn), ["2026-11-01"])

    def test_unfinished_run_is_ignored(self):
        self.put("2026-10-25", ["SPY"])
        self.conn.execute("INSERT INTO etf_universe_runs (run_date) VALUES ('2026-11-01')")
        self.assertEqual(U.runs(self.conn), ["2026-10-25"])

    def test_instruments_rows_have_no_cost_so_no_backtest(self):
        self.put("2026-11-01", ["NEWX"])
        row = self.conn.execute("SELECT asset_class, session, spread_bps FROM instruments"
                                " WHERE symbol='NEWX'").fetchone()
        self.assertEqual(row, ("etf", "us_equity", None))

    def test_research_ten_keep_their_vendor(self):
        with mock.patch.object(U.E, "backfill", return_value=[]) as bf:
            U.fetch_member_bars(self.conn, ["SPY", "NEWX", "TLT", "SGOV"], {})
        self.assertEqual(bf.call_args.kwargs["symbols"], ["NEWX", "SGOV"])
        self.assertEqual(bf.call_args.kwargs["source"], "alpaca")


class TestAlpacaEnd(unittest.TestCase):
    def test_bare_date_becomes_after_the_close(self):
        seen = []
        E.fetch_bars("SPY", "2026-11-02", {}, get=lambda url, h: seen.append(url) or {"bars": {}})
        self.assertEqual(parse_qs(urlparse(seen[0]).query)["end"], ["2026-11-02T21:00:00Z"])


class TestStoreOnlyChanges(unittest.TestCase):
    def test_unchanged_rows_are_not_rewritten(self):
        conn = db()
        self.addCleanup(conn.close)
        day = 86400
        bars = [(1700006400 + i * day, 1.0, 2.0, 0.5, 1.5, 100.0) for i in range(250)]
        E.store_bars(conn, "SGOV", bars)
        before = conn.total_changes
        E.store_bars(conn, "SGOV", bars + [(1700006400 + 250 * day, 1.0, 2.0, 0.5, 1.6, 100.0)])
        self.assertEqual(conn.total_changes - before, 1)              # only the new bar
        rebased = [(t, o * 0.99, h * 0.99, l * 0.99, c * 0.99, v) for t, o, h, l, c, v in bars]
        E.store_bars(conn, "SGOV", rebased)                            # dividend: all rewritten
        self.assertEqual(conn.execute("SELECT COUNT(*), MAX(close) FROM candles WHERE pair='SGOV'").fetchone(),
                         (250, 1.5 * 0.99))                           # and the extra date is gone


class TestReusedTicker(unittest.TestCase):
    def test_bars_before_the_last_long_gap_are_dropped(self):
        day = 86400
        old = [(1451865600 + i * day, 5.0, 5.0, 5.0, 5.0, 1.0) for i in range(100)]
        new = [(1775001600 + i * day, 25.0, 25.0, 25.0, 25.0, 1.0) for i in range(20)]
        self.assertEqual(U.current_listing(old + new), new)
        self.assertEqual(U.current_listing(old), old)
        weekend = old[:5] + [(old[4][0] + 3 * day, 5.0, 5.0, 5.0, 5.0, 1.0)]
        self.assertEqual(U.current_listing(weekend), weekend)
        self.assertEqual(U.current_listing([]), [])


class TestResearchToolsStayOnTheTen(unittest.TestCase):
    def test_snapshot_and_report_ignore_screen_funds(self):
        conn = db()
        self.addCleanup(conn.close)
        E.seed_universe(conn)
        conn.execute("INSERT INTO instruments (symbol, asset_class, session, source) VALUES"
                     " ('SGOV', 'etf', 'us_equity', 'alpaca')")
        for sym in ("SPY", "SGOV"):
            conn.execute("INSERT INTO candles (pair, ts, open, high, low, close, volume, session)"
                         " VALUES (?, 1700006400, 1, 1, 1, 1, 1, 'us_equity')", (sym,))
        conn.execute("UPDATE instruments SET source='tiingo' WHERE symbol='SPY'")
        E.snapshot(conn, "t")
        self.assertEqual(conn.execute("SELECT DISTINCT pair FROM bar_snapshots").fetchall(), [("SPY",)])
        self.assertEqual(conn.execute("SELECT sources FROM snapshot_meta").fetchone()[0], "tiingo")
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            E.report(conn)
        self.assertNotIn("SGOV", out.getvalue())


class TestCompute(unittest.TestCase):
    def test_end_to_end_without_network(self):
        z = tickers_zip([("SPY", "NYSE ARCA", "ETF", "USD", "1993-01-29", "2026-10-30"),
                         ("TQQQ", "NASDAQ", "ETF", "USD", "2010-02-11", "2026-10-30")])

        def get(url, headers):
            if "/assets" in url:
                return [{"symbol": "SPY", "name": "SPDR S&P 500 ETF", "exchange": "ARCA",
                         "tradable": True, "status": "active"},
                        {"symbol": "TQQQ", "name": "ProShares UltraPro QQQ", "exchange": "NASDAQ",
                         "tradable": True, "status": "active"}]
            q = parse_qs(urlparse(url).query)
            self.assertEqual(q["symbols"], ["SPY"])          # excluded funds are not even fetched
            return {"bars": {"SPY": [{"t": f"2026-10-{d:02d}T04:00:00Z", "c": 500.0, "v": 1e6}
                                     for d in range(1, 21)]}}

        members, stats = U.compute({}, download=lambda url: z, get=get, sleep=lambda s: None)
        self.assertEqual([m["symbol"] for m in members], ["SPY"])
        self.assertEqual(members[0]["dollar_vol"], 5e8)
        self.assertEqual(stats["excluded"], {"TQQQ": "leveraged"})


if __name__ == "__main__":
    unittest.main()
