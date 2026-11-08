"""
Daily equity regime tag (P7.6): two fixed rules, context only.

  trend       up     SPY close above its 200-day average and that average rising
                     over the last 20 trading days
              down   close below it and the average falling
              range  anything else
  volatility  calm   VIX below the 50th percentile of its past year
              vol    at or above it

  state = trend_<up|down>_<calm|vol> or range_<calm|vol>, e.g. trend_up_calm.

The VIX percentile is the share of the last 252 trading-day VIX closes,
today's included, that are strictly below today's. `band` keeps the three
sizing bands (<50, 50-90, >90) the dashboard's sizing line uses.

Nothing reads this to trade. Rebuilt whole on each run, in batches.

  python regime_equity.py
"""

import sqlite3

import main
import market_context as M

WINDOW = 252
SLOPE_DAYS = 20
STATES = ("trend_up_calm", "trend_up_vol", "trend_down_calm", "trend_down_vol", "range_calm", "range_vol")


def ensure_tables(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS equity_regime (
        date TEXT PRIMARY KEY, close REAL, sma200 REAL, sma_slope20 REAL, trend TEXT,
        vix REAL, vix_pct REAL, band TEXT, state TEXT
    )""")
    conn.commit()


def percentile(window, x):
    return 100.0 * sum(1 for v in window if v < x) / len(window)


def band(pct):
    return "<50" if pct < 50 else ("50-90" if pct <= 90 else ">90")


def tag(bars, vix):
    """bars [(date, close)], vix {date: close} -> rows for equity_regime."""
    c = [x for _, x in bars]
    s = M.sma(c, 200)
    vix_days = sorted(vix)
    pos = {d: i for i, d in enumerate(vix_days)}
    vals = [vix[d] for d in vix_days]
    out = []
    for i, (d, close) in enumerate(bars):
        if s[i] is None or i < SLOPE_DAYS or s[i - SLOPE_DAYS] is None or d not in pos:
            continue
        k = pos[d]
        if k + 1 < WINDOW:
            continue
        slope = s[i] - s[i - SLOPE_DAYS]
        trend = ("up" if close > s[i] and slope > 0 else
                 "down" if close < s[i] and slope < 0 else "range")
        pct = percentile(vals[k + 1 - WINDOW:k + 1], vals[k])
        vol = "calm" if pct < 50 else "vol"
        state = f"range_{vol}" if trend == "range" else f"trend_{trend}_{vol}"
        out.append((d.isoformat(), close, s[i], slope, trend, vals[k], pct, band(pct), state))
    return out


def run(conn):
    ensure_tables(conn)
    rows = tag(M.spy_closes(conn), dict(M.series(conn, "vix")))     # compute first
    conn.execute("DELETE FROM equity_regime")
    conn.commit()
    for i in range(0, len(rows), M.BATCH_ROWS):
        conn.executemany("INSERT INTO equity_regime VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         rows[i:i + M.BATCH_ROWS])
        conn.commit()
    if not rows:
        return "regime: no rows (needs SPY bars and ctx:vix)"
    last = rows[-1]
    return f"regime: {len(rows)} days, {rows[0][0]} to {last[0]}; latest {last[8]} (VIX {last[5]:.2f}, {last[6]:.0f}th pct)"


if __name__ == "__main__":
    print(run(sqlite3.connect(main.DB_PATH, timeout=main.DB_TIMEOUT_S)))
