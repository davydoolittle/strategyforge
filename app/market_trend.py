"""
Market trend indicators for the top of the dashboard: Bull, Flat or Bear for a
handful of markets, from one fixed rule. Context only (V2_PROMPT section 9): no
strategy reads this to trade. Whether switching strategies on it helps is a
separate, registered study, not something this module decides.

The rule is regime_equity.py's trend rule, applied to each market's daily closes:

  Bull  close above its 200-day average, and that average higher than 20 days ago
  Bear  close below its 200-day average, and that average lower than 20 days ago
  Flat  anything else (price and average disagree, or the average is flat)

Days are the market's own: trading days for the ETFs, calendar days (UTC) for
crypto, which trades every day. Crypto's daily close is the last hourly close
of each UTC day. Read-only: nothing is stored, the dashboard computes it on
request from candles.
"""

from datetime import date, datetime, timezone

SMA_DAYS = 200
SLOPE_DAYS = 20
STALE_DAYS = {"us_equity": 7, "crypto": 2}     # older than this, the card says stale

# (key, label, symbol, session, what it stands for)
MARKETS = (
    ("stocks", "US stocks", "SPY", "us_equity", "S&P 500 (SPY)"),
    ("crypto", "Crypto", "BTC-USD", "crypto", "Bitcoin (BTC-USD)"),
    ("bonds", "Treasuries", "IEF", "us_equity", "7-10 year Treasuries (IEF)"),
    ("gold", "Gold", "GLD", "us_equity", "gold (GLD)"),
    ("dollar", "US dollar", "UUP", "us_equity", "US dollar index (UUP)"),
)


def daily_closes(conn, symbol, session):
    """[(date iso, close)] oldest first. Crypto: last hourly close of each UTC day."""
    if session == "crypto":
        rows = conn.execute("SELECT ts, close FROM candles WHERE pair=? ORDER BY ts", (symbol,)).fetchall()
        by_day = {}
        for ts, c in rows:
            by_day[datetime.fromtimestamp(ts, timezone.utc).date().isoformat()] = c
        return sorted(by_day.items())
    if "session" not in {r[1] for r in conn.execute("PRAGMA table_info(candles)")}:
        return []                  # a crypto-only database: no ETF bars to read
    rows = conn.execute("SELECT ts, close FROM candles WHERE pair=? AND session=? AND close > 0 ORDER BY ts",
                        (symbol, session)).fetchall()
    return [(datetime.fromtimestamp(ts, timezone.utc).date().isoformat(), c) for ts, c in rows]


def states(closes):
    """[(date, close, sma, slope, state)] for every day with a full window."""
    out, run = [], 0.0
    c = [x for _, x in closes]
    sma = [None] * len(c)
    for i, x in enumerate(c):
        run += x
        if i >= SMA_DAYS:
            run -= c[i - SMA_DAYS]
        if i >= SMA_DAYS - 1:
            sma[i] = run / SMA_DAYS
    for i, (d, x) in enumerate(closes):
        if sma[i] is None or i < SLOPE_DAYS or sma[i - SLOPE_DAYS] is None:
            continue
        slope = sma[i] - sma[i - SLOPE_DAYS]
        st = "Bull" if x > sma[i] and slope > 0 else "Bear" if x < sma[i] and slope < 0 else "Flat"
        out.append((d, x, sma[i], slope, st))
    return out


def summary(series, session, today=None):
    """One card: the latest state, when it began, how far price is from the
    average, how often the state changed in the last year."""
    if not series:
        return None
    d, x, sma, slope, st = series[-1]
    k = len(series) - 1
    while k > 0 and series[k - 1][4] == st:
        k -= 1
    since = series[k][0]
    year_ago = series[-1][0][:4]
    last_year = [s for s in series if s[0] > f"{int(year_ago) - 1}{series[-1][0][4:]}"]
    flips = sum(1 for a, b in zip(last_year, last_year[1:]) if a[4] != b[4])
    today = today or date.today()
    age = (today - date.fromisoformat(d)).days
    return {"state": st, "as_of": d, "since": since, "close": x, "sma200": sma,
            "pct_vs_sma": 100.0 * (x / sma - 1), "slope_pct": 100.0 * slope / sma,
            "changes_last_year": flips, "stale": age > STALE_DAYS[session],
            "history": [(s[0], s[4]) for s in last_year]}


def panel(conn, today=None):
    out = {"rule": "Bull: above the 200-day average and the average rising over 20 days. "
                   "Bear: below it and the average falling. Flat: anything else.",
           "note": "Trend context only. No strategy changes on it unless a registered test shows it helps.",
           "markets": []}
    for key, label, symbol, session, what in MARKETS:
        card = summary(states(daily_closes(conn, symbol, session)), session, today)
        out["markets"].append({"key": key, "label": label, "symbol": symbol, "what": what,
                               **(card or {"state": None})})
    return out
