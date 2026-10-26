"""
StrategyForge event fingerprinting (P3.3) — what did the market look like
just before a big move, compared with just before nothing happened?

Research only: nothing here trades or filters anything.

Definitions (1h bars, H = 24). Windows [i, i+H] are taken on ONE grid,
every H bars, so they never overlap and moves and non-moves are sampled the
same way:
  move      |close[i+H] - close[i]| >= K x ATR14[i]   (K = 3, the spec)
  non-move  the same change < K x ATR14[i]
(A first version scanned bar by bar for moves and used the grid only for
non-moves. That greedy search picks move starts at particular bars and
biased hour / wick features; it also inflated moves to ~83% of windows.)

K = 3 hourly ATRs is NOT a rare event: 30-33% of 24h windows clear it on
every pair (median 1.9, p95 ~8 hourly ATRs, 2021-2026). The report says so
whenever moves exceed RARE_SHARE of windows; --k 8 gives roughly the top 5%.
Every window is fingerprinted AT ITS START, bar i: the ~31 shadow features of
features.FeatureComputer (OHLCV-derived ones look back 24h / 7d / 30d / 90d;
context levels at i) plus, for each context feed, its change over the
24h / 7d / 30d before i. Only data up to bar i is used; the label is
i .. i+H. Rows go to the `fingerprints` table (replaced per pair on each run).

The report gives, per feature, AUC = P(value before a move > value before a
non-move), pooled and separately for up / down moves, with:
  Table A  full candle history (2021-), OHLCV features only
  Table B  the context era only (the collector started 2026-09-02), all
           features. A few weeks of windows: anecdotes, not evidence, and
           the output says so.
and the multiple-comparison arithmetic for the number of AUCs printed.

Usage
  python fingerprint.py                      # all pairs, store + report
  python fingerprint.py --pair BTC-USD --no-store
  python fingerprint.py --k 4                # stricter move threshold
Storing writes ~10k rows: run it on the server (V2_PROMPT §10.9).
"""

import argparse
import json
import os
import sqlite3
from datetime import datetime, timezone

import analyze
import engine
import feature_evals as FE
import features
import main as collector

DB_PATH = os.environ.get("FORGE_DB", "/data/forge.db")

TF_SECONDS = 3600
HORIZON = 24                  # bars
MOVE_ATR = 3.0                # K
LOOKBACKS = (("24h", 86400), ("7d", 7 * 86400), ("30d", 30 * 86400))
# context feed -> pair scope (None = the window's own pair)
DELTA_METRICS = (("funding_rate", None), ("funding_rate_pred", None),
                 ("open_interest", None), ("dvol", None), ("ob_imbalance_1pct", None),
                 ("spot_spread_bps", None), ("perp_spread_bps", None),
                 ("fear_greed", "GLOBAL"), ("btc_dominance", "GLOBAL"))
PCT_METRICS = {"open_interest"}
MAX_AGE = {"fear_greed": 26 * 3600, "btc_dominance": 4 * 3600}   # as features.context_features
PAST_TOLERANCE_S = 4 * 3600
CONTEXT_LEVELS = ("funding_rate", "funding_z30", "open_interest", "oi_delta_24h_pct", "dvol",
                  "ob_imbalance_1pct", "spot_spread_bps", "perp_spread_bps", "fear_greed",
                  "btc_dominance", "iv_rv_spread")
DELTA_NAMES = tuple(f"{m}_chg_{lb}" for m, _ in DELTA_METRICS for lb, _ in LOOKBACKS)
CONTEXT_NAMES = set(CONTEXT_LEVELS) | set(DELTA_NAMES)
MIN_CLASS = 5                 # fewer moves or non-moves than this: no AUC
RARE_SHARE = 0.20             # above this share of windows, "move" is not an event


def ensure_tables(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS fingerprints (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_at TEXT, pair TEXT, tf_seconds INTEGER,
        start_ts INTEGER, end_ts INTEGER,   -- bar open times of bar i and i+H
        label TEXT,                         -- 'move' | 'none'
        direction TEXT,                     -- 'up' | 'down' | NULL
        move_atr REAL,                      -- (close[i+H]-close[i]) / ATR14[i]
        features_json TEXT
    )""")
    conn.commit()


def find_windows(c, atr, start=engine.WARMUP_BARS, horizon=HORIZON, k=MOVE_ATR):
    """([(i, r)] moves, [(i, r)] non-moves) on the grid start, start+H, ...;
    r = change over the window in ATR14[i] units."""
    moves, nones = [], []
    for i in range(start, len(c) - horizon, horizon):
        if not atr[i]:
            continue
        r = (c[i + horizon] - c[i]) / atr[i]
        (moves if abs(r) >= k else nones).append((i, r))
    return moves, nones


def context_deltas(fc, pair, at_ts):
    """{metric}_chg_{24h,7d,30d}: level at at_ts minus level L earlier (% for
    open interest). None when either poll is missing."""
    out = {}
    for metric, scope in DELTA_METRICS:
        p = scope or pair
        age = MAX_AGE.get(metric, features.CONTEXT_MAX_AGE_S)
        now = fc._ctx_at(metric, p, at_ts, max_age=age)
        for lb, secs in LOOKBACKS:
            then = None if now is None else fc._ctx_at(metric, p, at_ts - secs,
                                                       max_age=max(age, PAST_TOLERANCE_S))
            if then is None:
                v = None
            elif metric in PCT_METRICS:
                v = (now - then) / then * 100.0 if then else None
            else:
                v = now - then
            out[f"{metric}_chg_{lb}"] = v
    return out


def context_start(conn):
    try:
        return conn.execute("SELECT MIN(ts) FROM context").fetchone()[0]
    except sqlite3.OperationalError:
        return None


def build(conn, pair, k=MOVE_ATR, ctx_start=None):
    """Fingerprint every move and non-move window of one pair."""
    fc = features.FeatureComputer(conn, pair, TF_SECONDS)
    moves, nones = find_windows(fc.c, fc.atr, k=k)
    rows = []
    for label, wins in (("move", moves), ("none", nones)):
        for i, r in wins:
            ts = fc.ts[i]
            if ctx_start is not None and ts >= ctx_start:
                f = dict(fc.at(ts))
                f.update(context_deltas(fc, pair, ts))
            else:
                f = dict(fc.at(ts, names=[]))       # OHLCV only: no context to query
            rows.append({"pair": pair, "start_ts": ts, "end_ts": fc.ts[i + HORIZON],
                         "label": label,
                         "direction": ("up" if r > 0 else "down") if label == "move" else None,
                         "move_atr": r, "features": f})
    rows.sort(key=lambda x: x["start_ts"])
    return rows


def store(conn, pair, rows):
    ensure_tables(conn)
    conn.execute("DELETE FROM fingerprints WHERE pair=? AND tf_seconds=?", (pair, TF_SECONDS))
    run_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn.executemany(
        "INSERT INTO fingerprints (run_at, pair, tf_seconds, start_ts, end_ts, label, direction,"
        " move_atr, features_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(run_at, r["pair"], TF_SECONDS, r["start_ts"], r["end_ts"], r["label"], r["direction"],
          r["move_atr"], json.dumps(r["features"])) for r in rows])
    conn.commit()


# ------------------------------------------------------------------ report ---

def _vals(rows, name, pick):
    return [r["features"][name] for r in rows
            if pick(r) and isinstance(r["features"].get(name), (int, float))]


def auc_rows(rows, names):
    """Per feature: pooled / up / down AUC vs non-moves, n and two-sided p."""
    out = []
    for name in names:
        none = _vals(rows, name, lambda r: r["label"] == "none")
        res = {"feature": name, "n_none": len(none)}
        for key, pick in (("all", lambda r: r["label"] == "move"),
                          ("up", lambda r: r["direction"] == "up"),
                          ("down", lambda r: r["direction"] == "down")):
            mv = _vals(rows, name, pick)
            ok = len(mv) >= MIN_CLASS and len(none) >= MIN_CLASS
            auc = analyze.auc_rank(mv, none) if ok else None
            p1 = FE.auc_p_value(auc, len(mv), len(none)) if ok else None
            res[key] = {"auc": auc, "n_move": len(mv), "p": None if p1 is None else min(1.0, 2 * p1)}
        out.append(res)
    out.sort(key=lambda x: -abs((x["all"]["auc"] or 0.5) - 0.5))
    return out


def analyse(rows, ctx_start):
    names = sorted({k for r in rows for k in r["features"]})
    ohlcv = [n for n in names if n not in CONTEXT_NAMES]
    era = [r for r in rows if ctx_start is not None and r["start_ts"] >= ctx_start]
    table_a = auc_rows(rows, ohlcv)
    table_b = auc_rows(era, names) if era else []
    tests = [t[key]["p"] for t in table_a + table_b for key in ("all", "up", "down")
             if t[key]["p"] is not None]
    k = len(tests)
    bonf = 0.05 / k if k else None
    return {"table_a": table_a, "table_b": table_b, "n_tests": k,
            "expected_false_at_05": 0.05 * k,
            "bonferroni_p": bonf,
            "bonferroni_pass": [(t["feature"], key, tbl) for tbl, tab in (("A", table_a), ("B", table_b))
                                for t in tab for key in ("all", "up", "down")
                                if t[key]["p"] is not None and t[key]["p"] < bonf],
            "era": {"start": ctx_start, "n_windows": len(era),
                    "n_moves": sum(1 for r in era if r["label"] == "move")}}


def _d(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def _a(x):
    return "  n/a" if x is None else f"{x:.3f}"


def _table(tab):
    print(f"  {'feature':26s} {'AUC':>6s} {'p 2-sid':>7s} {'n move':>7s} {'n none':>7s} {'AUC up':>7s} {'AUC dn':>7s}")
    for t in tab:
        a = t["all"]
        p = "    n/a" if a["p"] is None else f"{a['p']:7.4f}"
        print(f"  {t['feature']:26s} {_a(a['auc']):>6s} {p} {a['n_move']:7d} {t['n_none']:7d}"
              f" {_a(t['up']['auc']):>7s} {_a(t['down']['auc']):>7s}")


def print_report(rows, res, k, now_ts):
    print("\n" + "=" * 86)
    print(f"EVENT FINGERPRINTS (P3.3) — move = |close[i+{HORIZON}] - close[i]| >= {k:g} x ATR14[i], 1h")
    print("=" * 86)
    for pair in sorted({r["pair"] for r in rows}):
        pr = [r for r in rows if r["pair"] == pair]
        up = sum(1 for r in pr if r["direction"] == "up")
        dn = sum(1 for r in pr if r["direction"] == "down")
        nn = sum(1 for r in pr if r["label"] == "none")
        print(f"  {pair:8s} {_d(pr[0]['start_ts'])} -> {_d(pr[-1]['start_ts'])}   moves {up + dn:5d}"
              f" (up {up}, down {dn})   non-moves {nn:5d}   moves are {100.0 * (up + dn) / max(1, up + dn + nn):.0f}%"
              f" of windows")
    share = sum(1 for r in rows if r["label"] == "move") / len(rows)
    if share > RARE_SHARE:
        print(f"\n  !! {share:.0%} of windows are 'moves' at K = {k:g}: that is ordinary volatility, not an")
        print("  !! event. The AUCs compare bigger days with smaller days. Try --k 8 (~top 5%).")
    print(f"\nTABLE A — full candle history, OHLCV features ({len(res['table_a'])}), all pairs pooled")
    _table(res["table_a"])

    e = res["era"]
    print("\nTABLE B — context era only, all features")
    if e["start"] is None or not e["n_windows"]:
        print("  no context data in range: the collector's context feeds have nothing to compare.")
    else:
        days = (now_ts - e["start"]) / 86400.0
        print(f"  !! context begins {_d(e['start'])}: {days:.0f} days, {e['n_windows']} windows, "
              f"{e['n_moves']} moves across all pairs.")
        print(f"  !! 30d deltas exist only from {_d(e['start'] + 30 * 86400)}; dvol only for BTC/ETH;"
              f" XRP has no perp/funding feed.")
        print("  !! These AUCs are anecdotes, not evidence. Re-run in a few months.")
        _table(res["table_b"])

    print(f"\nMULTIPLE COMPARISONS: {res['n_tests']} AUCs above have a p-value (pooled, up, down;"
          f" tables A and B).")
    if res["n_tests"]:
        print(f"  At p < 0.05, ~{res['expected_false_at_05']:.0f} would pass with NO real effect anywhere.")
        print(f"  Bonferroni bar: p < {res['bonferroni_p']:.1e}. Passing it: "
              + (", ".join(f"{f} ({key}, {tbl})" for f, key, tbl in res["bonferroni_pass"]) or "none"))
    print("\nRead: AUC > 0.5 means the feature tends to be HIGHER just before a move than just")
    print("before a quiet day. The p column is optimistic twice over: neighbouring windows")
    print("and the four pairs are correlated, so the effective n is far below the printed n.")
    print("Volatility features (atr_pct, rv7, bb_bw, vol_regime) predict the label partly by")
    print("construction: the threshold is K x ATR AT BAR i, so a quiet bar i (low ATR, a")
    print("weekend) makes a 'move' easier. AUC < 0.5 on those is mostly that, not foresight.")
    print("A pooled AUC near 0.5 with up and down on opposite sides is a directional feature,")
    print("not a useless one. Nothing here is a trading rule; a feature that looks good goes")
    print("through feature_evals.py (P3.2) and filters.py like any other.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pair", action="append", help="default: every pair in main.PAIRS")
    ap.add_argument("--k", type=float, default=MOVE_ATR, help="move threshold in ATRs (spec: 3)")
    ap.add_argument("--no-store", action="store_true", help="report only; write nothing")
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    ctx = context_start(conn)
    rows = []
    for pair in args.pair or collector.PAIRS:
        pr = build(conn, pair, args.k, ctx)
        if not args.no_store:
            store(conn, pair, pr)
        print(f"  {pair}: {len(pr)} windows{'' if args.no_store else ' stored'}", flush=True)
        rows += pr
    if not rows:
        raise SystemExit("no windows: not enough candles")
    newest = max(r["end_ts"] for r in rows)
    print_report(rows, analyse(rows, ctx), args.k, newest)
    conn.close()


if __name__ == "__main__":
    main()
