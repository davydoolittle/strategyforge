"""
TSP backtester: run the strategy library under the IFT rules.

  python tsp_backtest.py --data proxy                 # ETF proxies, 2004 -> today
  python tsp_backtest.py --data tsp                   # official share prices (after --import)
  python tsp_backtest.py --data proxy --audit shift_capture   # every simulated IFT
  python tsp_backtest.py --data proxy --out ../docs/TSP_BACKTEST.md

Simulation, per business day t (a day's close is the only price there is):
  1. holdings drift with each fund's close-to-close return;
  2. an IFT requested on day t-lag fills at t's close if the move budget of
     t's month allows it (tsp_rules.MoveBudget), else it is REJECTED and logged;
  3. after the close the strategy decides from ds.view(t) only; a request
     fills at the close of t + lag (default 1: submit next morning before noon).
The first day is funded at the strategy's initial allocation (not an IFT).
Every run's audit log is re-checked by tsp_rules.verify_audit; a violation
stops the run.

Read the results with suspicion: one sample path, 2008-09 and 2020 dominate
tactical results, and every parameter tried is counted in the deflation.
"""

import argparse
import itertools
import json
import math
import sqlite3
import sys
from datetime import date, datetime, timezone

import main
import tsp_data as D
import tsp_rules as R
import tsp_signals as S
import tsp_strategies as ST
import validation as V

WARMUP = 260            # business days of history every fund needs before day one


def ensure_tables(conn):
    D.ensure_tables(conn)
    conn.execute("""CREATE TABLE IF NOT EXISTS tsp_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_at TEXT, data_mode TEXT, strategy TEXT, params_json TEXT,
        start TEXT, end TEXT, fill_lag INTEGER, metrics_json TEXT
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS tsp_ifts (
        run_id INTEGER, seq INTEGER, request_date TEXT, fill_date TEXT, kind TEXT,
        status TEXT, weights_before_json TEXT, target_json TEXT, reason TEXT,
        confidence REAL, evidence TEXT, fill_prices_json TEXT, flexible_used_after INTEGER,
        PRIMARY KEY (run_id, seq)
    )""")
    conn.commit()


# --------------------------------------------------------------- simulation ---

def common_start(ds, cfg, start=None):
    """First day >= start on which every core fund has WARMUP days of prices."""
    need = cfg["funds"]
    first = {f: next((i for i, v in enumerate(ds.funds[f]) if v is not None), None) for f in need}
    if any(v is None for v in first.values()):
        missing = [f for f, v in first.items() if v is None]
        raise RuntimeError(f"no prices for {missing} in this dataset")
    lo = max(first.values()) + WARMUP
    if start:
        lo = max(lo, next((i for i, d in enumerate(ds.dates) if d >= start), len(ds)))
    if lo >= len(ds) - 2:
        raise RuntimeError("not enough data after the warm-up")
    return lo


def _weights(h):
    tot = sum(h.values())
    return {f: 100 * v / tot for f, v in h.items() if v > 0} if tot > 0 else {}


class Result:
    def __init__(self, name, params, s, dates):
        self.name, self.params, self.s = name, params, s
        self.dates, self.equity, self.g_weight, self.audit = dates, [], [], []
        self.regime = []

    def returns(self):
        e = self.equity
        return [b / a - 1 for a, b in zip(e, e[1:])]


def simulate(ds, strat, cfg, signals=None, s=None, lag=None, balance=None):
    lag = cfg["rules"]["fill_lag_days"] if lag is None else lag
    if lag < 1:
        raise R.RuleError("fill lag must be >= 1")
    s = common_start(ds, cfg) if s is None else s
    balance = balance or cfg["backtest"]["initial_balance"]
    n = len(ds)
    res = Result(strat.name, strat.p, s, ds.dates[s:])
    sig = (lambda t: signals[t]) if signals else (lambda t: None)
    init = R.validate_allocation(strat.initial(ds.view(s), sig(s)))
    for f in init:
        if ds.series(f)[s] is None:
            raise RuntimeError(f"{strat.name}: no {f} price on {ds.dates[s]}")
    hold = {f: balance * p / 100 for f, p in init.items()}
    held = dict(init)
    budget = R.MoveBudget(cfg["rules"]["flexible_moves_per_month"])
    pending = None
    regime_hist = [signals[x]["regime"] for x in range(max(0, s - 30), s)] if signals else []
    for t in range(s, n):
        if t > s:
            for f in hold:
                a, b = ds.series(f)[t - 1], ds.series(f)[t]
                hold[f] *= b / a
        if pending and pending["fill_t"] == t:
            d = ds.dates[t]
            w = _weights(hold)
            row = {"request_date": ds.dates[pending["t"]].isoformat(), "fill_date": d.isoformat(),
                   "weights_before": dict(w),
                   "target": pending["target"], "reason": pending["reason"],
                   "confidence": pending.get("confidence"), "evidence": pending.get("evidence"),
                   "fill_prices": {f: ds.series(f)[t] for f in set(w) | set(pending["target"])}}
            try:
                kind = budget.check(d, w, pending["target"])
                tot = sum(hold.values())
                hold = {f: tot * p / 100 for f, p in pending["target"].items()}
                budget.record(d, kind)
                held = dict(pending["target"])
                row.update(kind=kind, status="filled")
            except R.RuleError as e:
                row.update(kind=None, status="rejected", reason=f"{row['reason']} | REJECTED: {e}")
            row["flexible_used_after"] = budget.used.get(budget.key(d), 0)
            res.audit.append(row)
            pending = None
        res.equity.append(sum(hold.values()))
        res.g_weight.append(_weights(hold).get("G", 0.0))
        if signals:
            regime_hist.append(signals[t]["regime"])
            res.regime.append(signals[t]["regime"])
        fill_t = t + lag
        if pending or fill_t >= n:
            continue
        fd = ds.dates[fill_t]
        ctx = {"weights": _weights(hold), "held": held, "fill_date": fd,
               "moves_left": budget.remaining(fd),
               "days_left": R.business_days_left_in_month(fd),
               "signal": sig(t), "regime_hist": regime_hist}
        p = strat.decide(ds.view(t), ctx)
        if p:
            pending = dict(p, target=R.validate_allocation(p["target"]), t=t, fill_t=fill_t)
    bad = R.verify_audit(res.audit, cfg["rules"]["flexible_moves_per_month"])
    if bad:
        raise AssertionError(f"{strat.name}: IFT rule violated: {bad[:3]}")
    return res


# ------------------------------------------------------------------ metrics ---

def _excess(ds, res):
    g = ds.series("G")[res.s:]
    rg = [b / a - 1 for a, b in zip(g, g[1:])]
    return [r - x for r, x in zip(res.returns(), rg)]


def _sr(rets):
    sd = V._std(rets)
    return sum(rets) / len(rets) / sd * math.sqrt(252) if len(rets) > 2 and sd > 0 else 0.0


def slice_metrics(dates, equity, ex=None):
    rets = [b / a - 1 for a, b in zip(equity, equity[1:])]
    days = (dates[-1] - dates[0]).days or 1
    cagr = (equity[-1] / equity[0]) ** (365.25 / days) - 1
    peak, mdd = equity[0], 0.0
    for e in equity:
        peak = max(peak, e)
        mdd = max(mdd, 1 - e / peak)
    ex = ex if ex is not None else rets
    return {"cagr": 100 * cagr, "max_dd": 100 * mdd,
            "vol": 100 * V._std(rets) * math.sqrt(252) if len(rets) > 2 else 0.0,
            "sharpe": _sr(ex), "sortino": V.sortino(ex) * math.sqrt(252) if len(ex) > 2 else 0.0}


def metrics(ds, res, bench=None):
    ex = _excess(ds, res)
    m = slice_metrics(res.dates, res.equity, ex)
    month_end = {}
    for d, e in zip(res.dates, res.equity):
        month_end[(d.year, d.month)] = e
    me = [res.equity[0]] + list(month_end.values())
    mrets = [b / a - 1 for a, b in zip(me, me[1:])]
    worst_i = min(range(len(mrets)), key=lambda i: mrets[i])
    months = list(month_end)
    filled = [r for r in res.audit if r["status"] == "filled"]
    per_month = {}
    for r in filled:
        k = r["fill_date"][:7]
        per_month[k] = per_month.get(k, 0) + 1
    m.update({
        "start": res.dates[0].isoformat(), "end": res.dates[-1].isoformat(),
        "worst_month": 100 * mrets[worst_i],
        "worst_month_at": "%d-%02d" % months[worst_i],
        "avg_g_pct": sum(res.g_weight) / len(res.g_weight),
        "days_all_g_pct": 100 * sum(1 for g in res.g_weight if g >= 99) / len(res.g_weight),
        "ifts": len(filled), "g_only_ifts": sum(1 for r in filled if r["kind"] == "g_only"),
        "rejected": sum(1 for r in res.audit if r["status"] == "rejected"),
        "moves_per_month": len(filled) / len(months),
        "max_moves_in_month": max(per_month.values()) if per_month else 0,
        "months_with_2plus": sum(1 for v in per_month.values() if v >= 2),
        "final": res.equity[-1],
    })
    if bench is not None:
        act = [a - b for a, b in zip(res.returns(), bench.returns())]
        sk, ku = V.moments(act)
        sd = V._std(act)
        sr_d = sum(act) / len(act) / sd if sd > 0 else 0.0
        m["info_ratio_vs_c"] = sr_d * math.sqrt(252)
        m["psr_beats_c"] = V.probabilistic_sharpe(sr_d, len(act), sk, ku)
    return m


def period_table(ds, results, periods):
    out = {}
    for lo, hi in periods:
        lo_d, hi_d = date.fromisoformat(lo), date.fromisoformat(hi)
        row = {}
        for name, res in results.items():
            idx = [i for i, d in enumerate(res.dates) if lo_d <= d <= hi_d]
            if len(idx) > 20:
                row[name] = slice_metrics([res.dates[i] for i in idx], [res.equity[i] for i in idx])
        if row:
            out[f"{max(lo_d, results[next(iter(results))].dates[0])}..{min(hi_d, ds.dates[-1])}"] = row
    return out


# ---------------------------------------------- grid, walk-forward, deflation ---

def grid(cfg, name):
    g = cfg.get("sensitivity", {}).get(name) or {}
    keys = sorted(g)
    combos = [dict(zip(keys, vals)) for vals in itertools.product(*(g[k] for k in keys))]
    default = {k: cfg["strategies"][name][k] for k in keys}
    if default not in combos:
        combos.insert(0, default)
    return combos or [{}]


def walk_forward(res_by_params, train_years, test_years):
    """Pick, on each train window, the params with the best Sharpe (over G);
    record what they did on the next test window. Results all share dates."""
    any_res = next(iter(res_by_params.values()))[0]
    dates = any_res.dates
    folds, oos = [], []
    y0 = dates[0].year + (1 if dates[0].month > 1 else 0)
    y = y0
    while True:
        tr = [i for i, d in enumerate(dates) if y <= d.year < y + train_years]
        te = [i for i, d in enumerate(dates) if y + train_years <= d.year < y + train_years + test_years]
        if len(tr) < 250 or len(te) < 20:
            break

        def sr(res, idx):
            e = [res.equity[i] for i in idx]
            return _sr([b / a - 1 for a, b in zip(e, e[1:])])

        best_k = max(res_by_params, key=lambda k: sr(res_by_params[k][0], tr))
        best = res_by_params[best_k][0]
        e = [best.equity[i] for i in te]
        oos.extend(b / a - 1 for a, b in zip(e, e[1:]))
        folds.append({"train": f"{y}-{y + train_years - 1}",
                      "test": f"{y + train_years}-{dates[te[-1]].year}",
                      "chosen": best_k, "train_sharpe": sr(best, tr), "test_sharpe": sr(best, te),
                      "test_cagr": 100 * ((e[-1] / e[0]) ** (365.25 / max(1, (dates[te[-1]] - dates[te[0]]).days)) - 1)})
        y += test_years
    oos_eq = [1.0]
    for r in oos:
        oos_eq.append(oos_eq[-1] * (1 + r))
    chosen = [f["chosen"] for f in folds]
    return {"folds": folds, "oos_sharpe": _sr(oos),
            "oos_growth": oos_eq[-1], "param_changes": sum(1 for a, b in zip(chosen, chosen[1:]) if a != b)}


def deflation(all_results, ds, defaults):
    """Deflated Sharpe of each default config against every config run."""
    srs, per = [], {}
    for key, (res, _) in all_results.items():
        ex = _excess(ds, res)
        sd = V._std(ex)
        sr_d = sum(ex) / len(ex) / sd if sd > 0 else 0.0
        srs.append(sr_d)
        per[key] = (sr_d, len(ex), *V.moments(ex))
    var = V._std(srs) ** 2 if len(srs) > 1 else 0.0
    out = {}
    for name, key in defaults.items():
        sr_d, n, sk, ku = per[key]
        dsr, bench = V.deflated_sharpe(sr_d, n, sk, ku, len(srs), var)
        out[name] = {"dsr": dsr, "e_max_sr_ann": bench * math.sqrt(252), "n_trials": len(srs)}
    return out


def run_all(ds, cfg, names=None, lag=None, start=None, log=print):
    lag = cfg["rules"]["fill_lag_days"] if lag is None else lag
    s = common_start(ds, cfg, start)
    log(f"computing signals over {len(ds)} days (fill lag {lag}) ...")
    signals = S.compute(ds, cfg, lag)
    names = names or ST.library(cfg, ds.mode)
    all_results, defaults, sens = {}, {}, {}
    for name in names:
        rows = []
        for combo in grid(cfg, name):
            key = f"{name} {json.dumps(combo, sort_keys=True)}" if combo else name
            strat = ST.build(name, cfg, combo)
            try:
                res = simulate(ds, strat, cfg, signals if strat.uses_signals else None, s, lag)
            except RuntimeError as e:
                log(f"  {name}: skipped ({e})")
                break
            all_results[key] = (res, combo)
            rows.append((combo, res))
            if combo == {k: cfg["strategies"][name][k] for k in combo}:
                defaults[name] = key
        if rows:
            sens[name] = rows
    bench = all_results[defaults["buy_hold_c"]][0] if "buy_hold_c" in defaults else None
    report = {"data_mode": ds.mode, "fill_lag": lag, "start": ds.dates[s].isoformat(),
              "end": ds.dates[-1].isoformat(), "notes": ds.notes, "strategies": {},
              "sensitivity": {}, "walk_forward": {}}
    for name, key in defaults.items():
        report["strategies"][name] = metrics(ds, all_results[key][0], bench)
    for name, rows in sens.items():
        if len(rows) > 1:
            report["sensitivity"][name] = [
                {"params": c, "sharpe": _sr(_excess(ds, r)),
                 "cagr": slice_metrics(r.dates, r.equity)["cagr"],
                 "max_dd": slice_metrics(r.dates, r.equity)["max_dd"],
                 "ifts": sum(1 for a in r.audit if a["status"] == "filled")} for c, r in rows]
            report["walk_forward"][name] = walk_forward(
                {json.dumps(c, sort_keys=True): (r, c) for c, r in rows},
                cfg["backtest"]["walk_forward_train_years"], cfg["backtest"]["walk_forward_test_years"])
    if bench:
        report["walk_forward_bench"] = walk_forward(
            {"-": (bench, {})}, cfg["backtest"]["walk_forward_train_years"],
            cfg["backtest"]["walk_forward_test_years"])
    report["deflation"] = deflation(all_results, ds, defaults)
    report["periods"] = period_table(ds, {n: all_results[k][0] for n, k in defaults.items()},
                                     cfg["backtest"]["periods"])
    results = {n: all_results[k][0] for n, k in defaults.items()}
    return report, results, signals


# ------------------------------------------------------------------- output ---

DISCLAIMER = ("Hypothetical, backtested results on one historical path. Past performance does not "
              "guarantee future results. Analysis only: the module never places an IFT.")


def render(report, lag2=None):
    L = []
    w = L.append
    w(f"# TSP backtest: {report['data_mode']} data, {report['start']} to {report['end']}\n")
    w(f"Fill lag {report['fill_lag']} business day(s): a signal on day T's close fills at the close "
      f"{report['fill_lag']} business day(s) later. Max 2 flexible IFTs per month, further moves into G only.\n")
    for n in report["notes"]:
        w(f"- {n}")
    w("\n## Full period\n")
    w("| strategy | CAGR % | max DD % | vol % | Sharpe | Sortino | worst month % | avg G % | days 100% G | IFTs | moves/month | max in a month | IR vs C | P(beats C) | DSR |")
    w("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for name, m in report["strategies"].items():
        d = report["deflation"].get(name, {})
        w(f"| {name} | {m['cagr']:.2f} | {m['max_dd']:.1f} | {m['vol']:.1f} | {m['sharpe']:.2f} | "
          f"{m['sortino']:.2f} | {m['worst_month']:.1f} ({m['worst_month_at']}) | {m['avg_g_pct']:.0f} | "
          f"{m['days_all_g_pct']:.0f}% | {m['ifts']} ({m['g_only_ifts']} G-only) | {m['moves_per_month']:.2f} | "
          f"{m['max_moves_in_month']} | {m.get('info_ratio_vs_c', 0):+.2f} | {m.get('psr_beats_c', 0):.2f} | "
          f"{d.get('dsr', 0):.2f} |")
    any_d = next(iter(report["deflation"].values()), None)
    if any_d:
        w(f"\nSharpe is over the G Fund (its daily return is the risk-free rate). DSR deflates each "
          f"strategy's Sharpe by all {any_d['n_trials']} configurations run here (noise ceiling "
          f"{any_d['e_max_sr_ann']:.2f} annualised); >= 0.95 would be evidence of skill, not luck. "
          "P(beats C) is the probabilistic Sharpe of the daily return over 100% C being > 0.")
    w("\n## Sub-periods (CAGR % / max DD %)\n")
    names = list(report["strategies"])
    w("| period | " + " | ".join(names) + " |")
    w("|---|" + "---|" * len(names))
    for p, row in report["periods"].items():
        w(f"| {p} | " + " | ".join(f"{row[n]['cagr']:.1f} / {row[n]['max_dd']:.0f}" if n in row else "-"
                                   for n in names) + " |")
    w("\n## Walk-forward (params chosen on the train window by Sharpe, scored on the next test window)\n")
    wb = report.get("walk_forward_bench")
    for name, wf in report["walk_forward"].items():
        w(f"**{name}**: out-of-sample Sharpe {wf['oos_sharpe']:.2f}, growth x{wf['oos_growth']:.2f}"
          + (f" (100% C over the same windows: Sharpe {wb['oos_sharpe']:.2f}, x{wb['oos_growth']:.2f})" if wb else "")
          + f"; chosen params changed {wf['param_changes']} time(s).\n")
        w("| train | test | chosen | train Sharpe | test Sharpe | test CAGR % |")
        w("|---|---|---|---|---|---|")
        for f in wf["folds"]:
            w(f"| {f['train']} | {f['test']} | `{f['chosen']}` | {f['train_sharpe']:.2f} | "
              f"{f['test_sharpe']:.2f} | {f['test_cagr']:.1f} |")
        w("")
    w("## Parameter sensitivity (full period)\n")
    for name, rows in report["sensitivity"].items():
        sh = sorted(r["sharpe"] for r in rows)
        w(f"**{name}**: Sharpe {sh[0]:.2f} .. {sh[-1]:.2f}, median {sh[len(sh) // 2]:.2f} across {len(rows)} settings.\n")
        w("| params | Sharpe | CAGR % | max DD % | IFTs |")
        w("|---|---|---|---|---|")
        for r in rows:
            w(f"| `{json.dumps(r['params'], sort_keys=True)}` | {r['sharpe']:.2f} | {r['cagr']:.2f} | "
              f"{r['max_dd']:.1f} | {r['ifts']} |")
        w("")
    if lag2:
        w(f"## Fill-lag robustness (lag {lag2['fill_lag']})\n")
        w("| strategy | CAGR % | max DD % | Sharpe |")
        w("|---|---|---|---|")
        for name, m in lag2["strategies"].items():
            w(f"| {name} | {m['cagr']:.2f} | {m['max_dd']:.1f} | {m['sharpe']:.2f} |")
        w("")
    w(f"_{DISCLAIMER}_")
    return "\n".join(L)


def render_audit(res, limit=None):
    L = [f"IFT audit log: {res.name} {json.dumps(res.params, sort_keys=True, default=str)}",
         "request    fill       kind      status    flex-used  from -> to  | reason"]
    rows = res.audit if limit is None else res.audit[-limit:]
    for r in rows:
        L.append(f"{r['request_date']} {r['fill_date']} {str(r['kind']):9s} {r['status']:9s} "
                 f"{r['flexible_used_after']}/2  {R.fmt_alloc({k: round(v) for k, v in r['weights_before'].items()})}"
                 f" -> {R.fmt_alloc(r['target'])}  | {r['reason']}"
                 + (f" [{r['evidence']}]" if r.get("evidence") else ""))
    bad = R.verify_audit(res.audit)
    L.append(f"rule check: {'no violations' if not bad else bad}; {len(res.audit)} requests, "
             f"{sum(1 for r in res.audit if r['status'] == 'rejected')} rejected")
    return "\n".join(L)


def store(conn, report, results, cfg):
    ensure_tables(conn)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    for name, res in results.items():
        m = dict(report["strategies"][name])
        m["dsr"] = report["deflation"].get(name, {}).get("dsr")
        if name in report["walk_forward"]:
            wf = report["walk_forward"][name]
            m["wf_oos_sharpe"], m["wf_oos_growth"] = wf["oos_sharpe"], wf["oos_growth"]
        cur = conn.execute(
            "INSERT INTO tsp_runs (run_at, data_mode, strategy, params_json, start, end, fill_lag,"
            " metrics_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (now, report["data_mode"], name, json.dumps(res.params, default=str), report["start"],
             report["end"], report["fill_lag"], json.dumps(m)))
        rid = cur.lastrowid
        conn.executemany(
            "INSERT INTO tsp_ifts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(rid, i, r["request_date"], r["fill_date"], r["kind"], r["status"],
              json.dumps(r["weights_before"]), json.dumps(r["target"]), r["reason"],
              r.get("confidence"), r.get("evidence"), json.dumps(r["fill_prices"]),
              r["flexible_used_after"]) for i, r in enumerate(res.audit)])
        conn.commit()       # one strategy per transaction


def main_cli(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--data", choices=["tsp", "proxy"], default="tsp")
    ap.add_argument("--start", help="first trading day (default config backtest.start)")
    ap.add_argument("--strategies", help="comma list (default: the whole library)")
    ap.add_argument("--lag", type=int, help="fill lag in business days (default config)")
    ap.add_argument("--audit", metavar="STRATEGY", help="print that strategy's full IFT log")
    ap.add_argument("--out", help="also write the report as markdown here")
    ap.add_argument("--no-store", action="store_true", help="do not write tsp_runs / tsp_ifts")
    ap.add_argument("--config")
    args = ap.parse_args(argv)
    cfg = R.load_config(args.config)
    conn = sqlite3.connect(main.DB_PATH, timeout=main.DB_TIMEOUT_S)
    ds = D.load_dataset(conn, cfg, args.data)
    start = date.fromisoformat(args.start or cfg["backtest"]["start"])
    names = args.strategies.split(",") if args.strategies else None
    report, results, _ = run_all(ds, cfg, names, args.lag, start)
    lag = report["fill_lag"]
    rep2, _, _ = run_all(ds, cfg, names, lag + 1, start, log=lambda *_: None)
    text = render(report, rep2)
    print(text)
    if args.audit:
        print("\n" + render_audit(results[args.audit]))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print(f"\nwrote {args.out}")
    if not args.no_store:
        store(conn, report, results, cfg)
        print(f"stored {len(results)} runs in tsp_runs / tsp_ifts")
    best = max(report["strategies"], key=lambda n: report["strategies"][n]["sharpe"])
    c = report["strategies"].get("buy_hold_c")
    print(f"\nRead: {best} has the best full-period Sharpe over G here"
          + (f"; 100% C made {c['cagr']:.1f}%/yr with a {c['max_dd']:.0f}% drawdown" if c else "")
          + ". Before trusting any tactical line, compare its sub-period rows (most tactical edge "
            "comes from 2008-09 and 2020) and its walk-forward line with C's, and check DSR: below "
            "0.95 the result is not distinguishable from the best of the configurations tried.")
    return 0


if __name__ == "__main__":
    sys.exit(main_cli())
