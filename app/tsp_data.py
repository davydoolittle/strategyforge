"""
TSP data: fetch, cache, and align daily series for the tsp_* modules.

  python tsp_data.py --update                 # all configured series, incremental
  python tsp_data.py --import ~/fund-price-history.csv   # a CSV saved from tsp.gov
  python tsp_data.py --status                 # what is stored, how fresh

Storage (forge.db, created by ensure_tables):
  tsp_prices (source, symbol, date, close)  fund prices: source 'tsp' for the
      official share prices (G F C S I and L funds), 'yahoo' for the ETF
      proxies (adjusted close, i.e. dividends reinvested, like the funds)
  context (ts, metric 'tsp:<name>', 'GLOBAL', value)  macro inputs (VIX,
      yields, credit), the collector's table for market-wide metrics. ts is
      16:00 ET of the value's date: a daily close is not known before then.

Sources are small classes with fetch(symbol, start) -> [(date, close)],
looked up by name in SOURCES, so a series moves to another provider by
changing tsp_config.json. tsp.gov answers scripted downloads with HTTP 403
from some networks; the fetcher still tries every day with an honest
User-Agent, and --import loads a CSV downloaded in a browser instead.
"""

import argparse
import csv
import io
import json
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

import main
import tsp_rules as R

BATCH = 200                 # rows per write transaction (V2_PROMPT §10.9)
REFETCH_DAYS = 7            # re-read this many days before the last stored one


def ensure_tables(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS tsp_prices (
        source TEXT NOT NULL,           -- tsp | yahoo | ...
        symbol TEXT NOT NULL,           -- G F C S I 'L 2050' | SPY VXF ...
        date TEXT NOT NULL,             -- YYYY-MM-DD, the close's trading day
        close REAL NOT NULL,
        PRIMARY KEY (source, symbol, date)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS context (
        ts INTEGER NOT NULL, metric TEXT NOT NULL, pair TEXT NOT NULL, value REAL,
        PRIMARY KEY (ts, metric, pair)
    )""")
    conn.execute("CREATE TABLE IF NOT EXISTS poll_errors (ts INTEGER, source TEXT, error TEXT)")
    conn.commit()


# ---------------------------------------------------------------- sources ---

def _get(url, user_agent, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


class YahooSource:
    name = "yahoo"

    def __init__(self, user_agent):
        self.ua = user_agent

    def fetch(self, symbol, start):
        p1 = int(datetime.combine(start, datetime.min.time(), timezone.utc).timestamp())
        p2 = int(datetime.now(timezone.utc).timestamp()) + 86400
        url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}"
               f"?period1={p1}&period2={p2}&interval=1d&events=div%2Csplit")
        return self.parse(json.loads(_get(url, self.ua)))

    @staticmethod
    def parse(doc):
        res = (doc.get("chart") or {}).get("result") or []
        if not res:
            err = (doc.get("chart") or {}).get("error")
            raise RuntimeError(f"yahoo: no result ({err})")
        r = res[0]
        off = r["meta"].get("gmtoffset", 0)
        ts = r.get("timestamp") or []
        q = r["indicators"]["quote"][0]["close"]
        adj = (r["indicators"].get("adjclose") or [{}])[0].get("adjclose")
        vals = adj if adj and any(v is not None for v in adj) else q
        out = {}
        for t, v in zip(ts, vals):
            if v is not None:
                out[datetime.fromtimestamp(t + off, timezone.utc).date()] = float(v)
        return sorted(out.items())


class FredSource:
    name = "fred"

    def __init__(self, user_agent):
        self.ua = user_agent

    def fetch(self, symbol, start):
        url = (f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={urllib.parse.quote(symbol)}"
               f"&cosd={start.isoformat()}")
        return self.parse(_get(url, self.ua, timeout=60))

    @staticmethod
    def parse(text):
        out = []
        rows = csv.reader(io.StringIO(text))
        next(rows, None)
        for row in rows:
            if len(row) >= 2 and row[1] not in ("", "."):
                out.append((date.fromisoformat(row[0]), float(row[1])))
        return out


class TspCsvSource:
    """tsp.gov share-price history: Date, L funds..., G Fund, F Fund, C Fund,
    S Fund, I Fund. Returns {symbol: [(date, close)]}."""
    name = "tsp"

    def __init__(self, user_agent, url):
        self.ua, self.url = user_agent, url

    def fetch_all(self, start):
        q = urllib.parse.urlencode({"startdate": start.isoformat(),
                                    "enddate": date.today().isoformat(),
                                    "Lfunds": 1, "InvFunds": 1, "download": 1})
        return self.parse(_get(f"{self.url}?{q}", self.ua, timeout=60))

    @staticmethod
    def parse(text):
        text = text.lstrip("﻿")
        if text.lstrip().startswith("<"):
            raise RuntimeError("tsp.gov returned an HTML page, not the CSV")
        rows = list(csv.reader(io.StringIO(text)))
        if not rows:
            raise RuntimeError("empty TSP CSV")
        head = [h.strip() for h in rows[0]]
        if not head or head[0].lower() != "date":
            raise RuntimeError(f"not a TSP share-price CSV (header {head[:4]})")
        cols = {}
        for i, h in enumerate(head[1:], 1):
            sym = h[:-5].strip() if h.endswith(" Fund") else h
            if sym:
                cols[i] = sym
        out = {s: [] for s in cols.values()}
        for row in rows[1:]:
            if not row or not row[0].strip():
                continue
            d = _parse_date(row[0].strip())
            for i, sym in cols.items():
                v = row[i].strip() if i < len(row) else ""
                if v:
                    out[sym].append((d, float(v.replace(",", ""))))
        return {s: sorted(v) for s, v in out.items() if v}


def _parse_date(s):
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    raise RuntimeError(f"unrecognised date {s!r} in TSP CSV")


def sources(cfg):
    ua = cfg["data"]["user_agent"]
    return {"yahoo": YahooSource(ua), "fred": FredSource(ua),
            "tsp": TspCsvSource(ua, cfg["data"]["tsp_csv_url"])}


# ------------------------------------------------------------------ store ---

def store_prices(conn, source, symbol, rows):
    n = 0
    for i in range(0, len(rows), BATCH):
        chunk = rows[i:i + BATCH]
        conn.executemany("INSERT OR REPLACE INTO tsp_prices (source, symbol, date, close)"
                         " VALUES (?, ?, ?, ?)",
                         [(source, symbol, d.isoformat(), c) for d, c in chunk])
        conn.commit()
        n += len(chunk)
    return n


def close_ts(d):
    """Unix ts of 16:00 ET on date d: when a daily close becomes known."""
    return int(R.et_to_utc(d, "16:00").timestamp())


def store_macro(conn, name, rows):
    for i in range(0, len(rows), BATCH):
        conn.executemany("INSERT OR REPLACE INTO context (ts, metric, pair, value)"
                         " VALUES (?, ?, 'GLOBAL', ?)",
                         [(close_ts(d), f"tsp:{name}", float(v)) for d, v in rows[i:i + BATCH]])
        conn.commit()
    return len(rows)


def last_price_date(conn, source, symbol):
    r = conn.execute("SELECT MAX(date) FROM tsp_prices WHERE source=? AND symbol=?",
                     (source, symbol)).fetchone()[0]
    return date.fromisoformat(r) if r else None


def last_macro_date(conn, name):
    r = conn.execute("SELECT MAX(ts) FROM context WHERE metric=? AND pair='GLOBAL'",
                     (f"tsp:{name}",)).fetchone()[0]
    return R.to_et(datetime.fromtimestamp(r, timezone.utc)).date() if r else None


def _start(last, cfg):
    first = date.fromisoformat(cfg["data"]["history_start"])
    return first if last is None else max(first, last - timedelta(REFETCH_DAYS))


def import_tsp_csv(conn, text):
    data = TspCsvSource.parse(text)
    return {sym: store_prices(conn, "tsp", sym, rows) for sym, rows in data.items()}


def update(conn, cfg, srcs=None, log=print):
    """Fetch every configured series from just before its last stored date.
    A failing series is recorded in poll_errors and the rest go on.
    Returns {series: rows stored or 'error: ...'}."""
    ensure_tables(conn)
    srcs = srcs or sources(cfg)
    done = {}

    def attempt(label, fn):
        try:
            done[label] = fn()
        except Exception as e:  # noqa: BLE001 — recorded, retried next run
            main.safe_record_error(conn, f"tsp-{label}", e)
            done[label] = f"error: {str(e)[:120]}"

    attempt("tsp.gov", lambda: sum(
        store_prices(conn, "tsp", sym, rows) for sym, rows in
        srcs["tsp"].fetch_all(_start(last_price_date(conn, "tsp", "C"), cfg)).items()))
    for syms in cfg["data"]["proxies"].values():
        for sym in syms:
            attempt(sym, lambda sym=sym: store_prices(
                conn, "yahoo", sym, srcs["yahoo"].fetch(sym, _start(last_price_date(conn, "yahoo", sym), cfg))))
    for name, spec in cfg["data"]["macro"].items():
        attempt(name, lambda name=name, spec=spec: store_macro(
            conn, name, srcs[spec["source"]].fetch(spec["symbol"], _start(last_macro_date(conn, name), cfg))))
    for k, v in done.items():
        log(f"  {k:10s} {v}")
    if str(done.get("tsp.gov", "")).startswith("error"):
        log("  tsp.gov did not serve the CSV. Download it from tsp.gov/share-price-history in a "
            "browser and run: python tsp_data.py --import <file>")
    return done


# ---------------------------------------------------------------- dataset ---

class LookAheadError(IndexError):
    pass


class Dataset:
    """Daily series aligned on one business-day calendar.

    dates: sorted list of date. funds / macro: name -> list aligned to dates
    (None where unknown). Macro values are carried forward from the last
    known close only (never back-filled), so day i holds what was known at
    day i's close. Strategies and signals read through view(t)."""

    def __init__(self, dates, funds, macro, mode, notes=()):
        self.dates, self.funds, self.macro, self.mode = dates, funds, macro, mode
        self.notes = list(notes)
        self.index = {d: i for i, d in enumerate(dates)}

    def __len__(self):
        return len(self.dates)

    def series(self, name):
        if name in self.funds:
            return self.funds[name]
        if name in self.macro:
            return self.macro[name]
        raise KeyError(f"no series {name!r}")

    def has(self, name):
        return name in self.funds or name in self.macro

    def view(self, t):
        return View(self, t)

    def truncated(self, n):
        """The first n days only: the data as it stood on dates[n-1]."""
        return Dataset(self.dates[:n], {k: v[:n] for k, v in self.funds.items()},
                       {k: v[:n] for k, v in self.macro.items()}, self.mode, self.notes)


class View:
    """Dataset as known at the close of day t. Any read past t raises."""

    def __init__(self, ds, t):
        if not 0 <= t < len(ds):
            raise LookAheadError(f"view at {t} outside 0..{len(ds) - 1}")
        self.ds, self.t = ds, t
        self.date = ds.dates[t]

    def at(self, name, i):
        if i > self.t:
            raise LookAheadError(f"{name}[{i}] read at day {self.t} ({self.date})")
        if i < 0:
            return None
        return self.ds.series(name)[i]

    def at_date(self, i):
        if i > self.t:
            raise LookAheadError(f"dates[{i}] read at day {self.t} ({self.date})")
        return self.ds.dates[i]

    def value(self, name, lag=0):
        return self.at(name, self.t - lag)

    def tail(self, name, n):
        """The last n values up to and including day t (fewer at the start)."""
        lo = max(0, self.t - n + 1)
        return self.ds.series(name)[lo:self.t + 1]

    def has(self, name):
        return self.ds.has(name)

    def dates_tail(self, n):
        return self.ds.dates[max(0, self.t - n + 1):self.t + 1]


def _prices(conn, source, symbol):
    return {date.fromisoformat(d): c for d, c in conn.execute(
        "SELECT date, close FROM tsp_prices WHERE source=? AND symbol=? ORDER BY date",
        (source, symbol))}


def _macro(conn, name):
    out = {}
    for ts, v in conn.execute("SELECT ts, value FROM context WHERE metric=? AND pair='GLOBAL'"
                              " ORDER BY ts", (f"tsp:{name}",)):
        out[R.to_et(datetime.fromtimestamp(ts, timezone.utc)).date()] = v
    return out


def _carry(dates, by_date):
    out, last = [], None
    for d in dates:
        if d in by_date:
            last = by_date[d]
        out.append(last)
    return out


def _carry_from_past(dates, by_date):
    """Like _carry, but a value dated between two calendar days (a FRED date
    with no TSP close) is picked up on the next calendar day, never earlier."""
    keys = sorted(by_date)
    out, last, j = [], None, 0
    for d in dates:
        while j < len(keys) and keys[j] <= d:
            last = by_date[keys[j]]
            j += 1
        out.append(last)
    return out


def _splice(dates, series_list):
    """Total-return index from the first series (by priority) that has a
    close on both of two consecutive calendar days. None until one does."""
    idx, out = None, []
    for i, d in enumerate(dates):
        if i == 0:
            out.append(None)
            continue
        p = dates[i - 1]
        r = next((s[d] / s[p] - 1 for s in series_list if d in s and p in s), None)
        if r is None:
            if idx is None and any(d in s for s in series_list):
                idx = 100.0
            out.append(idx)
            continue
        idx = 100.0 if idx is None else idx * (1 + r)
        out.append(idx)
    return out


def load_dataset(conn, cfg, mode="tsp", start=None, end=None):
    """mode 'tsp': the official fund prices, on TSP's own calendar.
    mode 'proxy': ETF proxies (C SPY, S VXF, I VEU then EFA, F AGG; G a
    synthetic index accruing the 13-week T-bill yield), on SPY's calendar.
    The proxy G understates the real G Fund, whose rate follows longer
    Treasuries; the proxy I tracked MSCI EAFE-like indices, while the I Fund
    follows MSCI ACWI IMI ex USA ex China ex Hong Kong since 2024."""
    ensure_tables(conn)
    notes = []
    macro_raw = {name: _macro(conn, name) for name in cfg["data"]["macro"]}
    if mode == "tsp":
        syms = [r[0] for r in conn.execute(
            "SELECT DISTINCT symbol FROM tsp_prices WHERE source='tsp'")]
        raw = {sym: _prices(conn, "tsp", sym) for sym in syms}
        if not raw.get("C"):
            raise RuntimeError("no TSP share prices stored: run tsp_data.py --import <csv> "
                               "(or --update if tsp.gov serves the CSV), or use --data proxy")
        dates = sorted(raw["C"])
        funds = {sym: _carry(dates, s) for sym, s in raw.items()}
    elif mode == "proxy":
        px = {f: [_prices(conn, "yahoo", s) for s in syms]
              for f, syms in cfg["data"]["proxies"].items()}
        anchor = px["C"][0]
        if not anchor:
            raise RuntimeError("no proxy prices stored: run tsp_data.py --update")
        dates = sorted(anchor)
        funds = {f: _splice(dates, lst) for f, lst in px.items()}
        bill = _carry_from_past(dates, macro_raw.get("y3m") or {})
        g, idx = [], None
        for i, d in enumerate(dates):
            if i and bill[i - 1] is not None:
                idx = (idx or 100.0) * (1 + bill[i - 1] / 100 * (d - dates[i - 1]).days / 365)
            g.append(idx)
        funds["G"] = g
        notes.append("proxy data: C=" + "/".join(cfg["data"]["proxies"]["C"]) +
                     " S=" + "/".join(cfg["data"]["proxies"]["S"]) +
                     " I=" + "/".join(cfg["data"]["proxies"]["I"]) +
                     " F=" + "/".join(cfg["data"]["proxies"]["F"]) +
                     " (adjusted closes); G = 13-week T-bill accrual, below the real G Fund rate")
    else:
        raise ValueError(f"unknown data mode {mode!r}")
    macro = {name: _carry_from_past(dates, s) for name, s in macro_raw.items() if s}
    for name in cfg["data"]["macro"]:
        if name not in macro:
            notes.append(f"macro series {name} not stored: inputs that need it are skipped")
    lo = sum(1 for d in dates if start and d < start)
    hi = sum(1 for d in dates if not end or d <= end)
    return Dataset(dates[lo:hi], {k: v[lo:hi] for k, v in funds.items()},
                   {k: v[lo:hi] for k, v in macro.items()}, mode, notes)


def tracking_report(conn, cfg):
    """Daily-return correlation and annualised tracking error of each proxy
    against the official fund, over the dates both have."""
    tsp = {f: _prices(conn, "tsp", f) for f in cfg["data"]["proxies"]}
    out = {}
    for f, syms in cfg["data"]["proxies"].items():
        a = tsp.get(f) or {}
        b = _prices(conn, "yahoo", syms[0])
        common = sorted(set(a) & set(b))
        ra = [a[d] / a[p] - 1 for p, d in zip(common, common[1:])]
        rb = [b[d] / b[p] - 1 for p, d in zip(common, common[1:])]
        if len(ra) < 30:
            continue
        ma, mb = sum(ra) / len(ra), sum(rb) / len(rb)
        cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
        va = sum((x - ma) ** 2 for x in ra)
        vb = sum((y - mb) ** 2 for y in rb)
        diff = [x - y for x, y in zip(ra, rb)]
        md = sum(diff) / len(diff)
        te = (sum((x - md) ** 2 for x in diff) / (len(diff) - 1)) ** 0.5 * 252 ** 0.5
        out[f] = {"proxy": syms[0], "days": len(ra), "corr": cov / (va * vb) ** 0.5 if va and vb else None,
                  "tracking_error_pct": 100 * te}
    return out


def status(conn, cfg):
    ensure_tables(conn)
    print("stored series (first .. last, rows):")
    for src, sym, lo, hi, n in conn.execute(
            "SELECT source, symbol, MIN(date), MAX(date), COUNT(*) FROM tsp_prices"
            " GROUP BY 1, 2 ORDER BY 1, 2"):
        print(f"  {src:6s} {sym:8s} {lo} .. {hi}  {n}")
    for name in cfg["data"]["macro"]:
        r = conn.execute("SELECT MIN(ts), MAX(ts), COUNT(*) FROM context WHERE metric=?"
                         " AND pair='GLOBAL'", (f"tsp:{name}",)).fetchone()
        if r[2]:
            lo, hi = (R.to_et(datetime.fromtimestamp(x, timezone.utc)).date() for x in r[:2])
            print(f"  macro  {name:8s} {lo} .. {hi}  {r[2]}")
        else:
            print(f"  macro  {name:8s} (none)")
    last = last_price_date(conn, "tsp", "C")
    for f, t in tracking_report(conn, cfg).items():
        print(f"  proxy {t['proxy']:4s} for {f}: daily-return correlation {t['corr']:.3f}, "
              f"tracking error {t['tracking_error_pct']:.1f}%/yr over {t['days']} days")
    print("Read: TSP share prices " + (f"run to {last}." if last else
          "are not stored yet; backtests can only use --data proxy until a CSV is imported."))


def main_cli(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[1],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--update", action="store_true", help="fetch every configured series")
    ap.add_argument("--import", dest="import_csv", metavar="CSV",
                    help="load a share-price CSV downloaded from tsp.gov")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--config")
    args = ap.parse_args(argv)
    cfg = R.load_config(args.config)
    import sqlite3
    conn = sqlite3.connect(main.DB_PATH, timeout=main.DB_TIMEOUT_S)
    ensure_tables(conn)
    if args.import_csv:
        with open(args.import_csv, encoding="utf-8-sig") as f:
            got = import_tsp_csv(conn, f.read())
        print("imported: " + ", ".join(f"{k} {v}" for k, v in sorted(got.items())))
        print("Read: official share prices are stored; tsp_backtest.py --data tsp now runs on them.")
    if args.update:
        update(conn, cfg)
    if args.status or not (args.update or args.import_csv):
        status(conn, cfg)


if __name__ == "__main__":
    sys.exit(main_cli())
