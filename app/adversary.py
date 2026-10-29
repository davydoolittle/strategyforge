"""
StrategyForge adversary — the second agent whose only job is to disprove
the optimizer's winners.

It never asks "is this good?"  It asks "how would this be wrong?" and runs
the test for each answer.  A candidate survives only if NONE of the attacks
lands.  Every attack, its threshold, the raw numbers and the verdict are
written to adversarial_reports so the decision is auditable later.

Attacks (each returns pass/fail + evidence):
  A1 sample_size      OOS trade count below a statistical floor
  A2 deflated_sharpe  SR is below what the best of N noise trials would show.
                      N = trials of the candidate's own strategy family on its
                      pair/tf (P5.2): each family is its own search. Also
                      reports DSR at N_eff, the effective number of
                      independent trials from neff.py — information only
  A3 walk_forward     fewer than K of N embargoed folds are profitable
  A4 plateau          +/-10% and +/-25% parameter nudges collapse the OOS result
                      (spike, not plateau): < 70% of neighbours profitable, or
                      the WORST neighbour's OOS PF < 1.0
  A5 cost_stress      edge disappears at 2x realistic costs
  A6 lookahead        result IMPROVES when fills are delayed -> leakage
  A7 transfer         no other pair reaches PF >= 1.1 on >= 15 OOS trades. Only
                      pairs with a verified tick size (main.py TICK_SIZE) count,
                      and each is run at its own tick, not the tuned pair's
  A8 drawdown         Monte-Carlo 95th-pct drawdown exceeds the risk budget
  A9 regime           all profit comes from one market regime: the full-window
                      trades OUTSIDE the most profitable regime net <= 0.
                      Regime = daily trend_up / trend_down / chop at signal
                      time (features.RegimeTagger)

Verdicts
  REJECTED    one or more HARD attacks landed (A1 A2 A6)
  FRAGILE     no hard failure but >= 2 soft failures (A3 A4 A5 A7 A8 A9)
  SURVIVED    no hard failure and <= 1 soft failure
'SURVIVED' is a licence to paper-trade. It is not a licence to trade.

Usage
  python adversary.py --candidate 1033
  python adversary.py --top 10 --tf 1h            # attack the 10 best OOS survivors
  python adversary.py --candidate 1033 --json     # machine-readable
"""

import argparse
import json
import os
import sqlite3
import time

import engine
import features
import main as collector
import lineage
import neff as NE
import strategies
import validation as V
import windows as W

DB_PATH = os.environ.get("FORGE_DB", "/data/forge.db")

# --- thresholds. Deliberately conservative; loosen only with a written reason.
MIN_OOS_TRADES = 30          # A1  below this, PF is a coin-flip estimate
MIN_FULL_TRADES = 50         # A1  full-window floor for DSR to mean anything
MIN_DSR = 0.95               # A2  standard bar in BLdP
MIN_WF_CONSISTENCY = 0.60    # A3  >= 3 of 5 folds profitable
MIN_PLATEAU = 0.70           # A4  >= 70% of neighbours profitable
PLATEAU_STEPS = (0.10, 0.25) # A4  nudge sizes; 2026-10-26: +/-10% alone passed 100% of
                             #     20 reports (FINDINGS F1), so it discriminated nothing
MIN_WORST_NEIGHBOUR_PF = 1.0 # A4  ...and no single neighbour may lose money
MIN_PF_AT_2X_COST = 1.10     # A5
MIN_TRANSFER_PF = 1.10       # A7  2026-10-26: was net > 0 on >= 5 trades, which
MIN_TRANSFER_TRADES = 15     #     a coin flip clears; raised with A4 (FINDINGS F4)
MAX_DD_P95_PCT = 25.0        # A8  of initial capital
WF_FOLDS = 5
WF_EMBARGO_BARS = 24

HARD = {"A1", "A2", "A6"}


def ensure_tables(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS adversarial_reports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_at TEXT, candidate_id INTEGER, pair TEXT, tf TEXT,
        verdict TEXT, hard_fails INTEGER, soft_fails INTEGER,
        report_json TEXT
    )""")
    conn.commit()


def attack(conn, cid, verbose=True):
    row = conn.execute(
        "SELECT pair, tf, params_json, oos_trades, oos_pf, oos_net, windows_backfilled"
        " FROM candidates WHERE id=?", (cid,)).fetchone()
    if not row:
        raise SystemExit(f"candidate {cid} not found")
    pair, tf, pj, oos_trades, oos_pf, oos_net, backfilled = row
    cfg = json.loads(pj)
    tf_s = W.tf_seconds(tf)
    # pinned at optimizer time (P2.1): candles collected since then are not
    # silently folded into "OOS"
    is_start, split, end = W.for_candidate(conn, cid)
    end_ts = W._ts(end)
    # ...nor are older candles from a backward backfill: every backtest below
    # (all attacks copy cfg) starts its indicator warmup where the optimizer's did
    start_ts = W.history_start(is_start, tf_s)
    cfg = W.pin_cfg(cfg, is_start, tf_s)
    family = strategies.family_of(cfg)
    cap = cfg["initial_capital"]
    t0 = time.time()

    full = engine.backtest(conn, pair, cfg, is_start, end, tf_s)
    oos = engine.backtest(conn, pair, cfg, split, end, tf_s)
    rets = V.trade_returns(full["trades"], cap)
    sr = V.sharpe(rets)
    skew, kurt = V.moments(rets)
    sr_var, n_sampled, n_trials, n_sr_missing = V.sharpe_variance_from_candidates(
        conn, pair, tf, family)
    # every trial of every family: information only, the verdict uses n_trials
    n_trials_all = conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]

    attacks = {}

    # A1 sample size
    attacks["A1"] = {
        "name": "sample_size",
        "oos_trades": oos["n_trades"], "full_trades": full["n_trades"],
        "min_oos": MIN_OOS_TRADES, "min_full": MIN_FULL_TRADES,
        "passed": oos["n_trades"] >= MIN_OOS_TRADES and full["n_trades"] >= MIN_FULL_TRADES,
    }

    # A2 deflated sharpe (against this family's pair/tf trial count AND the global one)
    dsr_local, bench_local = V.deflated_sharpe(sr, len(rets), skew, kurt, n_trials, sr_var)
    dsr_global, bench_global = V.deflated_sharpe(sr, len(rets), skew, kurt, n_trials_all, sr_var)
    # INFORMATION ONLY: the same DSR with N replaced by the effective number of
    # independent trials (neff.py). The verdict below still uses N.
    ne = NE.lookup(conn, pair, tf, family=family)
    dsr_neff = bench_neff = None
    if ne:
        dsr_neff, bench_neff = V.deflated_sharpe(sr, len(rets), skew, kurt, ne["n_eff"], sr_var)
    breakeven = V.breakeven_trials(sr, len(rets), skew, kurt, sr_var, MIN_DSR)
    attacks["A2"] = {
        "name": "deflated_sharpe",
        "family": family,
        "sharpe_per_trade": sr, "skew": skew, "kurtosis": kurt, "n_obs": len(rets),
        "sr_variance_across_trials": sr_var, "sr_var_sampled_from": n_sampled,
        "sr_missing": n_sr_missing,
        "sr_degenerate_excluded": V.degenerate_trials(conn, pair, tf, family),
        "n_trials_pair_tf": n_trials, "expected_max_sr_pair_tf": bench_local, "dsr_pair_tf": dsr_local,
        "n_trials_all": n_trials_all, "expected_max_sr_all": bench_global, "dsr_all": dsr_global,
        "n_eff_pair_tf": ne["n_eff"] if ne else None,
        "n_eff_computed_on": ne["n_series"] if ne else None,
        "n_eff_computed_at": ne["computed_at"] if ne else None,
        "expected_max_sr_neff": bench_neff, "dsr_neff": dsr_neff,
        "would_pass_with_neff": None if dsr_neff is None else dsr_neff >= MIN_DSR,
        "breakeven_trials": breakeven,
        "min_dsr": MIN_DSR,
        "passed": dsr_local >= MIN_DSR,
    }

    # A3 walk-forward
    wf = V.walk_forward(conn, pair, cfg, tf_s, WF_FOLDS, embargo_bars=WF_EMBARGO_BARS,
                        end_ts=end_ts, start_ts=start_ts)
    attacks["A3"] = {
        "name": "walk_forward",
        "folds": [{k: f[k] for k in ("fold", "n_trades", "net", "pf")} for f in wf["folds"]],
        "folds_profitable": wf["folds_profitable"], "n_folds": wf["n_folds"],
        "consistency": wf["consistency"], "min_consistency": MIN_WF_CONSISTENCY,
        "embargo_bars": WF_EMBARGO_BARS,
        "passed": wf["consistency"] >= MIN_WF_CONSISTENCY,
    }

    # A4 plateau
    sens = V.param_sensitivity(conn, pair, cfg, tf_s, split, end, pct=PLATEAU_STEPS)
    worst = sorted(sens["neighbours"], key=lambda r: r["pf"])[:3]
    attacks["A4"] = {
        "name": "plateau",
        "plateau_score": sens["plateau_score"], "min_plateau": MIN_PLATEAU,
        "steps": list(PLATEAU_STEPS),
        "n_neighbours": len(sens["neighbours"]),
        "worst_pf": sens["worst_pf"], "min_worst_pf": MIN_WORST_NEIGHBOUR_PF,
        "worst_neighbours": worst,
        "passed": (sens["plateau_score"] >= MIN_PLATEAU and sens["worst_pf"] is not None
                   and sens["worst_pf"] >= MIN_WORST_NEIGHBOUR_PF),
    }

    # A5 cost stress on OOS
    cs = V.cost_stress(conn, pair, cfg, tf_s, split, end)
    pf2 = next((r["pf"] for r in cs if r["cost_x"] == 2.0), None)
    attacks["A5"] = {
        "name": "cost_stress", "levels": cs, "pf_at_2x": pf2,
        "min_pf_at_2x": MIN_PF_AT_2X_COST,
        "passed": pf2 is not None and pf2 >= MIN_PF_AT_2X_COST,
    }

    # A6 lookahead
    la = V.lookahead_shift(conn, pair, cfg, tf_s, is_start, end)
    attacks["A6"] = {"name": "lookahead", **la, "passed": not la["improves_when_delayed"]}

    # A7 cross-pair transfer on the other pairs' OOS windows, cut at the same
    # moment this candidate's windows were pinned
    others = [r[0] for r in conn.execute("SELECT DISTINCT pair FROM candles") if r[0] != pair]
    tr, excluded = transfer(conn, cfg, others, tf_s, end_ts, start_ts)
    n_hold = sum(1 for x in tr if x["holds"])
    attacks["A7"] = {
        "name": "transfer", "results": tr, "excluded_unverified_tick": excluded,
        "pairs_holding": n_hold, "pairs_tested": len(tr),
        "min_pf": MIN_TRANSFER_PF, "min_trades": MIN_TRANSFER_TRADES,
        "passed": len(tr) == 0 or n_hold >= 1,
    }

    # A8 Monte Carlo drawdown on full-window trades
    mc = V.monte_carlo(full["trades"], cap)
    dd95_pct = (mc.get("dd_p95", 0.0) / cap * 100.0) if mc.get("n_sims") else 0.0
    attacks["A8"] = {
        "name": "drawdown", **mc, "dd_p95_pct_of_capital": dd95_pct,
        "max_dd_p95_pct": MAX_DD_P95_PCT,
        "passed": dd95_pct <= MAX_DD_P95_PCT,
    }

    # A9 regime concentration on full-window trades
    rt = features.RegimeTagger(conn, pair, start_ts, end_ts)
    attacks["A9"] = {"name": "regime",
                     **regime_split(full["trades"], lambda t: rt.at(t["signal_ts"]))}

    hard_fails = [k for k in HARD if not attacks[k]["passed"]]
    soft_fails = [k for k in attacks if k not in HARD and not attacks[k]["passed"]]
    if hard_fails:
        verdict = "REJECTED"
    elif len(soft_fails) >= 2:
        verdict = "FRAGILE"
    else:
        verdict = "SURVIVED"

    report = {
        "candidate_id": cid, "pair": pair, "tf": tf, "family": family, "params": cfg,
        "windows": {"is_start": is_start, "split": split, "end": end,
                    "backfilled": bool(backfilled)},
        "full": {"n_trades": full["n_trades"], "net": full["net"],
                 "pf": None if full["pf"] == float("inf") else full["pf"], "max_dd": full["max_dd"]},
        "oos": {"n_trades": oos["n_trades"], "net": oos["net"],
                "pf": None if oos["pf"] == float("inf") else oos["pf"], "max_dd": oos["max_dd"]},
        "attacks": attacks,
        "hard_fails": hard_fails, "soft_fails": soft_fails,
        "verdict": verdict,
        "elapsed_s": round(time.time() - t0, 1),
    }
    ensure_tables(conn)
    conn.execute(
        "INSERT INTO adversarial_reports (run_at, candidate_id, pair, tf, verdict, hard_fails,"
        " soft_fails, report_json) VALUES (datetime('now'), ?, ?, ?, ?, ?, ?, ?)",
        (cid, pair, tf, verdict, len(hard_fails), len(soft_fails), json.dumps(report)))
    conn.commit()
    if verbose:
        print_report(report)
    return report


def regime_split(trades, tag):
    """Net / PF per regime and the A9 verdict. Trades tag() returns None for
    (daily warmup) are reported as 'n/a' and left out of the verdict."""
    by = {}
    for t in trades:
        r = by.setdefault(tag(t) or "n/a", {"n_trades": 0, "net": 0.0, "gross_win": 0.0,
                                           "gross_loss": 0.0})
        r["n_trades"] += 1
        r["net"] += t["pnl"]
        r["gross_win" if t["pnl"] > 0 else "gross_loss"] += abs(t["pnl"])
    for r in by.values():
        r["pf"] = r["gross_win"] / r["gross_loss"] if r["gross_loss"] else None
        del r["gross_win"], r["gross_loss"]
    known = {k: v for k, v in by.items() if k != "n/a"}
    best = max(known, key=lambda k: known[k]["net"]) if known else None
    rest = sum(v["net"] for k, v in known.items() if k != best)
    return {"regimes": by, "best_regime": best, "net_outside_best": rest,
            "passed": best is not None and rest > 0}


def transfer(conn, cfg, others, tf_s, end_ts, start_ts):
    """Run cfg on each other pair's OOS window at THAT pair's verified tick
    size. Pairs without one are returned in `excluded`, not tested: a cost model
    we have not checked cannot count as evidence either way."""
    tr, excluded = [], []
    for other in others:
        tick = collector.TICK_SIZE.get(other)
        if tick is None:
            excluded.append(other)
            continue
        ow = W.compute(conn, other, tf_s, end_ts, start_ts)
        if ow is None:
            continue
        _, osplit, oend = ow
        r = engine.backtest(conn, other, {**cfg, "tick_size": tick}, osplit, oend, tf_s)
        pf = None if r["pf"] == float("inf") else r["pf"]
        tr.append({"pair": other, "tick_size": tick, "n_trades": r["n_trades"], "net": r["net"],
                   "pf": pf,
                   "holds": r["n_trades"] >= MIN_TRANSFER_TRADES
                            and (pf is None or pf >= MIN_TRANSFER_PF) and r["net"] > 0})
    return tr, excluded


def _f(x, nd=2):
    return "n/a" if x is None else f"{x:.{nd}f}"


def print_report(r):
    a = r["attacks"]
    print("\n" + "=" * 74)
    print(f"ADVERSARIAL REPORT — candidate #{r['candidate_id']} {r.get('family', strategies.DEFAULT_FAMILY)}"
          f" {r['pair']} {r['tf']}")
    print("=" * 74)
    w = r["windows"]
    print(f"windows     : IS {w['is_start']} -> {w['split']} | OOS -> {w['end']}"
          f"{'  (backfilled from run_at)' if w.get('backfilled') else '  (pinned at insert)'}")
    print(f"full window : {r['full']['n_trades']} trades  net {r['full']['net']:+.0f}  "
          f"pf {_f(r['full']['pf'])}  maxDD {r['full']['max_dd']:.0f}")
    print(f"OOS window  : {r['oos']['n_trades']} trades  net {r['oos']['net']:+.0f}  "
          f"pf {_f(r['oos']['pf'])}")
    print()
    rows = [
        ("A1", "sample size", f"OOS {a['A1']['oos_trades']} / full {a['A1']['full_trades']} trades",
         f"need {a['A1']['min_oos']} / {a['A1']['min_full']}"),
        ("A2", "deflated Sharpe", f"SR {a['A2']['sharpe_per_trade']:.3f}  DSR {a['A2']['dsr_pair_tf']:.3f}",
         f"E[max SR | {a['A2']['n_trials_pair_tf']} {a['A2'].get('family', '')} trials] = "
         f"{a['A2']['expected_max_sr_pair_tf']:.2f}; need DSR >= {a['A2']['min_dsr']}"),
        ("A3", "walk-forward", f"{a['A3']['folds_profitable']}/{a['A3']['n_folds']} folds profitable",
         f"need >= {a['A3']['min_consistency']:.0%}  (embargo {a['A3']['embargo_bars']} bars)"),
        ("A4", "plateau", f"{a['A4']['plateau_score']:.0%} of {a['A4']['n_neighbours']} neighbours profitable,"
                          f" worst pf {_f(a['A4'].get('worst_pf'))}",
         f"+/-{'/'.join(f'{x:.0%}' for x in a['A4'].get('steps', [0.10]))} nudges on OOS: need >= "
         f"{a['A4']['min_plateau']:.0%} profitable and worst pf >= {a['A4'].get('min_worst_pf', 0)}"),
        ("A5", "cost stress", f"OOS pf at 2x costs = {_f(a['A5']['pf_at_2x'])}",
         f"need >= {a['A5']['min_pf_at_2x']}"),
        ("A6", "look-ahead", f"net {a['A6']['base_net']:+.0f} -> {a['A6']['delayed_net']:+.0f} when delayed 1 bar",
         "must NOT improve"),
        ("A7", "transfer", "  ".join(f"{x['pair']} pf={_f(x['pf'])}/{x['n_trades']}t" for x in a['A7']['results'])
                           or "no verified pair to test",
         f"need >= 1 other pair with pf >= {a['A7'].get('min_pf', 0)} on >= "
         f"{a['A7'].get('min_trades', 5)} OOS trades"
         + (f"; excluded (tick unverified): {', '.join(a['A7']['excluded_unverified_tick'])}"
            if a['A7'].get('excluded_unverified_tick') else "")),
        ("A8", "drawdown", f"MC p95 DD = {a['A8']['dd_p95_pct_of_capital']:.1f}% of capital",
         f"need <= {a['A8']['max_dd_p95_pct']:.0f}%"),
    ]
    if "A9" in a:
        rows.append(("A9", "regime", "  ".join(
            f"{k} {v['net']:+.0f}/{v['n_trades']}t" for k, v in sorted(a["A9"]["regimes"].items())),
            f"net outside the best regime ({a['A9']['best_regime']}) must be > 0: "
            f"{a['A9']['net_outside_best']:+.0f}"))
    for code, name, value, rule in rows:
        mark = "PASS" if a[code]["passed"] else ("FAIL*" if code in HARD else "fail ")
        print(f"  {code} {mark:5s} {name:16s} {value}")
        print(f"     {'':5s} {'':16s} rule: {rule}")
        if code == "A2":
            print(f"     {'':5s} {'':16s} SR variance {a['A2']['sr_variance_across_trials']:.4f} "
                  f"from {a['A2']['sr_var_sampled_from']} trials with >= "
                  f"{V.MIN_TRADES_FOR_SR_VARIANCE} IS trades")
            degen = a["A2"].get("sr_degenerate_excluded") or []
            if degen:
                print(f"     {'':5s} {'':16s} left out as degenerate (per-trade return std < "
                      f"{V.MIN_RETURN_STD_RISK_UNITS} risk units): "
                      + ", ".join(f"#{i}" for i in degen))
            a2 = a["A2"]
            if a2.get("n_eff_pair_tf") is not None:
                print(f"     {'':5s} {'':16s} info: N_eff {a2['n_eff_pair_tf']} of "
                      f"{a2['n_eff_computed_on']} trials (neff.py, {a2['n_eff_computed_at']}): "
                      f"E[max SR] = {a2['expected_max_sr_neff']:.2f}, DSR {a2['dsr_neff']:.3f}"
                      f" -> would {'PASS' if a2['would_pass_with_neff'] else 'still fail'}"
                      f"; verdict uses N")
                if a2["n_eff_computed_on"] != a2["n_trials_pair_tf"]:
                    print(f"     {'':5s} {'':16s} info: N_eff is stale ({a2['n_trials_pair_tf']} "
                          f"trials now); rerun neff.py")
            else:
                print(f"     {'':5s} {'':16s} info: N_eff not computed (run neff.py)")
            print(f"     {'':5s} {'':16s} info: passes only if the search had been <= "
                  f"{a2['breakeven_trials']} independent trials")
            if a["A2"].get("sr_missing"):
                print(f"     {'':5s} {'':16s} WARNING {a['A2']['sr_missing']} trials have no "
                      f"is_sharpe (run optimizer.py --backfill-sharpe); excluded")
    print()
    print(f"VERDICT: {r['verdict']}   hard fails {r['hard_fails']}   soft fails {r['soft_fails']}   "
          f"({r['elapsed_s']}s)")
    if r["verdict"] == "REJECTED":
        print("  A hard attack landed. This parameter set is not distinguishable from")
        print("  the best of N random draws. Do not paper-trade it; do not export it.")
    elif r["verdict"] == "FRAGILE":
        print("  Nothing proves it is noise, but it breaks under mild stress. Candidate")
        print("  for more data, not for deployment.")
    else:
        print("  Survived every attack. Eligible for promotion to 'oos_validated' via")
        print("  lineage.py and for a paper-trading run. Still not a licence to trade.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidate", type=int)
    ap.add_argument("--top", type=int, help="attack the N best OOS survivors")
    ap.add_argument("--tf", default="1h", choices=["1h", "4h"])
    ap.add_argument("--family", choices=strategies.names(),
                    help="with --top: only this strategy family (default: all)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    ensure_tables(conn)
    W.ensure_columns(conn)
    lineage.ensure_family_column(conn, "candidates")
    ids = []
    if args.candidate:
        ids = [args.candidate]
    elif args.top:
        ids = [r[0] for r in conn.execute(
            "SELECT id FROM candidates WHERE passed=1 AND tf=? AND family=COALESCE(?, family)"
            " ORDER BY oos_pf DESC LIMIT ?", (args.tf, args.family, args.top))]
    else:
        ap.error("give --candidate ID or --top N")

    summary = []
    for cid in ids:
        rep = attack(conn, cid, verbose=not args.json)
        summary.append((cid, rep["pair"], rep["verdict"], rep["hard_fails"], rep["soft_fails"]))
        if args.json:
            print(json.dumps(rep))
    if len(ids) > 1 and not args.json:
        print("\n" + "=" * 74)
        print("SUMMARY")
        print("=" * 74)
        for cid, pair, v, hf, sf in summary:
            print(f"  #{cid:5d} {pair:8s} {v:9s} hard={hf} soft={sf}")
        counts = {}
        for _, _, v, _, _ in summary:
            counts[v] = counts.get(v, 0) + 1
        print(f"  {counts}")
    conn.close()


if __name__ == "__main__":
    main()
