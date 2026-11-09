"""
Tests for the free point-in-time S&P 500 universe (P12: app/stocks.py) and the
SEC filings fetcher (P13.1: app/filings.py). Hand-built inputs, no network.
Run from the repo root:   python -m unittest discover -s tests -v
"""

import json
import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import filings as F  # noqa: E402
import filings_forward as FF  # noqa: E402
import stock_bars as SB  # noqa: E402
import stocks as S  # noqa: E402

FJA = """date,tickers
2003-06-02,"AAA,BBB,CCC"
2004-03-01,"AAA,BBB,DDD"
2006-05-01,"AAA,DDD,EEE"
2008-01-02,"AAA,BBB,EEE"
"""

CHANGES_HTML = """<table><tr><th>Effective Date</th></tr><tr><th>Ticker</th></tr>
<tr><td>January 2, 2008</td><td>BBB</td><td>Bee Corp.</td><td>DDD</td><td>Dee &amp; Co</td><td>Dee acquired<sup>[1]</sup></td><td>[1]</td></tr>
<tr><td>May 1, 2006</td><td>EEE</td><td>Eee Inc.</td><td>BBB</td><td>Bee Corp.</td><td>Index change</td><td></td></tr>
<tr><td>bad date</td><td>X</td><td>X</td><td>Y</td><td>Y</td><td>Z</td><td></td></tr>
</table>"""

CURRENT_HTML = """<table><tr><th>Symbol</th><th>Security</th><th>CIK</th></tr>
<tr><td><a>AAA</a></td><td>Aaa Inc.</td><td>Tech</td><td>0000000001</td></tr>
<tr><td>EEE</td><td>Eee Inc.</td><td>Tech</td><td>0000000005</td></tr>
</table>"""


class TestMembers(unittest.TestCase):
    def test_intervals_from_change_rows(self):
        rows = S.parse_fja(FJA)
        ivs = S.intervals(rows, since="2004-01-01")
        self.assertEqual(ivs, [("AAA", "2004-01-01", None),
                               ("BBB", "2004-01-01", "2006-05-01"), ("BBB", "2008-01-02", None),
                               ("CCC", "2004-01-01", "2004-03-01"),
                               ("DDD", "2004-03-01", "2008-01-02"),
                               ("EEE", "2006-05-01", None)])
        self.assertEqual(S.counts(rows, since="2005-01-01"), [("2006-05-01", 3), ("2008-01-02", 3)])

    def test_wiki_changes_and_cross_check(self):
        ch = S.parse_wiki_changes(CHANGES_HTML)
        self.assertEqual(ch[0], ("2008-01-02", "BBB", "Bee Corp.", "DDD", "Dee & Co", "Dee acquired"))
        self.assertEqual(len(ch), 2)
        ivs = S.intervals(S.parse_fja(FJA), since="2004-01-01")
        checked, misses = S.cross_check(ivs, ch, since="2005-01-01")
        self.assertEqual((checked, misses), (4, []))
        late = [("2008-01-20", "BBB", "Bee", None, None, "")]
        self.assertEqual(S.cross_check(ivs, late, since="2005-01-01")[1], [("2008-01-20", "added", "BBB")])

    def test_later_wikipedia_changes_extend_the_list(self):
        ivs = S.intervals(S.parse_fja(FJA), since="2004-01-01")
        later = [("2009-03-02", "FFF", "Fff", "AAA", "Aaa", ""), ("2007-01-01", "ZZZ", "Z", None, None, "")]
        got, applied = S.extend_with_wiki(ivs, later, "2008-01-02")
        self.assertIn(("AAA", "2004-01-01", "2009-03-02"), got)
        self.assertIn(("FFF", "2009-03-02", None), got)
        self.assertNotIn("ZZZ", [g[0] for g in got])            # older than the file: already in it
        self.assertEqual(applied, [("2009-03-02", "removed", "AAA"), ("2009-03-02", "added", "FFF")])

    def test_wiki_current_has_ciks(self):
        self.assertEqual(S.parse_wiki_current(CURRENT_HTML),
                         [("AAA", "Aaa Inc.", "0000000001"), ("EEE", "Eee Inc.", "0000000005")])


class TestCik(unittest.TestCase):
    def test_names(self):
        self.assertEqual(S.norm_name("The Walt Disney Company"), "walt disney")
        self.assertEqual(S.norm_name("Dee & Co"), "dee and")
        self.assertEqual(S.norm_name("LEHMAN BROTHERS HOLDINGS INC"), "lehman brothers")
        idx = S.parse_cik_lookup("BEE CORP:0000000002:\nDEE & CO:0000000004:\nTWIN INC:0000000007:\n"
                                 "TWIN CORP:0000000008:\n")
        self.assertEqual(idx["bee"], {"0000000002"})
        self.assertEqual(idx["twin"], {"0000000007", "0000000008"})

    def setUp(self):
        self.ivs = S.intervals(S.parse_fja(FJA), since="2004-01-01")
        self.ch = S.parse_wiki_changes(CHANGES_HTML)
        self.cur = S.parse_wiki_current(CURRENT_HTML)
        self.idx = S.parse_cik_lookup("BEE CORP:0000000002:\nDEE & CO:0000000004:\n")

    def test_candidates_and_choice(self):
        cands = S.candidates(self.ivs, self.cur, {"CCC": 3}, self.ch, self.idx)
        self.assertEqual(cands[("AAA", "2004-01-01")][0], ("0000000001", "wikipedia-current", "Aaa Inc."))
        self.assertEqual(cands[("BBB", "2004-01-01")][0][:2], ("0000000002", "name-ticker"))
        self.assertEqual(cands[("CCC", "2004-01-01")][0], ("0000000003", "sec-ticker", None))
        periodic = {"0000000002": ["2004-05-10", "2008-05-01"], "0000000004": ["2005-05-10"],
                    "0000000003": ["2019-05-10"]}          # CCC's ticker now belongs to a later company
        got, amb = S.choose(self.ivs, cands, lambda c: periodic.get(c, []))
        self.assertEqual(got[("BBB", "2004-01-01")][0], "0000000002")
        self.assertEqual(got[("DDD", "2004-03-01")][0], "0000000004")
        self.assertNotIn(("CCC", "2004-01-01"), got)        # a reused ticker is refused
        self.assertEqual(amb, [])

    def test_a_common_name_is_settled_by_filings(self):
        idx = S.parse_cik_lookup("BEE CORP:0000000002:\nBEE INC:0000000022:\nDEE & CO:0000000004:\n")
        cands = S.candidates(self.ivs, self.cur, {}, self.ch, idx)
        self.assertEqual({c[0] for c in cands[("BBB", "2004-01-01")] if c[1] == "name-ticker"},
                         {"0000000002", "0000000022"})
        periodic = {"0000000002": ["2004-05-10", "2005-05-10"], "0000000022": ["1999-01-01", "2020-01-01"]}
        got, _ = S.choose(self.ivs, cands, lambda c: periodic.get(c, []))
        self.assertEqual(got[("BBB", "2004-01-01")][0], "0000000002")

    def test_name_guards(self):
        self.assertTrue(S.ticker_fits("BSC", "Bear Stearns Companies"))
        self.assertFalse(S.ticker_fits("BSC", "Ambac Financial"))
        self.assertTrue(S.ticker_fits("AET", "Aetna"))
        self.assertFalse(S.ticker_fits("AET", "Stericycle"))
        self.assertTrue(S.name_agrees("Paramount Global", "PARAMOUNT GLOBAL"))
        self.assertFalse(S.name_agrees("Paramount Global", "Parabolic Technologies, Inc."))

    def test_a_reused_ticker_needs_the_name(self):
        ivs = [("PARA", "2022-02-17", "2025-08-08")]
        cands = {("PARA", "2022-02-17"): [("0000000100", "sec-ticker", None),
                                          ("0000000200", "name-ticker", "Paramount Global")]}
        periodic = {"0000000100": ["2023-05-01"], "0000000200": ["2023-05-01"]}
        got, _ = S.choose(ivs, cands, lambda c: periodic.get(c, []), (),
                          {"0000000100": ["Parabolic Technologies, Inc."], "0000000200": ["Paramount Global"]})
        self.assertEqual(got[("PARA", "2022-02-17")][0], "0000000200")
        only_sec = {("PARA", "2022-02-17"): [("0000000100", "sec-ticker", None)]}
        got, _ = S.choose(ivs, only_sec, lambda c: periodic.get(c, []), (), {"0000000100": ["Paramount Global"]})
        self.assertEqual(got[("PARA", "2022-02-17")][0], "0000000100")   # no wiki name: the ticker alone

    def test_name_date_needs_the_ticker_to_fit(self):
        ivs = [("BSC", "2004-01-01", "2008-06-02")]
        cands = {("BSC", "2004-01-01"): [("0000000300", "name-date", "Ambac Financial"),
                                         ("0000000400", "name-date", "Bear Stearns Companies")]}
        got, amb = S.choose(ivs, cands, lambda c: ["2006-01-01"])
        self.assertEqual(got[("BSC", "2004-01-01")][0], "0000000400")

    def test_ticker_change_inherits_the_cik(self):
        ivs = [("OLDT", "2004-01-01", "2010-06-01"), ("NEWT", "2010-06-01", None)]
        cands = {("NEWT", "2010-06-01"): [("0000000042", "wikipedia-current", "Same Co")]}
        periodic = {"0000000042": ["2005-05-01", "2011-05-01"]}
        got, _ = S.choose(ivs, cands, lambda c: periodic.get(c, []))
        self.assertNotIn(("OLDT", "2004-01-01"), got)           # choose() no longer guesses renames
        got = {("NEWT", "2010-06-01"): cands[("NEWT", "2010-06-01")][0]}
        S.link_renames(ivs, got, lambda c: periodic.get(c, []), [])
        self.assertEqual(got[("OLDT", "2004-01-01")], ("0000000042", "rename", "Same Co"))
        # but not when the index changed that day, for any ticker: then it is a swap
        ch = [("2010-06-01", "XXX", "X", "YYY", "Y", "")]
        got = {("NEWT", "2010-06-01"): cands[("NEWT", "2010-06-01")][0]}
        S.link_renames(ivs, got, lambda c: periodic.get(c, []), ch)
        self.assertNotIn(("OLDT", "2004-01-01"), got)

    def test_two_verified_name_date_candidates_stay_unmatched(self):
        idx = S.parse_cik_lookup("BEE CORP:0000000002:\nBOB INC:0000000006:\n")
        ivs = [("B", "2004-01-01", "2008-01-02")]          # fits both names
        ch = [("2008-01-02", None, None, "QQQ1", "Bee Corp.", ""), ("2008-01-02", None, None, "QQQ2", "Bob Inc", "")]
        cands = S.candidates(ivs, [], {}, ch, idx)
        self.assertEqual({c[1] for c in cands[("B", "2004-01-01")]}, {"name-date"})
        got, amb = S.choose(ivs, cands, lambda c: ["2006-01-01"])
        self.assertEqual(got, {})
        self.assertEqual(len(amb), 1)


class TestSecClient(unittest.TestCase):
    def test_rate_limit_and_user_agent(self):
        t = [0.0]
        slept, seen = [], []
        sec = S.Sec("Owner owner@example.org", get=lambda url, h: seen.append(h) or b"{}",
                    sleep=lambda x: (slept.append(x), t.__setitem__(0, t[0] + x)), clock=lambda: t[0])
        for _ in range(3):
            sec("https://data.sec.gov/x")
        self.assertEqual(seen[0]["User-Agent"], "Owner owner@example.org")
        self.assertEqual(len(slept), 2)
        self.assertAlmostEqual(slept[0], 1 / S.SEC_RATE)

    def test_user_agent_file_required(self):
        with self.assertRaises(SystemExit):
            S.sec_user_agent("/nonexistent/sec.env")


def submissions(recent, files=()):
    return {"name": "Bee Corp", "tickers": ["BEE"], "formerNames": [{"name": "OLD BEE"}],
            "filings": {"recent": recent, "files": [{"name": f} for f in files]}}


def block(rows):
    keys = ("accessionNumber", "form", "filingDate", "acceptanceDateTime", "items")
    return {k: [r[i] for r in rows] for i, k in enumerate(keys)}


class TestFilings(unittest.TestCase):
    def test_fetch_with_extra_pages_and_form_filter(self):
        pages = {
            F.SUBMISSIONS.format(cik="0000000002"): submissions(block([
                ("a1", "8-K", "2010-01-05", "2010-01-04T22:01:00.000Z", "1.01,9.01"),
                ("a2", "4", "2010-01-06", "2010-01-06T15:00:00.000Z", ""),
                ("a3", "10-Q", "2010-05-01", "2010-05-01T14:00:00.000Z", "")]), ["CIK0000000002-submissions-001.json"]),
            F.EXTRA.format(name="CIK0000000002-submissions-001.json"): block([
                ("b1", "8-K", "2005-03-01", "2005-03-01T13:00:00.000Z", "2.02")]),
        }
        company, rows = F.fetch_company(lambda url: json.dumps(pages[url]).encode(), 2)
        self.assertEqual(company[:4], ("0000000002", "Bee Corp", '["BEE"]', '["OLD BEE"]'))
        self.assertEqual(sorted(r[0] for r in rows), ["a1", "a3", "b1"])    # Form 4 left out
        a1 = next(r for r in rows if r[0] == "a1")
        self.assertEqual(a1[4:], ("2010-01-04T22:01:00Z", "1.01,9.01"))

        conn = sqlite3.connect(":memory:")
        F.ensure_tables(conn)
        F.store_company(conn, company, rows)
        self.assertTrue(F.fresh(conn, "0000000002"))
        self.assertEqual(F.periodic_dates(conn, "0000000002"), ["2010-05-01"])
        calls = []
        F.fetch_all(conn, ["2"], sec=lambda url: calls.append(url))
        self.assertEqual(calls, [])                          # fetched today: skipped

    def test_a_failure_is_reported_not_raised(self):
        conn = sqlite3.connect(":memory:")

        def boom(url):
            raise OSError("503")
        done, failed = F.fetch_all(conn, ["7"], sec=boom, log=lambda *a: None)
        self.assertEqual((done, failed), (0, [("0000000007", "503")]))


def members_db():
    conn = sqlite3.connect(":memory:")
    S.ensure_tables(conn)
    conn.executemany("INSERT INTO pit_members (ticker, start, end) VALUES (?, ?, ?)",
                     [("AAA", "2004-01-01", None), ("BRK.B", "2004-01-01", None), ("OLD", "2004-01-01", "2008-01-02")])
    conn.commit()
    return conn


class TestStockBars(unittest.TestCase):
    def test_wanted_order_and_spelling(self):
        conn = members_db()
        F.ensure_tables(conn)
        conn.execute("UPDATE pit_members SET cik='0000000009' WHERE ticker='OLD'")
        conn.execute("INSERT INTO sec_companies VALUES ('0000000009', 'Old Co', '[\"NEWT\"]', '[]', 'x')")
        self.assertEqual(SB.wanted(conn), ["SPY", "AAA", "BRK-B", "OLD", "NEWT"])

    def test_quota_budget_and_restart(self):
        conn = members_db()
        SB.ensure_tables(conn)
        calls = []

        def fetch(t, h):
            calls.append(t)
            if t == "OLD":
                import urllib.error
                raise urllib.error.HTTPError("u", 404, "nf", None, None)
            return [(1104710400 + i * 86400, 1, 1, 1, 1, 1) for i in range(3)]
        from datetime import datetime, timezone
        now = datetime(2026, 11, 9, tzinfo=timezone.utc)
        log = lambda *a: None   # noqa: E731
        SB.run(conn, {}, now, fetch, daily_max=2, budget=3, log=log)
        self.assertEqual(calls, ["SPY", "AAA"])
        SB.run(conn, {}, now, fetch, daily_max=2, budget=3, log=log)       # budget leaves one
        self.assertEqual(calls, ["SPY", "AAA", "BRK-B"])
        SB.run(conn, {}, now, fetch, daily_max=2, budget=3, log=log)       # spent for November
        self.assertEqual(len(calls), 3)
        SB.run(conn, {}, datetime(2026, 12, 1, tzinfo=timezone.utc), fetch, daily_max=2, budget=3, log=log)
        self.assertEqual(calls[-1], "OLD")
        st = dict(conn.execute("SELECT ticker, status FROM stock_bar_status").fetchall())
        self.assertEqual(st, {"SPY": "ok", "AAA": "ok", "BRK-B": "ok", "OLD": "missing"})
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM stock_bars").fetchone()[0], 9)
        self.assertEqual(SB.todo(conn), [])

    def test_server_error_is_retried_later(self):
        conn = members_db()
        import urllib.error

        def fetch(t, h):
            raise urllib.error.HTTPError("u", 503, "busy", None, None)
        SB.run(conn, {}, None, fetch, daily_max=5, budget=50, log=lambda *a: None)
        self.assertIn("SPY", SB.todo(conn))


class TestForward(unittest.TestCase):
    def run_once(self, conn, day, current, bars=None):
        from datetime import datetime, timezone
        return FF.run(conn, now=datetime.fromisoformat(day + "T04:00:00+00:00"), current=current,
                      sec=lambda url: (_ for _ in ()).throw(OSError("offline")), alpaca_headers={},
                      bars_fn=lambda syms, start, end, h: bars or {}, log=lambda *a: None)

    def test_start_is_fixed_and_spans_track_changes(self):
        conn = sqlite3.connect(":memory:")
        members = [(f"T{i:03d}", f"Co {i}", f"{i:010d}") for i in range(500)]
        self.run_once(conn, "2026-11-10", members)
        r = self.run_once(conn, "2026-11-11", members[1:] + [("NEW", "New Co", "0000099999")])
        self.assertEqual((r["new"], r["gone"]), (["NEW"], ["T000"]))
        self.assertEqual(S.meta(conn, "fwd_start"), "2026-11-10")
        self.assertEqual(conn.execute("SELECT first_seen, last_seen FROM fwd_members WHERE ticker='T000'").fetchone(),
                         ("2026-11-10", "2026-11-10"))
        self.assertEqual(conn.execute("SELECT first_seen, last_seen FROM fwd_members WHERE ticker='T001'").fetchone(),
                         ("2026-11-10", "2026-11-11"))
        self.assertEqual(len(r["failed"]), 501)             # every member ever seen; SEC offline: reported

    def test_a_broken_page_records_nothing(self):
        conn = sqlite3.connect(":memory:")
        with self.assertRaises(SystemExit):
            self.run_once(conn, "2026-11-10", [("A", "A", "1")])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM fwd_members").fetchone()[0], 0)

    def test_bars_write_only_changes(self):
        conn = sqlite3.connect(":memory:")
        FF.ensure_tables(conn)
        b = {"SPY": [(1, 1.0, 1.0, 1.0, 1.0, 5.0), (2, 1.0, 1.0, 1.0, 1.1, 5.0)]}
        self.assertEqual(FF.store_bars(conn, b), 2)
        self.assertEqual(FF.store_bars(conn, b), 0)
        b["SPY"][0] = (1, 0.9, 0.9, 0.9, 0.9, 5.0)                  # a dividend re-bases history
        self.assertEqual(FF.store_bars(conn, b), 1)


if __name__ == "__main__":
    unittest.main()
