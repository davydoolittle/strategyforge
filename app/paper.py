"""
StrategyForge paper reconciliation (P4.1) — did the engine agree with the
TradingView alert?

davesbridge logs every authenticated webhook to its signals.db (read-only
here). A Pine script made by pine_export.py sends, on the close of a signal
bar where it also calls strategy.entry():

    {"strategy": "SF <lineage name>", "side": "long"|"short",
     "symbol": "BTCUSD", "tf": "60", "price": <close>}

For each such alert not yet reconciled:
  1. strategy  "SF <name>" -> strategies.name (lineage). Unknown names are
               stored with strategy_id NULL; alerts with no "strategy" field
               (manual tests, pre-P4 payloads) are counted and skipped.
  2. bar       the alert fires at bar close, so the signal bar is the one
               that ENDED at or before received_at: open = floor(t/tf)*tf - tf.
  3. engine    the strategy's cfg (pinned like the adversary's) is run on our
               own candles through that bar; engine_fired = 1 if it took an
               entry decision on that bar with the same side
               (engine.backtest()["entries"]). The engine never looks past
               bar i, so one run to the newest alert answers every alert.
  4. prices    price_alert = the Pine close in the payload; price_engine =
               our candle close for the same bar. A gap here is a data-feed
               difference (TradingView vs Coinbase API), not a logic one.
An alert whose bar is not in candles yet is left for the next run.

Stored in paper_trades (one row per davesbridge signal id; re-runs skip
rows already there). The report also lists engine entries that got NO
alert in the reconciled span — the other half of the agreement P4.3 needs.

Usage
  python paper.py                         # SIGNALS_DB, default /bridge/signals.db
  python paper.py --signals-db W:/davesbridge/data/signals.db
"""

import argparse
import json
import os
import sqlite3
from datetime import datetime, timezone

import engine
import lineage
import windows as W

DB_PATH = os.environ.get("FORGE_DB", "/data/forge.db")
SIGNALS_DB = os.environ.get("SIGNALS_DB", "/bridge/signals.db")
NAME_PREFIX = "SF "
TV_PERIOD = {3600: "60", 14400: "240"}     # Pine timeframe.period


def ensure_tables(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS paper_trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        strategy_id INTEGER,           -- NULL: alert named no known strategy
        signal_id INTEGER UNIQUE,      -- davesbridge signals.id
        alert_ts INTEGER,              -- davesbridge received_at, unix UTC
        bar_ts INTEGER,                -- open time of the signal bar
        engine_fired INTEGER,          -- 1 = engine took the same-side entry on bar_ts
        side TEXT,
        price_alert REAL, price_engine REAL,
        symbol TEXT, tf TEXT,
        note TEXT,
        reconciled_at TEXT
    )""")
    conn.commit()


def open_signals(path):
    if not os.path.exists(path):
        raise SystemExit(f"signals db not found at {path}. On Matrix, sf.sh mounts davesbridge's "
                         f"data dir read-only at /bridge; elsewhere pass --signals-db.")
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _ts(received_at):
    return int(datetime.strptime(received_at, "%Y-%m-%d %H:%M:%S")
               .replace(tzinfo=timezone.utc).timestamp())


def _iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def new_alerts(conn, sconn):
    """(alerts with a strategy field, count skipped for having none), oldest first."""
    done = {r[0] for r in conn.execute("SELECT signal_id FROM paper_trades")}
    out, skipped = [], 0
    for sid, received_at, raw in sconn.execute(
            "SELECT id, received_at, raw_payload FROM signals ORDER BY id"):
        if sid in done:
            continue
        try:
            p = json.loads(raw)
        except (TypeError, ValueError):
            skipped += 1
            continue
        if not isinstance(p, dict) or not p.get("strategy"):
            skipped += 1
            continue
        out.append({"signal_id": sid, "alert_ts": _ts(received_at), "payload": p})
    return out, skipped


def strategy_by_name(conn, title):
    name = title[len(NAME_PREFIX):] if title.startswith(NAME_PREFIX) else title
    r = conn.execute("SELECT id, candidate_id, pair, tf, params_json FROM strategies WHERE name=?",
                     (name,)).fetchone()
    if not r:
        return None
    return {"id": r[0], "candidate_id": r[1], "pair": r[2], "tf": r[3],
            "cfg": json.loads(r[4]), "name": name}


def engine_entries(conn, strat, through_ts):
    """Entry decisions of the strategy's cfg from its pinned IS start through
    the bar opening at through_ts, keyed by bar ts."""
    tf_s = W.tf_seconds(strat["tf"])
    is_start = W.for_candidate(conn, strat["candidate_id"])[0]
    cfg = W.pin_cfg(strat["cfg"], is_start, tf_s)
    r = engine.backtest(conn, strat["pair"], cfg, is_start, _iso(through_ts), tf_s)
    return {e["ts"]: e for e in r["entries"]}


def _price(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def reconcile(conn, sconn, now=None):
    """Store one paper_trades row per new alert whose bar we have candles for.
    Returns a summary dict for the report."""
    ensure_tables(conn)
    lineage.ensure_tables(conn)
    alerts, skipped = new_alerts(conn, sconn)
    now = now or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    by_strategy, unknown, waiting = {}, [], 0
    for a in alerts:
        s = strategy_by_name(conn, a["payload"]["strategy"])
        if s is None:
            unknown.append(a)
        else:
            by_strategy.setdefault(s["id"], (s, []))[1].append(a)

    rows = []
    for a in unknown:
        p = a["payload"]
        rows.append((None, a["signal_id"], a["alert_ts"], None, None, p.get("side"),
                     _price(p.get("price")), None, p.get("symbol"), p.get("tf"),
                     f"no strategy named {p['strategy']!r}", now))

    engine_only = {}
    for sid, (s, alist) in by_strategy.items():
        tf_s = W.tf_seconds(s["tf"])
        newest = conn.execute("SELECT MAX(ts) FROM candles WHERE pair=?", (s["pair"],)).fetchone()[0]
        for a in alist:
            a["bar_ts"] = a["alert_ts"] // tf_s * tf_s - tf_s
        ready = [a for a in alist if newest is not None and a["bar_ts"] <= newest]
        waiting += len(alist) - len(ready)
        if not ready:
            continue
        hi = max(a["bar_ts"] for a in ready)
        # one bar past the newest alert (if we have it) so the +1-bar check can see it
        ent = engine_entries(conn, s, min(hi + tf_s, newest))
        alerted = set()
        for a in ready:
            p = a["payload"]
            e = ent.get(a["bar_ts"])
            fired = e is not None and e["side"] == p.get("side")
            notes = []
            if e is not None and not fired:
                notes.append(f"engine entered {e['side']} on that bar")
            if not fired:
                near = [x for x in (a["bar_ts"] - tf_s, a["bar_ts"] + tf_s)
                        if x in ent and ent[x]["side"] == p.get("side")]
                if near:
                    notes.append(f"engine fired same side {(near[0] - a['bar_ts']) // tf_s:+d} bar")
            want_sym = s["pair"].replace("-", "")
            if p.get("symbol") and p["symbol"] != want_sym:
                notes.append(f"symbol {p['symbol']} != {want_sym}")
            if p.get("tf") and p["tf"] != TV_PERIOD.get(tf_s, p["tf"]):
                notes.append(f"tf {p['tf']} != {TV_PERIOD.get(tf_s)}")
            # the bar's close = the close of its last hourly candle (1h or 4h)
            close = conn.execute("SELECT close FROM candles WHERE pair=? AND ts=?",
                                 (s["pair"], a["bar_ts"] + tf_s - 3600)).fetchone()
            price_engine = e["close"] if e is not None else (close[0] if close else None)
            rows.append((sid, a["signal_id"], a["alert_ts"], a["bar_ts"], int(fired), p.get("side"),
                         _price(p.get("price")), price_engine, p.get("symbol"), p.get("tf"),
                         "; ".join(notes) or None, now))
            alerted.add(a["bar_ts"])
        alerted |= {r[0] for r in conn.execute(
            "SELECT bar_ts FROM paper_trades WHERE strategy_id=?", (sid,))}
        lo = min(a["bar_ts"] for a in ready)
        engine_only[sid] = [e for t, e in sorted(ent.items())
                            if lo <= t <= hi and t not in alerted]

    conn.executemany(
        "INSERT INTO paper_trades (strategy_id, signal_id, alert_ts, bar_ts, engine_fired, side,"
        " price_alert, price_engine, symbol, tf, note, reconciled_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    conn.commit()
    return {"new_rows": len(rows), "skipped_no_strategy_field": skipped,
            "waiting_for_candles": waiting, "unknown": len(unknown),
            "engine_only": engine_only}


def print_report(conn, summary):
    print("\n" + "=" * 78)
    print("PAPER RECONCILIATION (P4.1) — TradingView alerts vs engine")
    print("=" * 78)
    print(f"this run: {summary['new_rows']} alerts reconciled, "
          f"{summary['waiting_for_candles']} waiting for candles, "
          f"{summary['unknown']} naming no known strategy, "
          f"{summary['skipped_no_strategy_field']} without a 'strategy' field (tests / pre-P4)")
    rows = conn.execute(
        "SELECT p.strategy_id, s.name, COUNT(*), SUM(p.engine_fired), MIN(p.alert_ts),"
        " MAX(p.alert_ts) FROM paper_trades p LEFT JOIN strategies s ON s.id = p.strategy_id"
        " GROUP BY p.strategy_id ORDER BY p.strategy_id").fetchall()
    if not rows:
        print("\nNo strategy alerts have reached davesbridge yet. Nothing is deployed: export a")
        print("lineage strategy (pine_export.py --strategy N), add it to a TradingView chart, and")
        print("point an alert at the davesbridge webhook. Nothing here can be validated until then.")
        return
    print(f"\n{'strategy':28s} {'alerts':>6s} {'engine agreed':>13s} {'agree':>6s} "
          f"{'med |px diff| bps':>17s}  span")
    for sid, name, n, agreed, t0, t1 in rows:
        if sid is None:
            print(f"{'(unknown strategy name)':28s} {n:6d}")
            continue
        diffs = sorted(abs(pa - pe) / pe * 1e4 for pa, pe in conn.execute(
            "SELECT price_alert, price_engine FROM paper_trades WHERE strategy_id=?"
            " AND price_alert IS NOT NULL AND price_engine", (sid,)))
        med = f"{diffs[len(diffs) // 2]:.1f}" if diffs else "n/a"
        print(f"#{sid:<3d}{name[:24]:25s} {n:6d} {agreed or 0:13d} {100.0 * (agreed or 0) / n:5.0f}% "
              f"{med:>17s}  {_iso(t0)[:16]} -> {_iso(t1)[:16]}")
    for sid, ents in summary["engine_only"].items():
        if ents:
            print(f"  #{sid}: engine took {len(ents)} entries with no alert in this run's span: "
                  + ", ".join(f"{_iso(e['ts'])[:16]} {e['side']}" for e in ents[:5])
                  + (" ..." if len(ents) > 5 else ""))
    print("\nRead: 'engine agreed' = on the alert's bar our engine, on our candles, took the")
    print("same-side entry. Disagreement means the Pine port, the data feed or the alert")
    print("timing differs; the note column says which bar the engine fired on instead. Price")
    print("gaps are TradingView's feed vs Coinbase's API. Engine entries with no alert are")
    print("missed or unconfigured alerts. P4.3 promotion needs >= 90% agreement over")
    print(">= 30 reconciled trades and >= 60 days; nothing here is P&L yet (that is P4.2).")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--signals-db", default=SIGNALS_DB)
    args = ap.parse_args()
    sconn = open_signals(args.signals_db)
    conn = sqlite3.connect(DB_PATH)
    ensure_tables(conn)
    summary = reconcile(conn, sconn)
    print_report(conn, summary)
    conn.close()
    sconn.close()


if __name__ == "__main__":
    main()
