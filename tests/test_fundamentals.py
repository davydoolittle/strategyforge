"""
Tests for P9.4 fundamentals (app/fundamentals.py): fact parsing, tag choice,
trailing twelve months with a derived Q4, the ratios on a hand-built company,
Piotroski, Altman (not for financials), percentiles, and the paid fields shown
as not available. No network.
Run from the repo root:   python -m unittest discover -s tests -v
"""

import json
import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import fundamentals as F  # noqa: E402

CIK = "0000000001"


def fact(tag, unit, start, end, val, form="10-Q", fy=2025, fp="Q1", filed=None, accn=None):
    return (CIK, tag, unit, start, end, fy, fp, form, filed or end, accn or f"a-{tag}-{end}-{start}", val)


def company_db(price=50.0):
    conn = sqlite3.connect(":memory:")
    F.ensure_tables(conn)
    conn.execute("CREATE TABLE candles (pair TEXT, ts INTEGER, close REAL, session TEXT)")
    conn.execute("INSERT INTO candles VALUES ('TST', 1764000000, ?, 'us_equity')", (price,))
    conn.execute("INSERT INTO fund_companies VALUES ('TST', ?, 'Test Co', '3571', 'Computers', 'x')", (CIK,))
    rows = []
    usd = "USD"
    # three fiscal years (Jan-Dec) of annual flows, and 2025's quarters Q1-Q3
    for y, rev, ni, cfo in ((2022, 800.0, 80.0, 100.0), (2023, 900.0, 90.0, 120.0), (2024, 1000.0, 100.0, 150.0),
                            (2025, 1200.0, 120.0, 180.0)):
        s, e = f"{y}-01-01", f"{y}-12-31"
        for tag, v in (("us-gaap:Revenues", rev), ("us-gaap:NetIncomeLoss", ni),
                       ("us-gaap:NetCashProvidedByUsedInOperatingActivities", cfo),
                       ("us-gaap:PaymentsToAcquirePropertyPlantAndEquipment", 30.0),
                       ("us-gaap:OperatingIncomeLoss", ni * 1.5), ("us-gaap:CostOfRevenue", rev * 0.6),
                       ("us-gaap:DepreciationDepletionAndAmortization", 20.0), ("us-gaap:InterestExpense", 10.0)):
            rows.append(fact(tag, usd, s, e, v, "10-K", y, "FY"))
        for tag, v in (("us-gaap:Assets", 2000.0 + 100 * (y - 2022)), ("us-gaap:StockholdersEquity", 1000.0),
                       ("us-gaap:AssetsCurrent", 600.0), ("us-gaap:LiabilitiesCurrent", 300.0 - 10 * (y - 2022)),
                       ("us-gaap:LongTermDebtNoncurrent", 400.0 - 20 * (y - 2022)),
                       ("us-gaap:CashAndCashEquivalentsAtCarryingValue", 100.0),
                       ("us-gaap:RetainedEarningsAccumulatedDeficit", 500.0)):
            rows.append(fact(tag, usd, None, e, v, "10-K", y, "FY"))
        rows.append(fact("us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding", "shares", s, e,
                         100.0 - (y - 2022), "10-K", y, "FY"))
        rows.append(fact("us-gaap:EarningsPerShareDiluted", "USD/shares", s, e, ni / 100.0, "10-K", y, "FY"))
    for q, (s, e) in enumerate((("2025-01-01", "2025-03-31"), ("2025-04-01", "2025-06-30"), ("2025-07-01", "2025-09-30")), 1):
        rows.append(fact("us-gaap:Revenues", usd, s, e, 250.0 + 10 * q, fp=f"Q{q}"))
    # 2026 Q1 so the trailing window moves past the fiscal year
    rows.append(fact("us-gaap:Revenues", usd, "2026-01-01", "2026-03-31", 300.0, fy=2026))
    F.store_facts(conn, CIK, rows)
    return conn


class TestFacts(unittest.TestCase):
    def test_parse_keeps_only_known_tags(self):
        payload = {"facts": {"us-gaap": {"Revenues": {"units": {"USD": [
            {"start": "2025-01-01", "end": "2025-03-31", "val": 5, "accn": "x", "fy": 2025, "fp": "Q1",
             "form": "10-Q", "filed": "2025-05-01"}]}}, "Goodwill": {"units": {"USD": [{"end": "2025-03-31", "val": 1,
                                                                                         "accn": "y"}]}}}}}
        rows = F.parse_facts(CIK, payload)
        self.assertEqual([r[1] for r in rows], ["us-gaap:Revenues"])

    def test_store_writes_only_new_rows(self):
        conn = company_db()
        n = conn.execute("SELECT COUNT(*) FROM fund_facts").fetchone()[0]
        self.assertEqual(F.store_facts(conn, CIK, [fact("us-gaap:Revenues", "USD", "2026-01-01", "2026-03-31", 300.0, fy=2026)]), 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM fund_facts").fetchone()[0], n)

    def test_the_most_recent_tag_wins(self):
        conn = company_db()
        F.store_facts(conn, CIK, [fact("us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax", "USD",
                                       "2018-01-01", "2018-12-31", 5.0, "10-K", 2018, "FY")])
        self.assertEqual(F.series(conn, CIK, "revenue")[-1][1], "2026-03-31")      # Revenues is newer


class TestTrailing(unittest.TestCase):
    def test_q4_is_the_year_minus_three_quarters(self):
        rows = F.series(company_db(), CIK, "revenue")
        q = F.quarters(rows)
        self.assertEqual(q["2025-12-31"], 1200.0 - (260.0 + 270.0 + 280.0))
        self.assertEqual(F.ttm(rows), (270.0 + 280.0 + 390.0 + 300.0, "2026-03-31"))

    def test_falls_back_to_the_fiscal_year(self):
        rows = F.series(company_db(), CIK, "net_income")
        self.assertEqual(F.ttm(rows), (120.0, "2025-12-31"))


class TestRatios(unittest.TestCase):
    def test_company(self):
        m = F.derive(company_db(price=50.0), "TST")
        mcap = 50.0 * 97.0                                   # latest diluted shares
        self.assertAlmostEqual(m["market_cap"], mcap)
        self.assertAlmostEqual(m["pe"], mcap / 120.0)
        self.assertAlmostEqual(m["ps"], mcap / 1240.0)
        self.assertAlmostEqual(m["pb"], mcap / 1000.0)
        self.assertAlmostEqual(m["fcf"], 150.0)                # fiscal-year fallbacks: 180 - 30
        self.assertAlmostEqual(m["fcf_yield"], 150.0 / mcap)
        self.assertAlmostEqual(m["current_ratio"], 600.0 / 270.0)
        self.assertAlmostEqual(m["debt_to_equity"], 340.0 / 1000.0)
        self.assertAlmostEqual(m["revenue_growth_3y"], (1200.0 / 800.0) ** (1 / 3) - 1)
        self.assertAlmostEqual(m["share_change_1y"], 97.0 / 98.0 - 1)
        self.assertEqual((m["forward_pe"], m["peg"], m["estimates"]), (F.NOT_AVAILABLE,) * 3)
        self.assertIsNotNone(m["altman_z"])

    def test_piotroski(self):
        conn = company_db()
        fa = {c: F.annual(F.series(conn, CIK, c)) for c in F.CONCEPTS}
        f, tests = F.piotroski(fa)
        self.assertEqual(len(tests), 9)
        self.assertTrue(tests["positive ROA"] and tests["less long-term leverage"] and tests["no new shares"])
        self.assertEqual(f, sum(tests.values()))

    def test_altman_not_for_financials(self):
        self.assertIsNone(F.altman_z("6021", 1, 10, 1, 1, 10, 5, 10))
        self.assertIsNotNone(F.altman_z("3571", 1, 10, 1, 1, 10, 5, 10))
        self.assertIsNone(F.altman_z("3571", 1, 10, None, 1, 10, 5, 10))

    def test_class_mismatch_leaves_price_ratios_out(self):
        conn = company_db()
        conn.execute("UPDATE fund_companies SET symbol='BRK.B'")
        conn.execute("UPDATE candles SET pair='BRK.B'")
        m = F.derive(conn, "BRK.B")
        self.assertIsNone(m["pe"])
        self.assertIn("class-A", m["note"])

    def test_non_positive_denominators(self):
        self.assertIsNone(F.div(10, -5))
        self.assertIsNone(F.div(10, 0))
        self.assertIsNone(F.div(None, 5))


class TestPercentiles(unittest.TestCase):
    def test_all_and_sector(self):
        ms = [{"symbol": s, "sector2": sec, "pe": pe} for s, sec, pe in
              (("A", "35", 10.0), ("B", "35", 20.0), ("C", "35", 30.0), ("D", "60", 5.0), ("E", "60", None))]
        F.add_percentiles(ms)
        by = {m["symbol"]: m for m in ms}
        self.assertEqual(by["C"]["pct_all"]["pe"], 100.0)
        self.assertEqual(by["D"]["pct_all"]["pe"], 0.0)
        self.assertEqual(by["B"]["pct_sector"]["pe"], 50.0)
        self.assertIsNone(by["D"]["pct_sector"]["pe"])               # sector 60 has one value: too few peers
        self.assertIsNone(by["E"]["pct_all"]["pe"])


if __name__ == "__main__":
    unittest.main()
