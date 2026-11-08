"""
Market context for the dashboard (P7.6): daily series with plain-language
explanations. Context only: no strategy reads these, and none is an entry signal
(V2_PROMPT section 9).

Each series goes in `context` as metric 'ctx:<name>', pair 'GLOBAL', ts = 00:00
UTC of the date the value describes. Monthly series use the month's first day.

  vix              CBOE VIX close, 1990 on               cdn.cboe.com
  t10y2y           10-year minus 2-year Treasury yield   FRED (key in fred.env)
  hy_oas           high-yield bond spread, ICE BofA      FRED; FRED only carries 3 years
  sahm             Sahm rule, real-time                  FRED, monthly
  fedfunds         effective fed funds rate              FRED, monthly
  putcall_equity   CBOE equity put/call ratio            CBOE archive to 2019-10, then CBOE daily files
  cape             Shiller CAPE                          shillerdata.com (linked from his Yale page), monthly
  spy_sma200       SPY's 200-day average                 derived from SPY bars
  spy_above_sma200 1 above, 0 below                      derived
  spy_cross        +1 golden cross, -1 death cross, on the day it happens (50- vs 200-day)
  spy_rsi_weekly   RSI(14) on weekly closes, on each week's last trading day

A source that fails is skipped and named; nothing falls back to a mirror. Every
series but put/call is replaced whole on each run, in batches (section 10.9).
Put/call is incremental: a killed run resumes at the first missing day.

  python market_context.py --refresh    # fetch, store, then tag regimes (the daily job)
"""

import argparse
import csv
import io
import json
import os
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

import equities
import main
import xls

UA = "strategyforge/1.0"
PREFIX = "ctx:"
BATCH_ROWS = 100
VIX_URL = "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"
FRED_URL = "https://api.stlouisfed.org/fred/series/observations"
FRED_SERIES = {"t10y2y": "T10Y2Y", "hy_oas": "BAMLH0A0HYM2", "sahm": "SAHMREALTIME",
               "fedfunds": "FEDFUNDS"}
FRED_GAP_S = 2.0                       # FRED's robots.txt asks for a 2 s crawl delay
PC_ARCHIVE = "https://cdn.cboe.com/resources/options/volume_and_call_put_ratios/equitypc.csv"
PC_DAILY = "https://cdn.cboe.com/data/us/options/market_statistics/daily/{d}_daily_options"
PC_NAME = "EQUITY PUT/CALL RATIO"
PC_GAP_S = 0.25
SHILLER_PAGE = "https://shillerdata.com/"
CACHE_DAYS = 7


# ------------------------------ storage --------------------------------------

def _ts(d):
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())


def _day(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).date()


def replace_series(conn, name, rows):
    """rows: [(date, value)]. Computed before this is called; written in batches."""
    metric = PREFIX + name
    conn.execute("DELETE FROM context WHERE metric=? AND pair='GLOBAL'", (metric,))
    conn.commit()
    data = [(_ts(d), metric, "GLOBAL", v) for d, v in rows]
    for i in range(0, len(data), BATCH_ROWS):
        conn.executemany("INSERT OR REPLACE INTO context (ts, metric, pair, value) VALUES (?, ?, ?, ?)",
                         data[i:i + BATCH_ROWS])
        conn.commit()
    return len(data)


def _has(conn, table, column=None):
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    return bool(cols) and (column is None or column in cols)


def series(conn, name):
    """[(date, value)] oldest first; empty on a DB without the context table."""
    if not _has(conn, "context"):
        return []
    return [(_day(t), v) for t, v in conn.execute(
        "SELECT ts, value FROM context WHERE metric=? AND pair='GLOBAL' ORDER BY ts", (PREFIX + name,))]


# ------------------------------ sources --------------------------------------

def _get(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def parse_vix(text):
    return [(datetime.strptime(r["DATE"], "%m/%d/%Y").date(), float(r["CLOSE"]))
            for r in csv.DictReader(io.StringIO(text))]


def fred_key(path=None):
    path = path or os.path.join(os.path.dirname(os.path.abspath(main.DB_PATH)), "fred.env")
    if not os.path.exists(path):
        raise RuntimeError(f"missing {path}: needs FRED_API_KEY")
    with open(path) as f:
        for line in f:
            k, _, v = line.strip().partition("=")
            if k == "FRED_API_KEY" and v.strip():
                return v.strip().strip("'\"")
    raise RuntimeError(f"{path} has no FRED_API_KEY")


def parse_fred(payload):
    """FRED marks a missing day with '.'."""
    return [(date.fromisoformat(o["date"]), float(o["value"]))
            for o in payload["observations"] if o["value"] not in (".", "")]


def parse_pc_archive(text):
    """CBOE's archive: a disclaimer, a header, then DATE, CALL, PUT, TOTAL, P/C Ratio."""
    out = []
    for row in csv.reader(io.StringIO(text)):
        if len(row) >= 5 and re.fullmatch(r"\d{1,2}/\d{1,2}/\d{4}", row[0].strip()):
            try:
                out.append((datetime.strptime(row[0].strip(), "%m/%d/%Y").date(), float(row[4])))
            except ValueError:
                continue
    return out


def parse_pc_daily(payload):
    for r in payload.get("ratios", []):
        if r.get("name") == PC_NAME:
            return float(r["value"])
    return None


def shiller_xls_url(page_html):
    m = re.search(r'href="((?:https?:)?//[^"]+/ie_data\.xls[^"]*)"', page_html)
    if not m:
        raise RuntimeError("no ie_data.xls link on shillerdata.com")
    return ("https:" + m.group(1)) if m.group(1).startswith("//") else m.group(1)


def parse_cape(data):
    """[(first of month, CAPE)] from ie_data.xls. The CAPE column is the cell
    reading exactly 'CAPE' on the row whose first cell reads 'Date'. Dates are
    year.month, with October written .1."""
    cells = xls.read_sheet(data, "Data")
    hdr = next(r for (r, c), v in cells.items() if c == 0 and v == "Date")
    col = next(c for (r, c), v in cells.items() if r == hdr and v == "CAPE")
    out = []
    r = hdr + 1
    while (r, 0) in cells:
        d, v = cells[(r, 0)], cells.get((r, col))
        if isinstance(d, float) and isinstance(v, float):
            y = int(d)
            m = int(round((d - y) * 100))
            if 1 <= m <= 12:
                out.append((date(y, m, 1), v))
        r += 1
    return out


def fetch_cape(cache_dir):
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, "ie_data.xls")
    if not os.path.exists(path) or time.time() - os.path.getmtime(path) > CACHE_DAYS * 86400:
        url = shiller_xls_url(_get(SHILLER_PAGE).decode("utf-8", "replace"))
        data = _get(url, timeout=120)
        with open(path + ".tmp", "wb") as f:
            f.write(data)
        os.replace(path + ".tmp", path)
    with open(path, "rb") as f:
        return parse_cape(f.read())


def trading_days(conn, after, through):
    return [_day(t) for (t,) in conn.execute(
        "SELECT ts FROM candles WHERE pair='SPY' AND session='us_equity' AND ts>? AND ts<=? ORDER BY ts",
        (_ts(after), _ts(through)))]


def refresh_putcall(conn, get=_get, sleep=time.sleep, today=None):
    """The archive once, then one CBOE daily file per SPY trading day after it.
    Days already stored are skipped, and each 50 are committed as they land."""
    metric = PREFIX + "putcall_equity"
    have = {_day(t) for (t,) in conn.execute("SELECT ts FROM context WHERE metric=?", (metric,))}
    if not have:
        rows = parse_pc_archive(get(PC_ARCHIVE).decode("utf-8", "replace"))
        if not rows:
            raise RuntimeError("CBOE put/call archive had no rows")
        conn.executemany("INSERT OR REPLACE INTO context VALUES (?, ?, 'GLOBAL', ?)",
                         [(_ts(d), metric, v) for d, v in rows])
        conn.commit()
        have = {d for d, _ in rows}
    today = today or datetime.now(timezone.utc).date()
    todo = [d for d in trading_days(conn, max(have), today - timedelta(days=1)) if d not in have]
    missing, chunk = [], []
    for d in todo:
        try:
            v = parse_pc_daily(json.loads(get(PC_DAILY.format(d=d.isoformat()))))
        except urllib.error.HTTPError as e:
            if e.code not in (403, 404):
                raise
            v = None
        if v is None:
            missing.append(d)
        else:
            chunk.append((_ts(d), metric, v))
        if len(chunk) >= 50:
            conn.executemany("INSERT OR REPLACE INTO context VALUES (?, ?, 'GLOBAL', ?)", chunk)
            conn.commit()
            chunk = []
        sleep(PC_GAP_S)
    if chunk:
        conn.executemany("INSERT OR REPLACE INTO context VALUES (?, ?, 'GLOBAL', ?)", chunk)
        conn.commit()
    return len(todo) - len(missing), missing


# ------------------------------ derived from SPY ------------------------------

def sma(xs, k):
    out, s = [None] * len(xs), 0.0
    for i, x in enumerate(xs):
        s += x
        if i >= k:
            s -= xs[i - k]
        if i >= k - 1:
            out[i] = s / k
    return out


def wilder_rsi(xs, k=14):
    out = [None] * len(xs)
    if len(xs) <= k:
        return out
    gains = [max(xs[i] - xs[i - 1], 0.0) for i in range(1, len(xs))]
    losses = [max(xs[i - 1] - xs[i], 0.0) for i in range(1, len(xs))]
    up, dn = sum(gains[:k]) / k, sum(losses[:k]) / k
    for i in range(k, len(xs)):
        if i > k:
            up = (up * (k - 1) + gains[i - 1]) / k
            dn = (dn * (k - 1) + losses[i - 1]) / k
        out[i] = 100.0 if dn == 0 else 100 - 100 / (1 + up / dn)
    return out


def spy_closes(conn):
    if not _has(conn, "candles", "session"):
        return []
    return [(_day(t), c) for t, c in conn.execute(
        "SELECT ts, close FROM candles WHERE pair='SPY' AND session='us_equity' ORDER BY ts")]


def derive_spy(bars):
    """{name: [(date, value)]} for the four SPY series."""
    days = [d for d, _ in bars]
    c = [x for _, x in bars]
    s50, s200 = sma(c, 50), sma(c, 200)
    out = {"spy_sma200": [], "spy_above_sma200": [], "spy_cross": [], "spy_rsi_weekly": []}
    prev = None
    for i, d in enumerate(days):
        if s200[i] is None:
            continue
        out["spy_sma200"].append((d, s200[i]))
        out["spy_above_sma200"].append((d, 1.0 if c[i] > s200[i] else 0.0))
        side = s50[i] > s200[i]
        if prev is not None and side != prev:
            out["spy_cross"].append((d, 1.0 if side else -1.0))
        prev = side
    week_ends = [i for i in range(len(days))
                 if i == len(days) - 1 or days[i].isocalendar()[:2] != days[i + 1].isocalendar()[:2]]
    if week_ends and week_ends[-1] == len(days) - 1 and days[-1].weekday() < 4:
        week_ends = week_ends[:-1]           # this week is not finished
    r = wilder_rsi([c[i] for i in week_ends])
    out["spy_rsi_weekly"] = [(days[i], v) for i, v in zip(week_ends, r) if v is not None]
    return out


# ------------------------------ the job --------------------------------------

def refresh(conn, cache_dir=None):
    """Every source in turn; one that fails is reported and skipped. Returns the
    names that failed."""
    cache_dir = cache_dir or os.path.join(os.path.dirname(os.path.abspath(main.DB_PATH)), "cache")
    failed = {}

    def step(name, fn):
        try:
            print(f"{name}: {fn()}")
        except Exception as e:                    # noqa: BLE001 — named and skipped, never substituted
            failed[name] = f"{type(e).__name__}: {e}"
            main.safe_record_error(conn, f"ctx-{name}", e)
            print(f"{name}: SKIPPED ({failed[name]})")

    step("vix", lambda: f"{replace_series(conn, 'vix', parse_vix(_get(VIX_URL).decode()))} rows")
    key = None
    try:
        key = fred_key()
    except RuntimeError as e:
        for name in FRED_SERIES:
            failed[name] = str(e)
        print(f"FRED: SKIPPED ({e})")
    for name, sid in FRED_SERIES.items() if key else ():
        q = urllib.parse.urlencode({"series_id": sid, "file_type": "json", "api_key": key})
        step(name, lambda sid=sid, name=name, q=q: f"{replace_series(conn, name, parse_fred(json.loads(_get(f'{FRED_URL}?{q}'))))} rows")
        time.sleep(FRED_GAP_S)
    def putcall():
        added, missing = refresh_putcall(conn)
        return f"{added} new days" + (f", no CBOE file for {len(missing)}" if missing else "")
    step("putcall_equity", putcall)
    step("cape", lambda: f"{replace_series(conn, 'cape', fetch_cape(cache_dir))} months")

    def spy():
        equities.ensure_tables(conn)
        equities.backfill(conn, equities.load_tiingo_credentials(), symbols={"SPY"})
        derived = derive_spy(spy_closes(conn))
        return ", ".join(f"{k} {replace_series(conn, k, v)}" for k, v in derived.items())
    step("spy", spy)
    return failed


def main_cli():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--refresh", action="store_true", help="fetch every source, then tag regimes")
    args = ap.parse_args()
    conn = sqlite3.connect(main.DB_PATH, timeout=main.DB_TIMEOUT_S)
    failed = refresh(conn) if args.refresh else {}
    import regime_equity
    print(regime_equity.run(conn))
    if failed:
        print("Read: skipped, not substituted: " + "; ".join(f"{k} ({v})" for k, v in failed.items()))
        raise SystemExit(1)


if __name__ == "__main__":
    main_cli()


# ------------------------------ the dashboard panel --------------------------

def _vix_status(v, _):
    return "high" if v > 30 else ("elevated" if v >= 20 else "calm")


def _fed_status(v, hist):
    old = [x for d, x in hist if d <= hist[-1][0] - timedelta(days=182)]
    if not old:
        return "steady"
    move = v - old[-1]
    return "rising" if move > 0.25 else ("falling" if move < -0.25 else "steady")


# key, label, unit, sparkline points, status rule (value, history) -> word, the rule in words
INDICATORS = [
    ("vix", "VIX", "", 252, _vix_status, "calm < 20, elevated 20-30, high > 30"),
    ("t10y2y", "10y minus 2y yield", " pts", 252, lambda v, h: "inverted" if v < 0 else "normal",
     "inverted < 0"),
    ("hy_oas", "High-yield spread", " pts", 252,
     lambda v, h: "crisis" if v >= 8 else ("stressed" if v >= 5 else "normal"), "stressed >= 5, crisis >= 8"),
    ("sahm", "Sahm rule", " pts", 60,
     lambda v, h: "triggered" if v >= 0.5 else ("rising" if v >= 0.3 else "normal"),
     "rising >= 0.30, triggered >= 0.50"),
    ("fedfunds", "Fed funds rate", "%", 60, _fed_status, "rising / falling = moved > 0.25 pts in 6 months"),
    ("putcall_equity", "Equity put/call", "", 252,
     lambda v, h: "fearful" if v > 1.0 else ("complacent" if v < 0.5 else "normal"),
     "complacent < 0.5, fearful > 1.0"),
    ("cape", "Shiller CAPE", "", 60,
     lambda v, h: "high" if v >= 30 else ("low" if v <= 15 else "middle"), "low <= 15, high >= 30"),
    ("spy_rsi_weekly", "SPY weekly RSI(14)", "", 104,
     lambda v, h: "overbought" if v > 70 else ("oversold" if v < 30 else "neutral"),
     "oversold < 30, overbought > 70"),
]

SIZING = (("<50", "normal size"),
          ("50-90", "new positions 75% of normal, widen stops"),
          (">90", "new positions 50% of normal, no adds on dips until VIX < 90th pct"))

FOOTER = ("Traditional readings are historical observations, not recommendations from this tool. "
          "None of these series is used as an entry signal (V2_PROMPT §9).")


def pct_1y(hist):
    """Share of the past year's observations strictly below the latest, in %."""
    last_d, last_v = hist[-1]
    year = [v for d, v in hist if d > last_d - timedelta(days=365)]
    return 100.0 * sum(1 for v in year if v < last_v) / len(year)


def sizing_line(vix, pct):
    import regime_equity
    b = regime_equity.band(pct)
    return {"band": b, "text": dict(SIZING)[b], "vix": vix, "vix_pct": pct,
            "trigger": f"VIX {vix:.2f} is at the {pct:.0f}th percentile of the past year (band {b})"}


def panel(conn):
    """Everything the dashboard's Market context panel shows."""
    out = {"indicators": [], "regime": None, "sizing": None, "footer": FOOTER}
    for key, label, unit, n, rule, words in INDICATORS:
        hist = series(conn, key)
        row = {"key": key, "label": label, "unit": unit, "rule": words, "value": None, "date": None,
               "pct_1y": None, "status": "no data", "spark": []}
        if hist:
            d, v = hist[-1]
            row.update(value=v, date=d.isoformat(), pct_1y=pct_1y(hist), status=rule(v, hist),
                       spark=[x for _, x in hist[-n:]])
        out["indicators"].append(row)
    closes = spy_closes(conn)
    sma200 = series(conn, "spy_sma200")
    if closes and sma200:
        c = dict(closes)
        d, s = sma200[-1]
        above = c.get(d, 0) > s
        out["indicators"].append({
            "key": "spy_sma200", "label": "SPY vs 200-day average", "unit": "", "rule": "above / below",
            "value": c.get(d), "date": d.isoformat(), "pct_1y": None, "status": "above" if above else "below",
            "spark": [c[x] for x, _ in sma200[-252:] if x in c], "sma": s})
    crosses = series(conn, "spy_cross")
    if crosses:
        d, v = crosses[-1]
        out["indicators"].append({
            "key": "spy_cross", "label": "50/200-day cross", "unit": "", "rule": "last cross of the 50-day over the 200-day",
            "value": v, "date": d.isoformat(), "pct_1y": None,
            "status": ("golden cross" if v > 0 else "death cross") + f" since {d.isoformat()}", "spark": []})
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='equity_regime'").fetchone():
        r = conn.execute("SELECT date, state, trend, close, sma200, sma_slope20, vix, vix_pct, band"
                         " FROM equity_regime ORDER BY date DESC LIMIT 1").fetchone()
        if r:
            keys = ("date", "state", "trend", "close", "sma200", "sma_slope20", "vix", "vix_pct", "band")
            out["regime"] = dict(zip(keys, r))
            out["sizing"] = sizing_line(r[6], r[7])
    return out
