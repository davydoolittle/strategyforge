"""
Daily bars for the point-in-time S&P 500 universe (P12.2, V2_PROMPT 8i), from
Tiingo's free tier, inside its quota.

The free tier allows 500 unique symbols a month (and 50 requests an hour). The
research refresh already spends ~44 of them a week on the ETF sets, so this job
keeps to MONTHLY_BUDGET new symbols a month and DAILY_MAX a run. About 1,100
tickers therefore take roughly three months. Every symbol asked for is recorded
in tiingo_usage before the request, so a crash cannot overspend.

Which tickers: each member interval's own ticker, then any current ticker SEC
lists for its CIK (renamed companies), then SPY for beta. A ticker Tiingo does
not know is recorded as missing, not retried every day.

  stock_bars       ticker, ts, open, high, low, close, volume (Tiingo adjusted)
  stock_bar_status ticker, status (ok | missing | error), first, last, n, fetched_at
  tiingo_usage     month, ticker

  python stock_bars.py             # one run: up to DAILY_MAX new tickers (the daily job)
  python stock_bars.py --status

Into stocks.db, never forge.db. Fetch first, then write in batches; restartable.
"""

import argparse
import json
import urllib.error
import urllib.parse
from datetime import datetime, timezone

import equities as E
import stocks as S

START = "2003-01-01"          # 2005 events need about 250 trading days before them
MONTHLY_BUDGET = 400
DAILY_MAX = 15
BENCHMARK = "SPY"
BATCH_ROWS = 500


def ensure_tables(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS stock_bars (
        ticker TEXT NOT NULL, ts INTEGER NOT NULL, open REAL, high REAL, low REAL, close REAL, volume REAL,
        PRIMARY KEY (ticker, ts))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS stock_bar_status (
        ticker TEXT PRIMARY KEY, status TEXT, first TEXT, last TEXT, n INTEGER, fetched_at TEXT, error TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS tiingo_usage (
        month TEXT NOT NULL, ticker TEXT NOT NULL, PRIMARY KEY (month, ticker))""")
    conn.commit()


def wanted(conn):
    """Tickers to fetch, in order: SPY, each interval's ticker (oldest interval
    first), then SEC's current tickers for mapped CIKs. Dots become dashes,
    Tiingo's spelling for share classes (BRK.B -> BRK-B)."""
    out = [BENCHMARK]
    for (t,) in conn.execute("SELECT ticker FROM pit_members ORDER BY start, ticker"):
        out.append(t)
    has_sec = conn.execute("SELECT 1 FROM sqlite_master WHERE name='sec_companies'").fetchone()
    if has_sec:
        for (tj,) in conn.execute("SELECT c.tickers FROM sec_companies c JOIN pit_members m ON m.cik=c.cik"
                                  " ORDER BY m.start"):
            out += json.loads(tj or "[]")
    seen, uniq = set(), []
    for t in out:
        t = t.upper().replace(".", "-")
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq


def todo(conn, month=None):
    """Wanted tickers not yet settled; an 'error' is retried on a later run."""
    done = {r[0] for r in conn.execute("SELECT ticker FROM stock_bar_status WHERE status IN ('ok', 'missing')")}
    return [t for t in wanted(conn) if t not in done]


def used(conn, month):
    return conn.execute("SELECT COUNT(*) FROM tiingo_usage WHERE month=?", (month,)).fetchone()[0]


def fetch(ticker, headers, get=E._get):
    q = urllib.parse.urlencode({"startDate": START, "format": "json"})
    return [E._parse_tiingo(b) for b in get(f"{E.TIINGO_URL}/{urllib.parse.quote(ticker)}/prices?{q}", headers)]


def store(conn, ticker, bars):
    rows = [(ticker, *b) for b in bars]
    for i in range(0, len(rows), BATCH_ROWS):
        conn.executemany("INSERT OR REPLACE INTO stock_bars VALUES (?, ?, ?, ?, ?, ?, ?)", rows[i:i + BATCH_ROWS])
        conn.commit()


def run(conn, headers=None, now=None, fetch_fn=fetch, daily_max=DAILY_MAX, budget=MONTHLY_BUDGET, log=print):
    ensure_tables(conn)
    now = now or datetime.now(timezone.utc)
    month = now.strftime("%Y-%m")
    room = min(daily_max, budget - used(conn, month))
    queue = todo(conn, month)
    if room <= 0:
        log(f"Tiingo budget for {month} spent ({used(conn, month)}/{budget}); {len(queue)} tickers wait")
        return []
    headers = headers if headers is not None else E.load_tiingo_credentials()
    done = []
    for t in queue[:room]:
        conn.execute("INSERT OR IGNORE INTO tiingo_usage VALUES (?, ?)", (month, t))   # before the request
        conn.commit()
        stamp = now.isoformat(timespec="seconds")
        try:
            bars = fetch_fn(t, headers)
        except urllib.error.HTTPError as e:
            status = "missing" if e.code == 404 else "error"
            conn.execute("INSERT OR REPLACE INTO stock_bar_status VALUES (?, ?, NULL, NULL, 0, ?, ?)",
                         (t, status, stamp, f"HTTP {e.code}"))
            conn.commit()
            if status == "error":
                log(f"  {t}: HTTP {e.code}, stopping this run")
                break
            continue
        if not bars:
            conn.execute("INSERT OR REPLACE INTO stock_bar_status VALUES (?, 'missing', NULL, NULL, 0, ?, 'no bars')",
                         (t, stamp))
            conn.commit()
            continue
        store(conn, t, bars)
        conn.execute("INSERT OR REPLACE INTO stock_bar_status VALUES (?, 'ok', ?, ?, ?, ?, NULL)",
                     (t, E._date(bars[0][0]), E._date(bars[-1][0]), len(bars), stamp))   # last: the marker
        conn.commit()
        done.append(t)
    left = len(todo(conn, month))
    log(f"stock bars: {len(done)} fetched this run; {used(conn, month)}/{budget} symbols used in {month};"
        f" {left} tickers still to fetch")
    return done


def status(conn):
    ensure_tables(conn)
    by = dict(conn.execute("SELECT status, COUNT(*) FROM stock_bar_status GROUP BY 1").fetchall())
    print(f"tickers wanted {len(wanted(conn))}; status {by}; bars {conn.execute('SELECT COUNT(*) FROM stock_bars').fetchone()[0]}")


def main_cli(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args(argv)
    conn = S.connect()
    S.ensure_tables(conn)
    if args.status:
        status(conn)
    else:
        run(conn)
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
