"""
Route B of P13 (V2_PROMPT 8i, P13F): record the material-agreement test's
inputs forward, from the day this job first runs, so nothing in them was known
when the rule was fixed.

Each run (the daily job `filings_forward`):
  members   Wikipedia's current S&P 500 table. A member seen for the first time
            opens a span on that date; one no longer listed keeps the date it
            was last seen. Point in time by construction.
  filings   SEC submissions for every member ever seen (filings.py; the full
            history the first time, the recent page after that).
  bars      Alpaca daily bars (adjustment all) for every member ever seen and
            SPY, from BETA_LOOKBACK_DAYS before the start to the last close,
            re-read in full each run so the adjustments stay consistent; only
            changed rows are written.

  fwd_members   ticker, cik, name, first_seen, last_seen
  fwd_bars      ticker, ts, open, high, low, close, volume

The start date is stored once (stocks_meta 'fwd_start') and never moved. The
evaluator counts only filings accepted on or after it.

  python filings_forward.py          # one run
  python filings_forward.py --status

Into stocks.db, never forge.db.
"""

import argparse
import urllib.parse
from datetime import date, datetime, timedelta, timezone

import equities as E
import filings
import stocks as S

BETA_LOOKBACK_DAYS = 400        # calendar days: ~275 trading days before the start
BENCHMARK = "SPY"
BATCH_SYMBOLS = 100
BATCH_ROWS = 500


def ensure_tables(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS fwd_members (
        ticker TEXT NOT NULL, cik TEXT, name TEXT, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
        PRIMARY KEY (ticker, first_seen))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS fwd_bars (
        ticker TEXT NOT NULL, ts INTEGER NOT NULL, open REAL, high REAL, low REAL, close REAL, volume REAL,
        PRIMARY KEY (ticker, ts))""")
    conn.commit()


def start_date(conn, today):
    got = S.meta(conn, "fwd_start")
    if got:
        return got
    S.meta(conn, "fwd_start", today)
    return today


def update_members(conn, current, today):
    """current: [(ticker, name, cik)]. Returns (new spans, members not listed today)."""
    open_ = {r[0]: r[1] for r in conn.execute(
        "SELECT ticker, first_seen FROM fwd_members m WHERE last_seen=(SELECT MAX(last_seen) FROM fwd_members)")}
    new = []
    for t, name, cik in current:
        if t in open_:
            conn.execute("UPDATE fwd_members SET last_seen=?, cik=?, name=? WHERE ticker=? AND first_seen=?",
                         (today, cik, name, t, open_[t]))
        else:
            conn.execute("INSERT OR IGNORE INTO fwd_members VALUES (?, ?, ?, ?, ?)", (t, cik, name, today, today))
            new.append(t)
    conn.commit()
    gone = sorted(set(open_) - {t for t, _, _ in current})
    return new, gone


def alpaca_bars(symbols, start, end_date, headers, get=E._get, sleep=None):
    """{symbol: [(ts, o, h, l, c, v)]} for daily bars start..end_date (inclusive),
    adjustment all; many symbols a request, every page."""
    import time
    sleep = sleep or time.sleep
    out = {}
    syms = sorted(set(symbols))
    for i in range(0, len(syms), BATCH_SYMBOLS):
        token = None
        while True:
            q = {"symbols": ",".join(syms[i:i + BATCH_SYMBOLS]), "timeframe": "1Day", "start": start,
                 "end": f"{end_date}T21:00:00Z", "adjustment": "all", "feed": E.FEED, "limit": 10000,
                 "sort": "asc"}
            if token:
                q["page_token"] = token
            page = get(f"{E.DATA_URL}?{urllib.parse.urlencode(q)}", headers)
            for sym, bars in (page.get("bars") or {}).items():
                out.setdefault(sym, []).extend(E._parse(b) for b in bars)
            token = page.get("next_page_token")
            sleep(E.PAGE_SLEEP_S)
            if not token:
                break
    return out


def store_bars(conn, bars):
    """Write only rows that changed; returns the number written."""
    n = 0
    for sym, rows in bars.items():
        have = {r[0]: r[1:] for r in conn.execute(
            "SELECT ts, open, high, low, close, volume FROM fwd_bars WHERE ticker=?", (sym,))}
        changed = [(sym, *b) for b in rows if have.get(b[0]) != tuple(b[1:])]
        for i in range(0, len(changed), BATCH_ROWS):
            conn.executemany("INSERT OR REPLACE INTO fwd_bars VALUES (?, ?, ?, ?, ?, ?, ?)", changed[i:i + BATCH_ROWS])
            conn.commit()
        n += len(changed)
    return n


def run(conn, now=None, current=None, sec=None, alpaca_headers=None, bars_fn=alpaca_bars, log=print):
    S.ensure_tables(conn)
    filings.ensure_tables(conn)
    ensure_tables(conn)
    now = now or datetime.now(timezone.utc)
    today = now.date().isoformat()
    start = start_date(conn, today)
    current = current if current is not None else S.parse_wiki_current(S.wiki_html(S.WIKI_CURRENT))
    if not 480 <= len(current) <= 520:
        raise SystemExit(f"Wikipedia's table has {len(current)} members: page changed? nothing recorded")
    new, gone = update_members(conn, current, today)
    ciks = [r[0] for r in conn.execute("SELECT DISTINCT cik FROM fwd_members WHERE cik IS NOT NULL")]
    done, failed = filings.fetch_all(conn, ciks, sec, log=log, recent_only=True)
    tickers = [r[0] for r in conn.execute("SELECT DISTINCT ticker FROM fwd_members")] + [BENCHMARK]
    bar_start = (date.fromisoformat(start) - timedelta(days=BETA_LOOKBACK_DAYS)).isoformat()
    last_close = (now.date() - timedelta(days=1)).isoformat()
    headers = alpaca_headers if alpaca_headers is not None else E.load_credentials()
    bars = bars_fn(tickers, bar_start, last_close, headers)
    written = store_bars(conn, bars)
    missing = sorted(set(tickers) - set(bars))
    n8k = conn.execute("SELECT COUNT(*) FROM sec_filings f JOIN fwd_members m ON m.cik=f.cik"
                       " WHERE f.form='8-K' AND f.accepted>=?", (start,)).fetchone()[0]
    log(f"forward recorder since {start}: {len(current)} members ({len(new)} new: {new[:10]},"
        f" {len(gone)} gone: {gone[:10]}); SEC {done} companies updated, {len(failed)} failed;"
        f" bars {written} rows written, {len(missing)} tickers without bars {missing[:10]};"
        f" {n8k} 8-Ks accepted since the start")
    return {"new": new, "gone": gone, "failed": failed, "missing": missing, "n8k": n8k}


def status(conn):
    ensure_tables(conn)
    print(f"start {S.meta(conn, 'fwd_start')}; spans {conn.execute('SELECT COUNT(*) FROM fwd_members').fetchone()[0]};"
          f" bars {conn.execute('SELECT COUNT(*) FROM fwd_bars').fetchone()[0]}")


def main_cli(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args(argv)
    conn = S.connect()
    if args.status:
        status(conn)
    else:
        run(conn)
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
