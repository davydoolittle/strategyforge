"""
Long-term watchlist: funds to compare for buy-and-hold, not to trade.

Bars go in watchlist_bars, never in candles: a fund picked for last year's
return must not reach a research universe, A7 or a snapshot. Prices are Tiingo
adjusted closes, so returns include dividends; a run replaces the whole series,
because a new dividend re-bases it.

  python watchlist.py             # refresh, then report
  python watchlist.py --report    # report what is stored

The P9.4 tracker's watchlist (display only) lives here too, in its own table
`watchlist`: one row per name someone (the owner, or a declared screen) wants
on the Tracker panel. Rows are never deleted: a user's remove marks the row
'removed', a screen's row whose condition no longer holds is marked 'lapsed'.

  python watchlist.py add AAPL --notes "why"
  python watchlist.py remove AAPL
  python watchlist.py list
"""

import argparse
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import equities
import main

# symbol -> why it is here. VOO stands in for the TSP C Fund.
WATCHLIST = {
    "VOO": "S&P 500, the C Fund's index", "VTI": "whole US market",
    "VXUS": "non-US stocks", "BND": "US bonds",
    "FTEC": "Fidelity tech sector", "FDTX": "Fidelity active tech",
    "FBCG": "Fidelity large growth",
}
BASELINE = "VOO"
YEAR_S = 365.25 * 86400


def ensure_tables(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS watchlist_bars (
        symbol TEXT NOT NULL, ts INTEGER NOT NULL, close REAL,
        PRIMARY KEY (symbol, ts)
    )""")
    conn.commit()


def refresh(conn, headers, today=None, fetch=equities.fetch_tiingo):
    today = today or datetime.now(timezone.utc).date()
    end = (today - timedelta(days=1)).isoformat()
    for sym in WATCHLIST:
        bars = fetch(sym, end, headers)         # network first, no lock held
        equities.check_bars(sym, bars)
        conn.execute("DELETE FROM watchlist_bars WHERE symbol=?", (sym,))
        conn.commit()
        for i in range(0, len(bars), equities.BATCH_ROWS):
            conn.executemany("INSERT INTO watchlist_bars VALUES (?, ?, ?)",
                             [(sym, b[0], b[4]) for b in bars[i:i + equities.BATCH_ROWS]])
            conn.commit()


def stats(closes):
    """Annualised 1/3/5-year return and max drawdown from [(ts, close)], oldest
    first. A period longer than the history is None."""
    last_ts, last = closes[-1]

    def ret(years):
        before = [c for t, c in closes if t <= last_ts - years * YEAR_S]
        return (last / before[-1]) ** (1 / years) - 1 if before else None
    peak, dd = 0.0, 0.0
    for _, c in closes:
        peak = max(peak, c)
        dd = min(dd, c / peak - 1)
    return {"y1": ret(1), "y3": ret(3), "y5": ret(5), "max_dd": dd,
            "first": closes[0][0], "last": last_ts}


def _pct(x):
    return "    n/a" if x is None else f"{x * 100:6.1f}%"


def report(conn):
    print(f"{'symbol':7}{'since':12}{'1y':>8}{'3y/yr':>8}{'5y/yr':>8}{'max DD':>8}  what")
    rows = {}
    for sym, what in WATCHLIST.items():
        closes = conn.execute("SELECT ts, close FROM watchlist_bars WHERE symbol=? ORDER BY ts",
                              (sym,)).fetchall()
        if not closes:
            print(f"{sym:7}(not fetched)")
            continue
        s = rows[sym] = stats(closes)
        print(f"{sym:7}{equities._date(s['first']):12}{_pct(s['y1']):>8}{_pct(s['y3']):>8}"
              f"{_pct(s['y5']):>8}{_pct(s['max_dd']):>8}  {what}")
    if rows:
        print(f"Read: past returns, dividends included, through {equities._date(max(r['last'] for r in rows.values()))},"
              f" before any taxes. A fund added for its recent return tells you what\n"
              f"already happened, not what comes next. Compare drawdowns and fees with {BASELINE}.")


# ------------------------------------------------- P9.4 tracker watchlist ---

STATUSES = ("active", "lapsed", "removed")
USER = "user"
MAX_NOTES = 500


def ensure_watch_table(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS watchlist (
        id INTEGER PRIMARY KEY,
        ticker TEXT NOT NULL,
        added_by TEXT NOT NULL,          -- 'user' or a screen name
        added_on TEXT NOT NULL,
        trigger_values TEXT,             -- JSON: what put it here
        status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'lapsed', 'removed')),
        status_on TEXT,
        notes TEXT
    )""")
    conn.commit()


def valid_ticker(conn, ticker):
    """The ticker as stored if it is an equity instrument, else None."""
    t = (ticker or "").strip().upper()
    if not t or len(t) > 10:
        return None
    r = conn.execute("SELECT symbol FROM instruments WHERE symbol=? AND asset_class='equity'"
                     " AND session='us_equity'", (t,)).fetchone()
    return r[0] if r else None


def add(conn, ticker, added_by=USER, trigger_values=None, notes=None, today=None):
    """Insert one active row; an active row for the same ticker and adder is
    returned instead. Returns (row id, created) or raises ValueError."""
    t = valid_ticker(conn, ticker)
    if not t:
        raise ValueError(f"{ticker!r} is not an equity in the instruments table")
    if notes is not None and len(notes) > MAX_NOTES:
        raise ValueError(f"notes longer than {MAX_NOTES} characters")
    today = today or datetime.now(timezone.utc).date().isoformat()
    got = conn.execute("SELECT id FROM watchlist WHERE ticker=? AND added_by=? AND status='active'",
                       (t, added_by)).fetchone()
    if got:
        return got[0], False
    cur = conn.execute("INSERT INTO watchlist (ticker, added_by, added_on, trigger_values, status, status_on, notes)"
                       " VALUES (?, ?, ?, ?, 'active', ?, ?)",
                       (t, added_by, today, json.dumps(trigger_values or {}), today, notes))
    conn.commit()
    return cur.lastrowid, True


def set_status(conn, row_id, status, today=None):
    if status not in STATUSES:
        raise ValueError(status)
    today = today or datetime.now(timezone.utc).date().isoformat()
    conn.execute("UPDATE watchlist SET status=?, status_on=? WHERE id=?", (status, today, row_id))
    conn.commit()


def remove(conn, ticker, today=None):
    """Mark the user's active row for the ticker 'removed' (kept as history).
    Returns how many rows changed; a screen's rows are not the user's to remove."""
    t = (ticker or "").strip().upper()
    rows = conn.execute("SELECT id FROM watchlist WHERE ticker=? AND added_by=? AND status='active'",
                        (t, USER)).fetchall()
    for (rid,) in rows:
        set_status(conn, rid, "removed", today)
    return len(rows)


def rows(conn, include_inactive=False):
    q = "SELECT id, ticker, added_by, added_on, trigger_values, status, status_on, notes FROM watchlist"
    q += "" if include_inactive else " WHERE status='active'"
    cols = ("id", "ticker", "added_by", "added_on", "trigger_values", "status", "status_on", "notes")
    out = [dict(zip(cols, r)) for r in conn.execute(q + " ORDER BY ticker, added_by")]
    for r in out:
        r["trigger_values"] = json.loads(r["trigger_values"] or "{}")
    return out


def main_cli(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--report", action="store_true", help="skip the refresh (fund comparison)")
    sub = ap.add_subparsers(dest="cmd")
    a = sub.add_parser("add")
    a.add_argument("ticker")
    a.add_argument("--notes")
    r = sub.add_parser("remove")
    r.add_argument("ticker")
    ls = sub.add_parser("list")
    ls.add_argument("--all", action="store_true", help="include lapsed and removed rows")
    args = ap.parse_args(argv)
    conn = sqlite3.connect(main.DB_PATH, timeout=main.DB_TIMEOUT_S)
    if args.cmd:
        ensure_watch_table(conn)
        if args.cmd == "add":
            rid, created = add(conn, args.ticker, notes=args.notes)
            print(f"{'added' if created else 'already on the list'}: #{rid} {args.ticker.upper()}")
        elif args.cmd == "remove":
            n = remove(conn, args.ticker)
            print(f"{n} row(s) marked removed for {args.ticker.upper()}" if n else
                  f"no active user row for {args.ticker.upper()}")
        else:
            for x in rows(conn, args.all):
                print(f"#{x['id']:<4} {x['ticker']:<6} {x['status']:<8} {x['added_by']:<16} {x['added_on']}"
                      f"  {json.dumps(x['trigger_values'])}  {x['notes'] or ''}")
        return 0
    ensure_tables(conn)
    if not args.report:
        refresh(conn, equities.load_tiingo_credentials())
    report(conn)
    return 0


if __name__ == "__main__":
    main_cli()
