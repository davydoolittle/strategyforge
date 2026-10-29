"""
TSP daily report and alert. Analysis only: it never logs in to TSP or places
an IFT; you make every move yourself and record it here.

  python tsp_report.py                         # print today's report
  python tsp_report.py --update --notify       # the scheduled job: fetch, report, push once per data day
  python tsp_report.py --record-ift "C=60,S=20,G=20" --date 2026-10-30
                                               # an IFT you made (date = its fill date)

The report: data date, regime and what drove it, active shift flags with
confidence and track record, the live strategy's suggestion (or "hold"),
moves left this month, business days left, the 12:00 ET deadline, and the
stored backtest evidence for the strategy. The push goes through ntfy
(NTFY_TOPIC or --ntfy-topic), the same channel as health.py.
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import date, datetime, timezone

import health
import main
import tsp_backtest as B
import tsp_data as D
import tsp_rules as R
import tsp_signals as S
import tsp_strategies as ST


def ensure_tables(conn):
    B.ensure_tables(conn)
    conn.execute("""CREATE TABLE IF NOT EXISTS tsp_my_ifts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        fill_date TEXT NOT NULL,        -- the close it filled at
        target_json TEXT NOT NULL,
        kind TEXT NOT NULL,             -- flexible | g_only
        recorded_at TEXT, note TEXT
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS tsp_reports (
        data_date TEXT PRIMARY KEY, data_mode TEXT, created_at TEXT, text TEXT,
        notified INTEGER DEFAULT 0
    )""")
    conn.commit()


# --------------------------------------------------------------- my state ---

def my_ifts(conn):
    return [{"id": i, "fill_date": date.fromisoformat(d), "target": json.loads(t), "kind": k}
            for i, d, t, k in conn.execute(
                "SELECT id, fill_date, target_json, kind FROM tsp_my_ifts ORDER BY fill_date, id")]


def record_ift(conn, cfg, target, fill_date, note=None):
    """Store an IFT you made. Refuses one TSP would not have accepted (a third
    move in a month that is not into G only), judged against the last
    recorded allocation."""
    ensure_tables(conn)
    target = R.validate_allocation(target, cfg["funds"] + _l_funds(cfg))
    rows = my_ifts(conn)
    prev = rows[-1]["target"] if rows else cfg["base_allocation"]
    used = sum(1 for r in rows if r["kind"] == "flexible"
               and (r["fill_date"].year, r["fill_date"].month) == (fill_date.year, fill_date.month))
    if used < cfg["rules"]["flexible_moves_per_month"]:
        kind = "flexible"
    elif R.is_g_only_move(prev, target):
        kind = "g_only"
    else:
        raise R.RuleError(f"{fill_date:%Y-%m} already has {used} flexible IFTs and "
                          f"{R.fmt_alloc(prev)} -> {R.fmt_alloc(target)} is not into G only")
    conn.execute("INSERT INTO tsp_my_ifts (fill_date, target_json, kind, recorded_at, note)"
                 " VALUES (?, ?, ?, ?, ?)", (fill_date.isoformat(), json.dumps(target), kind,
                                             datetime.now(timezone.utc).isoformat(), note))
    conn.commit()
    return kind


def _l_funds(cfg):
    return [a for s in cfg["strategies"].values() for a in s.get("allocation", {}) if a.startswith("L ")]


def current_state(conn, cfg, ds, fill_date):
    """(held target, drifted weights, flexible moves used in fill_date's month,
    notes). Drift is applied from the last recorded fill to the data date."""
    rows, notes = my_ifts(conn), []
    if rows:
        held, since = rows[-1]["target"], rows[-1]["fill_date"]
    else:
        held, since = dict(cfg["base_allocation"]), None
        notes.append("no IFT recorded: assuming you hold the base allocation "
                     "(record yours with --record-ift)")
    weights = dict(held)
    if since and since in ds.index and all(ds.has(f) for f in held):
        a, b = ds.index[since], len(ds) - 1
        val = {f: p * ds.series(f)[b] / ds.series(f)[a] for f, p in held.items()
               if ds.series(f)[a] and ds.series(f)[b]}
        tot = sum(val.values())
        if tot:
            weights = {f: 100 * v / tot for f, v in val.items()}
    used = sum(1 for r in rows if r["kind"] == "flexible"
               and (r["fill_date"].year, r["fill_date"].month) == (fill_date.year, fill_date.month))
    return held, weights, used, notes


# ------------------------------------------------------------------ report ---

def pick_mode(conn, cfg, mode):
    if mode != "auto":
        return mode, []
    last_tsp = D.last_price_date(conn, "tsp", "C")
    last_px = D.last_price_date(conn, "yahoo", cfg["data"]["proxies"]["C"][0])
    if last_tsp and (not last_px or (last_px - last_tsp).days <= cfg["data"]["stale_after_days"]):
        return "tsp", []
    why = (f"TSP share prices stop at {last_tsp}" if last_tsp else "no TSP share prices imported")
    return "proxy", [f"{why}: signals use the ETF proxies (import the tsp.gov CSV to fix)"]


def evidence(conn, name, mode):
    rows = {}
    for strat in (name, "buy_hold_c"):
        r = conn.execute("SELECT run_at, data_mode, start, end, metrics_json FROM tsp_runs"
                         " WHERE strategy=? ORDER BY data_mode=? DESC, id DESC LIMIT 1",
                         (strat, mode)).fetchone()
        if r:
            rows[strat] = {"run_at": r[0], "mode": r[1], "start": r[2], "end": r[3], **json.loads(r[4])}
    return rows


def build_report(conn, cfg, now_utc=None, mode="auto"):
    ensure_tables(conn)
    now_utc = now_utc or datetime.now(timezone.utc)
    mode, notes = pick_mode(conn, cfg, mode)
    ds = D.load_dataset(conn, cfg, mode)
    signals = S.compute(ds, cfg)
    t = len(ds) - 1
    sig = signals[t]
    fill_date, dl = R.deadline(now_utc, cfg["rules"]["cutoff_et"])
    held, weights, used, more = current_state(conn, cfg, ds, fill_date)
    notes += more
    moves_left = max(0, cfg["rules"]["flexible_moves_per_month"] - used)
    days_left = R.business_days_left_in_month(fill_date)
    strat = ST.build(cfg["live_strategy"], cfg)
    ctx = {"weights": weights, "held": held, "fill_date": fill_date, "moves_left": moves_left,
           "days_left": days_left, "signal": sig, "regime_hist": [s["regime"] for s in signals]}
    prop = strat.decide(ds.view(t), ctx)
    active = [(signals[i]["date"], fl) for i in range(max(0, t - cfg["shifts"]["active_days"] + 1), t + 1)
              for fl in signals[i]["flags"]]
    data_date = ds.dates[t]
    last_bd = _last_business_day(now_utc)
    if data_date < last_bd:
        notes.append(f"data ends {data_date}, the last business close was {last_bd}: stale")
    return {"data_date": data_date, "mode": mode, "regime": sig["regime"], "score": sig["score"],
            "drivers": sig["drivers"], "active": active, "proposal": prop, "held": held,
            "weights": weights, "moves_left": moves_left, "days_left": days_left,
            "fill_date": fill_date, "deadline_utc": dl, "notes": notes + ds.notes,
            "strategy": cfg["live_strategy"], "evidence": evidence(conn, cfg["live_strategy"], mode),
            "disclaimer": cfg["report"]["disclaimer"]}


def _last_business_day(now_utc):
    """The latest business day whose 16:00 ET close has happened by now."""
    et = R.to_et(now_utc)
    d = et.date()
    return d if R.is_business_day(d) and et.hour >= 16 else R.previous_business_day(d)


def render(rep, short=False):
    L = []
    w = L.append
    dl_et = R.to_et(rep["deadline_utc"])
    w(f"TSP {rep['data_date']} ({rep['mode']} data): regime {str(rep['regime']).upper()} "
      f"(score {rep['score']:+d})")
    drivers = sorted((d for d in rep["drivers"] if d[2]), key=lambda d: d[2])
    w("Drivers: " + ("; ".join(f"{d[0]} {d[1]} {d[2]:+d}" for d in drivers) or "none"))
    if rep["active"]:
        w("Shift flags (last days):")
        for d, fl in rep["active"]:
            hr = f"{fl['hits']}/{fl['n']}" if fl["n"] else "no history"
            w(f"  {d} {fl['type']} ({fl['direction']}): {fl['why']}; confidence {fl['confidence']:.2f}, "
              f"hit rate {hr}, mean edge {fl['mean_edge_pct']:+.2f}%"
              + ("" if fl["enough_history"] else " [too little history]"))
    else:
        w("Shift flags: none in the last days")
    w(f"You hold (as recorded): {R.fmt_alloc(rep['held'])}; drifted to "
      f"{R.fmt_alloc({f: round(v) for f, v in rep['weights'].items()})}")
    p = rep["proposal"]
    if p:
        conf = f", confidence {p['confidence']:.2f}" if p.get("confidence") is not None else ""
        w(f"SUGGESTION ({rep['strategy']}{conf}): move to {R.fmt_alloc(p['target'])}")
        w(f"  Why: {p['reason']}")
        if p.get("evidence"):
            w(f"  Evidence: {p['evidence']}")
        if rep["moves_left"] <= 0:
            w("  (budget spent: this is a move into G only, which TSP still allows)")
    else:
        w(f"SUGGESTION ({rep['strategy']}): hold, no move")
    w(f"Moves left in {rep['fill_date']:%b %Y}: {rep['moves_left']} flexible + unlimited into G; "
      f"{rep['days_left']} business days left after {rep['fill_date']}")
    w(f"Deadline: submit before {dl_et:%a %Y-%m-%d %H:%M} ET to fill at that day's close")
    ev = rep["evidence"].get(rep["strategy"])
    c = rep["evidence"].get("buy_hold_c")
    if ev:
        w(f"Backtest ({ev['mode']}, {ev['start']}..{ev['end']}): CAGR {ev['cagr']:.1f}%, max DD "
          f"{ev['max_dd']:.0f}%, Sharpe {ev['sharpe']:.2f}, DSR {ev.get('dsr') or 0:.2f}"
          + (f"; walk-forward OOS Sharpe {ev['wf_oos_sharpe']:.2f}" if ev.get("wf_oos_sharpe") is not None else "")
          + (f" | 100% C: CAGR {c['cagr']:.1f}%, max DD {c['max_dd']:.0f}%, Sharpe {c['sharpe']:.2f}" if c else ""))
    else:
        w("Backtest: none stored for this strategy (run tsp_backtest.py)")
    if not short:
        for n in rep["notes"]:
            w(f"Note: {n}")
    else:
        for n in rep["notes"]:
            if "stale" in n or "assuming" in n or "proxies" in n:
                w(f"Note: {n}")
    w(rep["disclaimer"])
    return "\n".join(L)


def notify_once(conn, rep, topic, title):
    """Push the report once per data date. True if pushed now."""
    ensure_tables(conn)
    d = rep["data_date"].isoformat()
    row = conn.execute("SELECT notified FROM tsp_reports WHERE data_date=?", (d,)).fetchone()
    text = render(rep, short=True)
    if row and row[0]:
        return False
    ok = health.send_ntfy(topic, title, text) if topic else False
    conn.execute("INSERT OR REPLACE INTO tsp_reports (data_date, data_mode, created_at, text, notified)"
                 " VALUES (?, ?, ?, ?, ?)", (d, rep["mode"], datetime.now(timezone.utc).isoformat(),
                                             text, int(ok)))
    conn.commit()
    return ok


def main_cli(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--update", action="store_true", help="fetch data first (tsp_data.update)")
    ap.add_argument("--notify", action="store_true", help="push via ntfy, once per data date")
    ap.add_argument("--ntfy-topic", default=os.environ.get("NTFY_TOPIC", ""))
    ap.add_argument("--data", choices=["auto", "tsp", "proxy"], default="auto")
    ap.add_argument("--record-ift", metavar="ALLOC", help='an IFT you made, e.g. "C=60,S=20,G=20"')
    ap.add_argument("--date", help="its fill date (default: the fill date for a request made now)")
    ap.add_argument("--config")
    args = ap.parse_args(argv)
    cfg = R.load_config(args.config)
    conn = sqlite3.connect(main.DB_PATH, timeout=main.DB_TIMEOUT_S)
    ensure_tables(conn)
    if args.record_ift:
        fd = date.fromisoformat(args.date) if args.date else R.fill_date_for_request(datetime.now(timezone.utc))
        kind = record_ift(conn, cfg, R.parse_allocation(args.record_ift), fd)
        print(f"recorded {args.record_ift} filling {fd} as a {kind} move")
        return 0
    if args.update:
        D.update(conn, cfg)
    rep = build_report(conn, cfg, mode=args.data)
    print(render(rep))
    if args.notify:
        if not args.ntfy_topic:
            print("!! NTFY_TOPIC not set: report NOT pushed")
        elif notify_once(conn, rep, args.ntfy_topic, cfg["report"]["ntfy_title"]):
            print("pushed via ntfy")
        else:
            print("not pushed (already sent for this data date, or ntfy failed)")
    return 0


if __name__ == "__main__":
    sys.exit(main_cli())
