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

Seeds (P2.2): --seed is a BASE seed. Each pair draws from its own stream,
seed_for_pair = base_seed * 1000 + index of the pair in main.PAIRS, so trial
k on BTC is not the same parameter set as trial k on ETH. Every row stores
base_seed, the per-pair seed and the DNA of its params (lineage.dna_of), so a
parameter set that was already tried shows up as a duplicate. Rows from
before P2.2 have base_seed NULL: those runs used one seed for every pair.

Families (P5.2): --family picks the strategy family (app/strategies/) whose
sample_cfg() draws the trials; default smrt_v41. Each row stores its family.
A family's trials are its own search: adversary A2 deflates a candidate by
the trial count of its own family on that pair/tf, not by every trial run.

Usage:
  python optimizer.py --tf 1h --trials 300          # every pair in main.PAIRS
  python optimizer.py --pairs BTC-USD,ETH-USD,SOL-USD --tf 1h --trials 300
  python optimizer.py --pairs BTC-USD --tf 4h --trials 200 --seed 7
  python optimizer.py --family core --tf 1h --trials 300 --seed 5301
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
import lineage
import main as collector
import strategies
import validation as V
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


def sample_cfg(rng, family=strategies.DEFAULT_FAMILY):
    return strategies.get(family).sample_cfg(rng)


def ensure_tables(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS candidates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_at TEXT, pair TEXT, tf TEXT, seed INTEGER, trial INTEGER,
        params_json TEXT,
        is_trades INTEGER, is_net REAL, is_pf REAL, is_dd REAL, is_wr REAL,
        oos_trades INTEGER, oos_net REAL, oos_pf REAL, oos_dd REAL, oos_wr REAL,
        passed INTEGER
    )""")
    have = {r[1] for r in conn.execute("PRAGMA table_info(candidates)")}
    for name, typ in (("base_seed", "INTEGER"), ("dna", "TEXT"),
                      ("is_sharpe", "REAL"), ("oos_sharpe", "REAL"),
                      ("is_skew", "REAL"), ("is_kurt", "REAL")):
        if name not in have:
            conn.execute(f"ALTER TABLE candidates ADD COLUMN {name} {typ}")
    conn.commit()
    windows.ensure_columns(conn)
    lineage.ensure_family_column(conn, "candidates")
    backfill_dna(conn)


def backfill_dna(conn):
    """DNA for rows inserted before P2.2. Pure hashing, no backtests."""
    rows = conn.execute("SELECT id, params_json FROM candidates WHERE dna IS NULL").fetchall()
    for cid, pj in rows:
        conn.execute("UPDATE candidates SET dna=? WHERE id=?",
                     (lineage.dna_of(json.loads(pj)), cid))
    conn.commit()
    return len(rows)


def trade_stats(trades, initial_capital):
    """(per-trade Sharpe, skew, NON-excess kurtosis) of a backtest's trades,
    as validation computes them for the deflated Sharpe ratio."""
    rets = V.trade_returns(trades, initial_capital)
    skew, kurt = V.moments(rets)
    return V.sharpe(rets), skew, kurt


def backfill_sharpe(conn, progress=True):
    """Re-run IS (and OOS where the optimizer ran it) on each row's pinned
    windows to fill is_sharpe / is_skew / is_kurt / oos_sharpe for rows
    inserted before P2.3. Also checks the trade counts still match what was
    stored: a mismatch means the row is not reproducible and is reported,
    not written. Returns (filled, mismatches)."""
    rows = conn.execute(
        "SELECT id, pair, tf, params_json, is_start, split_ts, oos_end, is_trades, oos_trades"
        " FROM candidates WHERE is_sharpe IS NULL ORDER BY id").fetchall()
    filled, bad = 0, []
    t0 = time.time()
    for k, (cid, pair, tf, pj, s, sp, e, ist, oot) in enumerate(rows):
        if s is None:
            raise SystemExit(f"candidate {cid} has no pinned windows — "
                             f"run `python windows.py --backfill` first")
        tf_s = windows.tf_seconds(tf)
        cfg = windows.pin_cfg(json.loads(pj), s, tf_s)
        is_r = engine.backtest(conn, pair, cfg, s, sp, tf_s)
        oos_sr = None
        oos_n = None
        if oot is not None:
            oos_r = engine.backtest(conn, pair, cfg, sp, e, tf_s)
            oos_n = oos_r["n_trades"]
            oos_sr = trade_stats(oos_r["trades"], cfg["initial_capital"])[0]
        if is_r["n_trades"] != ist or oos_n != oot:
            bad.append((cid, (ist, oot), (is_r["n_trades"], oos_n)))
            continue
        sr, skew, kurt = trade_stats(is_r["trades"], cfg["initial_capital"])
        conn.execute("UPDATE candidates SET is_sharpe=?, is_skew=?, is_kurt=?, oos_sharpe=?"
                     " WHERE id=?", (sr, skew, kurt, oos_sr, cid))
        filled += 1
        if k % 100 == 99:
            conn.commit()
            if progress:
                print(f"  ...{k + 1}/{len(rows)} ({(k + 1) / (time.time() - t0):.1f}/s)")
    conn.commit()
    return filled, bad


def seed_for_pair(base_seed, pair, pairs=None):
    """Per-pair RNG seed. Index comes from main.PAIRS, so that list may be
    appended to but never reordered — reordering would silently change
    which seed a pair gets."""
    pairs = collector.PAIRS if pairs is None else pairs
    if pair not in pairs:
        raise SystemExit(f"{pair} is not in PAIRS {pairs}; add it there first "
                         f"(append only) so its seed is stable")
    if len(pairs) >= 1000:
        raise SystemExit("seed_for_pair supports < 1000 pairs")
    return base_seed * 1000 + pairs.index(pair)


def run_pair(conn, pair, tf, trials, base_seed, family=strategies.DEFAULT_FAMILY):
    fam = strategies.get(family)
    tf_s = 3600 if tf == "1h" else 14400
    seed = seed_for_pair(base_seed, pair)
    rng = random.Random(seed)

    # computed once per pair and stored on every row (P2.1), so the windows
    # stay fixed even as the collector keeps appending candles
    w = windows.compute(conn, pair, tf_s)
    if w is None:
        print(f"[{pair}] no candles, skipping")
        return []
    is_start_iso, split_iso, end_iso = w
    print(f"[{family} {pair} {tf}] seed {seed} (base {base_seed}) | "
          f"IS {is_start_iso} -> {split_iso} | OOS {split_iso} -> {end_iso}")

    survivors = []
    dupes = []
    t0 = time.time()
    for trial in range(trials):
        cfg = fam.for_timeframe(fam.sample_cfg(rng), tf_s)
        dna = lineage.dna_of(cfg)
        seen = conn.execute(
            "SELECT id, pair, tf FROM candidates WHERE dna=? ORDER BY id LIMIT 1",
            (dna,)).fetchone()
        if seen:
            dupes.append((trial, dna, seen))
        try:
            is_r = engine.backtest(conn, pair, cfg, is_start_iso, split_iso, tf_s)
        except Exception as e:
            print(f"  trial {trial} IS error: {e}")
            continue

        is_sr, is_skew, is_kurt = trade_stats(is_r["trades"], cfg["initial_capital"])
        row = {
            "pair": pair, "tf": tf, "seed": seed, "trial": trial,
            "params_json": json.dumps(cfg),
            "is_trades": is_r["n_trades"], "is_net": is_r["net"],
            "is_pf": None if is_r["pf"] == float("inf") else is_r["pf"],
            "is_dd": is_r["max_dd"], "is_wr": is_r["win_rate"],
            "is_sharpe": is_sr, "is_skew": is_skew, "is_kurt": is_kurt,
            "oos_trades": None, "oos_net": None, "oos_pf": None,
            "oos_dd": None, "oos_wr": None, "oos_sharpe": None, "passed": 0,
        }

        if is_r["n_trades"] >= IS_MIN_TRADES and is_r["pf"] >= IS_MIN_PF:
            oos_r = engine.backtest(conn, pair, cfg, split_iso, end_iso, tf_s)
            row["oos_trades"] = oos_r["n_trades"]
            row["oos_net"] = oos_r["net"]
            row["oos_pf"] = None if oos_r["pf"] == float("inf") else oos_r["pf"]
            row["oos_dd"] = oos_r["max_dd"]
            row["oos_wr"] = oos_r["win_rate"]
            row["oos_sharpe"] = trade_stats(oos_r["trades"], cfg["initial_capital"])[0]
            if (oos_r["n_trades"] >= OOS_MIN_TRADES
                    and oos_r["pf"] >= OOS_MIN_PF and oos_r["net"] > 0):
                row["passed"] = 1

        cur = conn.execute(
            """INSERT INTO candidates (run_at, pair, tf, seed, trial, params_json,
               is_trades, is_net, is_pf, is_dd, is_wr,
               oos_trades, oos_net, oos_pf, oos_dd, oos_wr, passed,
               is_start, split_ts, oos_end, windows_backfilled, base_seed, dna,
               is_sharpe, is_skew, is_kurt, oos_sharpe, family)
               VALUES (datetime('now'), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                       ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?)""",
            (row["pair"], row["tf"], row["seed"], row["trial"], row["params_json"],
             row["is_trades"], row["is_net"], row["is_pf"], row["is_dd"], row["is_wr"],
             row["oos_trades"], row["oos_net"], row["oos_pf"], row["oos_dd"],
             row["oos_wr"], row["passed"],
             is_start_iso, split_iso, end_iso, base_seed, dna,
             row["is_sharpe"], row["is_skew"], row["is_kurt"], row["oos_sharpe"], family))
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

    if dupes:
        print(f"  {len(dupes)} of {trials} trials repeat a parameter set already in candidates:")
        for trial, dna, (sid, spair, stf) in dupes[:5]:
            print(f"    trial {trial} dna={dna} first seen as #{sid} ({spair} {stf})")
        print("  Read: a repeat is the same hypothesis tested again, not new evidence.")
    else:
        print(f"  all {trials} parameter sets are new (no DNA seen before)")
    return survivors, is_start_iso, end_iso


def parse_args(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairs", default=",".join(collector.PAIRS),
                    help="comma list, each must be in main.PAIRS (default: all of them)")
    ap.add_argument("--tf", default="1h", choices=["1h", "4h"])
    ap.add_argument("--family", default=strategies.DEFAULT_FAMILY, choices=strategies.names(),
                    help="strategy family to search (app/strategies/)")
    ap.add_argument("--trials", type=int, default=300)
    ap.add_argument("--seed", type=int, default=42,
                    help="base seed; each pair gets base*1000 + its index in PAIRS")
    ap.add_argument("--backfill-sharpe", action="store_true",
                    help="fill is_sharpe/oos_sharpe/is_skew/is_kurt on old rows and exit")
    return ap.parse_args(argv)


def main():
    global NTFY_TOPIC
    import os
    NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")

    args = parse_args()

    conn = sqlite3.connect(DB_PATH)
    ensure_tables(conn)

    if args.backfill_sharpe:
        t0 = time.time()
        filled, bad = backfill_sharpe(conn)
        print(f"filled per-trial Sharpe on {filled} candidates ({time.time() - t0:.0f}s); "
              f"{len(bad)} not reproducible")
        for cid, stored, now in bad[:20]:
            print(f"  #{cid}: stored (is_trades, oos_trades)={stored}  rerun={now}  — left NULL")
        srs = [r[0] for r in conn.execute(
            "SELECT is_sharpe FROM candidates WHERE is_sharpe IS NOT NULL"
            " AND is_trades >= ?", (V.MIN_TRADES_FOR_SR_VARIANCE,))]
        if srs:
            print(f"IS Sharpe over {len(srs)} trials with >= {V.MIN_TRADES_FOR_SR_VARIANCE} "
                  f"trades: mean {V._mean(srs):+.3f}  var {V._std(srs) ** 2:.4f}")
        print("\nRead: every trial now carries its own IS Sharpe, so the deflated Sharpe")
        print("benchmark uses all trials instead of a 150-trial sample. Rows left NULL")
        print("could not be reproduced on their pinned windows and are excluded.")
        conn.close()
        return

    all_survivors = []
    for pair in [p.strip() for p in args.pairs.split(",")]:
        result = run_pair(conn, pair, args.tf, args.trials, args.seed, args.family)
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

    print(f"\n========== LEADERBOARD {args.family} (ranked by OOS profit factor) ==========")
    total = conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
    fam_total = conn.execute("SELECT COUNT(*) FROM candidates WHERE family=?",
                             (args.family,)).fetchone()[0]
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
    distinct = conn.execute("SELECT COUNT(DISTINCT dna) FROM candidates").fetchone()[0]
    print(f"(total candidates in table: {total}, of which {args.family}: {fam_total};"
          f" distinct parameter sets: {distinct})")
    print(f"Read: A2 deflates a {args.family} candidate by the {args.family} trials on its"
          f" pair/tf, so this run raised that bar for this family only.")
    print("\nInspect any candidate's parameters:")
    print("  SELECT params_json FROM candidates WHERE id=<ID>;")
    conn.close()


if __name__ == "__main__":
    main()
