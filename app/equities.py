"""
Equities and ETF data layer (P9.1): daily, split- and dividend-adjusted bars
from Tiingo (deep history) or Alpaca (2016 on) into forge.db, next to crypto.

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

One vendor per symbol: a series is never spliced, because each vendor adjusts
from its own base. --compare checks the two against each other on their overlap.

Credentials, next to forge.db: tiingo.env (TIINGO_API_KEY=... or the bare
token), alpaca.env (APCA_API_KEY_ID, APCA_API_SECRET_KEY).

  python equities.py --backfill           # symbols not fetched through yesterday
  python equities.py --backfill --force   # all of them
  python equities.py --backfill --source alpaca
  python equities.py --compare            # Tiingo vs Alpaca, daily returns
  python equities.py --snapshot NAME      # freeze today's bars for a research run
  python equities.py --report             # history depth per symbol
"""

import argparse
import hashlib
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
TIINGO_URL = "https://api.tiingo.com/tiingo/daily"
FEED = "sip"              # consolidated tape; IEX alone is ~2% of volume
ADJUSTMENT = "all"
EARLIEST = "1990-01-01"   # before any vendor's first bar: ask for everything
SESSION = "us_equity"
_ALPACA_START = 1451606400   # 2016-01-01: Alpaca has nothing earlier
TICK_SIZE = 0.01
BATCH_ROWS = 100          # section 10.9
MIN_YEARS = 10.0
PAGE_SLEEP_S = 0.35       # Alpaca free plan: 200 requests a minute
RETURN_TOL = 0.005        # vendors disagree if a daily return differs by this
OHLC_TOL = 1e-6           # adjusted prices are floats: allow rounding, not errors

# Quoted spread in bps, set wide on purpose: one figure has to cover 1993-2026,
# and today's SPY spread is far under this. A5 stresses it 2x and 3x.
SPREAD_BPS = {"SPY": 2.0, "QQQ": 2.0, "IWM": 3.0, "EFA": 4.0, "EEM": 5.0, "TLT": 3.0,
              "IEF": 3.0, "GLD": 3.0, "DBC": 10.0, "UUP": 6.0}
SLIPPAGE_FRAC = 0.5       # cross half the spread per side
MAX_LEVERAGE = 2.0        # Reg-T

# Append only: a symbol's position here sets its optimizer seed, as main.PAIRS
# does for crypto. symbol -> fund inception. Reference dates: Alpaca does not publish them, and
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
    if "source" not in {r[1] for r in conn.execute("PRAGMA table_info(instruments)")}:
        conn.execute("ALTER TABLE instruments ADD COLUMN source TEXT")
        conn.execute("UPDATE instruments SET source='alpaca' WHERE fetched_through IS NOT NULL")
    have = {r[1] for r in conn.execute("PRAGMA table_info(instruments)")}
    for name, typ in (("spread_bps", "REAL"), ("slippage_frac", f"REAL DEFAULT {SLIPPAGE_FRAC}"),
                      ("commission_pct", "REAL DEFAULT 0"), ("max_leverage", f"REAL DEFAULT {MAX_LEVERAGE}")):
        if name not in have:
            conn.execute(f"ALTER TABLE instruments ADD COLUMN {name} {typ}")
    conn.execute("""CREATE TABLE IF NOT EXISTS bar_snapshots (
        snapshot TEXT NOT NULL, pair TEXT NOT NULL, ts INTEGER NOT NULL,
        open REAL, high REAL, low REAL, close REAL, volume REAL,
        PRIMARY KEY (snapshot, pair, ts)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS snapshot_meta (
        snapshot TEXT PRIMARY KEY, created_at TEXT, n_rows INTEGER, sha256 TEXT,
        data_end TEXT, sources TEXT     -- written last: the restart marker
    )""")
    have = {r[1] for r in conn.execute("PRAGMA table_info(candles)")}
    if "session" not in have:
        conn.execute("ALTER TABLE candles ADD COLUMN session TEXT NOT NULL DEFAULT 'crypto'")
    conn.commit()


def seed_universe(conn):
    for sym, listed in UNIVERSE.items():
        conn.execute("INSERT OR IGNORE INTO instruments (symbol, asset_class, tick_size, session,"
                     " listed_from) VALUES (?, 'etf', ?, ?, ?)", (sym, TICK_SIZE, SESSION, listed))
        conn.execute("UPDATE instruments SET spread_bps=? WHERE symbol=? AND spread_bps IS NULL",
                     (SPREAD_BPS[sym], sym))
    conn.commit()


def _env_path(name):
    return os.path.join(os.path.dirname(os.path.abspath(main.DB_PATH)), name)


def load_tiingo_credentials(path=None):
    path = path or _env_path("tiingo.env")
    if not os.path.exists(path):
        raise SystemExit(f"missing {path}: needs TIINGO_API_KEY")
    with open(path) as f:
        lines = [x.strip() for x in f if x.strip() and not x.startswith("#")]
    token = lines[0].rpartition("=")[2].strip("'\" ") if lines else ""
    if not token:
        raise SystemExit(f"{path} has no token")
    return {"Authorization": f"Token {token}"}


def load_credentials(path=None):
    path = path or _env_path("alpaca.env")
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


def fetch_tiingo(symbol, end_date, headers, get=_get):
    """Every daily bar through end_date, oldest first, on Tiingo's adjusted prices."""
    q = urllib.parse.urlencode({"startDate": EARLIEST, "endDate": end_date, "format": "json"})
    return [_parse_tiingo(b) for b in get(f"{TIINGO_URL}/{urllib.parse.quote(symbol)}/prices?{q}",
                                          headers)]


def _parse_tiingo(b):
    day = datetime.fromisoformat(b["date"].replace("Z", "+00:00")).date()
    ts = int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())
    return ts, b["adjOpen"], b["adjHigh"], b["adjLow"], b["adjClose"], b["adjVolume"]


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
        if not (lo > 0 and lo <= min(o, c) * (1 + OHLC_TOL) and max(o, c) <= h * (1 + OHLC_TOL)):
            raise ValueError(f"{symbol}: inconsistent OHLC on {day.date()}")
        seen.add(ts)


def store_bars(conn, symbol, bars):
    """Replace the symbol's rows, in batches so the collector can get the lock.
    A date the new series lacks goes too: one symbol, one vendor."""
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
    new = {b[0] for b in bars}
    stale = [(symbol, r[0]) for r in conn.execute("SELECT ts FROM candles WHERE pair=?", (symbol,))
             if r[0] not in new]
    for i in range(0, len(stale), BATCH_ROWS):
        conn.executemany("DELETE FROM candles WHERE pair=? AND ts=?", stale[i:i + BATCH_ROWS])
        conn.commit()


def _date(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def compare(a, b):
    """Two vendors' series for one symbol: dates only one has, and how often
    their close-to-close returns differ. Returns, because each adjusts from its
    own base, so price levels are not comparable."""
    ca, cb = {r[0]: r[4] for r in a}, {r[0]: r[4] for r in b}
    both = sorted(set(ca) & set(cb))
    gaps = [abs((ca[t] / ca[p]) - (cb[t] / cb[p])) for p, t in zip(both, both[1:])]
    return {"overlap": len(both), "only_a": len(ca) - len(both), "only_b": len(cb) - len(both),
            "n_diff": sum(1 for g in gaps if g > RETURN_TOL), "max_diff": max(gaps, default=0.0),
            "total_a": ca[both[-1]] / ca[both[0]] - 1 if both else None,
            "total_b": cb[both[-1]] / cb[both[0]] - 1 if both else None}


def backfill(conn, headers, force=False, symbols=None, today=None, fetch=fetch_tiingo,
             exchange=lambda sym, headers: None, source="tiingo"):
    """Fetch first, then write. A killed run resumes at the first symbol whose
    fetched_through is not yet yesterday. Yesterday, because today's bar may be
    partial; yesterday's session always closed before 00:00 UTC."""
    today = today or datetime.now(timezone.utc).date()
    end_date = (today - timedelta(days=1)).isoformat()
    wanted = conn.execute("SELECT symbol, fetched_through, source FROM instruments WHERE session=?"
                          " ORDER BY symbol", (SESSION,)).fetchall()
    done = []
    for sym, through, have in wanted:
        if (symbols and sym not in symbols) or (through == end_date and have == source and not force):
            continue
        bars = fetch(sym, end_date, headers)
        check_bars(sym, bars)
        exch = exchange(sym, headers)
        store_bars(conn, sym, bars)
        conn.execute("UPDATE instruments SET exchange=COALESCE(?, exchange), data_start=?, data_end=?,"
                     " n_bars=?, source=?, feed=?, adjustment=?, fetched_through=?, fetched_at=?"
                     " WHERE symbol=?",
                     (exch, _date(bars[0][0]), _date(bars[-1][0]), len(bars), source,
                      FEED if source == "alpaca" else None, ADJUSTMENT, end_date, datetime.now(timezone.utc).isoformat(timespec="seconds"), sym))
        conn.commit()
        main.log(f"[{sym}] {len(bars)} daily bars {_date(bars[0][0])} .. {_date(bars[-1][0])}")
        done.append(sym)
    return done


def report(conn):
    rows = conn.execute("SELECT symbol, listed_from, data_start, data_end, n_bars, source"
                        " FROM instruments WHERE session=? ORDER BY symbol", (SESSION,)).fetchall()
    print(f"{'symbol':7}{'source':8}{'listed':12}{'first bar':12}{'last bar':12}{'bars':>6}{'years':>7}")
    short = []
    for sym, listed, start, end, n, src in rows:
        if not start:
            print(f"{sym:7}(not fetched)")
            short.append(sym)
            continue
        yrs = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).days / 365.25
        print(f"{sym:7}{src or '?':8}{listed:12}{start:12}{end:12}{n:>6}{yrs:>7.2f}")
        if yrs < MIN_YEARS:
            short.append(sym)
    if short:
        print(f"Read: under {MIN_YEARS:.0f} years for {', '.join(short)}. Deeper history needs"
              " Tiingo's free tier (a key from the owner).")
    else:
        first = max(r[2] for r in rows)
        print(f"Read: every symbol has at least {MIN_YEARS:.0f} years. All ten overlap from {first}.")


def snapshot(conn, name):
    """Copy every us_equity bar to bar_snapshots under `name`. Restartable:
    rerun after a kill and only the missing rows go in. A finished snapshot is
    never changed, so reusing its name is refused unless the bars still match."""
    rows = conn.execute("SELECT pair, ts, open, high, low, close, volume FROM candles"
                        " WHERE session=? ORDER BY pair, ts", (SESSION,)).fetchall()
    if not rows:
        raise ValueError("no us_equity bars to snapshot")
    digest = hashlib.sha256(repr(rows).encode()).hexdigest()
    done = conn.execute("SELECT sha256 FROM snapshot_meta WHERE snapshot=?", (name,)).fetchone()
    if done:
        if done[0] != digest:
            raise ValueError(f"snapshot {name} exists with different bars")
        return digest
    have = {(r[0], r[1]) for r in conn.execute(
        "SELECT pair, ts FROM bar_snapshots WHERE snapshot=?", (name,))}
    todo = [(name, *r) for r in rows if (r[0], r[1]) not in have]
    for i in range(0, len(todo), BATCH_ROWS):
        conn.executemany("INSERT INTO bar_snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                         todo[i:i + BATCH_ROWS])
        conn.commit()
    sources = ",".join(sorted({r[0] for r in conn.execute(
        "SELECT source FROM instruments WHERE session=? AND source IS NOT NULL", (SESSION,))}))
    conn.execute("INSERT INTO snapshot_meta VALUES (?, ?, ?, ?, ?, ?)",
                 (name, datetime.now(timezone.utc).isoformat(timespec="seconds"), len(rows), digest,
                  _date(max(r[1] for r in rows)), sources))
    conn.commit()
    return digest


def compare_report(conn):
    """Read-only: fetches both vendors and prints where they disagree."""
    th, ah = load_tiingo_credentials(), load_credentials()
    end = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
    print(f"{'symbol':7}{'overlap':>8}{'tiingo only':>12}{'alpaca only':>12}"
          f"{'days >' + format(RETURN_TOL, '.1%'):>11}{'max diff':>10}{'total T':>9}{'total A':>9}")
    bad = []
    for (sym,) in conn.execute("SELECT symbol FROM instruments WHERE session=? ORDER BY symbol",
                               (SESSION,)).fetchall():
        tb = [b for b in fetch_tiingo(sym, end, th) if b[0] >= _ALPACA_START]
        c = compare(tb, fetch_bars(sym, end, ah))
        print(f"{sym:7}{c['overlap']:>8}{c['only_a']:>12}{c['only_b']:>12}{c['n_diff']:>11}"
              f"{c['max_diff']:>10.2%}{c['total_a']:>9.1%}{c['total_b']:>9.1%}")
        if c["n_diff"] or c["only_a"] or c["only_b"]:
            bad.append(sym)
    print(f"Read: vendors differ on {', '.join(bad)}; see the columns above." if bad
          else "Read: the two vendors agree on every date and every daily return.")


def main_cli():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--backfill", action="store_true")
    ap.add_argument("--force", action="store_true", help="re-fetch symbols already current")
    ap.add_argument("--source", choices=("tiingo", "alpaca"), default="tiingo")
    ap.add_argument("--compare", action="store_true", help="Tiingo vs Alpaca, read-only")
    ap.add_argument("--snapshot", metavar="NAME")
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()
    conn = sqlite3.connect(main.DB_PATH, timeout=main.DB_TIMEOUT_S)
    ensure_tables(conn)
    seed_universe(conn)
    if args.compare:
        compare_report(conn)
    if args.backfill and args.source == "alpaca":
        backfill(conn, load_credentials(), force=args.force, fetch=fetch_bars,
                 exchange=fetch_exchange, source="alpaca")
    elif args.backfill:
        backfill(conn, load_tiingo_credentials(), force=args.force)
    if args.snapshot:
        print(f"snapshot {args.snapshot}: sha256 {snapshot(conn, args.snapshot)}")
    report(conn)


if __name__ == "__main__":
    main_cli()
