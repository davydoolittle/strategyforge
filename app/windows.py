"""
StrategyForge windows — pinned IS/OOS windows per candidate (P2.1).

Before v2 every consumer recomputed the 70/30 split from MAX(ts) of the
candles table, so a candidate's "OOS" quietly moved every time the collector
added a bar (#1033: OOS PF 2.86 when stored, 1.73 six weeks later). Now the
optimizer stores the three timestamps it actually used, and adversary.py /
analyze.py / validation.py read them back.

Columns on candidates (ISO-8601 UTC, the format engine.backtest() takes):
  is_start            first bar the IS backtest may trade (after warmup)
  split_ts            IS end / OOS start
  oos_end             last bar the optimizer could see
  windows_backfilled  1 = reconstructed from run_at by --backfill, not
                      recorded at insert time

Usage
  python windows.py --backfill            # fill rows that have no windows
  python windows.py --verify [--limit N]  # re-run passed candidates on their
                                          # pinned windows, compare to stored stats
"""

import argparse
import json
import os
import sqlite3
import time
from datetime import datetime, timezone

import engine

DB_PATH = os.environ.get("FORGE_DB", "/data/forge.db")
WF_SPLIT = 0.70

COLUMNS = [("is_start", "TEXT"), ("split_ts", "TEXT"), ("oos_end", "TEXT"),
           ("windows_backfilled", "INTEGER DEFAULT 0")]


def _iso(t):
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _ts(iso):
    return int(datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp())


def tf_seconds(tf):
    return 3600 if tf == "1h" else 14400


def ensure_columns(conn):
    """Add the window columns to candidates if they are missing. No-op when
    the candidates table does not exist yet (optimizer creates it)."""
    have = {r[1] for r in conn.execute("PRAGMA table_info(candidates)")}
    if not have:
        return
    for name, typ in COLUMNS:
        if name not in have:
            conn.execute(f"ALTER TABLE candidates ADD COLUMN {name} {typ}")
    conn.commit()


def compute(conn, pair, tf_secs, as_of_ts=None):
    """The optimizer's split rule over the candles that existed at as_of_ts
    (default: now). Returns (is_start, split, end) as ISO strings, or None
    if the pair has no candles."""
    if as_of_ts is None:
        lo, hi = conn.execute(
            "SELECT MIN(ts), MAX(ts) FROM candles WHERE pair=?", (pair,)).fetchone()
    else:
        lo, hi = conn.execute(
            "SELECT MIN(ts), MAX(ts) FROM candles WHERE pair=? AND ts<=?",
            (pair, as_of_ts)).fetchone()
    if lo is None:
        return None
    is_start = lo + engine.WARMUP_BARS * tf_secs
    split = lo + int((hi - lo) * WF_SPLIT)
    return _iso(is_start), _iso(split), _iso(hi)


def for_candidate(conn, cid):
    """Pinned (is_start, split, end) for a candidate. Raises if the row has
    no windows: silently recomputing from today's data is exactly the drift
    this module exists to stop."""
    row = conn.execute(
        "SELECT is_start, split_ts, oos_end FROM candidates WHERE id=?", (cid,)).fetchone()
    if row is None:
        raise SystemExit(f"candidate {cid} not found")
    if any(x is None for x in row):
        raise SystemExit(f"candidate {cid} has no pinned windows — run "
                         f"`python windows.py --backfill` first")
    return row


def backfill(conn):
    """Reconstruct windows for rows that have none from run_at: the candle
    range is cut at the last bar whose ts <= run_at. Approximate — the
    collector may have been a bar behind — so rows are marked
    windows_backfilled=1. A one-bar error moves the split by < 1 h."""
    ensure_columns(conn)
    rows = conn.execute(
        "SELECT id, run_at, pair, tf FROM candidates WHERE is_start IS NULL"
        " OR split_ts IS NULL OR oos_end IS NULL").fetchall()
    cache = {}
    n = 0
    for cid, run_at, pair, tf in rows:
        key = (pair, tf, run_at)
        if key not in cache:
            w = compute(conn, pair, tf_seconds(tf), _ts(run_at.replace(" ", "T")))
            if w is None:
                print(f"  #{cid}: no candles for {pair} before {run_at}, left empty")
            cache[key] = w
        w = cache[key]
        if w is None:
            continue
        conn.execute("UPDATE candidates SET is_start=?, split_ts=?, oos_end=?,"
                     " windows_backfilled=1 WHERE id=?", (*w, cid))
        n += 1
    conn.commit()
    return n, len(rows)


def verify(conn, limit=None):
    """Re-run passed candidates on their pinned windows and compare trade
    count and net with what the optimizer stored. A mismatch means the
    pinned windows are not the ones the optimizer used."""
    q = ("SELECT id, pair, tf, params_json, is_trades, is_net, oos_trades, oos_net,"
         " is_start, split_ts, oos_end FROM candidates WHERE passed=1 ORDER BY id")
    if limit:
        q += f" LIMIT {int(limit)}"
    ok, bad = 0, []
    for cid, pair, tf, pj, ist, isn, oot, oon, s, sp, e in conn.execute(q).fetchall():
        cfg = json.loads(pj)
        a = engine.backtest(conn, pair, cfg, s, sp, tf_seconds(tf))
        b = engine.backtest(conn, pair, cfg, sp, e, tf_seconds(tf))
        if (a["n_trades"] == ist and abs(a["net"] - isn) < 0.01
                and b["n_trades"] == oot and abs(b["net"] - oon) < 0.01):
            ok += 1
        else:
            bad.append((cid, pair, tf, (ist, isn, oot, oon),
                        (a["n_trades"], a["net"], b["n_trades"], b["net"])))
    return ok, bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--limit", type=int)
    args = ap.parse_args()
    if not (args.backfill or args.verify):
        ap.error("give --backfill and/or --verify")

    conn = sqlite3.connect(DB_PATH)
    ensure_columns(conn)
    if args.backfill:
        n, todo = backfill(conn)
        total = conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
        print(f"backfilled {n} of {todo} candidates without windows ({total} in table)")
        for r in conn.execute(
                "SELECT tf, pair, MIN(split_ts), MAX(oos_end), COUNT(*) FROM candidates"
                " WHERE windows_backfilled=1 GROUP BY tf, pair ORDER BY tf, pair"):
            print(f"  {r[0]} {r[1]}  split {r[2]}  oos_end {r[3]}  ({r[4]} rows)")
        print("\nRead: these are the windows the optimizer most likely used, rebuilt")
        print("from run_at. They are marked windows_backfilled=1 because the cut is")
        print("inferred, not recorded. Run --verify to check them against stored stats.")
    if args.verify:
        t0 = time.time()
        ok, bad = verify(conn, args.limit)
        print(f"\nverify: {ok} of {ok + len(bad)} passed candidates reproduce their stored "
              f"IS+OOS trade count and net to the cent ({time.time() - t0:.0f}s)")
        for cid, pair, tf, stored, now in bad[:20]:
            print(f"  MISMATCH #{cid} {pair} {tf}  stored {stored}  pinned-window rerun {now}")
        if bad:
            print("\nRead: a mismatch means the pinned window is not the one the optimizer")
            print("used. Results for those candidates are not reproducible; treat their")
            print("stored stats as unverified.")
        else:
            print("\nRead: the pinned windows reproduce what the optimizer saw. Adversary and")
            print("analyze results on these candidates no longer drift as candles arrive.")
    conn.close()


if __name__ == "__main__":
    main()
