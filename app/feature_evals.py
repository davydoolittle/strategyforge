"""
StrategyForge feature evaluations (P3.2) — when does a shadow feature earn
a trial as a filter?

A feature becomes a CANDIDATE FILTER only when both hold:
  1. Gate.    AUC (winners vs losers, analyze.auc_rank) >= 0.60 or <= 0.40
              on >= MIN_TRADES distinct annotated trades. The first run that
              sees this stores a 'first' row in feature_evals: measured_at,
              the AUC, its direction, and cutoff_ts = the latest signal in the
              sample.
  2. Holdout. On trades whose signal is AFTER cutoff_ts (they did not exist
              when the AUC was measured), the AUC falls on the same side of
              0.5. Decided once, the first time >= MIN_HOLDOUT_TRADES such
              trades exist; later runs report the holdout AUC but never
              re-decide (re-checking until it looks good is a search too).

'Distinct': trade_features holds one row per (candidate, trade), and many
candidates take the same signal. Rows are deduplicated on (pair, tf, signal
bar, direction), keeping the first annotated, so one market event counts
once. Raw counts are printed for comparison.

A candidate filter is a hypothesis, not a result: it gets ONE filters.py
child per parent (threshold = the feature's median in the gate sample, not
fitted), and that child must survive adversary.py like anything else. With
~30 features, ~2 clear the gate by chance and half of those hold direction
on new trades by chance, so expect about one false candidate per pass.

New holdout trades only appear when candidates are run on candles that
arrived after cutoff_ts: `--annotate-new` extends every already-annotated
candidate from its pinned IS start to the newest candle and annotates the
trades it has not annotated yet. That writes to forge.db: on the server.

Usage
  python feature_evals.py                       # evaluate every feature
  python feature_evals.py --feature vol_of_vol
  python feature_evals.py --annotate-new        # then evaluate
  python feature_evals.py --dry-run             # evaluate, store nothing
"""

import argparse
import json
import math
import os
import sqlite3
from datetime import datetime, timezone

import analyze
import engine
import features
import validation as V
import windows as W

DB_PATH = os.environ.get("FORGE_DB", "/data/forge.db")

MIN_TRADES = 200             # gate sample, distinct trades (V2_PROMPT P3.2)
AUC_HI, AUC_LO = 0.60, 0.40  # gate
MIN_HOLDOUT_TRADES = 50      # below this a direction check is a coin flip with
                             # a +/-0.08 AUC standard error; not in the spec,
                             # added so that 3 new trades cannot decide

FIRST, HOLDOUT = "first", "holdout"
DECIDED = {"candidate_filter", "rejected"}


def ensure_tables(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS feature_evals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        feature TEXT NOT NULL,
        kind TEXT NOT NULL,            -- 'first' (gate passed) | 'holdout' (re-check)
        measured_at TEXT NOT NULL,     -- wall clock, UTC
        cutoff_ts INTEGER,             -- latest signal in the 'first' sample;
                                       -- holdout = signals after it
        first_id INTEGER,              -- holdout rows: the 'first' row checked
        n_trades INTEGER, n_wins INTEGER, auc REAL,
        status TEXT,                   -- awaiting_holdout | candidate_filter | rejected
        suggested_op TEXT, suggested_value REAL,
        detail_json TEXT
    )""")
    conn.commit()


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def load_trades(conn):
    """[(signal_ts, won, features)] deduplicated on (pair, tf, signal, dir),
    first annotated row kept; plus the raw row count."""
    seen = set()
    out = []
    n_raw = 0
    for pair, tf, sig, d, pnl, fj in conn.execute(
            "SELECT pair, tf_seconds, signal_ts, direction, pnl, features_json"
            " FROM trade_features ORDER BY id"):
        n_raw += 1
        key = (pair, tf, sig, d)
        if key in seen:
            continue
        seen.add(key)
        out.append((sig, pnl > 0, json.loads(fj)))
    return out, n_raw


def feature_names(trades):
    names = set()
    for _, _, f in trades:
        names.update(k for k, v in f.items() if isinstance(v, (int, float)) and v is not None)
    return sorted(names)


def measure(trades, name):
    """AUC of `name` over trades where it is not None. None if one class is empty."""
    w = [f[name] for _, won, f in trades if won and f.get(name) is not None]
    l = [f[name] for _, won, f in trades if not won and f.get(name) is not None]
    auc = analyze.auc_rank(w, l)
    return {"auc": auc, "n": len(w) + len(l), "n_wins": len(w), "values": sorted(w + l)}


def auc_p_value(auc, n_w, n_l):
    """One-sided p of an AUC this far from 0.5 under no separation
    (Mann-Whitney normal approximation, no tie correction). Information."""
    if auc is None or not n_w or not n_l:
        return None
    se = math.sqrt((n_w + n_l + 1) / (12.0 * n_w * n_l))
    return 1.0 - V.norm_cdf(abs(auc - 0.5) / se)


def passes_gate(m):
    return m["auc"] is not None and m["n"] >= MIN_TRADES and (m["auc"] >= AUC_HI or m["auc"] <= AUC_LO)


def _median(sorted_vals):
    k = len(sorted_vals)
    return sorted_vals[k // 2] if k % 2 else (sorted_vals[k // 2 - 1] + sorted_vals[k // 2]) / 2.0


def _row(conn, sql, args):
    cur = conn.execute(sql, args)
    cols = [c[0] for c in cur.description]
    r = cur.fetchone()
    return dict(zip(cols, r)) if r else None


def first_eval(conn, name):
    return _row(conn, "SELECT * FROM feature_evals WHERE feature=? AND kind=? ORDER BY id LIMIT 1",
                (name, FIRST))


def decision(conn, first_id):
    return _row(conn, "SELECT * FROM feature_evals WHERE first_id=? AND status IN"
                      " ('candidate_filter', 'rejected') ORDER BY id LIMIT 1", (first_id,))


def evaluate(conn, names=None, store=True, at=None):
    """One result dict per feature. Stores a 'first' row when a feature
    passes the gate for the first time and a 'holdout' row on every later
    run (the first holdout row with enough trades carries the decision)."""
    ensure_tables(conn)
    at = at or now_iso()
    trades, n_raw = load_trades(conn)
    names = names or feature_names(trades)
    out = []
    for name in names:
        first = first_eval(conn, name)
        if first is None:
            m = measure(trades, name)
            r = {"feature": name, "stage": "gate", "auc": m["auc"], "n": m["n"],
                 "n_raw_rows": n_raw, "p": auc_p_value(m["auc"], m["n_wins"], m["n"] - m["n_wins"])}
            if not passes_gate(m):
                r["status"] = "below_gate"
                out.append(r)
                continue
            op = ">=" if m["auc"] > 0.5 else "<="
            r.update(status="awaiting_holdout", cutoff_ts=max(s for s, _, _ in trades),
                     suggested_op=op, suggested_value=_median(m["values"]))
            if store:
                cur = conn.execute(
                    "INSERT INTO feature_evals (feature, kind, measured_at, cutoff_ts, n_trades,"
                    " n_wins, auc, status, suggested_op, suggested_value, detail_json)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (name, FIRST, at, r["cutoff_ts"], m["n"], m["n_wins"], m["auc"], r["status"],
                     op, r["suggested_value"], json.dumps({"n_raw_rows": n_raw, "p": r["p"]})))
                r["first_id"] = cur.lastrowid
            out.append(r)
            continue

        new = [t for t in trades if t[0] > first["cutoff_ts"]]
        m = measure(new, name)
        same_side = m["auc"] is not None and (m["auc"] > 0.5) == (first["auc"] > 0.5)
        prior = decision(conn, first["id"])
        if prior:
            status = prior["status"]
        elif m["n"] < MIN_HOLDOUT_TRADES:
            status = "awaiting_holdout"
        else:
            status = "candidate_filter" if same_side else "rejected"
        r = {"feature": name, "stage": "holdout", "first_id": first["id"],
             "first_auc": first["auc"], "first_n": first["n_trades"],
             "measured_at": first["measured_at"], "cutoff_ts": first["cutoff_ts"],
             "auc": m["auc"], "n": m["n"], "same_side": same_side,
             "p": auc_p_value(m["auc"], m["n_wins"], m["n"] - m["n_wins"]),
             "status": status, "decided_earlier": prior is not None,
             "suggested_op": first["suggested_op"], "suggested_value": first["suggested_value"]}
        if store:
            conn.execute(
                "INSERT INTO feature_evals (feature, kind, measured_at, cutoff_ts, first_id,"
                " n_trades, n_wins, auc, status, suggested_op, suggested_value, detail_json)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (name, HOLDOUT, at, first["cutoff_ts"], first["id"], m["n"], m["n_wins"], m["auc"],
                 status, first["suggested_op"], first["suggested_value"],
                 json.dumps({"same_side": same_side, "p": r["p"],
                             "decided_earlier": prior is not None})))
        out.append(r)
    if store:
        conn.commit()
    return out


def _iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def annotate_new(conn, progress=True):
    """Extend every candidate already in trade_features to the newest candle
    and annotate its trades that are not annotated yet. Returns rows added."""
    features.ensure_tables(conn)
    rows = conn.execute(
        "SELECT tf.candidate_id, MAX(tf.signal_ts), c.pair, c.tf, c.params_json"
        " FROM trade_features tf JOIN candidates c ON c.id = tf.candidate_id"
        " GROUP BY tf.candidate_id ORDER BY tf.candidate_id").fetchall()
    added = 0
    for cid, last_sig, pair, tf, pj in rows:
        tf_s = W.tf_seconds(tf)
        is_start = W.for_candidate(conn, cid)[0]
        cfg = W.pin_cfg(json.loads(pj), is_start, tf_s)
        newest = conn.execute("SELECT MAX(ts) FROM candles WHERE pair=?", (pair,)).fetchone()[0]
        r = engine.backtest(conn, pair, cfg, is_start, _iso(newest), tf_s)
        new = [t for t in r["trades"] if t["signal_ts"] > last_sig]
        if new:
            added += features.annotate_trades(conn, cid, pair, tf_s, new)
        if progress:
            print(f"  #{cid} {pair} {tf}: +{len(new)} trades after {_iso(last_sig)}", flush=True)
    return added


def _f(x, nd=3):
    return "  n/a" if x is None else f"{x:.{nd}f}"


def print_report(results, n_raw, n_distinct):
    print("\n" + "=" * 78)
    print("FEATURE PROMOTION (P3.2)")
    print("=" * 78)
    print(f"annotated trades: {n_raw} rows, {n_distinct} distinct signals (the rule counts these)")
    print(f"gate: AUC >= {AUC_HI} or <= {AUC_LO} on >= {MIN_TRADES} trades; holdout: same side of 0.5"
          f" on >= {MIN_HOLDOUT_TRADES} trades signalled after the gate's cutoff\n")
    gate = sorted((r for r in results if r["stage"] == "gate"),
                  key=lambda r: -abs((r["auc"] or 0.5) - 0.5))
    hold = [r for r in results if r["stage"] == "holdout"]
    if hold:
        print(f"{'feature':22s} {'first AUC':>9s} {'n':>5s}  {'cutoff':16s} {'holdout AUC':>11s}"
              f" {'n':>5s}  status")
        for r in hold:
            print(f"{r['feature']:22s} {_f(r['first_auc']):>9s} {r['first_n']:5d}  "
                  f"{_iso(r['cutoff_ts'])[:16]:16s} {_f(r['auc']):>11s} {r['n']:5d}  {r['status']}"
                  f"{' (decided earlier)' if r['decided_earlier'] else ''}")
            if r["status"] == "candidate_filter":
                print(f"{'':22s} -> python filters.py --candidate <ID> --filter "
                      f"\"{r['feature']}{r['suggested_op']}{r['suggested_value']:.4g}\"")
        print()
    newly = [r for r in gate if r["status"] == "awaiting_holdout"]
    for r in newly:
        print(f"GATE PASSED NOW: {r['feature']}  AUC {r['auc']:.3f} on {r['n']} trades; "
              f"holdout starts after {_iso(r['cutoff_ts'])}")
    print(f"{'feature (below gate)':22s} {'AUC':>6s} {'n':>5s} {'p (1-sided)':>11s}")
    for r in gate:
        if r["status"] == "below_gate":
            print(f"{r['feature']:22s} {_f(r['auc']):>6s} {r['n']:5d} {_f(r['p'], 4):>11s}")
    print("\nRead: a feature is only a candidate filter after its AUC clears the gate AND")
    print("keeps its direction on trades that did not exist when it was measured. The")
    print(f"p column ignores that {len(gate) + len(hold)} feature(s) were tested in this pass (with")
    print("all ~30, about two clear the gate by luck). 'awaiting_holdout' means the market has not produced")
    print("enough new trades yet: run with --annotate-new on the server as candles arrive.")
    print("A candidate filter is one trial for filters.py + adversary.py, not an edge.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--feature", action="append", help="limit to these features (repeatable)")
    ap.add_argument("--annotate-new", action="store_true",
                    help="first annotate trades after each candidate's last annotated signal")
    ap.add_argument("--dry-run", action="store_true", help="evaluate without storing rows")
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    ensure_tables(conn)
    W.ensure_columns(conn)
    if args.annotate_new:
        n = annotate_new(conn)
        print(f"annotated {n} new trades")
    trades, n_raw = load_trades(conn)
    results = evaluate(conn, args.feature, store=not args.dry_run)
    print_report(results, n_raw, len(trades))
    conn.close()


if __name__ == "__main__":
    main()
