"""
Momentum screen (P9.1b). Read-only on forge.db: a ranking, not a validated
signal. Nothing here has passed the adversary; F16 and F17 found no timing
skill in this universe, so the output must never be read as a trade list.

For every ETF in the latest top-50 list (etf_universe.py: the most-traded US
ETFs, re-ranked weekly; before the first list, every us_equity instrument),
from the latest adjusted daily closes in `candles`:

  ret_1m, ret_3m, ret_6m, ret_12m   total return over 21 / 63 / 126 / 252
                                    trading days (adjusted closes, so
                                    dividends are included)
  vol_12m                           annualised realised volatility: stdev of
                                    252 daily log returns x sqrt(252)
  vol_adj_12m                       ret_12m / vol_12m; the ranking key,
                                    highest first, except that a fund with
                                    vol_12m under 2% (T-bills: BIL, SGOV) is
                                    flagged cash-like and ranked last

An instrument whose 1-month return is in the top or bottom EXTREME_PCT of the
universe is flagged "short-term extreme (reversal risk)". With n instruments
that is the round(n x 10%) highest and lowest, at least one each (10 ETFs:
one each side; 50 ETFs: five each side). An ETF with under 253 bars, i.e. a fund
less than a year old, is listed as skipped.

The push (--notify) carries the top PUSH_TOP_N, any flagged ETF below them, and
which ETFs entered or left the top 50 since the previous list. The dashboard
shows all of them.

  python screen.py                  # table to stdout, JSON to <db dir>/screen.json
  python screen.py --out x.json     # JSON elsewhere
  python screen.py --no-json        # table only
  python screen.py --notify         # also push a short version via ntfy

The JSON is what the dashboard's Screen panel shows (web.py /api/screen). It
is written to a temp file and renamed, so the dashboard never reads half of
it. The collector runs this weekly, Monday 06:00 UTC (main.JOB_SPECS). It
also pushes a short version via ntfy (`--notify`, topic NTFY_TOPIC, the
channel health.py and tsp_report.py use), once per data date: the date last
pushed is kept in screen_notified.json next to forge.db, so the DB stays
read-only and a re-run or a missed slot caught up does not push twice. It
does not fetch bars: ETF bars are refreshed by the `patterns` job, Sunday
22:00 UTC, so Friday's close is in. If that run failed or was missed, each row
still carries its own last bar date and stale rows are marked.
"""

import argparse
import json
import math
import os
import sqlite3
from datetime import datetime, timezone

import etf_universe
import health
import main

HORIZONS = {"1m": 21, "3m": 63, "6m": 126, "12m": 252}
VOL_DAYS = 252
EXTREME_PCT = 10
STALE_DAYS = 5          # calendar days behind the freshest instrument
CASH_VOL = 0.02         # under 2% a year: a T-bill fund, whose return / vol means nothing
CASH_LABEL = "cash-like (vol under 2%): ranked last"
NOTE = "ranking, not a validated signal"
NTFY_TITLE = "StrategyForge momentum screen"
PUSH_TOP_N = 15
EXTREME_LABEL = "short-term extreme (reversal risk)"


def default_out(db_path=None):
    return os.path.join(os.path.dirname(os.path.abspath(db_path or main.DB_PATH)), "screen.json")


def universe(conn):
    """(symbols, info): the latest top-50 list, or every us_equity instrument
    before the first list. info says which, and who entered and left."""
    now, entered, left = etf_universe.latest(conn)
    if now is not None:
        done = etf_universe.runs(conn)
        return now, {"source": f"top {len(now)} US ETFs by dollar volume", "run_date": done[0],
                     "previous_run": done[1] if len(done) > 1 else None,
                     "entered": entered, "left": left, "liquidity_rank": {s: i for i, s in enumerate(now, 1)}}
    syms = [r[0] for r in conn.execute(
        "SELECT symbol FROM instruments WHERE session='us_equity' ORDER BY symbol")]
    return syms, {"source": "us_equity instruments", "run_date": None, "previous_run": None,
                  "entered": [], "left": [], "liquidity_rank": {}}


def closes(conn, symbol, n):
    """The latest n daily closes, oldest first, as (ts, close)."""
    rows = conn.execute("SELECT ts, close FROM candles WHERE pair=? AND session='us_equity'"
                        " AND close > 0 ORDER BY ts DESC LIMIT ?", (symbol, n)).fetchall()
    return rows[::-1]


def realised_vol(px):
    rets = [math.log(b / a) for a, b in zip(px, px[1:])]
    if len(rets) < 2:
        return None
    m = sum(rets) / len(rets)
    return math.sqrt(sum((r - m) ** 2 for r in rets) / (len(rets) - 1)) * math.sqrt(252)


def metrics(series):
    """series: (ts, close) oldest first, at least 253 points. None if too short."""
    need = max(max(HORIZONS.values()), VOL_DAYS) + 1
    if len(series) < need:
        return None
    px = [c for _, c in series]
    out = {f"ret_{k}": px[-1] / px[-1 - n] - 1 for k, n in HORIZONS.items()}
    vol = realised_vol(px[-1 - VOL_DAYS:])
    out["vol_12m"] = vol
    out["vol_adj_12m"] = out["ret_12m"] / vol if vol and vol > 1e-9 else None
    return out


def n_extreme(n):
    return max(1, round(n * EXTREME_PCT / 100)) if n >= 2 else 0


def compute(conn):
    need = max(max(HORIZONS.values()), VOL_DAYS) + 1
    rows, skipped = [], []
    syms, info = universe(conn)
    for sym in syms:
        series = closes(conn, sym, need)
        m = metrics(series)
        if m is None:
            skipped.append({"symbol": sym, "bars": len(series), "why": f"fewer than {need} daily bars"})
            continue
        rows.append({"symbol": sym, "as_of": _date(series[-1][0]), **m,
                     "liquidity_rank": info["liquidity_rank"].get(sym),
                     "new": sym in info["entered"]})
    for r in rows:
        r["cash_like"] = r["vol_12m"] is not None and r["vol_12m"] < CASH_VOL
    rows.sort(key=lambda r: (r["cash_like"], r["vol_adj_12m"] is None, -(r["vol_adj_12m"] or 0), r["symbol"]))
    for i, r in enumerate(rows, 1):
        r["rank"] = i
        r["flags"] = [CASH_LABEL] if r["cash_like"] else []
    k = n_extreme(len(rows))
    by_1m = sorted(rows, key=lambda r: (r["ret_1m"], r["symbol"]))
    for r in by_1m[:k]:
        r["flags"].append(EXTREME_LABEL)
        r["extreme"] = "bottom"
    for r in by_1m[len(by_1m) - k:]:
        if "extreme" not in r:
            r["flags"].append(EXTREME_LABEL)
        r["extreme"] = "top"
    latest = max((r["as_of"] for r in rows), default=None)
    for r in rows:
        r.setdefault("extreme", None)
        r["stale"] = bool(latest) and (datetime.fromisoformat(latest) -
                                       datetime.fromisoformat(r["as_of"])).days > STALE_DAYS
        if r["stale"]:
            r["flags"].append(f"stale: last bar {r['as_of']}")
    return {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "as_of": latest, "note": NOTE, "rank_by": "vol_adj_12m",
            "horizons_days": HORIZONS, "vol_days": VOL_DAYS, "extreme_pct": EXTREME_PCT,
            "rows": rows, "skipped": skipped,
            "universe": {k: v for k, v in info.items() if k != "liquidity_rank"}}


def _date(ts):
    return datetime.fromtimestamp(ts, timezone.utc).date().isoformat()


def render(screen):
    L = [f"Momentum screen, data as of {screen['as_of']}: {screen['note'].upper()}",
         f"{'#':>2} {'symbol':<6} {'1m %':>7} {'3m %':>7} {'6m %':>7} {'12m %':>7} {'vol %':>6} "
         f"{'12m/vol':>7}  flags"]
    for r in screen["rows"]:
        L.append(f"{r['rank']:>2} {r['symbol']:<6} {100 * r['ret_1m']:>+7.1f} {100 * r['ret_3m']:>+7.1f} "
                 f"{100 * r['ret_6m']:>+7.1f} {100 * r['ret_12m']:>+7.1f} {100 * r['vol_12m']:>6.1f} "
                 f"{r['vol_adj_12m']:>+7.2f}  {'; '.join(r['flags'])}")
    for s in screen["skipped"]:
        L.append(f"   {s['symbol']:<6} skipped: {s['why']} ({s['bars']})")
    if not screen["rows"] and not screen["skipped"]:
        L.append("   no us_equity instruments (equities.py --seed / --backfill)")
    L.append(f"Ranked by 12-month return / 12-month realised volatility. Flag: 1-month return in the "
             f"top or bottom {screen['extreme_pct']}%. F16/F17: no ETF timing rule tested here has passed.")
    return "\n".join(L)


def render_push(screen):
    """The ntfy body: one line per instrument, narrow enough for a phone."""
    L = [f"Data as of {screen['as_of']}. {screen['note'].capitalize()}."]
    uni = screen.get("universe") or {}
    if uni.get("run_date"):
        L[0] += f" {len(screen['rows'])} of the {uni['source']}."
    shown = [r for r in screen["rows"] if r["rank"] <= PUSH_TOP_N or r["extreme"] or r["stale"]]
    for i, r in enumerate(shown):
        if i and r["rank"] > shown[i - 1]["rank"] + 1:
            L.append("...")
        tags = []
        if r["extreme"]:
            tags.append(f"1m {r['extreme']} {screen['extreme_pct']}%, reversal risk")
        if r["stale"]:
            tags.append(f"stale, last bar {r['as_of']}")
        L.append(f"{r['rank']}. {r['symbol']} 12m {100 * r['ret_12m']:+.1f}%, "
                 f"12m/vol {r['vol_adj_12m']:+.2f}" + (f" [{'; '.join(tags)}]" if tags else ""))
    if len(shown) < len(screen["rows"]):
        L.append(f"{len(shown)} of {len(screen['rows'])} shown: all on the dashboard")
    if uni.get("previous_run"):
        L.append(f"Top 50 since {uni['previous_run']}: in {', '.join(uni['entered']) or 'none'}; "
                 f"out {', '.join(uni['left']) or 'none'}")
    if screen["skipped"]:
        L.append("Skipped (fewer than 253 daily bars): " + ", ".join(s["symbol"] for s in screen["skipped"]))
    if not screen["rows"] and not screen["skipped"]:
        L.append("no us_equity instruments")
    L.append("F16/F17: no ETF timing rule tested here has passed. Not a trade list.")
    return "\n".join(L)


def default_state(db_path=None):
    return os.path.join(os.path.dirname(os.path.abspath(db_path or main.DB_PATH)),
                        "screen_notified.json")


def notify_once(screen, topic, state_path):
    """Push once per data date. True if pushed now. The date is recorded only
    after ntfy accepted the push, so a failed push is retried next run."""
    try:
        with open(state_path) as f:
            last = json.load(f).get("as_of")
    except (OSError, ValueError):
        last = None
    if screen["as_of"] is not None and screen["as_of"] == last:
        return False
    if not health.send_ntfy(topic, NTFY_TITLE, render_push(screen),
                            priority="default", tags="bar_chart"):
        return False
    write_json({"as_of": screen["as_of"],
                "pushed_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}, state_path)
    return True


def write_json(screen, path):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(screen, f, indent=1)
    os.replace(tmp, path)


def connect_ro(db_path=None):
    path = db_path or main.DB_PATH
    if not os.path.exists(path):
        raise SystemExit(f"database not found: {path}")
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=main.DB_TIMEOUT_S)


def main_cli(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", help="JSON path (default: screen.json next to forge.db)")
    ap.add_argument("--no-json", action="store_true")
    ap.add_argument("--notify", action="store_true", help="push via ntfy, once per data date")
    ap.add_argument("--ntfy-topic", default=os.environ.get("NTFY_TOPIC", ""))
    args = ap.parse_args(argv)
    conn = connect_ro()
    try:
        screen = compute(conn)
    finally:
        conn.close()
    print(render(screen))
    if not args.no_json:
        path = args.out or default_out()
        write_json(screen, path)
        print(f"wrote {path}")
    if args.notify:
        if not args.ntfy_topic:
            print("!! NTFY_TOPIC not set: screen NOT pushed")
        elif notify_once(screen, args.ntfy_topic, default_state()):
            print("pushed via ntfy")
        else:
            print(f"not pushed: already sent for {screen['as_of']}, or ntfy failed (see above)")


if __name__ == "__main__":
    main_cli()
