"""
Macro history for the pattern ledger (patterns.py), from the primary sources.

  treasury  daily par yields, 1990 on      home.treasury.gov
  cboe      VIX close, 1990 on             cdn.cboe.com
  bls       CPI, unemployment, 1990 on     api.bls.gov
  nyfed     fed funds target, 2000 on      markets.newyorkfed.org
  tiingo    adjusted closes for ETFs outside the research universe

macro_series keeps `date` (what the number describes) and `usable` (the first
day it could be known). A pattern may only read a value on or after `usable`:
CPI for June is published in July, and using it on June 30 would be look-ahead.
BLS values are today's vintage, so revisions are a small remaining leak.

Nothing goes in candles or bar_snapshots, so research runs cannot see this.

  python macro_data.py        # fetch everything, replacing each series
"""

import csv
import io
import json
import sqlite3
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone

import equities
import main

TREASURY = ("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
            "daily-treasury-rates.csv/{y}/all?type=daily_treasury_yield_curve&_format=csv")
VIX_URL = "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"
BLS_URL = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
EFFR_URL = ("https://markets.newyorkfed.org/api/rates/unsecured/effr/search.json"
            "?startDate={a}&endDate={b}")
BLS_SERIES = {"cpi": "CUUR0000SA0", "unemployment": "LNS14000000"}
# conservative release days: CPI is out by mid-month, jobs by the second Friday
BLS_RELEASE_DAY = {"cpi": 20, "unemployment": 12}
TIINGO_EXTRA = ("IWD", "IWF", "XLE", "XLP", "XLU", "HYG", "LQD", "USO")
FIRST_YEAR = 1990
BLS_SPAN = 10               # the unregistered BLS API caps a request at 10 years
BATCH_ROWS = 100
UA = "strategyforge/1.0"


def ensure_tables(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS macro_series (
        series TEXT NOT NULL, date TEXT NOT NULL, usable TEXT NOT NULL, value REAL,
        source TEXT, PRIMARY KEY (series, date)
    )""")
    conn.commit()


def _text(url, data=None, headers=None):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read().decode()


def parse_treasury(text):
    """{"y3m": [(date, pct)], "y2y": ..., "y10y": ...} from one year's CSV. Early
    years leave some tenors blank."""
    out = {"y3m": [], "y2y": [], "y10y": []}
    cols = {"y3m": "3 Mo", "y2y": "2 Yr", "y10y": "10 Yr"}
    for row in csv.DictReader(io.StringIO(text)):
        d = datetime.strptime(row["Date"], "%m/%d/%Y").date()
        for key, col in cols.items():
            if (row.get(col) or "").strip():
                out[key].append((d, float(row[col])))
    return out


def parse_vix(text):
    return [(datetime.strptime(r["DATE"], "%m/%d/%Y").date(), float(r["CLOSE"]))
            for r in csv.DictReader(io.StringIO(text))]


def parse_bls(payload, name):
    """[(first of the month, value, usable date)], oldest first."""
    rows = []
    for x in payload:
        if x["period"].startswith("M") and x["period"] != "M13" and x["value"] != "-":
            y, m = int(x["year"]), int(x["period"][1:])
            nxt = date(y + (m == 12), m % 12 + 1, BLS_RELEASE_DAY[name])
            rows.append((date(y, m, 1), float(x["value"]), nxt))
    return sorted(rows)


def parse_effr(text):
    """[(date, top of the target range or the target)] oldest first."""
    rows = []
    for x in json.loads(text)["refRates"]:
        target = x.get("targetRateTo", x.get("targetRateFrom"))
        if target is not None:
            rows.append((date.fromisoformat(x["effectiveDate"]), float(target)))
    return sorted(rows)


def store(conn, series, rows, source):
    """rows: (date, value, usable). Fetch first; each series replaced whole."""
    conn.execute("DELETE FROM macro_series WHERE series=?", (series,))
    conn.commit()
    data = [(series, d.isoformat(), u.isoformat(), v, source) for d, v, u in rows]
    for i in range(0, len(data), BATCH_ROWS):
        conn.executemany("INSERT OR REPLACE INTO macro_series VALUES (?, ?, ?, ?, ?)",
                         data[i:i + BATCH_ROWS])
        conn.commit()
    return len(data)


def next_day(rows):
    """A daily close is usable the next calendar day."""
    return [(d, v, d + timedelta(days=1)) for d, v in rows]


def refresh(conn):
    this_year = datetime.now(timezone.utc).year
    curves = {"y3m": [], "y2y": [], "y10y": []}
    for y in range(FIRST_YEAR, this_year + 1):
        got = parse_treasury(_text(TREASURY.format(y=y)))
        for k in curves:
            curves[k] += got[k]
        time.sleep(0.2)
    for k, rows in curves.items():
        print(f"{k}: {store(conn, k, next_day(sorted(rows)), 'treasury')} rows")
    print(f"vix: {store(conn, 'vix', next_day(parse_vix(_text(VIX_URL))), 'cboe')} rows")
    for name, sid in BLS_SERIES.items():
        rows = []
        for y in range(FIRST_YEAR, this_year + 1, BLS_SPAN):
            body = json.dumps({"seriesid": [sid], "startyear": str(y),
                               "endyear": str(min(y + BLS_SPAN - 1, this_year))}).encode()
            res = json.loads(_text(BLS_URL, body, {"Content-Type": "application/json"}))
            if res.get("status") != "REQUEST_SUCCEEDED":
                raise RuntimeError(f"BLS {sid}: {res.get('message')}")
            rows += res["Results"]["series"][0]["data"]
        print(f"{name}: {store(conn, name, [(d, v, u) for d, v, u in parse_bls(rows, name)], 'bls')} rows")
    today = datetime.now(timezone.utc).date()
    rows = []
    for y in range(2000, this_year + 1):
        rows += parse_effr(_text(EFFR_URL.format(a=f"{y}-01-01", b=f"{y}-12-31")))
        time.sleep(0.2)
    print(f"fed_target: {store(conn, 'fed_target', next_day(sorted(set(rows))), 'nyfed')} rows")
    headers = equities.load_tiingo_credentials()
    end = (today - timedelta(days=1)).isoformat()
    for sym in TIINGO_EXTRA:
        bars = equities.fetch_tiingo(sym, end, headers)
        equities.check_bars(sym, bars)
        rows = [(datetime.fromtimestamp(b[0], tz=timezone.utc).date(), b[4]) for b in bars]
        print(f"{sym}: {store(conn, 'px_' + sym, next_day(rows), 'tiingo')} rows")


if __name__ == "__main__":
    c = sqlite3.connect(main.DB_PATH, timeout=main.DB_TIMEOUT_S)
    ensure_tables(c)
    refresh(c)
