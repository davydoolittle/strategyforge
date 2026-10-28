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

Paper P&L (P4.2). The paper position is the one TradingView holds: the
strategy's alerts (for its own symbol and timeframe, agreed or not) are fed
to engine.backtest as its entry signals, so fills (next bar open + slippage),
risk sizing, stop/target/trailing exits, flips and fees are the engine's own,
starting at initial_capital on the first alert. Rebuilt from scratch every
run, so a partial 4h bar or a late candle never leaves stale state:
  paper_positions  one row per alert: closed (realised pnl), open (marked to
                   the newest hourly close; unrealised = pnl if closed at
                   market now), pending (fills next bar) or ignored (already
                   on that side)
  paper_summary    per strategy per UTC day from the first alert to today:
                   closed trades by exit day, net, pf, and pf / count / net
                   over the trailing 30 days (pf NULL = no losing trade)

Usage
  python paper.py                         # SIGNALS_DB, default /bridge/signals.db
  python paper.py --signals-db W:/davesbridge/data/signals.db
  python paper.py --evidence 3            # P4.3 checks for strategy #3, read-only

Promotion evidence (P4.3): `lineage promote --to paper_validated --evidence
paper:<id>` calls evidence() and refuses unless the strategy has >= 30
reconciled alerts spanning >= 60 days, engine/alert agreement >= 90 % (engine
entries in that span that never got an alert count as disagreements), and a
30-day paper PF >= 1.2 at the newest candle.
"""

import argparse
import json
import os
import sqlite3
from datetime import datetime, timezone

import engine
import lineage
import strategies
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
    conn.execute("""CREATE TABLE IF NOT EXISTS paper_positions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        strategy_id INTEGER, signal_id INTEGER, bar_ts INTEGER, side TEXT,
        status TEXT,                   -- closed | open | pending | ignored
        entry_ts INTEGER, entry REAL, qty REAL,
        exit_ts INTEGER, exit REAL, reason TEXT,
        pnl REAL,                      -- realised (closed only)
        mark_ts INTEGER, mark REAL, unrealised REAL,   -- open only
        updated_at TEXT
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS paper_summary (
        strategy_id INTEGER, day TEXT,
        n_trades INTEGER, net REAL, pf REAL,
        rolling_30d_pf REAL, rolling_30d_trades INTEGER, rolling_30d_net REAL,
        cum_net REAL,
        PRIMARY KEY (strategy_id, day)
    )""")
    conn.commit()


def open_signals(path):
    if not os.path.exists(path):
        raise SystemExit(f"signals db not found at {path}. On Matrix, sf.sh mounts davesbridge's "
                         f"data dir read-only at /bridge; the collector container needs the same "
                         f"mount (-v /mnt/user/appdata/davesbridge/data:/bridge:ro) for its "
                         f"hourly paper job; elsewhere pass --signals-db.")
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
    lineage.ensure_family_column(conn, "strategies")
    r = conn.execute("SELECT id, candidate_id, pair, tf, params_json, family FROM strategies"
                     " WHERE name=?", (name,)).fetchone()
    if not r:
        return None
    cfg = json.loads(r[4])
    # engine.backtest dispatches on the cfg's family (P5.2); the column must agree
    if strategies.family_of(cfg) != r[5]:
        raise SystemExit(f"strategy {name!r}: family column {r[5]!r} but params are "
                         f"{strategies.family_of(cfg)!r}")
    return {"id": r[0], "candidate_id": r[1], "pair": r[2], "tf": r[3],
            "cfg": cfg, "name": name, "family": r[5]}


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


# ------------------------------ P4.2 paper P&L ------------------------------

DAY = 86400
ROLLING_DAYS = 30


def _day(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def _pf(pnls):
    """Profit factor; None when there is no losing trade (or no trade)."""
    loss = -sum(p for p in pnls if p <= 0)
    return sum(p for p in pnls if p > 0) / loss if loss > 0 else None


def own_alerts(conn, strat):
    """{bar_ts: (side, signal_id)}: this strategy's reconciled alerts that came
    from its own symbol and timeframe. The first alert on a bar wins."""
    tf_s = W.tf_seconds(strat["tf"])
    sym, per = strat["pair"].replace("-", ""), TV_PERIOD.get(tf_s)
    out = {}
    for sig_id, bar_ts, side, symbol, tf in conn.execute(
            "SELECT signal_id, bar_ts, side, symbol, tf FROM paper_trades"
            " WHERE strategy_id=? AND bar_ts IS NOT NULL ORDER BY signal_id", (strat["id"],)):
        if side not in ("long", "short") or (symbol and symbol != sym) or (tf and per and tf != per):
            continue
        out.setdefault(bar_ts, (side, sig_id))
    return out


def simulate(conn, strat, alerts, through_ts):
    """engine.backtest driven by the alerts, from the first alert through the
    bar holding through_ts (the newest hourly candle)."""
    tf_s = W.tf_seconds(strat["tf"])
    is_start = W.for_candidate(conn, strat["candidate_id"])[0]
    cfg = W.pin_cfg(strat["cfg"], is_start, tf_s)
    return engine.backtest(conn, strat["pair"], cfg, _iso(min(alerts)), _iso(through_ts), tf_s,
                           signals={t: side for t, (side, _) in alerts.items()})


def position_rows(strat, alerts, r, through_ts, now):
    by_signal = {t["signal_ts"]: t for t in r["trades"]}
    op, pend = r["open_position"], r["pending_entry"]
    rows = []
    for bar_ts, (side, sig_id) in sorted(alerts.items()):
        row = dict(strategy_id=strat["id"], signal_id=sig_id, bar_ts=bar_ts, side=side,
                   status="ignored", entry_ts=None, entry=None, qty=None, exit_ts=None,
                   exit=None, reason=None, pnl=None, mark_ts=None, mark=None,
                   unrealised=None, updated_at=now)
        t = by_signal.get(bar_ts)
        if t is not None:
            row.update(status="closed", entry_ts=t["entry_ts"], entry=t["entry"], qty=t["qty"],
                       exit_ts=t["exit_ts"], exit=t["exit"], reason=t["reason"], pnl=t["pnl"])
        elif op is not None and op["signal_ts"] == bar_ts:
            row.update(status="open", entry_ts=op["entry_ts"], entry=op["entry"], qty=op["qty"],
                       mark_ts=through_ts, mark=op["mark"], unrealised=op["unrealised"])
        elif pend is not None and pend["signal_ts"] == bar_ts:
            row.update(status="pending")
        else:
            row.update(reason=f"already {side}")
        rows.append(row)
    return rows


def daily_summary(closed, first_ts, last_ts):
    """closed: [(exit_ts, pnl)]. One dict per UTC day first..last inclusive,
    trades counted on their exit day."""
    by_day = {}
    for ts, pnl in closed:
        by_day.setdefault(_day(ts), []).append(pnl)
    out, cum = [], 0.0
    d = first_ts // DAY * DAY
    while d <= last_ts:
        today = by_day.get(_day(d), [])
        window = [p for k in range(ROLLING_DAYS) for p in by_day.get(_day(d - k * DAY), [])]
        cum += sum(today)
        out.append({"day": _day(d), "n_trades": len(today), "net": sum(today), "pf": _pf(today),
                    "rolling_30d_pf": _pf(window), "rolling_30d_trades": len(window),
                    "rolling_30d_net": sum(window), "cum_net": cum})
        d += DAY
    return out


def update_pnl(conn, now=None):
    """Rebuild paper_positions and paper_summary for every strategy with at
    least one reconciled alert. Returns {strategy_id: snapshot} for the report."""
    ensure_tables(conn)
    now = now or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    out = {}
    for (sid,) in conn.execute("SELECT DISTINCT strategy_id FROM paper_trades"
                               " WHERE strategy_id IS NOT NULL ORDER BY strategy_id").fetchall():
        name = conn.execute("SELECT name FROM strategies WHERE id=?", (sid,)).fetchone()
        strat = strategy_by_name(conn, name[0]) if name else None
        alerts = own_alerts(conn, strat) if strat else {}
        r = None
        if alerts:
            # simulate before the first write: the write lock is then held only
            # for this strategy's delete + inserts (the collector writes too)
            newest = conn.execute("SELECT MAX(ts) FROM candles WHERE pair=?",
                                  (strat["pair"],)).fetchone()[0]
            r = simulate(conn, strat, alerts, newest)
        conn.execute("DELETE FROM paper_positions WHERE strategy_id=?", (sid,))
        conn.execute("DELETE FROM paper_summary WHERE strategy_id=?", (sid,))
        if r is None:
            conn.commit()
            continue
        rows = position_rows(strat, alerts, r, newest, now)
        conn.executemany(
            "INSERT INTO paper_positions (strategy_id, signal_id, bar_ts, side, status, entry_ts,"
            " entry, qty, exit_ts, exit, reason, pnl, mark_ts, mark, unrealised, updated_at)"
            " VALUES (:strategy_id, :signal_id, :bar_ts, :side, :status, :entry_ts, :entry, :qty,"
            " :exit_ts, :exit, :reason, :pnl, :mark_ts, :mark, :unrealised, :updated_at)", rows)
        days = daily_summary([(t["exit_ts"], t["pnl"]) for t in r["trades"]], min(alerts), newest)
        conn.executemany(
            "INSERT INTO paper_summary (strategy_id, day, n_trades, net, pf, rolling_30d_pf,"
            " rolling_30d_trades, rolling_30d_net, cum_net) VALUES (:sid, :day, :n_trades, :net,"
            " :pf, :rolling_30d_pf, :rolling_30d_trades, :rolling_30d_net, :cum_net)",
            [{**d, "sid": sid} for d in days])
        out[sid] = {"name": strat["name"], "capital": strat["cfg"]["initial_capital"],
                    "rows": rows, "last": days[-1], "n_closed": len(r["trades"]),
                    "net": r["net"], "pf": _pf([t["pnl"] for t in r["trades"]])}
        conn.commit()
    return out


# --------------------------- P4.3 promotion evidence ---------------------------

PROMOTE_MIN_TRADES = 30      # reconciled alerts
PROMOTE_MIN_DAYS = 60        # first to last reconciled alert bar
PROMOTE_MIN_AGREE = 0.90     # engine agreed / (reconciled alerts + engine entries never alerted)
PROMOTE_MIN_PF = 1.2         # 30-day paper PF at the newest candle


def evidence(conn, strategy_id):
    """The P4.3 checks behind `lineage promote --to paper_validated --evidence
    paper:<id>`. Read-only: paper P&L is re-simulated, not read from
    paper_summary, so it cannot be stale. Returns {"passed", "failed": [...], ...}."""
    ensure_tables(conn)
    name = conn.execute("SELECT name FROM strategies WHERE id=?", (strategy_id,)).fetchone()
    strat = strategy_by_name(conn, name[0]) if name else None
    if strat is None:
        return {"passed": False, "failed": [f"strategy {strategy_id} not found"]}
    rows = conn.execute("SELECT bar_ts, engine_fired FROM paper_trades WHERE strategy_id=?"
                        " AND bar_ts IS NOT NULL", (strategy_id,)).fetchall()
    ev = {"strategy_id": strategy_id, "n_reconciled": len(rows), "span_days": 0.0,
          "agreed": sum(f for _, f in rows), "engine_only": 0, "agreement": None,
          "n_closed": 0, "rolling_30d_trades": 0, "rolling_30d_pf": None}
    alerts = own_alerts(conn, strat)
    if rows:
        lo, hi = min(t for t, _ in rows), max(t for t, _ in rows)
        ev["span_days"] = (hi - lo) / DAY
        alerted = {t for t, _ in rows}
        ev["engine_only"] = sum(1 for t in engine_entries(conn, strat, hi)
                                if lo <= t <= hi and t not in alerted)
        ev["agreement"] = ev["agreed"] / (len(rows) + ev["engine_only"])
    if alerts:
        newest = conn.execute("SELECT MAX(ts) FROM candles WHERE pair=?",
                              (strat["pair"],)).fetchone()[0]
        r = simulate(conn, strat, alerts, newest)
        last = daily_summary([(t["exit_ts"], t["pnl"]) for t in r["trades"]],
                             min(alerts), newest)[-1]
        ev.update(n_closed=len(r["trades"]), rolling_30d_trades=last["rolling_30d_trades"],
                  rolling_30d_pf=last["rolling_30d_pf"])

    failed = []
    if ev["n_reconciled"] < PROMOTE_MIN_TRADES:
        failed.append(f"{ev['n_reconciled']} reconciled alerts < {PROMOTE_MIN_TRADES}")
    if ev["span_days"] < PROMOTE_MIN_DAYS:
        failed.append(f"span {ev['span_days']:.1f} days < {PROMOTE_MIN_DAYS}")
    if ev["agreement"] is None or ev["agreement"] < PROMOTE_MIN_AGREE:
        a = "n/a" if ev["agreement"] is None else f"{100 * ev['agreement']:.0f}%"
        failed.append(f"engine/alert agreement {a} < {100 * PROMOTE_MIN_AGREE:.0f}% "
                      f"({ev['agreed']} agreed of {ev['n_reconciled']} alerts"
                      f" + {ev['engine_only']} engine entries never alerted)")
    if ev["rolling_30d_trades"] == 0:
        failed.append("no paper trade closed in the last 30 days")
    elif ev["rolling_30d_pf"] is not None and ev["rolling_30d_pf"] < PROMOTE_MIN_PF:
        failed.append(f"30-day paper PF {ev['rolling_30d_pf']:.2f} < {PROMOTE_MIN_PF}")
    ev["failed"] = failed
    ev["passed"] = not failed
    return ev


def print_pnl(snap):
    print("\n" + "-" * 78)
    print("PAPER P&L (P4.2) — the positions TradingView's alerts opened, run by the engine")
    print("-" * 78)
    if not snap:
        print("No reconciled strategy alerts, so no paper positions.")
        return
    fmt = lambda x: "-" if x is None else f"{x:.2f}"   # noqa: E731
    print(f"{'strategy':28s} {'closed':>6s} {'net':>9s} {'net %':>6s} {'pf':>5s} "
          f"{'30d n':>5s} {'30d pf':>6s}  open/pending/ignored")
    for sid, s in snap.items():
        st = [x["status"] for x in s["rows"]]
        last = s["last"]
        print(f"#{sid:<3d}{s['name'][:24]:25s} {s['n_closed']:6d} {s['net']:+9.2f} "
              f"{100 * s['net'] / s['capital']:+5.1f}% {fmt(s['pf']):>5s} "
              f"{last['rolling_30d_trades']:5d} {fmt(last['rolling_30d_pf']):>6s}  "
              f"{st.count('open')}/{st.count('pending')}/{st.count('ignored')}")
        for x in s["rows"]:
            if x["status"] == "open":
                print(f"      open {x['side']:5s} since {_iso(x['entry_ts'])[:16]} @ {x['entry']:.6g}"
                      f"  mark {x['mark']:.6g} at {_iso(x['mark_ts'])[:16]}"
                      f"  unrealised {x['unrealised']:+.2f}")
    print("\nRead: net is realised P&L on the engine's own sizing (initial_capital, risk_pct)")
    print("since the first alert; pf '-' = no losing trade yet. Open positions are marked to")
    print("the newest hourly close, net of fees and slippage. An 'ignored' alert came while")
    print("the position was already on that side. P4.3 wants >= 30 reconciled trades over")
    print(">= 60 days and a rolling PF >= 1.2: a handful of paper trades says nothing yet.")


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
    print(">= 30 reconciled trades and >= 60 days.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--signals-db", default=SIGNALS_DB)
    ap.add_argument("--evidence", type=int, metavar="STRATEGY_ID",
                    help="print the P4.3 promotion checks for one strategy and exit")
    args = ap.parse_args()
    if args.evidence is not None:
        conn = sqlite3.connect(DB_PATH)
        ev = evidence(conn, args.evidence)
        conn.close()
        for k, v in ev.items():
            if k != "failed":
                print(f"{k:20s} {v}")
        for f in ev["failed"]:
            print(f"FAIL  {f}")
        print("-> promotable to paper_validated" if ev["passed"] else "-> NOT promotable")
        return
    sconn = open_signals(args.signals_db)
    conn = sqlite3.connect(DB_PATH)
    ensure_tables(conn)
    summary = reconcile(conn, sconn)
    print_report(conn, summary)
    print_pnl(update_pnl(conn))
    conn.close()
    sconn.close()


if __name__ == "__main__":
    main()
