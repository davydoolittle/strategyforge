"""
Equities and ETF data layer (P9.1): daily, split- and dividend-adjusted bars
from Alpaca into forge.db, next to crypto.

Bars go in `candles` with session='us_equity'; `pair` holds the symbol. The
`session` column is added with a constant default, so no crypto row is
rewritten and every existing pair reads 'crypto'. `instruments` is the
universe and records how much history each symbol really has.

  ts      = 00:00 UTC of the trading date, so a bar is one date whatever the
            DST offset. The bar itself runs 09:30-16:00 ET on that date.
  prices  = adjustment=all. A new dividend re-bases the whole adjusted series,
            so a symbol is always re-fetched in full, never appended to.

Data comes from data.alpaca.markets, which is the same host for paper and live
keys. The only trading-API call is GET /v2/assets on the paper endpoint, for
the exchange. Nothing here can place an order.

Credentials: alpaca.env next to forge.db (APCA_API_KEY_ID, APCA_API_SECRET_KEY).

  python equities.py --backfill           # symbols not fetched through yesterday
  python equities.py --backfill --force   # all of them
  python equities.py --report             # history depth per symbol
"""

import argparse
import json
import os
import sqlite3
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import main

DATA_URL = "https://data.alpaca.markets/v2/stocks/bars"
PAPER_URL = "https://paper-api.alpaca.markets/v2"
FEED = "sip"              # consolidated tape; IEX alone is ~2% of volume
ADJUSTMENT = "all"
EARLIEST = "2010-01-01"   # before Alpaca's first bar: ask for everything
SESSION = "us_equity"
TICK_SIZE = 0.01
BATCH_ROWS = 100          # section 10.9
MIN_YEARS = 10.0
PAGE_SLEEP_S = 0.35       # free plan: 200 requests a minute

# symbol -> fund inception. Reference dates: Alpaca does not publish them, and
# listed_from is not the same thing as the first bar we hold (data_start).
UNIVERSE = {
    "SPY": "1993-01-29", "QQQ": "1999-03-10", "IWM": "2000-05-22",
    "EFA": "2001-08-14", "EEM": "2003-04-07", "TLT": "2002-07-22",
    "IEF": "2002-07-22", "GLD": "2004-11-18", "DBC": "2006-02-03",
    "UUP": "2007-02-20",
}


def ensure_tables(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS instruments (
        symbol TEXT PRIMARY KEY,
        asset_class TEXT NOT NULL,
        exchange TEXT,
        tick_size REAL NOT NULL DEFAULT 0.01,
        session TEXT NOT NULL DEFAULT 'us_equity',
        listed_from TEXT,
        data_start TEXT,                -- first bar held
        data_end TEXT,                  -- last bar held
        n_bars INTEGER,
        feed TEXT,
        adjustment TEXT,
        fetched_through TEXT,           -- written last: the restart marker
        fetched_at TEXT
    )""")
    have = {r[1] for r in conn.execute("PRAGMA table_info(candles)")}
    if "session" not in have:
        conn.execute("ALTER TABLE candles ADD COLUMN session TEXT NOT NULL DEFAULT 'crypto'")
    conn.commit()


def seed_universe(conn):
    for sym, listed in UNIVERSE.items():
        conn.execute("INSERT OR IGNORE INTO instruments (symbol, asset_class, tick_size, session,"
                     " listed_from) VALUES (?, 'etf', ?, ?, ?)", (sym, TICK_SIZE, SESSION, listed))
    conn.commit()


def load_credentials(path=None):
    path = path or os.path.join(os.path.dirname(os.path.abspath(main.DB_PATH)), "alpaca.env")
    if not os.path.exists(path):
        raise SystemExit(f"missing {path}: needs APCA_API_KEY_ID and APCA_API_SECRET_KEY")
    env = {}
    with open(path) as f:
        for line in f:
            k, sep, v = line.strip().partition("=")
            if sep and not k.startswith("#"):
                env[k.strip()] = v.strip().strip("'\"")
    missing = [k for k in ("APCA_API_KEY_ID", "APCA_API_SECRET_KEY") if not env.get(k)]
    if missing:
        raise SystemExit(f"{path} is missing {', '.join(missing)}")
    return {"APCA-API-KEY-ID": env["APCA_API_KEY_ID"],
            "APCA-API-SECRET-KEY": env["APCA_API_SECRET_KEY"]}


def _get(url, headers):
    req = urllib.request.Request(url, headers={**headers, "User-Agent": "strategyforge/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def fetch_exchange(symbol, headers, get=_get):
    return get(f"{PAPER_URL}/assets/{urllib.parse.quote(symbol)}", headers).get("exchange")


def fetch_bars(symbol, end_date, headers, get=_get):
    """Every daily bar through end_date, oldest first, as (ts, o, h, l, c, v)."""
    bars, token = [], None
    while True:
        q = {"symbols": symbol, "timeframe": "1Day", "start": EARLIEST, "end": end_date,
             "adjustment": ADJUSTMENT, "feed": FEED, "limit": 10000, "sort": "asc"}
        if token:
            q["page_token"] = token
        page = get(f"{DATA_URL}?{urllib.parse.urlencode(q)}", headers)
        bars.extend(_parse(b) for b in (page.get("bars") or {}).get(symbol, []))
        token = page.get("next_page_token")
        if not token:
            return bars
        time.sleep(PAGE_SLEEP_S)


def _parse(b):
    """Alpaca stamps a daily bar 00:00 ET, so the UTC date is the trading date."""
    day = datetime.fromisoformat(b["t"].replace("Z", "+00:00")).date()
    ts = int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())
    return ts, b["o"], b["h"], b["l"], b["c"], b["v"]


def check_bars(symbol, bars):
    """Refuse a bad series before any of it reaches the DB."""
    if not bars:
        raise ValueError(f"{symbol}: no bars returned")
    seen = set()
    for ts, o, h, lo, c, _ in bars:
        day = datetime.fromtimestamp(ts, tz=timezone.utc)
        if ts in seen or day.weekday() >= 5:
            raise ValueError(f"{symbol}: duplicate or weekend bar {day.date()}")
        if not (0 < lo <= min(o, c) and max(o, c) <= h):
            raise ValueError(f"{symbol}: inconsistent OHLC on {day.date()}")
        seen.add(ts)


def store_bars(conn, symbol, bars):
    """Replace the symbol's rows, in batches so the collector can get the lock."""
    other = conn.execute("SELECT session FROM candles WHERE pair=? AND session<>? LIMIT 1",
                         (symbol, SESSION)).fetchone()
    if other:
        raise ValueError(f"{symbol} already exists in candles as session {other[0]}")
    for i in range(0, len(bars), BATCH_ROWS):
        conn.executemany(
            "INSERT OR REPLACE INTO candles (pair, ts, open, high, low, close, volume, session)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(symbol, *b, SESSION) for b in bars[i:i + BATCH_ROWS]])
        conn.commit()


def _date(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def backfill(conn, headers, force=False, symbols=None, today=None, fetch=fetch_bars,
             exchange=fetch_exchange):
    """Fetch first, then write. A killed run resumes at the first symbol whose
    fetched_through is not yet yesterday. Yesterday, because today's bar may be
    partial; yesterday's session always closed before 00:00 UTC."""
    today = today or datetime.now(timezone.utc).date()
    end_date = (today - timedelta(days=1)).isoformat()
    wanted = conn.execute("SELECT symbol, fetched_through FROM instruments WHERE session=?"
                          " ORDER BY symbol", (SESSION,)).fetchall()
    done = []
    for sym, through in wanted:
        if (symbols and sym not in symbols) or (through == end_date and not force):
            continue
        bars = fetch(sym, end_date, headers)
        check_bars(sym, bars)
        exch = exchange(sym, headers)
        store_bars(conn, sym, bars)
        conn.execute("UPDATE instruments SET exchange=?, data_start=?, data_end=?, n_bars=?, feed=?,"
                     " adjustment=?, fetched_through=?, fetched_at=? WHERE symbol=?",
                     (exch, _date(bars[0][0]), _date(bars[-1][0]), len(bars), FEED, ADJUSTMENT,
                      end_date, datetime.now(timezone.utc).isoformat(timespec="seconds"), sym))
        conn.commit()
        main.log(f"[{sym}] {len(bars)} daily bars {_date(bars[0][0])} .. {_date(bars[-1][0])}")
        done.append(sym)
    return done


def report(conn):
    rows = conn.execute("SELECT symbol, listed_from, data_start, data_end, n_bars, exchange"
                        " FROM instruments WHERE session=? ORDER BY symbol", (SESSION,)).fetchall()
    print(f"{'symbol':7}{'exch':7}{'listed':12}{'first bar':12}{'last bar':12}{'bars':>6}{'years':>7}")
    short = []
    for sym, listed, start, end, n, exch in rows:
        if not start:
            print(f"{sym:7}(not fetched)")
            short.append(sym)
            continue
        yrs = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).days / 365.25
        print(f"{sym:7}{exch or '?':7}{listed:12}{start:12}{end:12}{n:>6}{yrs:>7.2f}")
        if yrs < MIN_YEARS:
            short.append(sym)
    if short:
        print(f"Read: under {MIN_YEARS:.0f} years for {', '.join(short)}. Deeper history needs"
              " Tiingo's free tier (a key from the owner).")
    else:
        print(f"Read: every symbol has at least {MIN_YEARS:.0f} years, but Alpaca's history starts"
              " in 2016, so no window here contains 2008-15.")


def main_cli():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--backfill", action="store_true")
    ap.add_argument("--force", action="store_true", help="re-fetch symbols already current")
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()
    conn = sqlite3.connect(main.DB_PATH, timeout=main.DB_TIMEOUT_S)
    ensure_tables(conn)
    seed_universe(conn)
    if args.backfill:
        backfill(conn, load_credentials(), force=args.force)
    report(conn)


if __name__ == "__main__":
    main_cli()
