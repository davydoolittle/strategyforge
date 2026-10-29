"""
Tests for the TSP module: IFT rule enforcement, fill dates, look-ahead,
data parsing, report. In-memory SQLite and synthetic prices only.
Run from the repo root:   python -m unittest discover -s tests -v
"""

import copy
import json
import os
import random
import sqlite3
import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import tsp_backtest as B  # noqa: E402
import tsp_data as D  # noqa: E402
import tsp_report as REP  # noqa: E402
import tsp_rules as R  # noqa: E402
import tsp_signals as S  # noqa: E402
import tsp_strategies as ST  # noqa: E402

CFG = R.load_config()
UTC = timezone.utc


def business_days(start, n):
    out, d = [], start
    while len(out) < n:
        if R.is_business_day(d):
            out.append(d)
        d += timedelta(1)
    return out


def synthetic(n=900, seed=3, crash_at=None):
    """Random-walk funds with a crash, plus VIX and VIX3M that react to it."""
    rng = random.Random(seed)
    dates = business_days(date(2015, 1, 2), n)
    drift = {"C": 0.0004, "S": 0.0004, "I": 0.0002, "F": 0.0001, "G": 0.0001}
    vol = {"C": 0.011, "S": 0.014, "I": 0.012, "F": 0.003, "G": 0.0}
    crash_at = crash_at or n // 2
    px = {f: [100.0] for f in drift}
    vix = [15.0]
    for i in range(1, n):
        shock = rng.gauss(0, 1)
        crash = -0.03 if crash_at <= i < crash_at + 12 else 0.0
        for f in drift:
            r = drift[f] + vol[f] * (0.7 * shock + 0.3 * rng.gauss(0, 1)) + (crash if f in "CSI" else 0)
            px[f].append(px[f][-1] * (1 + r))
        vix.append(max(10.0, vix[-1] + (-0.1 * (vix[-1] - 16)) + (6 if crash else 0) + rng.gauss(0, 0.8)))
    macro = {"vix": vix, "vix3m": [v * 0.92 + 2 for v in vix]}
    return D.Dataset(dates, px, macro, "tsp")


class TestAllocation(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(R.validate_allocation({"C": 60, "G": 40, "F": 0}), {"C": 60, "G": 40})

    def test_rejects(self):
        for bad in ({"C": 60, "G": 39}, {"C": 60.5, "G": 39.5}, {"C": 110, "G": -10},
                    {"X": 100}, {}, {"C": True, "G": 99}):
            with self.assertRaises(R.RuleError, msg=bad):
                R.validate_allocation(bad, CFG["funds"])

    def test_parse(self):
        self.assertEqual(R.parse_allocation("C=60, S=20,G=20"), {"C": 60, "S": 20, "G": 20})


class TestMoveBudget(unittest.TestCase):
    def test_two_flexible_then_g_only(self):
        b = R.MoveBudget(2)
        d = date(2024, 3, 5)
        w = {"C": 100.0}
        for _ in range(2):
            self.assertEqual(b.check(d, w, {"S": 100}), "flexible")
            b.record(d, "flexible")
        with self.assertRaises(R.RuleError):
            b.check(d, w, {"S": 100})                # third any-direction move
        with self.assertRaises(R.RuleError):
            b.check(d, w, {"C": 50, "F": 50})        # into F, not G
        self.assertEqual(b.check(d, w, {"C": 50, "G": 50}), "g_only")
        b.record(d, "g_only")
        self.assertEqual(b.check(d, {"C": 50.0, "G": 50.0}, {"G": 100}), "g_only")
        self.assertEqual(b.check(date(2024, 4, 1), w, {"S": 100}), "flexible")   # new month

    def test_g_only_is_judged_on_drifted_weights(self):
        # holding C 60 / G 40 that drifted to C 58: "C 60 G 40" would ADD to C
        self.assertFalse(R.is_g_only_move({"C": 58.0, "G": 42.0}, {"C": 60, "G": 40}))
        self.assertTrue(R.is_g_only_move({"C": 61.0, "G": 39.0}, {"C": 60, "G": 40}))

    def test_verify_audit(self):
        rows = [{"fill_date": "2024-03-0%d" % i, "status": "filled", "kind": "flexible",
                 "weights_before": {"C": 100.0}, "target": {"S": 100}} for i in (4, 5, 6)]
        self.assertEqual(len(R.verify_audit(rows)), 2)          # 3rd not G-only, booked flexible
        rows[2]["target"], rows[2]["kind"] = {"G": 100}, "g_only"
        self.assertEqual(R.verify_audit(rows), [])
        rows.append(dict(rows[0], status="rejected", fill_date="2024-03-07"))
        self.assertEqual(R.verify_audit(rows), [])              # rejected requests do not count

    def test_g_only_version(self):
        held = {"C": 60, "S": 20, "I": 20}
        cur = {"C": 61.5, "S": 18.7, "I": 19.8}
        v = R.g_only_version(cur, {"C": 30, "F": 30, "G": 40}, held)
        self.assertTrue(R.is_g_only_move(cur, v))
        self.assertEqual(v, {"C": 30, "G": 70})                  # F is not G: its share goes to G
        v = R.g_only_version(cur, {"C": 30, "S": 20, "G": 50}, held)
        self.assertEqual(v, {"C": 30, "S": 17, "G": 53})         # S kept: floor(18.7 - 1 margin)
        self.assertIsNone(R.g_only_version(cur, {"C": 70, "G": 30}, {"C": 70, "G": 30}))


class TestFillDates(unittest.TestCase):
    def et(self, y, m, d, hh, mm=0):
        return R.et_to_utc(date(y, m, d), f"{hh:02d}:{mm:02d}")

    def test_cutoff(self):
        self.assertEqual(R.fill_date_for_request(self.et(2024, 3, 5, 11, 59)), date(2024, 3, 5))
        self.assertEqual(R.fill_date_for_request(self.et(2024, 3, 5, 12, 0)), date(2024, 3, 6))

    def test_weekend_and_holidays(self):
        self.assertEqual(R.fill_date_for_request(self.et(2024, 3, 9, 9)), date(2024, 3, 11))      # Sat
        self.assertEqual(R.fill_date_for_request(self.et(2024, 3, 8, 15)), date(2024, 3, 11))     # Fri pm
        self.assertEqual(R.fill_date_for_request(self.et(2024, 11, 28, 9)), date(2024, 11, 29))   # Thanksgiving
        self.assertEqual(R.fill_date_for_request(self.et(2024, 3, 28, 13)), date(2024, 4, 1))     # Good Friday
        self.assertEqual(R.fill_date_for_request(self.et(2023, 6, 16, 14)), date(2023, 6, 20))    # Juneteenth

    def test_dst(self):
        # 11:30 ET is 15:30 UTC in summer and 16:30 UTC in winter
        summer = datetime(2024, 7, 9, 15, 30, tzinfo=UTC)
        winter = datetime(2024, 1, 9, 16, 30, tzinfo=UTC)
        self.assertEqual(R.fill_date_for_request(summer), date(2024, 7, 9))
        self.assertEqual(R.fill_date_for_request(winter), date(2024, 1, 9))
        self.assertEqual(R.fill_date_for_request(winter + timedelta(hours=1)), date(2024, 1, 10))

    def test_deadline(self):
        fd, dl = R.deadline(self.et(2024, 3, 8, 13))
        self.assertEqual(fd, date(2024, 3, 11))
        self.assertEqual(R.to_et(dl), datetime(2024, 3, 11, 12, 0))

    def test_backtest_lag(self):
        self.assertEqual(R.fill_index(10, 100, 1), 11)
        self.assertIsNone(R.fill_index(99, 100, 1))
        with self.assertRaises(R.RuleError):
            R.fill_index(10, 100, 0)

    def test_lag_zero_refused_in_config(self):
        cfg = copy.deepcopy(CFG)
        cfg["rules"]["fill_lag_days"] = 0
        path = os.path.join(os.path.dirname(__file__), "_tsp_cfg_lag0.json")
        with open(path, "w") as f:
            json.dump(cfg, f)
        self.addCleanup(os.remove, path)
        with self.assertRaises(R.RuleError):
            R.load_config(path)

    def test_days_left(self):
        self.assertEqual(R.business_days_left_in_month(date(2024, 2, 26)), 3)
        cal = [date(2024, 2, 27), date(2024, 2, 29), date(2024, 3, 1)]
        self.assertEqual(R.business_days_left_in_month(date(2024, 2, 26), cal), 2)


class TestLookAhead(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = synthetic()
        cls.sig = S.compute(cls.ds, CFG)

    def test_view_refuses_future(self):
        v = self.ds.view(100)
        self.assertEqual(v.value("C"), self.ds.funds["C"][100])
        with self.assertRaises(D.LookAheadError):
            v.at("C", 101)
        with self.assertRaises(D.LookAheadError):
            v.at_date(101)
        self.assertEqual(len(v.tail("C", 50)), 50)
        self.assertEqual(v.tail("C", 50)[-1], self.ds.funds["C"][100])

    def test_signals_unchanged_by_later_data(self):
        for cut in (400, 470, 700):
            short = S.compute(self.ds.truncated(cut), CFG)
            for t in range(cut):
                a, b = short[t], self.sig[t]
                self.assertEqual((a["regime"], a["score"]), (b["regime"], b["score"]), t)
                self.assertEqual(json.dumps(a["flags"], default=str, sort_keys=True),
                                 json.dumps(b["flags"], default=str, sort_keys=True), t)

    def test_signals_unchanged_when_future_prices_change(self):
        cut = 470
        ds2 = copy.deepcopy(self.ds)
        for f in ds2.funds:
            for i in range(cut, len(ds2)):
                ds2.funds[f][i] *= 1.5 if f == "C" else 0.7
        sig2 = S.compute(ds2, CFG)
        for t in range(cut):
            self.assertEqual(json.dumps(sig2[t]["flags"], default=str, sort_keys=True),
                             json.dumps(self.sig[t]["flags"], default=str, sort_keys=True), t)

    def test_backtest_decisions_unchanged_by_later_data(self):
        cut = 650
        for name in ST.library(CFG, "proxy"):
            strat = ST.build(name, CFG)
            sig_full = self.sig if strat.uses_signals else None
            full = B.simulate(self.ds, strat, CFG, sig_full, s=300)
            short_ds = self.ds.truncated(cut)
            short = B.simulate(short_ds, strat, CFG, S.compute(short_ds, CFG) if strat.uses_signals else None, s=300)
            full_rows = [r for r in full.audit if r["fill_date"] <= self.ds.dates[cut - 1].isoformat()]
            self.assertEqual(json.dumps(short.audit, default=str), json.dumps(full_rows, default=str), name)
            self.assertEqual(short.equity, full.equity[:len(short.equity)], name)

    def test_fills_at_a_later_close(self):
        res = B.simulate(self.ds, ST.build("regime_switch", CFG), CFG, self.sig, s=300)
        self.assertTrue(res.audit)
        for r in res.audit:
            self.assertGreater(r["fill_date"], r["request_date"])
            i = self.ds.index[date.fromisoformat(r["fill_date"])]
            self.assertEqual(self.ds.index[date.fromisoformat(r["request_date"])] + 1, i)
            for f, p in r["fill_prices"].items():
                self.assertEqual(p, self.ds.funds[f][i])

    def test_lag_two_fills_two_days_later(self):
        sig = S.compute(self.ds, CFG, lag=2)
        res = B.simulate(self.ds, ST.build("regime_switch", CFG), CFG, sig, s=300, lag=2)
        for r in res.audit:
            a = self.ds.index[date.fromisoformat(r["request_date"])]
            self.assertEqual(self.ds.index[date.fromisoformat(r["fill_date"])], a + 2)
        with self.assertRaises(R.RuleError):
            B.simulate(self.ds, ST.build("buy_hold_c", CFG), CFG, None, s=300, lag=0)

    def test_hit_rates_only_use_closed_windows(self):
        H = CFG["shifts"]["eval_horizon_days"]
        lag = CFG["rules"]["fill_lag_days"]
        occurred = {}
        for s_ in self.sig:
            for fl in s_["flags"]:
                # every flag counted in n had its window close by today
                closed = [t0 for t0 in occurred.get(fl["type"], []) if t0 + lag + H <= s_["t"]]
                self.assertEqual(fl["n"], len(closed), (s_["date"], fl["type"]))
            for fl in s_["flags"]:
                occurred.setdefault(fl["type"], []).append(s_["t"])


class GreedyStrategy(ST.AllocStrategy):
    """Asks for a random allocation every day, budget or not."""
    type = "greedy"

    def __init__(self, cfg, seed=1):
        super().__init__("greedy", {}, cfg)
        self.rng = random.Random(seed)

    def decide(self, view, ctx):
        a = self.rng.randint(0, 100)
        target = R.validate_allocation({"C": a, "F": 100 - a} if self.rng.random() < 0.7
                                       else {"C": a // 2, "G": 100 - a // 2})
        return {"target": target, "reason": "greedy", "confidence": None}


class TestRuleEnforcementInBacktest(unittest.TestCase):
    def test_greedy_strategy_cannot_break_the_budget(self):
        ds = synthetic(600)
        res = B.simulate(ds, GreedyStrategy(CFG), CFG, None, s=300)
        self.assertEqual(R.verify_audit(res.audit), [])
        filled = [r for r in res.audit if r["status"] == "filled"]
        self.assertTrue(any(r["status"] == "rejected" for r in res.audit))
        per_month = {}
        for r in filled:
            if r["kind"] == "flexible":
                per_month[r["fill_date"][:7]] = per_month.get(r["fill_date"][:7], 0) + 1
        self.assertLessEqual(max(per_month.values()), 2)
        for r in filled:
            if r["kind"] == "g_only":
                self.assertTrue(R.is_g_only_move(r["weights_before"], r["target"]))

    def test_library_runs_clean(self):
        ds = synthetic(800)
        sig = S.compute(ds, CFG)
        for name in ST.library(CFG, "proxy"):
            strat = ST.build(name, CFG)
            res = B.simulate(ds, strat, CFG, sig if strat.uses_signals else None, s=300)
            self.assertEqual(R.verify_audit(res.audit), [], name)
            m = B.metrics(ds, res)
            self.assertGreater(m["final"], 0)

    def test_escape_hatch_only_for_de_risking(self):
        strat = ST.build("shift_capture", CFG)
        ctx = {"moves_left": 0, "weights": {"C": 60.0, "S": 15.0, "I": 10.0, "F": 10.0, "G": 5.0},
               "held": dict(CFG["base_allocation"])}
        self.assertIsNone(strat.feasible({"C": 60, "S": 20, "I": 20}, ctx))   # risk-on: wait
        v = strat.feasible({"G": 100}, ctx)
        self.assertEqual(v, {"G": 100})


class TestSignals(unittest.TestCase):
    def test_regime_explains_itself(self):
        ds = synthetic()
        sig = S.compute(ds, CFG)
        later = [s_ for s_ in sig if s_["regime"]]
        self.assertTrue(later)
        for s_ in later[:50]:
            self.assertEqual(s_["score"], sum(d[2] for d in s_["drivers"]))
        regimes = {s_["regime"] for s_ in later}
        self.assertIn("risk_on", regimes)
        self.assertTrue(regimes & {"risk_off", "crisis"})       # the crash is seen

    def test_crash_raises_a_risk_off_flag(self):
        ds = synthetic()
        sig = S.compute(ds, CFG)
        crash = len(ds) // 2
        types = {fl["type"] for s_ in sig[crash:crash + 25] for fl in s_["flags"]}
        self.assertTrue(types & {"fast_drawdown", "ma200_cross_down", "regime_change_down"}, types)

    def test_score_move(self):
        fl = {"mean_edge_pct": 1.0, "enough_history": True}
        opp = {"rate_per_day": 0.05, "mean_edge_pct": 0.8}
        early = S.score_move(fl, 2, 20, False, opp)
        late = S.score_move(fl, 2, 1, False, opp)
        self.assertGreater(late["score"], early["score"])          # less month left, cheaper to spend
        self.assertLess(S.score_move(fl, 1, 20, False, opp)["score"], early["score"])
        self.assertEqual(S.score_move(fl, 0, 20, True, opp)["reserve_cost"], 0.0)   # escape hatch
        self.assertEqual(S.score_move(fl, 0, 20, False, opp)["score"], float("-inf"))
        self.assertEqual(S.score_move({"mean_edge_pct": 5.0, "enough_history": False}, 2, 5, False,
                                      opp)["edge"], 0.0)


TSP_CSV = """Date,L Income,L 2050,G Fund,F Fund,C Fund,S Fund,I Fund
2024-01-03,25.1,30.2,17.9,19.0,72.1,75.0,40.1
2024-01-02,25.0,30.0,17.8,19.1,72.5,76.0,40.0
"""


class TestData(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        REP.ensure_tables(self.conn)

    def test_tsp_csv(self):
        data = D.TspCsvSource.parse(TSP_CSV)
        self.assertEqual(data["C"], [(date(2024, 1, 2), 72.5), (date(2024, 1, 3), 72.1)])
        self.assertIn("L 2050", data)
        with self.assertRaises(RuntimeError):
            D.TspCsvSource.parse("<!DOCTYPE html><html>blocked</html>")
        alt = TSP_CSV.replace("2024-01-03", "Jan 3, 2024").replace("2024-01-02", "01/02/2024")
        self.assertEqual(D.TspCsvSource.parse(alt.replace("Jan 3, 2024", '"Jan 3, 2024"'))["G"][0],
                         (date(2024, 1, 2), 17.8))

    def test_import_and_load(self):
        got = D.import_tsp_csv(self.conn, TSP_CSV)
        self.assertEqual(got["C"], 2)
        D.import_tsp_csv(self.conn, TSP_CSV)                  # idempotent
        ds = D.load_dataset(self.conn, CFG, "tsp")
        self.assertEqual(ds.dates, [date(2024, 1, 2), date(2024, 1, 3)])
        self.assertEqual(ds.funds["C"], [72.5, 72.1])

    def test_yahoo_parse(self):
        doc = {"chart": {"result": [{"meta": {"gmtoffset": -14400},
                                     "timestamp": [1704205800, 1704292200],
                                     "indicators": {"quote": [{"close": [470.0, None]}],
                                                    "adjclose": [{"adjclose": [460.0, None]}]}}]}}
        self.assertEqual(D.YahooSource.parse(doc), [(date(2024, 1, 2), 460.0)])

    def test_fred_parse(self):
        self.assertEqual(D.FredSource.parse("DATE,T10Y2Y\n2024-01-02,-0.38\n2024-01-03,.\n"),
                         [(date(2024, 1, 2), -0.38)])

    def test_macro_known_only_after_its_close(self):
        D.store_macro(self.conn, "vix", [(date(2024, 1, 2), 13.0), (date(2024, 1, 4), 14.0)])
        ts = self.conn.execute("SELECT MIN(ts) FROM context").fetchone()[0]
        self.assertEqual(R.to_et(datetime.fromtimestamp(ts, UTC)), datetime(2024, 1, 2, 16, 0))
        D.import_tsp_csv(self.conn, TSP_CSV)
        ds = D.load_dataset(self.conn, CFG, "tsp")
        self.assertEqual(ds.macro["vix"], [13.0, 13.0])       # Jan 4 is not visible on Jan 3

    def test_update_records_failures_and_continues(self):
        class Boom:
            def fetch(self, *a):
                raise RuntimeError("down")

            def fetch_all(self, *a):
                raise RuntimeError("HTTP Error 403: Forbidden")

        class Ok:
            def fetch(self, sym, start):
                return [(date(2024, 1, 2), 1.0)]

        cfg = copy.deepcopy(CFG)
        cfg["data"]["macro"] = {"vix": {"source": "fred", "symbol": "VIXCLS"}}
        done = D.update(self.conn, cfg, {"tsp": Boom(), "yahoo": Ok(), "fred": Boom()}, log=lambda *_: None)
        self.assertTrue(done["tsp.gov"].startswith("error"))
        self.assertEqual(done["SPY"], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM poll_errors").fetchone()[0], 2)


class TestReport(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        REP.ensure_tables(self.conn)
        ds = synthetic(500)
        for f, s in ds.funds.items():
            D.store_prices(self.conn, "tsp", f, list(zip(ds.dates, s)))
        D.store_macro(self.conn, "vix", list(zip(ds.dates, ds.macro["vix"])))
        self.last = ds.dates[-1]

    def now(self):
        return R.et_to_utc(R.next_business_day(self.last), "09:00")

    def test_report_and_disclaimer(self):
        rep = REP.build_report(self.conn, CFG, self.now(), mode="tsp")
        text = REP.render(rep)
        self.assertIn("past performance does not guarantee", text.lower())
        self.assertIn("Deadline: submit before", text)
        self.assertEqual(rep["fill_date"], R.next_business_day(self.last))
        self.assertEqual(rep["moves_left"], 2)

    def test_record_ift_counts_and_refuses(self):
        d = R.next_business_day(self.last)
        self.assertEqual(REP.record_ift(self.conn, CFG, {"C": 100}, d), "flexible")
        self.assertEqual(REP.record_ift(self.conn, CFG, {"S": 100}, d), "flexible")
        with self.assertRaises(R.RuleError):
            REP.record_ift(self.conn, CFG, {"C": 100}, d)
        self.assertEqual(REP.record_ift(self.conn, CFG, {"S": 50, "G": 50}, d), "g_only")
        rep = REP.build_report(self.conn, CFG, self.now(), mode="tsp")
        self.assertEqual(rep["moves_left"], 0)

    def test_notify_once_per_data_date(self):
        rep = REP.build_report(self.conn, CFG, self.now(), mode="tsp")
        with mock.patch.object(REP.health, "send_ntfy", return_value=True) as push:
            self.assertTrue(REP.notify_once(self.conn, rep, "topic", "TSP"))
            self.assertFalse(REP.notify_once(self.conn, rep, "topic", "TSP"))
        self.assertEqual(push.call_count, 1)
        self.assertIn("Analysis only", push.call_args[0][2])


if __name__ == "__main__":
    unittest.main()
