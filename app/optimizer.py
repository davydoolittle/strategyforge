"""
StrategyForge optimizer — random parameter search with walk-forward validation.

The anti-overfitting contract, enforced structurally:
  1. History is split: first WF_SPLIT (70%) = in-sample (IS), rest = out-of-
     sample (OOS). Parameters are judged on IS first; OOS is touched only for
     candidates that pass the IS gate, and final ranking uses OOS ONLY.
  2. Gates require minimum trade counts (stats on 5 trades are noise).
  3. Every trial is stored in the candidates table, pass or fail, so the
     hit rate itself is visible (if 3 of 400 trials pass, that 0.75% base
     rate is your multiple-comparisons warning).

Ranking metric is OOS profit factor — NOT win rate, which is trivially
gameable (see: martingale). Win rate is recorded but never optimized.

Usage:
  python optimizer.py --pairs BTC-USD,ETH-USD,SOL-USD --tf 1h --trials 300
  python optimizer.py --pairs BTC-USD --tf 4h --trials 200 --seed 7
"""

import argparse
import json
import os
import random
import sqlite3
import time
import urllib.request

import engine
import features
import windows

DB_PATH = os.environ.get("FORGE_DB", "/data/forge.db")
WF_SPLIT = windows.WF_SPLIT

# IS gate: worth evaluating OOS at all
IS_MIN_TRADES = 20
IS_MIN_PF = 1.05
# OOS gate: "survived validation"
OOS_MIN_TRADES = 8
OOS_MIN_PF = 1.2
# how many survivors get full-window feature annotation
ANNOTATE_TOP = 3

NTFY_TOPIC = None  # set from env in main


def ntfy(msg):
    if not NTFY_TOPIC:
        return
    try:
        req = urllib.request.Request(
            f"https://ntfy.sh/{NTFY_TOPIC}", data=msg.encode(),
            headers={"Title": "StrategyForge Optimizer"})
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass


def sample_cfg(rng):
    cfg = engine.default_cfg()
    f = rng.randint(5, 13)
    m = rng.randint(max(f + 4, 15), 35)
    s = rng.randint(max(m + 10, 40), 80)
    cfg["ema_fast"], cfg["ema_mid"], cfg["ema_slow"] = f, m, s
    cfg["rsi_buy"] = round(rng.uniform(52, 60), 1)
    cfg["rsi_sell"] = round(rng.uniform(40, 48), 1)
    cfg["adx_threshold"] = round(rng.uniform(18, 30), 1)
    cfg["min_ema_sep"] = round(rng.uniform(0.5, 2.5), 2)
    cfg["min_rel_vol"] = round(rng.uniform(0.8, 1.5), 2)
    cfg["max_compression"] = round(rng.uniform(0.8, 2.0), 2)
    cfg["max_dist_ema"] = round(rng.uniform(2.0, 4.5), 2)
    cfg["stop_atr"] = round(rng.uniform(1.5, 3.5), 2)
    cfg["rr_ratio"] = round(rng.uniform(1.5, 3.5), 2)
    cfg["use_trailing"] = rng.random() < 0.5
    cfg["trail_atr"] = round(rng.uniform(1.2, 2.5), 2)
    cfg["use_shorts"] = rng.random() < 0.5
    for flag in ("use_htf", "use_vwap", "use_volume", "avoid_chop",
                 "avoid_late", "avoid_fake"):
        cfg[flag] = rng.random() < 0.7   # keep filters mostly on
    return cfg


def ensure_tables(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS candidates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_at TEXT, pair TEXT, tf TEXT, seed INTEGER, trial INTEGER,
        params_json TEXT,
        is_trades INTEGER, is_net REAL, is_pf REAL, is_dd REAL, is_wr REAL,
        oos_trades INTEGER, oos_net REAL, oos_pf REAL, oos_dd REAL, oos_wr REAL,
        passed INTEGER
    )""")
    conn.commit()
    windows.ensure_columns(conn)


def run_pair(conn, pair, tf, trials, seed):
    tf_s = 3600 if tf == "1h" else 14400
    rng = random.Random(seed)

    # computed once per pair and stored on every row (P2.1), so the windows
    # stay fixed even as the collector keeps appending candles
    w = windows.compute(conn, pair, tf_s)
    if w is None:
        print(f"[{pair}] no candles, skipping")
        return []
    is_start_iso, split_iso, end_iso = w
    print(f"[{pair} {tf}] IS {is_start_iso} -> {split_iso} | OOS {split_iso} -> {end_iso}")

    survivors = []
    t0 = time.time()
    for trial in range(trials):
        cfg = sample_cfg(rng)
        if tf == "4h":
            cfg["htf_seconds"] = 86400
        try:
            is_r = engine.backtest(conn, pair, cfg, is_start_iso, split_iso, tf_s)
        except Exception as e:
            print(f"  trial {trial} IS error: {e}")
            continue

        row = {
            "pair": pair, "tf": tf, "seed": seed, "trial": trial,
            "params_json": json.dumps(cfg),
            "is_trades": is_r["n_trades"], "is_net": is_r["net"],
            "is_pf": None if is_r["pf"] == float("inf") else is_r["pf"],
            "is_dd": is_r["max_dd"], "is_wr": is_r["win_rate"],
            "oos_trades": None, "oos_net": None, "oos_pf": None,
            "oos_dd": None, "oos_wr": None, "passed": 0,
        }

        if is_r["n_trades"] >= IS_MIN_TRADES and is_r["pf"] >= IS_MIN_PF:
            oos_r = engine.backtest(conn, pair, cfg, split_iso, end_iso, tf_s)
            row["oos_trades"] = oos_r["n_trades"]
            row["oos_net"] = oos_r["net"]
            row["oos_pf"] = None if oos_r["pf"] == float("inf") else oos_r["pf"]
            row["oos_dd"] = oos_r["max_dd"]
            row["oos_wr"] = oos_r["win_rate"]
            if (oos_r["n_trades"] >= OOS_MIN_TRADES
                    and oos_r["pf"] >= OOS_MIN_PF and oos_r["net"] > 0):
                row["passed"] = 1

        cur = conn.execute(
            """INSERT INTO candidates (run_at, pair, tf, seed, trial, params_json,
               is_trades, is_net, is_pf, is_dd, is_wr,
               oos_trades, oos_net, oos_pf, oos_dd, oos_wr, passed,
               is_start, split_ts, oos_end, windows_backfilled)
               VALUES (datetime('now'), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                       ?, ?, ?, 0)""",
            (row["pair"], row["tf"], row["seed"], row["trial"], row["params_json"],
             row["is_trades"], row["is_net"], row["is_pf"], row["is_dd"], row["is_wr"],
             row["oos_trades"], row["oos_net"], row["oos_pf"], row["oos_dd"],
             row["oos_wr"], row["passed"],
             is_start_iso, split_iso, end_iso))
        conn.commit()
        row["id"] = cur.lastrowid
        row["cfg"] = cfg
        if row["passed"]:
            survivors.append(row)
            print(f"  PASS trial {trial}: IS pf={row['is_pf']:.2f}/{row['is_trades']}t "
                  f"OOS pf={row['oos_pf']:.2f}/{row['oos_trades']}t net={row['oos_net']:+.0f}")
        if trial % 25 == 24:
            rate = (trial + 1) / (time.time() - t0)
            print(f"  ...{trial + 1}/{trials} trials ({rate:.1f}/s), "
                  f"{len(survivors)} passed so far")

    return survivors, is_start_iso, end_iso


def main():
    global NTFY_TOPIC
    import os
    NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")

    ap = argparse.ArgumentParser()
    ap.add_argument("--pairs", default="BTC-USD,ETH-USD,SOL-USD")
    ap.add_argument("--tf", default="1h", choices=["1h", "4h"])
    ap.add_argument("--trials", type=int, default=300)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    ensure_tables(conn)

    all_survivors = []
    for pair in [p.strip() for p in args.pairs.split(",")]:
        result = run_pair(conn, pair, args.tf, args.trials, args.seed)
        if not result:
            continue
        survivors, full_start, full_end = result
        # annotate top OOS performers with shadow features over the FULL window
        survivors.sort(key=lambda r: r["oos_pf"] or 0, reverse=True)
        tf_s = 3600 if args.tf == "1h" else 14400
        for row in survivors[:ANNOTATE_TOP]:
            full = engine.backtest(conn, pair, row["cfg"], full_start, full_end, tf_s)
            n = features.annotate_trades(conn, row["id"], pair, tf_s, full["trades"])
            print(f"  candidate {row['id']}: annotated {n} trades with shadow features")
        all_survivors.extend([(pair, r) for r in survivors])

    print("\n================ LEADERBOARD (ranked by OOS profit factor) ================")
    total = conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
    if not all_survivors:
        print("No candidates survived out-of-sample validation this run.")
        print("That is a legitimate result: it means none of the sampled parameter")
        print("sets carried edge into unseen data. Try more trials, a different")
        print("timeframe, or accept the strategy family may lack robust edge.")
        ntfy("Optimizer run complete: 0 survivors. See candidates table.")
    else:
        all_survivors.sort(key=lambda x: x[1]["oos_pf"] or 0, reverse=True)
        for pair, r in all_survivors[:10]:
            print(f"#{r['id']:4d} {pair} {r['tf']}  "
                  f"IS: pf={r['is_pf']:.2f} n={r['is_trades']} wr={r['is_wr']:.0f}%  |  "
                  f"OOS: pf={r['oos_pf']:.2f} n={r['oos_trades']} wr={r['oos_wr']:.0f}% "
                  f"net={r['oos_net']:+.0f} dd={r['oos_dd']:.0f}")
        best_pair, best = all_survivors[0]
        ntfy(f"Optimizer: {len(all_survivors)} survivors. Best: {best_pair} "
             f"candidate #{best['id']} OOS pf={best['oos_pf']:.2f} "
             f"({best['oos_trades']} trades)")
    print(f"(total candidates in table: {total})")
    print("\nInspect any candidate's parameters:")
    print("  SELECT params_json FROM candidates WHERE id=<ID>;")
    conn.close()


if __name__ == "__main__":
    main()
