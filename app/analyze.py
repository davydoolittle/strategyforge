"""
StrategyForge analyzer — three honesty checks on optimizer output.

  1. PARAMETER STRUCTURE — do survivors cluster in parameter space, or are
     they scattered? Clustered toggles/values = structural signal. Scattered
     = the pass list is mostly regime luck.
  2. FEATURE SEPARATION — across all shadow-annotated trades, which of the
     ~31 features actually separate winners from losers? Measured with a
     rank-based AUC (P(random winner's value > random loser's value)).
     0.50 = useless, >0.60 or <0.40 with decent n = worth watching.
     WARNING printed for multiple comparisons: with 31 features, ~2 will
     clear |AUC-0.5|>0.08 by pure chance.
  3. CROSS-PAIR TRANSFER — top survivors from each pair are re-run on the
     OTHER pairs' out-of-sample windows. Edge that only exists on the pair
     it was tuned on is another luck tell.

Usage:
  python analyze.py                     # all three sections
  python analyze.py --tf 1h --transfer-top 2
"""

import argparse
import json
import os
import sqlite3

import engine
import windows

DB_PATH = os.environ.get("FORGE_DB", "/data/forge.db")

NUMERIC_PARAMS = ["ema_fast", "ema_mid", "ema_slow", "rsi_buy", "rsi_sell",
                  "adx_threshold", "min_ema_sep", "min_rel_vol",
                  "max_compression", "max_dist_ema", "stop_atr", "rr_ratio",
                  "trail_atr"]
TOGGLE_PARAMS = ["use_trailing", "use_shorts", "use_htf", "use_vwap",
                 "use_volume", "avoid_chop", "avoid_late", "avoid_fake"]


def median(vals):
    if not vals:
        return None
    s = sorted(vals)
    m = len(s) // 2
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2.0


def auc_rank(winners, losers):
    """P(random winner value > random loser value), ties count half."""
    if not winners or not losers:
        return None
    combined = sorted(winners + losers)
    def rank_of(x):
        import bisect
        lo = bisect.bisect_left(combined, x)
        hi = bisect.bisect_right(combined, x)
        return (lo + hi + 1) / 2.0
    r_sum = sum(rank_of(w) for w in winners)
    n1, n2 = len(winners), len(losers)
    u = r_sum - n1 * (n1 + 1) / 2.0
    return u / (n1 * n2)


# ------------------------- section 1: parameters ---------------------------

def param_structure(conn, tf):
    print("\n" + "=" * 74)
    print("SECTION 1 — SURVIVOR PARAMETER STRUCTURE")
    print("=" * 74)
    rows = conn.execute(
        "SELECT params_json, passed FROM candidates WHERE tf=?", (tf,)).fetchall()
    if not rows:
        print("no candidates for this timeframe")
        return
    all_p = [json.loads(r[0]) for r in rows]
    passed_p = [json.loads(r[0]) for r in rows if r[1] == 1]
    print(f"candidates: {len(all_p)} total, {len(passed_p)} passed "
          f"({100.0 * len(passed_p) / len(all_p):.1f}% pass rate)\n")
    if not passed_p:
        print("no survivors to analyze")
        return

    print(f"{'numeric param':18s} {'median(all)':>12s} {'median(passed)':>15s}  shift")
    for p in NUMERIC_PARAMS:
        a = median([x[p] for x in all_p if p in x])
        b = median([x[p] for x in passed_p if p in x])
        if a is None or b is None:
            continue
        shift = ""
        if a and abs(b - a) / max(abs(a), 1e-9) > 0.10:
            shift = "<-- shifted"
        print(f"{p:18s} {a:12.2f} {b:15.2f}  {shift}")

    print(f"\n{'toggle':14s} {'% on (all)':>11s} {'% on (passed)':>14s}  shift")
    for p in TOGGLE_PARAMS:
        a = 100.0 * sum(1 for x in all_p if x.get(p)) / len(all_p)
        b = 100.0 * sum(1 for x in passed_p if x.get(p)) / len(passed_p)
        shift = "<-- shifted" if abs(b - a) > 15 else ""
        print(f"{p:14s} {a:11.0f} {b:14.0f}  {shift}")
    print("\nRead: parameters where 'passed' deviates a lot from 'all' are the")
    print("structural preferences of this strategy family. No shifts anywhere")
    print("means survival was mostly independent of parameters = regime effect.")


# ------------------------- section 2: features -----------------------------

def feature_separation(conn, min_n):
    print("\n" + "=" * 74)
    print("SECTION 2 — SHADOW FEATURE SEPARATION (winners vs losers)")
    print("=" * 74)
    rows = conn.execute(
        "SELECT pnl, features_json FROM trade_features").fetchall()
    if not rows:
        print("no annotated trades yet")
        return
    print(f"annotated trades: {len(rows)}")

    by_feature = {}
    for pnl, fj in rows:
        feats = json.loads(fj)
        bucket = "w" if pnl > 0 else "l"
        for k, v in feats.items():
            if v is None or isinstance(v, str):
                continue
            by_feature.setdefault(k, {"w": [], "l": []})[bucket].append(float(v))

    results = []
    for k, d in by_feature.items():
        n = len(d["w"]) + len(d["l"])
        if n < min_n or not d["w"] or not d["l"]:
            continue
        a = auc_rank(d["w"], d["l"])
        if a is None:
            continue
        results.append((abs(a - 0.5), a, n, k))
    results.sort(reverse=True)

    print(f"\n{'feature':22s} {'AUC':>6s} {'n':>6s}  interpretation")
    for dist, a, n, k in results[:15]:
        direction = "higher in winners" if a > 0.5 else "higher in losers"
        flag = " ***" if dist >= 0.10 and n >= 200 else ""
        print(f"{k:22s} {a:6.3f} {n:6d}  {direction}{flag}")
    print("\nWARNING: 31 features tested — expect ~2 to show |AUC-0.5| > 0.08 by")
    print("pure chance. Nothing here is validated. A feature earns 'candidate'")
    print("status only if it holds direction on NEW trades (dry-run log) that")
    print("did not exist when this analysis ran.")


# ------------------------- section 3: transfer -----------------------------

def oos_window(conn, pair, tf_s, as_of_iso, from_ts):
    """OOS window of `pair` as it stood when the candidate was pinned, so
    transfer is judged on the same calendar span as the home pair."""
    w = windows.compute(conn, pair, tf_s, windows._ts(as_of_iso), from_ts)
    return None if w is None else (w[1], w[2])


def transfer_test(conn, tf, top_n):
    print("\n" + "=" * 74)
    print("SECTION 3 — CROSS-PAIR TRANSFER (tuned on X, tested on Y's OOS)")
    print("=" * 74)
    tf_s = 3600 if tf == "1h" else 14400
    pairs = [r[0] for r in conn.execute(
        "SELECT DISTINCT pair FROM candidates WHERE tf=? AND passed=1", (tf,))]
    if not pairs:
        print("no survivors to transfer-test")
        return
    all_pairs = [r[0] for r in conn.execute("SELECT DISTINCT pair FROM candles")]

    for pair in pairs:
        tops = conn.execute(
            "SELECT id, params_json, oos_pf FROM candidates"
            " WHERE tf=? AND passed=1 AND pair=? ORDER BY oos_pf DESC LIMIT ?",
            (tf, pair, top_n)).fetchall()
        for cid, pj, home_pf in tops:
            is_start, _, as_of = windows.for_candidate(conn, cid)
            cfg = windows.pin_cfg(json.loads(pj), is_start, tf_s)
            from_ts = windows.history_start(is_start, tf_s)
            line = [f"#{cid} (tuned {pair}, home OOS pf={home_pf:.2f}) ->"]
            for other in all_pairs:
                if other == pair:
                    continue
                w = oos_window(conn, other, tf_s, as_of, from_ts)
                if not w:
                    continue
                try:
                    r = engine.backtest(conn, other, cfg, w[0], w[1], tf_s)
                    pf = r["pf"] if r["pf"] != float("inf") else 99.0
                    line.append(f"{other}: pf={pf:.2f}/{r['n_trades']}t")
                except Exception as e:
                    line.append(f"{other}: err")
            print("  " + "  ".join(line))
    print("\nRead: a parameter set that holds pf > 1 with a reasonable trade")
    print("count on pairs it was NEVER tuned on is showing real robustness.")
    print("One that collapses off its home pair was probably curve-lucky.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tf", default="1h", choices=["1h", "4h"])
    ap.add_argument("--transfer-top", type=int, default=2)
    ap.add_argument("--min-n", type=int, default=30)
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    windows.ensure_columns(conn)
    param_structure(conn, args.tf)
    feature_separation(conn, args.min_n)
    transfer_test(conn, args.tf, args.transfer_top)
    conn.close()


if __name__ == "__main__":
    main()
