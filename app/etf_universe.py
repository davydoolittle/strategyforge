"""
The screen's ETF universe (P9.1c): the 50 most-traded US-listed ETFs, re-ranked
every week, so funds rise into the list and fall out of it.

  listed     Tiingo's free supported_tickers.zip: assetType ETF, priced in USD,
             on a US exchange, with a bar in the last week of the file
  tradable   and an active Alpaca asset, which also gives the fund's name
  excluded   ETNs, and leveraged or inverse funds by name (2x, 3X, Ultra, Bull,
             Bear, Short, Inverse...; LEVERAGED_RE, INVERSE_RE): a daily-reset
             fund is a trading tool, and they would take a third of the list
  ranked     by the median daily dollar volume (close x volume, unadjusted)
             over the last 20 sessions, from Alpaca; at least 15 sessions

Most liquid, not largest: this is not an AUM ranking. Every run is kept
(etf_universe_runs, etf_universe), so who entered and who left is on record.

This list drives the momentum screen only. Research stays on the ten fixed ETFs
in equities.UNIVERSE (F16, F17, the pattern ledger): a list chosen today from
funds that survived until today would bias any backtest run on it.

Bars for members outside equities.UNIVERSE come from Alpaca (2016 on, adjusted),
cut at the last gap of over 30 days (a reused ticker), one vendor per symbol as
equities.py requires; the ten keep their Tiingo bars,
refreshed by the patterns job. A fund that drops out keeps its row and bars,
which are simply no longer refreshed.

  python etf_universe.py             # rank and print, write nothing
  python etf_universe.py --refresh   # rank, store, fetch members' bars (the job)
  python etf_universe.py --show      # the latest stored list
"""

import argparse
import csv
import io
import re
import sqlite3
import statistics
import time
import urllib.parse
import urllib.request
import zipfile
from datetime import date, datetime, timedelta, timezone

import equities as E
import main

TOP_N = 50
TIINGO_TICKERS_URL = "https://apimedia.tiingo.com/docs/tiingo/daily/supported_tickers.zip"
US_EXCHANGES = {"NYSE", "NYSE ARCA", "NYSE MKT", "NASDAQ", "BATS"}
LISTED_WITHIN_DAYS = 7      # of the newest endDate in the file
VOLUME_SESSIONS = 20
MIN_SESSIONS = 15
LOOKBACK_DAYS = 35          # calendar days that hold 20 sessions with holidays
BATCH_SYMBOLS = 200         # symbols per Alpaca bars request
END_LAG_MIN = 20            # the free plan refuses SIP bars from the last 15 minutes
LISTING_GAP_DAYS = 30       # a longer gap in a symbol's bars means the ticker was reused

# Lower-cased Alpaca names. "Short" alone is inverse (ProShares Short S&P500),
# but not in "Short-Term", "Short Treasury", "Short Duration"...
LEVERAGED_RE = re.compile(r"\bultra(pro|short)?\b|ultrapro|(?<![\w.])-?\d+(\.\d+)?x\b|\bleveraged\b"
                          r"|\bbull\b|\bbear\b|\bdaily target\b")
INVERSE_RE = re.compile(r"\binverse\b|\bshort\b(?![- ]?(term|duration|maturity|treasury|bond|tips"
                        r"|government|municipal|muni|corporate|high yield|income|-term))")
ETN_RE = re.compile(r"\betns?\b|exchange[- ]traded notes?")
# A long/short fund holds both sides: not inverse. Removed before INVERSE_RE.
LONG_SHORT_RE = re.compile(r"long[/ -]short|short horizon")


def ensure_tables(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS etf_universe_runs (
        run_date TEXT PRIMARY KEY,      -- UTC date of the run
        created_at TEXT,
        data_end TEXT,                  -- newest session in the volume window
        n_listed INTEGER, n_tradable INTEGER, n_excluded INTEGER, n_ranked INTEGER,
        top_n INTEGER,
        done_at TEXT                    -- written last: the restart marker
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS etf_universe (
        run_date TEXT NOT NULL, rank INTEGER NOT NULL, symbol TEXT NOT NULL,
        name TEXT, exchange TEXT, dollar_vol REAL, n_sessions INTEGER,
        PRIMARY KEY (run_date, symbol)
    )""")
    conn.commit()


# ------------------------------------------------------------------ fetch ---

def _download(url):
    req = urllib.request.Request(url, headers={"User-Agent": "strategyforge/1.0"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()


def listed_etfs(zip_bytes):
    """Tiingo's ticker list -> set of active US-listed ETF symbols."""
    z = zipfile.ZipFile(io.BytesIO(zip_bytes))
    rows = list(csv.DictReader(io.TextIOWrapper(z.open(z.namelist()[0]), encoding="utf-8")))
    etfs = [r for r in rows if r["assetType"] == "ETF" and r["priceCurrency"] == "USD"
            and r["exchange"] in US_EXCHANGES and r["endDate"]]
    if not etfs:
        raise ValueError("Tiingo ticker list has no US ETFs: format changed?")
    newest = max(r["endDate"] for r in etfs)
    cut = (date.fromisoformat(newest) - timedelta(days=LISTED_WITHIN_DAYS)).isoformat()
    return {r["ticker"].upper() for r in etfs if r["endDate"] >= cut}


def exclusion(name):
    """Why a fund is left out, or None."""
    n = (name or "").lower()
    if ETN_RE.search(n):
        return "ETN"
    if LEVERAGED_RE.search(n):
        return "leveraged"
    if INVERSE_RE.search(LONG_SHORT_RE.sub(" ", n)):
        return "inverse"
    return None


def tradable_assets(headers, get=E._get):
    """symbol -> (name, exchange) for active, tradable Alpaca US equities."""
    assets = get(f"{E.PAPER_URL}/assets?status=active&asset_class=us_equity", headers)
    return {a["symbol"]: (a.get("name") or "", a.get("exchange"))
            for a in assets if a.get("tradable") and a.get("status") == "active"}


def daily_dollar_volume(symbols, headers, now=None, get=E._get, sleep=time.sleep):
    """symbol -> [(date, close x volume)] oldest first, last LOOKBACK_DAYS."""
    now = now or datetime.now(timezone.utc)
    start = (now - timedelta(days=LOOKBACK_DAYS)).date().isoformat()
    end = (now - timedelta(minutes=END_LAG_MIN)).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = {}
    syms = sorted(symbols)
    for i in range(0, len(syms), BATCH_SYMBOLS):
        token = None
        while True:
            q = {"symbols": ",".join(syms[i:i + BATCH_SYMBOLS]), "timeframe": "1Day", "start": start,
                 "end": end, "adjustment": "raw", "feed": E.FEED, "limit": 10000, "sort": "asc"}
            if token:
                q["page_token"] = token
            page = get(f"{E.DATA_URL}?{urllib.parse.urlencode(q)}", headers)
            for sym, bars in (page.get("bars") or {}).items():
                out.setdefault(sym, []).extend((b["t"][:10], b["c"] * b["v"]) for b in bars)
            token = page.get("next_page_token")
            sleep(E.PAGE_SLEEP_S)
            if not token:
                break
    return out


# ------------------------------------------------------------------- rank ---

def rank(listed, assets, volumes, top_n=TOP_N):
    """Pure: the ranking from the three inputs. Returns (members, stats)."""
    tradable = {s: assets[s] for s in listed if s in assets}
    excluded = {s: why for s, (name, _) in tradable.items() if (why := exclusion(name))}
    scored = []
    for s, (name, exch) in tradable.items():
        if s in excluded:
            continue
        dv = [v for _, v in volumes.get(s, [])][-VOLUME_SESSIONS:]
        if len(dv) < MIN_SESSIONS:
            continue
        scored.append({"symbol": s, "name": name, "exchange": exch,
                       "dollar_vol": statistics.median(dv), "n_sessions": len(dv)})
    scored.sort(key=lambda r: (-r["dollar_vol"], r["symbol"]))
    members = scored[:top_n]
    for i, r in enumerate(members, 1):
        r["rank"] = i
    data_end = max((d for s in members for d, _ in volumes[s["symbol"]][-1:]), default=None)
    stats = {"n_listed": len(listed), "n_tradable": len(tradable), "n_excluded": len(excluded),
             "n_ranked": len(scored), "data_end": data_end, "excluded": excluded}
    return members, stats


def compute(headers=None, now=None, download=_download, get=E._get, sleep=time.sleep):
    headers = headers or E.load_credentials()
    listed = listed_etfs(download(TIINGO_TICKERS_URL))
    assets = tradable_assets(headers, get)
    candidates = {s for s in listed if s in assets and not exclusion(assets[s][0])}
    volumes = daily_dollar_volume(candidates, headers, now, get, sleep)
    return rank(listed, assets, volumes)


# ------------------------------------------------------------------ store ---

def store(conn, members, stats, run_date):
    """One run: membership rows, then instruments rows for new members, then
    the run marker. A rerun the same day replaces that day's list."""
    ensure_tables(conn)
    E.ensure_tables(conn)
    conn.execute("DELETE FROM etf_universe WHERE run_date=?", (run_date,))
    conn.execute("DELETE FROM etf_universe_runs WHERE run_date=?", (run_date,))
    conn.executemany("INSERT INTO etf_universe (run_date, rank, symbol, name, exchange, dollar_vol,"
                     " n_sessions) VALUES (?, ?, ?, ?, ?, ?, ?)",
                     [(run_date, m["rank"], m["symbol"], m["name"], m["exchange"], m["dollar_vol"],
                       m["n_sessions"]) for m in members])
    # spread_bps stays NULL for screen-only funds: engine.py then refuses to
    # backtest them until someone sets a cost on purpose.
    conn.executemany("INSERT OR IGNORE INTO instruments (symbol, asset_class, tick_size, session)"
                     " VALUES (?, 'etf', ?, ?)", [(m["symbol"], E.TICK_SIZE, E.SESSION) for m in members])
    conn.execute("INSERT INTO etf_universe_runs (run_date, created_at, data_end, n_listed, n_tradable,"
                 " n_excluded, n_ranked, top_n, done_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (run_date, datetime.now(timezone.utc).isoformat(timespec="seconds"), stats["data_end"],
                  stats["n_listed"], stats["n_tradable"], stats["n_excluded"], stats["n_ranked"],
                  TOP_N, datetime.now(timezone.utc).isoformat(timespec="seconds")))
    conn.commit()


def runs(conn):
    """Completed run dates, newest first."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='etf_universe_runs'"
                        ).fetchone():
        return []
    return [r[0] for r in conn.execute("SELECT run_date FROM etf_universe_runs WHERE done_at IS NOT NULL"
                                       " ORDER BY run_date DESC")]


def members(conn, run_date):
    return conn.execute("SELECT rank, symbol, name, dollar_vol FROM etf_universe WHERE run_date=?"
                        " ORDER BY rank", (run_date,)).fetchall()


def latest(conn):
    """(current members, entered, left) by symbol. members is None before the
    first run; entered and left compare with the run before, empty if none."""
    done = runs(conn)
    if not done:
        return None, [], []
    now = [r[1] for r in members(conn, done[0])]
    if len(done) < 2:
        return now, [], []
    before = {r[1] for r in members(conn, done[1])}
    return now, [s for s in now if s not in before], sorted(before - set(now))


def current_listing(bars):
    """Bars after the last gap of more than LISTING_GAP_DAYS. Alpaca keys bars
    by ticker, so a reused ticker joins two securities: DRAM is a company
    delisted around 2017 and, from 2026-04, the Roundhill Memory ETF."""
    start = 0
    for i in range(1, len(bars)):
        if bars[i][0] - bars[i - 1][0] > LISTING_GAP_DAYS * 86400:
            start = i
    return bars[start:]


def fetch_listing_bars(symbol, end_date, headers, get=E._get):
    return current_listing(E.fetch_bars(symbol, end_date, headers, get))


def fetch_member_bars(conn, symbols, headers, today=None):
    """Alpaca bars for members outside the research ten (equities.backfill:
    fetch first, write in batches, resumes where it stopped)."""
    todo = [s for s in symbols if s not in E.UNIVERSE]
    return E.backfill(conn, headers, symbols=todo, today=today, fetch=fetch_listing_bars,
                      exchange=E.fetch_exchange, source="alpaca")


# -------------------------------------------------------------------- cli ---

def render(members_, stats):
    L = [f"Top {len(members_)} US ETFs by median {VOLUME_SESSIONS}-day dollar volume, data through "
         f"{stats['data_end']}: {stats['n_listed']} listed, {stats['n_tradable']} tradable, "
         f"{stats['n_excluded']} excluded (leveraged, inverse, ETN), {stats['n_ranked']} ranked"]
    for m in members_:
        L.append(f"{m['rank']:>3} {m['symbol']:<6} ${m['dollar_vol'] / 1e9:>6.2f}B  {m['name'][:60]}")
    return "\n".join(L)


def main_cli(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--refresh", action="store_true", help="store the ranking and fetch members' bars")
    ap.add_argument("--show", action="store_true", help="print the latest stored list")
    args = ap.parse_args(argv)
    if args.show:
        conn = sqlite3.connect(f"file:{main.DB_PATH}?mode=ro", uri=True, timeout=main.DB_TIMEOUT_S)
        done = runs(conn)
        if not done:
            print("no ETF universe stored yet: etf_universe.py --refresh")
            return 0
        for r in members(conn, done[0]):
            print(f"{r[0]:>3} {r[1]:<6} ${r[3] / 1e9:>6.2f}B  {r[2]}")
        return 0
    headers = E.load_credentials()
    found, stats = compute(headers)
    print(render(found, stats))
    if len(found) < TOP_N:
        raise SystemExit(f"only {len(found)} ETFs ranked, expected {TOP_N}: nothing stored")
    if args.refresh:
        conn = sqlite3.connect(main.DB_PATH, timeout=main.DB_TIMEOUT_S)
        store(conn, found, stats, datetime.now(timezone.utc).date().isoformat())
        done = fetch_member_bars(conn, [m["symbol"] for m in found], headers)
        now, entered, left = latest(conn)
        print(f"stored; bars fetched for {len(done)} symbols; entered {entered or '-'}, left {left or '-'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
