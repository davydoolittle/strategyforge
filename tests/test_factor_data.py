"""
Phase 15 data: symbol choice, the price stop rule, company facts and 10-K word
counts. No network: Alpaca and SEC are stubbed.
"""
import json
import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import factor_data as F  # noqa: E402


def db():
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE pit_members (ticker TEXT NOT NULL, start TEXT NOT NULL, end TEXT, cik TEXT,
                    cik_source TEXT, name TEXT, PRIMARY KEY (ticker, start))""")
    conn.execute("CREATE TABLE sec_companies (cik TEXT PRIMARY KEY, name TEXT, tickers TEXT, former_names TEXT,"
                 " fetched_at TEXT)")
    conn.execute("CREATE TABLE stocks_meta (k TEXT PRIMARY KEY, v TEXT)")
    F.ensure_tables(conn)
    return conn


DAYS = ["2017-01-30", "2017-01-31", "2017-02-27", "2017-02-28", "2017-03-30", "2017-03-31", "2017-04-03"]


def fake_alpaca(series):
    """series: {symbol: [days]}; pages of 2 bars to exercise paging."""
    def get(url, headers):
        import urllib.parse as U
        q = dict(U.parse_qsl(U.urlparse(url).query))
        page = int(q.get("page_token", "0"))
        bars = {s: [{"t": d + "T05:00:00Z", "o": 10.0, "c": 11.0 if q["adjustment"] == "all" else 22.0}
                    for d in series[s]] for s in q["symbols"].split(",") if s in series}
        flat = [(s, b) for s in sorted(bars) for b in bars[s]]
        chunk = flat[page * 2:page * 2 + 2]
        out = {}
        for s, b in chunk:
            out.setdefault(s, []).append(b)
        return {"bars": out, "next_page_token": str(page + 1) if page * 2 + 2 < len(flat) else None}
    return get


class TestSymbols(unittest.TestCase):
    def test_current_ticker_first_then_the_interval_ticker(self):
        self.assertEqual(F.candidates("FB", ["META"]), ["META", "FB"])
        self.assertEqual(F.candidates("BRK.B", ["BRK-A", "BRK-B"]), ["BRK.A", "BRK.B"])
        self.assertEqual(F.candidates("SIVB", []), ["SIVB"])
        self.assertEqual(F.candidates("X", ["XPRA", "X-PA1"]), ["XPRA", "X"])     # preferreds dropped

    def test_choose_the_symbol_that_prices_most_member_days(self):
        days = ["a", "b", "c"]
        self.assertEqual(F.choose(["META", "FB"], days, {"META": {"a", "b", "c"}, "FB": {"a", "b", "c"}}),
                         ("META", 3))                                             # tie: the current one
        self.assertEqual(F.choose(["NEW", "OLD"], days, {"NEW": {"c"}, "OLD": {"a", "b"}}), ("OLD", 2))
        self.assertEqual(F.choose(["X"], days, {}), (None, 0))

    def test_member_days_end_is_exclusive(self):
        self.assertEqual(F.member_days(DAYS, "2017-01-31", "2017-02-28"), ["2017-01-31", "2017-02-27"])


class TestBarsAndCoverage(unittest.TestCase):
    def test_fetch_choose_store_and_coverage(self):
        conn = db()
        conn.executemany("INSERT INTO pit_members VALUES (?, ?, ?, ?, '', '')", [
            ("FB", "2013-12-23", "2017-03-01", "c1"), ("META", "2017-03-01", None, "c1"),
            ("GONE", "2010-01-01", "2017-02-15", "c2")])
        conn.execute("INSERT INTO sec_companies VALUES ('c1', 'Meta', '[\"META\"]', '[]', '')")
        series = {"SPY": DAYS, "META": DAYS, "FB": DAYS[:4], "GONE": DAYS[:3]}
        rows = F.fetch_bars(conn, {}, fake_alpaca(series), lambda s: None, log=lambda m: None)
        got = {(r[0], r[3], r[6]) for r in rows}
        self.assertIn(("FB", "META", "sec-current"), got)
        self.assertIn(("GONE", "GONE", "interval"), got)
        raw = conn.execute("SELECT close, raw_close FROM f15_bars WHERE symbol='META' LIMIT 1").fetchone()
        self.assertEqual(raw, (11.0, 22.0))
        c = F.coverage(conn)
        # formations 01-31 (FB, GONE), 02-28 (FB; GONE left 02-15), 03-31 (META): 4 member-months, all priced
        self.assertEqual((c["member_months"], c["unpriced"], c["passes"]), (4, 0, True))

    def test_an_unpriced_member_month_counts_against_the_stop_rule(self):
        conn = db()
        conn.execute("INSERT INTO pit_members VALUES ('LOST', '2010-01-01', NULL, NULL, '', '')")
        F.fetch_bars(conn, {}, fake_alpaca({"SPY": DAYS}), lambda s: None, log=lambda m: None)
        c = F.coverage(conn)
        self.assertEqual((c["member_months"], c["unpriced"], c["passes"]), (3, 3, False))

    def test_review_list_flags_late_interval_tickers(self):
        conn = db()
        conn.execute("INSERT INTO f15_symbols VALUES ('FI', '2023-06-07', 'c', 'FI', 10, 10, 'interval')")
        conn.execute("INSERT INTO f15_symbols VALUES ('OLD', '2004-01-01', 'c', 'OLD', 10, 10, 'interval')")
        self.assertEqual([r[0] for r in F.review_list(conn)], ["FI"])


class TestPreviousOwner(unittest.TestCase):
    def test_a_jump_at_the_start_drops_the_earlier_bars_only_for_that_symbol(self):
        conn = db()
        days = ["2016-11-28", "2016-11-29", "2016-11-30", "2016-12-01", "2016-12-02", "2016-12-05"]
        old = [22.0, 22.5, 22.7, 22.6, 69.9, 71.0]
        conn.executemany("INSERT INTO f15_bars VALUES ('EVHC', ?, 1, 1, ?)", list(zip(days, old)))
        conn.executemany("INSERT INTO f15_bars VALUES ('HOLX', ?, 1, 1, 40.0)", [(d,) for d in days])
        conn.execute("INSERT INTO f15_symbols VALUES ('EVHC', '2016-12-02', 'c', 'EVHC', 0, 0, 'interval')")
        conn.execute("INSERT INTO f15_symbols VALUES ('HOLX', '2016-12-01', 'h', 'HOLX', 0, 0, 'interval')")
        dropped = F.drop_previous_owner(conn, log=lambda m: None)
        self.assertEqual([d[0] for d in dropped], ["EVHC"])
        self.assertEqual(conn.execute("SELECT MIN(day) FROM f15_bars WHERE symbol='EVHC'").fetchone()[0],
                         "2016-12-02")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM f15_bars WHERE symbol='HOLX'").fetchone()[0], 6)


class TestFacts(unittest.TestCase):
    def test_keeps_usd_values_with_filing_dates_and_shares(self):
        payload = {"facts": {
            "us-gaap": {"Revenues": {"units": {"USD": [{"start": "2019-01-01", "end": "2019-12-31", "val": 100,
                                                        "form": "10-K", "filed": "2020-02-20", "accn": "a"}]}},
                        "Assets": {"units": {"EUR": [{"end": "2019-12-31", "val": 5, "filed": "2020-02-20"}]}}},
            "dei": {"EntityCommonStockSharesOutstanding": {"units": {"shares": [
                {"end": "2020-02-01", "val": 7, "form": "10-K", "filed": "2020-02-20", "accn": "a"}]}}}}}
        rows = F.parse_facts("c", payload)
        self.assertIn(("c", "revenue:0", "2019-01-01", "2019-12-31", 100, "10-K", "2020-02-20", "a"), rows)
        self.assertIn(("c", "shares:dei", None, "2020-02-01", 7, "10-K", "2020-02-20", "a"), rows)
        self.assertFalse([r for r in rows if r[1].startswith("assets")])          # not USD

    def test_a_failed_company_is_recorded_not_hidden(self):
        conn = db()
        conn.execute("INSERT INTO pit_members VALUES ('A', '2010-01-01', NULL, 'c1', '', '')")

        def sec(url):
            raise OSError("down")
        F.fetch_facts(conn, sec, log=lambda m: None)
        self.assertIn("OSError", conn.execute("SELECT note FROM f15_facts_done").fetchone()[0])


class TestText(unittest.TestCase):
    def test_visible_words_only(self):
        doc = ("<html><head><style>.x{color:red}</style><script>var a=1</script></head><body>"
               "<ix:header><ix:hidden>dei:Secret 123</ix:hidden></ix:header>"
               "<p>Risk Factors: our revenue may fall.</p><p>Revenue&amp;costs</p></body></html>")
        c = F.word_counts(doc)
        self.assertEqual(c["revenue"], 2)
        self.assertNotIn("secret", c)
        self.assertNotIn("color", c)
        self.assertNotIn("var", c)
        self.assertEqual(F.unpack(F.pack(c)), dict(c))

    def test_only_10ks_in_the_window_from_every_page(self):
        recent = {"accessionNumber": ["a1", "a2", "a3"], "form": ["10-K", "10-Q", "10-K"],
                  "filingDate": ["2024-02-01", "2024-05-01", "2014-02-01"],
                  "primaryDocument": ["k.htm", "q.htm", "old.htm"]}
        extra = {"accessionNumber": ["a4"], "form": ["10-K"], "filingDate": ["2015-03-01"],
                 "primaryDocument": ["k15.htm"]}

        def sec(url):
            if "CIK" in url:
                return json.dumps({"filings": {"recent": recent, "files": [{"name": "x.json"}]}}).encode()
            return json.dumps(extra).encode()
        self.assertEqual(F.ten_ks(sec, "c"), [("a4", "2015-03-01", "k15.htm"), ("a1", "2024-02-01", "k.htm")])


class TestFormation(unittest.TestCase):
    def test_last_trading_day_of_each_month_in_the_window(self):
        cal = ["2016-12-30", "2017-01-30", "2017-01-31", "2017-02-28", "2026-09-30", "2026-10-30"]
        self.assertEqual(F.formation_dates(cal), ["2017-01-31", "2017-02-28", "2026-09-30"])


if __name__ == "__main__":
    unittest.main()
