"""
StrategyForge decay monitor (P4.4) — is a validated strategy still behaving
like it did in its validation window?

For every strategy at oos_validated or above (not retired):
  1. baseline  the engine runs the strategy's pinned cfg from is_start to the
               newest candle; trades with split_ts <= signal < oos_end (the
               OOS window the adversary judged) give the mean mu and stdev
               sigma of per-trade returns. Return = pnl / equity before the
               trade, in %, because the engine sizes off running equity.
  2. forward   the trades since validation: the paper trades TradingView's
               alerts opened (paper.simulate) when the strategy has any,
               otherwise the engine's own trades with signal >= oos_end
               (candles the optimizer never saw). `source` says which.
  3. CUSUM     lower one-sided tabular CUSUM of z = (r - mu) / sigma:
                   S = min(0, S + z + K),  alarm when S <= -H
               K = 0.5 sigma (slack: drift smaller than half a sigma per trade
               is ignored), H = 3 (the spec's -3 sigma). The whole path is
               recomputed every run; alert = 1 once the path has ever crossed,
               so a breach is not lost if it recovers between daily runs.
               Simulated run lengths (K=0.5, H=3): with no decay it still
               false-alarms after ~117 trades on average for normal
               returns (~390 for a 40%-win, 2.4R : 1R mix, whose losses are
               capped). If that mix decays to 30% wins (edge ~0), the alarm
               takes ~105 trades on average (median 76); at 25% wins ~59.
               It is slow on purpose; an alert means "look", not "retire".
  4. ntfy      on the first day a strategy is in alert, until the push is
               delivered (notified = 1). No topic, or a failed push, is
               printed loudly and retried on the next run.

Written to health (strategy_id, day, cusum, alert, ...), one row per
strategy per UTC day; a second run on the same day replaces it.

Usage
  python health.py                    # all strategies at oos_validated+
  python health.py --strategy 3       # one strategy, whatever its status
  python health.py --no-ntfy          # never push
NTFY_TOPIC (env) or --ntfy-topic sets the topic.
"""

import argparse
import json
import os
import sqlite3
import statistics
import urllib.request
from datetime import datetime, timezone

import engine
import lineage
import paper
import windows as W

DB_PATH = os.environ.get("FORGE_DB", "/data/forge.db")
WATCH = lineage.LADDER[lineage.LADDER.index("oos_validated"):]
CUSUM_K = 0.5
CUSUM_H = 3.0
MIN_BASELINE_TRADES = 10


def ensure_tables(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS health (
        strategy_id INTEGER, day TEXT,
        cusum REAL,                    -- S after the newest forward trade (NULL: no baseline)
        alert INTEGER,                 -- 1 = the path has crossed -H
        source TEXT,                   -- paper | engine
        n_baseline INTEGER, mu REAL, sigma REAL,
        n_forward INTEGER, min_cusum REAL, breach_ts INTEGER,
        notified INTEGER DEFAULT 0,
        detail TEXT,
        measured_at TEXT,
        PRIMARY KEY (strategy_id, day)
    )""")
    conn.commit()


def returns(trades, capital):
    """[(trade, pnl as % of equity before it)], trades in exit order."""
    eq, out = capital, []
    for t in trades:
        out.append((t, 100.0 * t["pnl"] / eq))
        eq += t["pnl"]
    return out


def cusum(z, k=CUSUM_K, h=CUSUM_H):
    """Lower tabular CUSUM. Returns (final S, min S, index of first breach or None)."""
    s, lo, breach = 0.0, 0.0, None
    for i, x in enumerate(z):
        s = min(0.0, s + x + k)
        lo = min(lo, s)
        if breach is None and s <= -h:
            breach = i
    return s, lo, breach


def measure(conn, strat):
    """One health row (without day / notified) for a strategy dict from
    paper.strategy_by_name."""
    tf_s = W.tf_seconds(strat["tf"])
    is_start, split, oos_end = W.for_candidate(conn, strat["candidate_id"])
    cfg = W.pin_cfg(strat["cfg"], is_start, tf_s)
    newest = conn.execute("SELECT MAX(ts) FROM candles WHERE pair=?", (strat["pair"],)).fetchone()[0]
    r = engine.backtest(conn, strat["pair"], cfg, is_start, W._iso(newest), tf_s)
    split_ts, end_ts = W._ts(split), W._ts(oos_end)
    rets = returns(r["trades"], cfg["initial_capital"])
    base = [x for t, x in rets if split_ts <= t["signal_ts"] < end_ts]

    paper.ensure_tables(conn)
    alerts = paper.own_alerts(conn, strat)
    if alerts:
        source = "paper"
        fwd = returns(paper.simulate(conn, strat, alerts, newest)["trades"], cfg["initial_capital"])
    else:
        source = "engine"
        fwd = [(t, x) for t, x in rets if t["signal_ts"] >= end_ts]
    row = {"strategy_id": strat["id"], "source": source, "n_baseline": len(base),
           "mu": None, "sigma": None, "n_forward": len(fwd), "cusum": None,
           "min_cusum": None, "breach_ts": None, "alert": 0, "detail": None}
    if len(base) < MIN_BASELINE_TRADES:
        row["detail"] = f"only {len(base)} validation-window trades (< {MIN_BASELINE_TRADES}): no baseline"
        return row
    mu, sigma = statistics.mean(base), statistics.stdev(base)
    if sigma <= 0:
        row["detail"] = "validation-window returns have zero spread: no baseline"
        return row
    s, lo, breach = cusum([(x - mu) / sigma for _, x in fwd], CUSUM_K, CUSUM_H)
    row.update(mu=mu, sigma=sigma, cusum=s, min_cusum=lo, alert=int(breach is not None),
               breach_ts=fwd[breach][0]["exit_ts"] if breach is not None else None)
    if not fwd:
        row["detail"] = "no trades since the validation window"
    return row


def send_ntfy(topic, title, msg):
    """True if ntfy.sh accepted the push."""
    if not topic:
        return False
    try:
        req = urllib.request.Request(f"https://ntfy.sh/{topic}", data=msg.encode(),
                                     headers={"Title": title, "Priority": "high",
                                              "Tags": "warning"})
        with urllib.request.urlopen(req, timeout=10):
            return True
    except Exception as e:  # noqa: BLE001 — reported, retried next run
        print(f"!! ntfy push failed: {e}")
        return False


def run(conn, strategy_ids=None, day=None, topic=None, push=True):
    """Measure, store and (on a new breach) push. Returns the stored rows."""
    ensure_tables(conn)
    paper.ensure_tables(conn)
    lineage.ensure_tables(conn)
    day = day or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    if strategy_ids is None:
        q = ",".join("?" * len(WATCH))
        strategy_ids = [r[0] for r in conn.execute(
            f"SELECT id FROM strategies WHERE status IN ({q}) ORDER BY id", WATCH)]
    out = []
    for sid in strategy_ids:
        name = conn.execute("SELECT name FROM strategies WHERE id=?", (sid,)).fetchone()
        if not name:
            print(f"strategy {sid} not found")
            continue
        strat = paper.strategy_by_name(conn, name[0])
        try:
            row = measure(conn, strat)
        except (SystemExit, RuntimeError) as e:     # no pinned windows / no candles
            row = {"strategy_id": sid, "source": None, "n_baseline": 0, "mu": None,
                   "sigma": None, "n_forward": 0, "cusum": None, "min_cusum": None,
                   "breach_ts": None, "alert": 0, "detail": f"not measured: {e}"}
        delivered = conn.execute("SELECT 1 FROM health WHERE strategy_id=? AND notified=1",
                                 (sid,)).fetchone() is not None
        row["notified"] = int(delivered)
        if row["alert"] and not delivered:
            msg = (f"#{sid} {strat['name']}: decay CUSUM {row['min_cusum']:.2f} crossed "
                   f"-{CUSUM_H:g} sigma on {row['n_forward']} {row['source']} trades "
                   f"(validation mean {row['mu']:+.3f}%/trade). Look before it trades on.")
            if not push:
                print(f"(--no-ntfy) would push: {msg}")
            elif not topic:
                print(f"!! NTFY_TOPIC not set: alert NOT sent: {msg}")
            else:
                row["notified"] = int(send_ntfy(topic, "StrategyForge decay", msg))
        row.update(name=strat["name"], day=day, measured_at=now)
        conn.execute(
            "INSERT OR REPLACE INTO health (strategy_id, day, cusum, alert, source, n_baseline,"
            " mu, sigma, n_forward, min_cusum, breach_ts, notified, detail, measured_at)"
            " VALUES (:strategy_id, :day, :cusum, :alert, :source, :n_baseline, :mu, :sigma,"
            " :n_forward, :min_cusum, :breach_ts, :notified, :detail, :measured_at)", row)
        out.append(row)
    conn.commit()
    return out


def print_report(rows):
    print("\n" + "=" * 78)
    print(f"DECAY MONITOR (P4.4) — CUSUM of forward trades vs validation window "
          f"(K={CUSUM_K:g}, H={CUSUM_H:g})")
    print("=" * 78)
    if not rows:
        print(f"No strategy is at {' / '.join(WATCH)}: nothing to watch.")
        return
    f = lambda x, p=3: "-" if x is None else f"{x:+.{p}f}"   # noqa: E731
    print(f"{'strategy':28s} {'source':6s} {'base n':>6s} {'mu %':>7s} {'sigma':>6s} "
          f"{'fwd n':>5s} {'cusum':>6s} {'min':>6s}  alert")
    for r in rows:
        sigma = "-" if r["sigma"] is None else f"{r['sigma']:.3f}"
        flag = ""
        if r["alert"]:
            flag = f"BREACH {W._iso(r['breach_ts'])[:10]}" + ("" if r["notified"] else " (not pushed)")
        print(f"#{r['strategy_id']:<3d}{r['name'][:24]:25s} {r['source'] or '-':6s} "
              f"{r['n_baseline']:6d} {f(r['mu']):>7s} {sigma:>6s} {r['n_forward']:5d} "
              f"{f(r['cusum'], 2):>6s} {f(r['min_cusum'], 2):>6s}  {flag}")
        if r["detail"]:
            print(f"      {r['detail']}")
    print("\nRead: each forward trade adds (r - mu)/sigma + 0.5 to a running sum capped at 0;")
    print("a run of below-baseline trades drags it down, and crossing -3 is the alert. It is")
    print("slow (an edge that falls to zero takes ~75-105 trades to flag) and with no decay")
    print("still fires about once per 120-390 trades, so an alert means look, not retire.")
    print("Source 'engine' = no paper alerts yet: the engine's own trades on new candles.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", type=int, action="append",
                    help="check this strategy whatever its status (repeatable)")
    ap.add_argument("--ntfy-topic", default=os.environ.get("NTFY_TOPIC", ""))
    ap.add_argument("--no-ntfy", action="store_true")
    args = ap.parse_args()
    conn = sqlite3.connect(DB_PATH)
    rows = run(conn, args.strategy, topic=args.ntfy_topic, push=not args.no_ntfy)
    print_report(rows)
    conn.close()


if __name__ == "__main__":
    main()
