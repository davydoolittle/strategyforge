"""
Tests for P13's event selection and the P12.3 coverage check (app/event_drift.py):
the registered filters, the 09:30 New York cut, membership on the day, the
20-trading-day de-duplication, and the priced test. Hand-built inputs.
Run from the repo root:   python -m unittest discover -s tests -v
"""

import os
import sys
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import event_drift as ED  # noqa: E402


def weekdays(start, n):
    d, out = date.fromisoformat(start), []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


CAL = weekdays("2004-06-01", 1000)
IVS = {"0000000001": [("2004-01-01", None)], "0000000002": [("2004-01-01", "2007-01-01")]}


class TestTiming(unittest.TestCase):
    def test_new_york_open_cut(self):
        cal = ["2010-03-05", "2010-03-08", "2010-03-09"]        # Fri, Mon, Tue
        self.assertEqual(ED.reaction_day("2010-03-08T14:29:00Z", cal), 1)   # 09:29 EST: Monday
        self.assertEqual(ED.reaction_day("2010-03-08T14:31:00Z", cal), 2)   # 09:31: Tuesday
        self.assertEqual(ED.reaction_day("2010-03-06T15:00:00Z", cal), 1)   # Saturday: Monday
        self.assertEqual(ED.reaction_day("2010-03-05T23:00:00Z", cal), 1)   # Friday evening
        # daylight saving: 09:29 EDT is 13:29 UTC
        cal2 = ["2010-07-06", "2010-07-07"]
        self.assertEqual(ED.reaction_day("2010-07-06T13:29:00Z", cal2), 0)
        self.assertEqual(ED.reaction_day("2010-07-06T13:31:00Z", cal2), 1)
        self.assertIsNone(ED.reaction_day("2010-07-07T20:00:00Z", cal2))
        # a filing at 01:00 UTC is the previous evening in New York
        self.assertEqual(ED.ny_time("2010-07-07T01:00:00Z").date().isoformat(), "2010-07-06")


class TestSelection(unittest.TestCase):
    def f(self, acc, cik, accepted, items, form="8-K"):
        return (acc, cik, form, accepted, items)

    def test_filters(self):
        filings = [
            self.f("a", "0000000001", "2006-02-01T21:00:00Z", "1.01,9.01"),      # kept
            self.f("b", "0000000001", "2006-06-01T21:00:00Z", "1.01,2.03"),      # debt: out
            self.f("c", "0000000001", "2006-09-01T21:00:00Z", "1.01,2.02"),      # results: out
            self.f("d", "0000000001", "2006-12-01T21:00:00Z", "1.01", "8-K/A"),  # amendment
            self.f("e", "0000000001", "2004-12-15T21:00:00Z", "1.01"),           # before 2005
            self.f("g", "0000000002", "2007-03-01T21:00:00Z", "1.01"),           # left the index
            self.f("h", "0000000009", "2006-03-01T21:00:00Z", "1.01"),           # never a member
            self.f("i", "0000000001", "2006-04-03T21:00:00Z", "8.01"),           # no 1.01
        ]
        ev, counts = ED.select_events(filings, IVS, CAL)
        self.assertEqual([e[0] for e in ev], ["a"])
        self.assertEqual(counts, {"amendment": 1, "before_start": 1, "not_member": 2, "excluded_item": 2,
                                  "duplicate": 0, "after_end": 0})

    def test_dedup_counts_any_earlier_1_01(self):
        filings = [
            self.f("x1", "0000000001", "2006-02-01T21:00:00Z", "1.01,2.03"),     # excluded, but starts the clock
            self.f("x2", "0000000001", "2006-02-15T21:00:00Z", "1.01"),           # 10 trading days later: duplicate
            self.f("x3", "0000000001", "2006-04-03T21:00:00Z", "1.01"),           # > 20 trading days on: kept
            self.f("x4", "0000000001", "2006-04-28T21:00:00Z", "1.01"),           # 19 trading days after x3
        ]
        ev, counts = ED.select_events(filings, IVS, CAL)
        self.assertEqual([e[0] for e in ev], ["x3"])
        self.assertEqual((counts["duplicate"], counts["excluded_item"]), (2, 1))

    def test_membership_uses_new_york_date(self):
        ivs = {"0000000001": [("2006-03-02", None)]}
        filings = [self.f("n", "0000000001", "2006-03-02T02:00:00Z", "1.01")]     # 21:00 on 03-01 in NY
        ev, counts = ED.select_events(filings, ivs, CAL)
        self.assertEqual((ev, counts["not_member"]), ([], 1))


class TestPriced(unittest.TestCase):
    def test_needs_bars_around_the_event(self):
        spans = {"AAA": [("2003-01-02", "2026-11-02")], "NEWCO": [("2025-06-26", "2026-11-02")],
                 "GONE": [("2003-01-02", "2008-06-30")]}
        self.assertTrue(ED.priced(spans, ["AAA"], "2006-02-01"))
        self.assertFalse(ED.priced(spans, ["NEWCO"], "2006-02-01"))          # a later company on the ticker
        self.assertFalse(ED.priced(spans, ["GONE"], "2008-06-01"))           # ends inside the hold
        self.assertTrue(ED.priced(spans, ["GONE"], "2008-02-01"))
        self.assertTrue(ED.priced(spans, ["NEWCO", "AAA"], "2006-02-01"))    # any of its tickers
        self.assertTrue(ED.priced(spans, ["AAA"], "2026-10-15"))             # recent: bars to the list's end


if __name__ == "__main__":
    unittest.main()
