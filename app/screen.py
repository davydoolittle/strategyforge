"""
Momentum screen (P9.1b). Read-only on forge.db: a ranking, not a validated
signal. Nothing here has passed the adversary; F16 and F17 found no timing
skill in this universe, so the output must never be read as a trade list.

For every instrument in the equities universe (`instruments`, session
'us_equity'), from the latest adjusted daily closes in `candles`:

  ret_1m, ret_3m, ret_6m, ret_12m   total return over 21 / 63 / 126 / 252
                                    trading days (adjusted closes, so
                                    dividends are included)
  vol_12m                           annualised realised volatility: stdev of
                                    252 daily log returns x sqrt(252)
  vol_adj_12m                       ret_12m / vol_12m; the ranking key,
                                    highest first

An instrument whose 1-month return is in the top or bottom EXTREME_PCT of the
universe is flagged "short-term extreme (reversal risk)". With n instruments
that is the round(n x 10%) highest and lowest, at least one each (10 ETFs:
one each side).

  python screen.py                  # table to stdout, JSON to <db dir>/screen.json
  python screen.py --out x.json     # JSON elsewhere
  python screen.py --no-json        # table only

The JSON is what the dashboard's Screen panel shows (web.py /api/screen). It
is written to a temp file and renamed, so the dashboard never reads half of
it. The collector runs this weekly, Monday 06:00 UTC (main.JOB_SPECS). It
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

import main

HORIZONS = {"1m": 21, "3m": 63, "6m": 126, "12m": 252}
VOL_DAYS = 252
EXTREME_PCT = 10
STALE_DAYS = 5          # calendar days behind the freshest instrument
NOTE = "ranking, not a validated signal"
EXTREME_LABEL = "short-term extreme (reversal risk)"


def default_out(db_path=None):
    return os.path.join(os.path.dirname(os.path.abspath(db_path or main.DB_PATH)), "screen.json")


def universe(conn):
    return [r[0] for r in conn.execute(
        "SELECT symbol FROM instruments WHERE session='us_equity' ORDER BY symbol")]


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
    for sym in universe(conn):
        series = closes(conn, sym, need)
        m = metrics(series)
        if m is None:
            skipped.append({"symbol": sym, "bars": len(series), "why": f"fewer than {need} daily bars"})
            continue
        rows.append({"symbol": sym, "as_of": _date(series[-1][0]), **m})
    rows.sort(key=lambda r: (r["vol_adj_12m"] is None, -(r["vol_adj_12m"] or 0), r["symbol"]))
    for i, r in enumerate(rows, 1):
        r["rank"] = i
        r["flags"] = []
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
            "rows": rows, "skipped": skipped}


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
             f"top or bottom {screen['extreme_pct']}%. F16/F17: no timing skill found in this universe.")
    return "\n".join(L)


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


if __name__ == "__main__":
    main_cli()
