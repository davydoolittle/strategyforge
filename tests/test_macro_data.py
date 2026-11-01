"""
Parsers for the macro sources. No network.
"""
import json
import os
import sqlite3
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import macro_data as M  # noqa: E402


class TestParsers(unittest.TestCase):
    def test_treasury_skips_blank_tenors(self):
        t = 'Date,"3 Mo","2 Yr","10 Yr"\n12/31/1990,6.63,,8.08\n'
        got = M.parse_treasury(t)
        self.assertEqual(got["y3m"], [(date(1990, 12, 31), 6.63)])
        self.assertEqual(got["y2y"], [])

    def test_vix(self):
        t = "DATE,OPEN,HIGH,LOW,CLOSE\n01/02/1990,17,18,16,17.24\n"
        self.assertEqual(M.parse_vix(t), [(date(1990, 1, 2), 17.24)])

    def test_bls_value_is_usable_only_after_release(self):
        rows = M.parse_bls([{"year": "2022", "period": "M12", "value": "296.8"},
                            {"year": "2022", "period": "M13", "value": "1"}], "cpi")
        self.assertEqual(rows, [(date(2022, 12, 1), 296.8, date(2023, 1, 20))])

    def test_bls_skips_a_missing_month(self):
        self.assertEqual(M.parse_bls([{"year": "2025", "period": "M10", "value": "-"}], "cpi"), [])

    def test_effr_takes_the_top_of_the_range(self):
        t = json.dumps({"refRates": [{"effectiveDate": "2020-01-02", "targetRateFrom": 1.5,
                                      "targetRateTo": 1.75},
                                     {"effectiveDate": "2000-07-10", "targetRateFrom": 6.5}]})
        self.assertEqual(M.parse_effr(t), [(date(2000, 7, 10), 6.5), (date(2020, 1, 2), 1.75)])


class TestStore(unittest.TestCase):
    def test_a_series_is_replaced_whole(self):
        c = sqlite3.connect(":memory:")
        M.ensure_tables(c)
        d = date(2020, 1, 1)
        M.store(c, "vix", [(d, 1.0, d), (date(2020, 1, 2), 2.0, d)], "cboe")
        M.store(c, "vix", [(d, 3.0, d)], "cboe")
        self.assertEqual(c.execute("SELECT value FROM macro_series").fetchall(), [(3.0,)])


if __name__ == "__main__":
    unittest.main()
