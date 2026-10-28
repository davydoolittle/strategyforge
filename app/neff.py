"""
StrategyForge N_eff — effective number of independent trials for A2.

The deflated Sharpe benchmark E[max SR] grows with the number of trials N
and assumes they are independent. They are not: 900 random draws of the V4.1
family on one price history share most of their trades. This module
estimates N_eff (López de Prado, Machine Learning for Asset Managers ch. 8)
by clustering the trials of one pair/tf on the correlation of their return
series and counting the clusters (validation.effective_trials). Trials are
grouped by strategy family too (P5.2): each family is its own search.

Common window. Trials of one pair/tf were run on different pinned IS
windows (the 2026-09 runs on 2024-09 -> 2026-01, the 2026-10 run on
2021-05 -> 2025-03). Correlation needs one calendar, so every trial is
re-run on the pair/tf's REFERENCE window = the longest pinned IS window
among its trials, with warmup pinned to it. Each trial's trades are turned
into a daily return series (validation.bucket_returns) and stored in
trial_series, keyed by the reference window, so the job is resumable and only
new trials are run next time.

N_eff is information only. A2's verdict still uses N (adversary.py prints
both). See docs/FINDINGS.md.

Cost: one backtest per trial (~1 s at 1h on 2021-2025): ~15 min per 900
trials per worker. It writes one row per trial, so per V2_PROMPT §10 rule 9
it runs ON MATRIX via sf.sh, not over SMB.

Usage
  python neff.py                         # every pair/tf: fill series, cluster, cache
  python neff.py --pair BTC-USD --tf 1h --workers 2
  python neff.py --family core
  python neff.py --show                  # print cached results, run nothing
"""

import argparse
import json
import multiprocessing
import os
import sqlite3
import time

import engine
import lineage
import strategies
import validation as V
import windows as W

DB_PATH = os.environ.get("FORGE_DB", "/data/forge.db")
BUCKETS = {"daily": 1, "weekly": 7}       # name -> days per bucket


def ensure_tables(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS trial_series (
        candidate_id INTEGER, ref_start TEXT, ref_end TEXT,
        n_trades INTEGER, series_json TEXT,
        PRIMARY KEY (candidate_id, ref_start, ref_end)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS effective_trials (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        computed_at TEXT, pair TEXT, tf TEXT, bucket TEXT,
        ref_start TEXT, ref_end TEXT,
        n_trials INTEGER, n_series INTEGER, n_flat INTEGER, n_eff INTEGER,
        quality REAL, mean_abs_corr REAL, detail_json TEXT
    )""")
    conn.commit()
    lineage.ensure_family_column(conn, "effective_trials")
    lineage.ensure_family_column(conn, "candidates")


def reference_window(conn, pair, tf, family=strategies.DEFAULT_FAMILY):
    """Longest pinned IS window among this pair/tf/family's trials (latest
    split on a tie). Returns (is_start, split) or None."""
    rows = conn.execute(
        "SELECT DISTINCT is_start, split_ts FROM candidates"
        " WHERE pair=? AND tf=? AND family=? AND is_start IS NOT NULL",
        (pair, tf, family)).fetchall()
    if not rows:
        return None
    return max(rows, key=lambda r: (W._ts(r[1]) - W._ts(r[0]), W._ts(r[1])))


def encode(series):
    """Sparse JSON: only non-zero days are stored."""
    return json.dumps({"n": len(series),
                       "nz": [[i, v] for i, v in enumerate(series) if v != 0.0]})


def decode(text):
    d = json.loads(text)
    out = [0.0] * d["n"]
    for i, v in d["nz"]:
        out[i] = v
    return out


def trial_series(conn, pair, tf, params_json, ref):
    """(n_trades, daily series) of one trial re-run on the reference window."""
    tf_s = W.tf_seconds(tf)
    cfg = W.pin_cfg(json.loads(params_json), ref[0], tf_s)
    r = engine.backtest(conn, pair, cfg, ref[0], ref[1], tf_s)
    s = V.bucket_returns(r["trades"], cfg["initial_capital"], W._ts(ref[0]), W._ts(ref[1]))
    return r["n_trades"], s


def _work(job):
    cid, pair, tf, pj, ref = job
    conn = sqlite3.connect(f"file:{DB_PATH.replace(os.sep, '/')}?mode=ro", uri=True)
    try:
        n, s = trial_series(conn, pair, tf, pj, ref)
    finally:
        conn.close()
    return cid, n, encode(s)


def fill_series(conn, pair, tf, ref, workers=1, progress=True, family=strategies.DEFAULT_FAMILY):
    """Compute trial_series for every trial of pair/tf/family that has none
    for this reference window. Commits every 50 rows (resumable)."""
    todo = conn.execute(
        "SELECT c.id, c.params_json FROM candidates c WHERE c.pair=? AND c.tf=? AND c.family=?"
        " AND NOT EXISTS (SELECT 1 FROM trial_series t WHERE t.candidate_id=c.id"
        " AND t.ref_start=? AND t.ref_end=?) ORDER BY c.id",
        (pair, tf, family, ref[0], ref[1])).fetchall()
    if not todo:
        return 0
    jobs = [(cid, pair, tf, pj, ref) for cid, pj in todo]
    t0 = time.time()
    if workers > 1:
        pool = multiprocessing.Pool(workers)
        results = pool.imap_unordered(_work, jobs, chunksize=4)
    else:
        pool = None
        results = (_store_one(conn, j) for j in jobs)
    try:
        for k, (cid, n, js) in enumerate(results):
            conn.execute("INSERT OR REPLACE INTO trial_series VALUES (?, ?, ?, ?, ?)",
                         (cid, ref[0], ref[1], n, js))
            if k % 50 == 49:
                conn.commit()
                if progress:
                    print(f"  ...{k + 1}/{len(jobs)} ({(k + 1) / (time.time() - t0):.2f}/s)")
    finally:
        conn.commit()
        if pool:
            pool.close()
            pool.join()
    return len(jobs)


def _store_one(conn, job):
    cid, pair, tf, pj, ref = job
    n, s = trial_series(conn, pair, tf, pj, ref)
    return cid, n, encode(s)


def compute(conn, pair, tf, ref, bucket="daily", family=strategies.DEFAULT_FAMILY):
    """Cluster the stored series of pair/tf/family and cache the result."""
    rows = conn.execute(
        "SELECT t.candidate_id, t.series_json FROM trial_series t JOIN candidates c"
        " ON c.id=t.candidate_id WHERE c.pair=? AND c.tf=? AND c.family=?"
        " AND t.ref_start=? AND t.ref_end=? ORDER BY t.candidate_id",
        (pair, tf, family, ref[0], ref[1])).fetchall()
    n_trials = conn.execute("SELECT COUNT(*) FROM candidates WHERE pair=? AND tf=? AND family=?",
                            (pair, tf, family)).fetchone()[0]
    series = [decode(js) for _, js in rows]
    if BUCKETS[bucket] > 1:
        series = [V.rebucket(s, BUCKETS[bucket]) for s in series]
    t0 = time.time()
    res = V.effective_trials(series)
    sizes = {}
    for lab in res["labels"]:
        sizes[lab] = sizes.get(lab, 0) + 1
    detail = {"curve": res["curve"], "seconds": round(time.time() - t0, 1),
              "largest_clusters": sorted(sizes.values(), reverse=True)[:10],
              "labels": {str(cid): lab for (cid, _), lab in zip(rows, res["labels"])}}
    conn.execute(
        "INSERT INTO effective_trials (computed_at, pair, tf, bucket, ref_start, ref_end,"
        " n_trials, n_series, n_flat, n_eff, quality, mean_abs_corr, detail_json, family)"
        " VALUES (datetime('now'), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (pair, tf, bucket, ref[0], ref[1], n_trials, len(rows), res["n_flat"], res["n_eff"],
         res["quality"], res["mean_abs_corr"], json.dumps(detail), family))
    conn.commit()
    return {**res, "n_trials": n_trials, "n_series": len(rows), **detail}


def lookup(conn, pair, tf, bucket="daily", family=strategies.DEFAULT_FAMILY):
    """Latest cached N_eff for pair/tf/family, or None. Returns a dict with n_eff,
    n_series (trials it was computed on) and computed_at; the caller compares
    n_series to the current trial count to see whether it is stale."""
    try:
        row = conn.execute(
            "SELECT n_eff, n_series, n_trials, computed_at, ref_start, ref_end, mean_abs_corr"
            " FROM effective_trials WHERE pair=? AND tf=? AND bucket=? AND family=?"
            " ORDER BY id DESC LIMIT 1", (pair, tf, bucket, family)).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row:
        return None
    keys = ("n_eff", "n_series", "n_trials", "computed_at", "ref_start", "ref_end",
            "mean_abs_corr")
    return dict(zip(keys, row))


def show(conn):
    rows = conn.execute(
        "SELECT family, pair, tf, bucket, n_trials, n_series, n_flat, n_eff, quality,"
        " mean_abs_corr, computed_at FROM effective_trials e WHERE id = (SELECT MAX(id)"
        " FROM effective_trials WHERE pair=e.pair AND tf=e.tf AND bucket=e.bucket"
        " AND family=e.family) ORDER BY family, pair, tf, bucket").fetchall()
    if not rows:
        print("no N_eff computed yet — run `python neff.py`")
    for fam, p, tf, b, n, ns, nf, ne, q, mac, at in rows:
        print(f"  {fam:9s} {p:8s} {tf}  {b:6s}  N={n:4d}  series={ns:4d}  flat={nf:3d}  "
              f"N_eff={ne:4d}  quality={q if q is None else round(q, 2)}  "
              f"mean|rho|={mac if mac is None else round(mac, 3)}  ({at})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pair")
    ap.add_argument("--tf", choices=["1h", "4h"])
    ap.add_argument("--family", choices=strategies.names())
    ap.add_argument("--workers", type=int, default=1,
                    help="backtest processes (Matrix budget: <= 2)")
    ap.add_argument("--show", action="store_true", help="print cached results and exit")
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    ensure_tables(conn)
    if args.show:
        show(conn)
        conn.close()
        return
    combos = conn.execute("SELECT DISTINCT family, pair, tf FROM candidates"
                          " ORDER BY family, pair, tf").fetchall()
    combos = [(f, p, t) for f, p, t in combos
              if (args.pair is None or p == args.pair) and (args.tf is None or t == args.tf)
              and (args.family is None or f == args.family)]
    for fam, pair, tf in combos:
        ref = reference_window(conn, pair, tf, fam)
        if ref is None:
            print(f"[{fam} {pair} {tf}] no pinned windows; run `python windows.py --backfill`")
            continue
        print(f"[{fam} {pair} {tf}] reference window {ref[0]} -> {ref[1]}")
        t0 = time.time()
        n = fill_series(conn, pair, tf, ref, args.workers, family=fam)
        print(f"  {n} trial series computed ({time.time() - t0:.0f}s)")
        for bucket in BUCKETS:
            r = compute(conn, pair, tf, ref, bucket, family=fam)
            print(f"  {bucket:6s}: N={r['n_trials']}  N_eff={r['n_eff']}  "
                  f"(flat {r['n_flat']}, mean|rho| {r['mean_abs_corr'] or 0:.3f}, "
                  f"largest clusters {r['largest_clusters'][:5]}, {r['seconds']}s)")
    print()
    show(conn)
    print("\nRead: N_eff is how many independent bets the search really made. E[max SR]")
    print("with N_eff is the benchmark if the trials were that many independent draws.")
    print("It is INFORMATION ONLY: adversary A2 still decides on N. A small N_eff from a")
    print("family whose trials share trades is expected; it does not make an edge real.")
    conn.close()


if __name__ == "__main__":
    main()
