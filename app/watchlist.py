"""
Long-term watchlist: funds to compare for buy-and-hold, not to trade.

Bars go in watchlist_bars, never in candles: a fund picked for last year's
return must not reach a research universe, A7 or a snapshot. Prices are Tiingo
adjusted closes, so returns include dividends; a run replaces the whole series,
because a new dividend re-bases it.

  python watchlist.py             # refresh, then report
  python watchlist.py --report    # report what is stored
"""

import argparse
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


def main_cli():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--report", action="store_true", help="skip the refresh")
    args = ap.parse_args()
    conn = sqlite3.connect(main.DB_PATH, timeout=main.DB_TIMEOUT_S)
    ensure_tables(conn)
    if not args.report:
        refresh(conn, equities.load_tiingo_credentials())
    report(conn)


if __name__ == "__main__":
    main_cli()
