"""
Pattern ledger: traditional market patterns, each tested once and labelled.

The full list below was committed before any of it was run on real data, and it
keeps every pattern, including the ones that fail. A pattern is one claim: on
these days, this asset does better (sign +1) or worse (sign -1) than normal over
the next `h` trading days.

Each test compares independent windows, never overlapping ones: entries are at
least h days apart, and the baseline is every h-th day the signal was off. Eight
windows can only say so much, and the label says so.

  SUPPORTED     right direction, survives Benjamini-Hochberg at q = 0.10 across
                the whole list, right sign in both halves of the history
  SUGGESTIVE    right direction, one-sided p < 0.10, both halves agree, but it
                does not survive the correction: could be luck
  NOT SUPPORTED everything else that had enough windows
  OPPOSITE      survives the correction in the wrong direction
  TOO FEW       under MIN_WINDOWS independent windows

History, not advice: none of these is a trading rule. Only a rule that passes
the adversary may suggest a move (F1-F17). Nothing here does.

  python patterns.py             # run, store in pattern_results, print the ledger
  python patterns.py --refresh   # fetch new data first (the weekly job)
  python patterns.py --show      # print what is stored
"""

import argparse
import json
import math
import sqlite3
from datetime import date, datetime, timedelta, timezone

import equities
import macro_data
import main
import validation as V

CAL_SYMBOL = "SPY"
BH_Q = 0.10
SUGGESTIVE_P = 0.10
MIN_WINDOWS = 8
MIN_BASE_WINDOWS = 8

# Conditions, as opposed to one-day events: a note shows only while one is true.
CONDITIONS = {"below_200", "drop_3", "oversold", "long_run_up", "vix_30", "vix_13", "credit_stress",
              "dont_fight_ease", "dont_fight_hike", "yields_up", "yields_up_bonds",
              "inflation_stocks", "inflation_gold", "inflation_commodities", "inflation_energy",
              "real_yields_gold", "dollar_gold", "dollar_em", "dollar_intl", "dollar_commodities",
              "oil_spike", "gold_warning", "defensive_lead", "intl_catch_up", "value_yields"}

NOT_TESTED = {
    "FOMC meeting drift": "needs the FOMC calendar",
    "Valuation (CAPE, Buffett indicator)": "no point-in-time earnings or GDP data here",
    "Sentiment surveys (AAII, put/call)": "no history here",
    "Money supply growth (M2)": "no source reachable: FRED times out from this network",
    "Recessions, by NBER date": "dated with hindsight, so unusable when it mattered",
    "Party in the White House": "too few independent terms to separate from the economy",
    "Commodity supercycles, Kondratiev waves": "one or two cycles in any data set",
    "Hindenburg Omen, market breadth": "needs every stock's daily data",
}


def ensure_tables(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS pattern_results (
        pid TEXT PRIMARY KEY, family TEXT, claim TEXT, asset TEXT, h INTEGER, sign INTEGER,
        status TEXT, n INTEGER, n_base INTEGER, mean_on REAL, mean_base REAL, hit_on REAL,
        hit_base REAL, p REAL, first_half REAL, second_half REAL, first_date TEXT,
        active INTEGER, last_on TEXT, run_at TEXT
    )""")
    conn.commit()


# ------------------------------ data -----------------------------------------

class Market:
    """Everything aligned to the trading calendar. A macro value appears only
    from its `usable` date, and a price is None before the fund existed."""

    def __init__(self, conn):
        rows = conn.execute("SELECT ts, close FROM candles WHERE pair=? AND session='us_equity'"
                            " ORDER BY ts", (CAL_SYMBOL,)).fetchall()
        if not rows:
            raise SystemExit(f"no {CAL_SYMBOL} bars: run equities.py --backfill")
        self.ts = [r[0] for r in rows]
        self.days = [datetime.fromtimestamp(t, tz=timezone.utc).date() for t in self.ts]
        self.iso = [d.isoformat() for d in self.days]
        self.n = len(self.days)
        self.px = {}
        for sym in equities.UNIVERSE:
            got = dict(conn.execute("SELECT ts, close FROM candles WHERE pair=? AND session='us_equity'",
                                    (sym,)))
            self.px[sym] = [got.get(t) for t in self.ts]
        for sym in macro_data.TIINGO_EXTRA:
            got = dict(conn.execute("SELECT date, value FROM macro_series WHERE series=?",
                                    ("px_" + sym,)))
            self.px[sym] = [got.get(d) for d in self.iso]
        self.macro = {}
        for name in ("y3m", "y10y", "vix", "cpi", "unemployment", "fed_target"):
            self.macro[name] = self._asof(conn.execute(
                "SELECT usable, value FROM macro_series WHERE series=? ORDER BY usable, date",
                (name,)).fetchall())

    def _asof(self, rows):
        out, k, val = [], 0, None
        for d in self.iso:
            while k < len(rows) and rows[k][0] <= d:
                val = rows[k][1]
                k += 1
            out.append(val)
        return out


# ------------------------------ signal helpers -------------------------------

def sma(x, k):
    out = [None] * len(x)
    s, c = 0.0, 0
    for i, v in enumerate(x):
        if v is None:
            s, c = 0.0, 0
            continue
        s += v
        c += 1
        if c > k:
            s -= x[i - k]
            c = k
        if c == k:
            out[i] = s / k
    return out


def change(x, k):
    """x[i] / x[i - k] - 1."""
    return [x[i] / x[i - k] - 1 if i >= k and x[i] is not None and x[i - k] else None
            for i in range(len(x))]


def diff(x, k):
    return [x[i] - x[i - k] if i >= k and x[i] is not None and x[i - k] is not None else None
            for i in range(len(x))]


def drawdown(x):
    out, peak = [], None
    for v in x:
        if v is None:
            out.append(None)
            continue
        peak = v if peak is None else max(peak, v)
        out.append(v / peak - 1)
    return out


def rsi(x, k=14):
    out = [None] * len(x)
    up = dn = 0.0
    for i in range(1, len(x)):
        d = x[i] - x[i - 1]
        g, l = max(d, 0.0), max(-d, 0.0)
        if i <= k:
            up += g / k
            dn += l / k
        else:
            up = (up * (k - 1) + g) / k
            dn = (dn * (k - 1) + l) / k
        if i >= k:
            out[i] = 100.0 if dn == 0 else 100 - 100 / (1 + up / dn)
    return out


def test_all(xs, pred):
    return [x is not None and pred(x) for x in xs]


def rising_edge(on):
    return [v and (i == 0 or not on[i - 1]) for i, v in enumerate(on)]


def month_last(m):
    return [i == m.n - 1 or m.days[i].month != m.days[i + 1].month for i in range(m.n)]


def election_eve(m, years):
    out = [False] * m.n
    for y in years:
        d = date(y, 11, 1)
        while d.weekday() != 0:
            d += timedelta(days=1)
        tuesday = d + timedelta(days=1)
        i = max((k for k in range(m.n) if m.days[k] < tuesday), default=None)
        if i is not None and i + 1 < m.n:
            out[i] = True
    return out


def fed_first_moves(m, direction, quiet_days=365):
    """First move in `direction` after a year with none, on the day it is known."""
    t = m.macro["fed_target"]
    out, last = [False] * m.n, None
    for i in range(1, m.n):
        if t[i] is None or t[i - 1] is None or (t[i] - t[i - 1]) * direction <= 0:
            continue
        if last is None or (m.days[i] - last).days > quiet_days:
            out[i] = True
        last = m.days[i]
    return out


def yoy(xs):
    return change(xs, 252)


def sahm(m):
    u = m.macro["unemployment"]
    avg = sma(u, 63)
    on = []
    for i in range(m.n):
        low = [a for a in avg[max(0, i - 252):i + 1] if a is not None]
        on.append(bool(low) and avg[i] is not None and avg[i] - min(low) >= 0.5)
    return on


# ------------------------------ the ledger -----------------------------------

def P(pid, family, claim, asset, h, sign, signal, edge=False):
    return {"pid": pid, "family": family, "claim": claim, "asset": asset, "h": h, "sign": sign,
            "signal": signal, "edge": edge}


def registry():
    spy = lambda m: m.px["SPY"]                     # noqa: E731
    dd = lambda m: drawdown(spy(m))                 # noqa: E731
    curve = lambda m: [a - b if a is not None and b is not None else None   # noqa: E731
                       for a, b in zip(m.macro["y10y"], m.macro["y3m"])]
    yup = lambda m: test_all(diff(m.macro["y10y"], 252), lambda x: x >= 1.0)   # noqa: E731
    infl = lambda m: test_all(yoy(m.macro["cpi"]), lambda x: x >= 0.04)        # noqa: E731
    usd_down = lambda m: test_all(change(m.px["UUP"], 126), lambda x: x < 0)   # noqa: E731

    def years(m, pick):
        ends = [i < m.n - 1 and m.days[i].year != m.days[i + 1].year for i in range(m.n)]
        return [e and pick(m, i) for i, e in enumerate(ends)]

    def santa(m):
        out = [False] * m.n
        for y in sorted({d.year for d in m.days}):
            dec = [i for i in range(m.n) if m.days[i].year == y and m.days[i].month == 12]
            if len(dec) >= 15 and dec[-1] + 2 < m.n:
                out[dec[-6]] = True
        return out

    def calendar_day(m, month, nth_last=0):
        last = month_last(m)
        return [last[i] and m.days[i].month == month and i < m.n - 1 for i in range(m.n)]

    def jan_up(m):
        out = [False] * m.n
        for i in range(m.n - 1):
            if month_last(m)[i] and m.days[i].month == 1:
                j = i - 1
                while j >= 0 and m.days[j].month == 1:
                    j -= 1
                out[i] = j >= 0 and spy(m)[i] > spy(m)[j]
        return out

    def first5_up(m):
        out = [False] * m.n
        for y in sorted({d.year for d in m.days}):
            jan = [i for i in range(m.n) if m.days[i].year == y and m.days[i].month == 1]
            if len(jan) > 5 and jan[0] > 0:
                out[jan[4]] = spy(m)[jan[4]] > spy(m)[jan[0] - 1]
        return out

    def uninvert(m):
        c = curve(m)
        out = [False] * m.n
        run = 0
        for i in range(m.n):
            if c[i] is None:
                continue
            if c[i] < 0:
                run += 1
            else:
                out[i] = run >= 30
                run = 0
        return out

    def relative(m, a, b, k):
        return [x - y if x is not None and y is not None else None
                for x, y in zip(change(m.px[a], k), change(m.px[b], k))]

    def defensive(m):
        p, u, s = change(m.px["XLP"], 63), change(m.px["XLU"], 63), change(m.px["SPY"], 63)
        return [None not in (a, b, c) and (a + b) / 2 - c >= 0.05 for a, b, c in zip(p, u, s)]

    def real_falling(m):
        c = yoy(m.macro["cpi"])
        real = [None if a is None or b is None else a - b * 100 for a, b in zip(m.macro["y10y"], c)]
        return test_all(diff(real, 126), lambda x: x <= -0.5)

    def credit(m):
        h, l = m.px["HYG"], m.px["LQD"]
        return test_all(diff([x / y if x and y else None for x, y in zip(h, l)], 63),
                        lambda x: x <= -0.02)

    def eases(m):
        return test_all(diff(m.macro["fed_target"], 126), lambda x: x < 0)

    def hikes(m):
        return test_all(diff(m.macro["fed_target"], 126), lambda x: x > 0)

    return [
        # calendar and politics
        P("midterm", "calendar", "Stocks rise after midterm elections", "SPY", 126, 1,
          lambda m: election_eve(m, [y for y in range(1994, 2027, 4)])),
        P("pre_election_year", "calendar", "The year before a presidential election is the strongest",
          "SPY", 252, 1, lambda m: years(m, lambda m, i: (m.days[i].year + 1) % 4 == 3)),
        P("presidential_election", "calendar", "Stocks rally once a presidential election is over",
          "SPY", 63, 1, lambda m: election_eve(m, [y for y in range(1996, 2027, 4)])),
        P("sell_in_may", "calendar", "Sell in May: May to October is weak", "SPY", 126, -1,
          lambda m: calendar_day(m, 4)),
        P("halloween", "calendar", "November to April is the strong half", "SPY", 126, 1,
          lambda m: calendar_day(m, 10)),
        P("santa", "calendar", "Santa rally: last 5 days of December, first 2 of January",
          "SPY", 7, 1, santa),
        P("january_small", "calendar", "January effect: small caps beat large in January",
          ("IWM", "SPY"), 21, 1, lambda m: calendar_day(m, 12)),
        P("january_barometer", "calendar", "As January goes, so goes the year", "SPY", 231, 1, jan_up),
        P("first_five_days", "calendar", "A good first five days means a good year", "SPY", 247, 1,
          first5_up),
        P("september", "calendar", "September is the worst month", "SPY", 21, -1,
          lambda m: calendar_day(m, 8)),
        P("turn_of_month", "calendar", "Stocks do well around the turn of the month", "SPY", 4, 1,
          lambda m: [v and i < m.n - 1 for i, v in enumerate(month_last(m))]),
        P("monday", "calendar", "Mondays are weak", "SPY", 1, -1,
          lambda m: [i < m.n - 1 and m.days[i + 1].weekday() == 0 for i in range(m.n)]),
        # price action
        P("golden_cross", "technical", "Golden cross: 50-day above 200-day is bullish", "SPY", 126, 1,
          lambda m: rising_edge([a is not None and b is not None and a > b
                                 for a, b in zip(sma(spy(m), 50), sma(spy(m), 200))])),
        P("death_cross", "technical", "Death cross: 50-day below 200-day is bearish", "SPY", 126, -1,
          lambda m: rising_edge([a is not None and b is not None and a < b
                                 for a, b in zip(sma(spy(m), 50), sma(spy(m), 200))])),
        P("below_200", "technical", "Below the 200-day average, stocks are weak", "SPY", 63, -1,
          lambda m: [b is not None and p < b for p, b in zip(spy(m), sma(spy(m), 200))]),
        P("correction_10", "technical", "Buy a 10% correction", "SPY", 126, 1,
          lambda m: rising_edge(test_all(dd(m), lambda x: x <= -0.10))),
        P("bear_20", "technical", "Buy a 20% bear market", "SPY", 252, 1,
          lambda m: rising_edge(test_all(dd(m), lambda x: x <= -0.20))),
        P("drop_3", "technical", "Buy after a 3% one-day drop", "SPY", 21, 1,
          lambda m: test_all(change(spy(m), 1), lambda x: x <= -0.03)),
        P("strong_year", "technical", "A year up 20% or more is followed by another good year",
          "SPY", 252, 1, lambda m: years(m, lambda m, i: i >= 252 and spy(m)[i] / spy(m)[i - 252] >= 1.2)),
        P("new_high", "technical", "New highs are dangerous", "SPY", 63, -1,
          lambda m: test_all(dd(m), lambda x: x == 0)),
        P("oversold", "technical", "RSI under 30: oversold, buy", "SPY", 21, 1,
          lambda m: test_all(rsi(spy(m)), lambda x: x < 30)),
        P("long_run_up", "technical", "After a 60% three-year run, returns fade", "SPY", 252, -1,
          lambda m: test_all(change(spy(m), 756), lambda x: x >= 0.60)),
        # fear and credit
        P("vix_30", "fear", "VIX above 30: buy when others are fearful", "SPY", 126, 1,
          lambda m: test_all(m.macro["vix"], lambda x: x > 30)),
        P("vix_40", "fear", "VIX above 40: panic is a generational entry", "SPY", 252, 1,
          lambda m: rising_edge(test_all(m.macro["vix"], lambda x: x > 40))),
        P("vix_13", "fear", "VIX under 13: complacency precedes trouble", "SPY", 126, -1,
          lambda m: test_all(m.macro["vix"], lambda x: x < 13)),
        P("credit_stress", "fear", "Junk bonds lagging warns of trouble for stocks", "SPY", 63, -1, credit),
        # rates, the Fed, inflation, jobs
        P("curve_inverted", "macro", "Yield curve inversion precedes weak stocks", "SPY", 252, -1,
          lambda m: rising_edge(test_all(curve(m), lambda x: x < 0))),
        P("curve_uninverts", "macro", "The danger is when the curve un-inverts", "SPY", 126, -1, uninvert),
        P("first_cut", "macro", "The Fed's first rate cut is bullish", "SPY", 252, 1,
          lambda m: fed_first_moves(m, -1)),
        P("first_hike", "macro", "The Fed's first rate hike is bearish", "SPY", 252, -1,
          lambda m: fed_first_moves(m, 1)),
        P("dont_fight_ease", "macro", "Don't fight the Fed: stocks rise while it eases", "SPY", 126, 1, eases),
        P("dont_fight_hike", "macro", "Don't fight the Fed: stocks lag while it tightens", "SPY", 126, -1, hikes),
        P("yields_up", "macro", "A 1-point rise in 10-year yields hurts stocks", "SPY", 126, -1, yup),
        P("yields_up_bonds", "macro", "Rising yields keep hurting long bonds", "TLT", 126, -1, yup),
        P("inflation_stocks", "macro", "Inflation above 4% hurts stocks", "SPY", 252, -1, infl),
        P("inflation_gold", "macro", "Gold is an inflation hedge", "GLD", 252, 1, infl),
        P("inflation_commodities", "macro", "Commodities do well in high inflation", "DBC", 252, 1, infl),
        P("inflation_energy", "macro", "Energy beats the market in high inflation", ("XLE", "SPY"), 252, 1, infl),
        P("sahm", "macro", "Rising unemployment (Sahm rule) is bearish", "SPY", 252, -1,
          lambda m: rising_edge(sahm(m))),
        P("sahm_bonds", "macro", "Rising unemployment is good for bonds", "TLT", 252, 1,
          lambda m: rising_edge(sahm(m))),
        P("real_yields_gold", "macro", "Falling real yields lift gold", "GLD", 126, 1, real_falling),
        # currencies, commodities, rotation
        P("dollar_gold", "cross-asset", "A falling dollar lifts gold", "GLD", 63, 1, usd_down),
        P("dollar_em", "cross-asset", "A falling dollar lifts emerging markets", "EEM", 126, 1, usd_down),
        P("dollar_intl", "cross-asset", "A falling dollar helps foreign stocks beat US stocks",
          ("EFA", "SPY"), 126, 1, usd_down),
        P("dollar_commodities", "cross-asset", "A falling dollar lifts commodities", "DBC", 126, 1, usd_down),
        P("oil_spike", "cross-asset", "An oil spike hurts stocks", "SPY", 126, -1,
          lambda m: test_all(change(m.px["USO"], 126), lambda x: x >= 0.30)),
        P("gold_warning", "cross-asset", "A gold surge warns of trouble for stocks", "SPY", 126, -1,
          lambda m: test_all(change(m.px["GLD"], 126), lambda x: x >= 0.15)),
        P("flight_to_safety", "rotation", "In a stock correction, long bonds rise", "TLT", 63, 1,
          lambda m: rising_edge(test_all(dd(m), lambda x: x <= -0.10))),
        P("small_after_bear", "rotation", "Small caps lead out of bear markets", ("IWM", "SPY"), 252, 1,
          lambda m: rising_edge(test_all(dd(m), lambda x: x <= -0.20))),
        P("defensive_lead", "rotation", "Defensive sectors leading warns of trouble", "SPY", 126, -1, defensive),
        P("intl_catch_up", "rotation", "After US stocks far outrun foreign ones, foreign catch up",
          ("EFA", "SPY"), 252, 1,
          lambda m: test_all(relative(m, "SPY", "EFA", 1260), lambda x: x >= 0.50)),
        P("value_yields", "rotation", "Value beats growth when yields rise", ("IWD", "IWF"), 126, 1, yup),
    ]


# ------------------------------ the test -------------------------------------

def forward(m, asset, i, h):
    if i + h >= m.n:
        return None
    if isinstance(asset, tuple):
        a, b = forward(m, asset[0], i, h), forward(m, asset[1], i, h)
        return None if a is None or b is None else a - b
    x = m.px[asset]
    return x[i + h] / x[i] - 1 if x[i] and x[i + h] else None


def spaced(days, h):
    out, last = [], None
    for i in days:
        if last is None or i - last >= h:
            out.append(i)
            last = i
    return out


def welch_p(a, b, sign):
    """One-sided p that a beats b in the claimed direction."""
    if len(a) < 2 or len(b) < 2:
        return 1.0
    se = math.sqrt(V._std(a) ** 2 / len(a) + V._std(b) ** 2 / len(b))
    if se == 0:
        return 1.0
    return 1 - V.norm_cdf((V._mean(a) - V._mean(b)) * sign / se)


def evaluate(m, p, on=None):
    on = p["signal"](m) if on is None else on
    h, asset, sign = p["h"], p["asset"], p["sign"]
    active = bool(on[-1]) if p["pid"] in CONDITIONS else any(on[-h:])
    valid = [i for i in range(m.n) if forward(m, asset, i, h) is not None]
    if not valid:
        return {"n": 0, "n_base": 0, "active": active, "last_on": None}
    entries = spaced([i for i in valid if on[i]], h)
    off = [i for i in range(valid[0], valid[-1] + 1, h) if not on[i] and i in set(valid)]
    a = [forward(m, asset, i, h) for i in entries]
    b = [forward(m, asset, i, h) for i in off]
    mid = valid[len(valid) // 2]
    halves = []
    for side in (lambda i: i < mid, lambda i: i >= mid):
        x = [forward(m, asset, i, h) for i in entries if side(i)]
        y = [forward(m, asset, i, h) for i in off if side(i)]
        halves.append(V._mean(x) - V._mean(y) if x and y else None)
    last = max((i for i in range(m.n) if on[i]), default=None)
    # an event stays active for its h-day window, a condition only while it holds
    return {"n": len(a), "n_base": len(b),
            "mean_on": V._mean(a) if a else None, "mean_base": V._mean(b) if b else None,
            "hit_on": sum(x > 0 for x in a) / len(a) if a else None,
            "hit_base": sum(x > 0 for x in b) / len(b) if b else None,
            "p": welch_p(a, b, sign), "first_half": halves[0], "second_half": halves[1],
            "first_date": m.iso[valid[0]], "active": active,
            "last_on": m.iso[last] if last is not None else None}


def benjamini_hochberg(ps, q=BH_Q):
    """Set of keys that survive, from {key: p}. Two-sided use: pass min(p, 1 - p)."""
    ranked = sorted(ps.items(), key=lambda kv: kv[1])
    cut = 0
    for k, (_, p) in enumerate(ranked, 1):
        if p <= q * k / len(ranked):
            cut = k
    return {key for key, _ in ranked[:cut]}


def label(results, signs):
    testable = {k: r for k, r in results.items()
                if r["n"] >= MIN_WINDOWS and r["n_base"] >= MIN_BASE_WINDOWS}
    two_sided = {k: min(r["p"], 1 - r["p"]) * 2 for k, r in testable.items()}
    found = benjamini_hochberg(two_sided)
    out = {}
    for k, r in results.items():
        if k not in testable:
            out[k] = "TOO FEW"
            continue
        right = r["p"] < 0.5
        both = (None not in (r["first_half"], r["second_half"])
                and r["first_half"] * signs[k] > 0 and r["second_half"] * signs[k] > 0)
        if k in found and right and both:
            out[k] = "SUPPORTED"
        elif k in found and not right:
            out[k] = "OPPOSITE"
        elif right and both and r["p"] < SUGGESTIVE_P:
            out[k] = "SUGGESTIVE"
        else:
            out[k] = "NOT SUPPORTED"
    return out


def asset_name(a):
    return f"{a[0]} minus {a[1]}" if isinstance(a, tuple) else a


def run(conn):
    ensure_tables(conn)
    m = Market(conn)
    reg = registry()
    results = {p["pid"]: evaluate(m, p) for p in reg}       # compute first, then write
    status = label(results, {p["pid"]: p["sign"] for p in reg})
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cols = ("n", "n_base", "mean_on", "mean_base", "hit_on", "hit_base", "p", "first_half",
            "second_half", "first_date")
    conn.execute("DELETE FROM pattern_results")
    for p in reg:
        r = results[p["pid"]]
        conn.execute("INSERT INTO pattern_results VALUES (?, ?, ?, ?, ?, ?, ?, " + ", ".join("?" * len(cols))
                     + ", ?, ?, ?)",
                     (p["pid"], p["family"], p["claim"], asset_name(p["asset"]), p["h"], p["sign"],
                      status[p["pid"]], *[r.get(c) for c in cols], int(r["active"]), r["last_on"], now))
    conn.commit()


def show(conn):
    rows = conn.execute("SELECT pid, status, asset, h, sign, n, mean_on, mean_base, hit_on, p, active"
                        " FROM pattern_results ORDER BY CASE status WHEN 'SUPPORTED' THEN 0 WHEN"
                        " 'SUGGESTIVE' THEN 1 WHEN 'OPPOSITE' THEN 2 WHEN 'NOT SUPPORTED' THEN 3"
                        " ELSE 4 END, p").fetchall()
    print(f"{'pattern':23}{'status':15}{'asset':14}{'h':>4}{'n':>5}{'on':>8}{'normal':>8}{'up':>6}{'p':>7}  now")
    for pid, st, asset, h, sign, n, on, base, hit, p, active in rows:
        f = lambda x, s="{:7.1%}": "    n/a" if x is None else s.format(x)  # noqa: E731
        print(f"{pid:23}{st:15}{asset:14}{h:>4}{n:>5} {f(on)} {f(base)}{f(hit, '{:6.0%}')} "
              f"{f(p, '{:6.3f}')}  {'ACTIVE' if active else ''}")
    counts = {}
    for r in rows:
        counts[r[1]] = counts.get(r[1], 0) + 1
    print(f"\nRead: {len(rows)} patterns tested once, plus {len(NOT_TESTED)} not testable here. {counts}")
    print("Past behaviour only. None of these is a trading rule.")


def noteworthy(conn, limit=6):
    """Lines for the daily report: patterns on right now, evidence first. Empty
    if the ledger has not been run."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='pattern_results'").fetchone():
        return []
    rows = conn.execute("SELECT claim, status, asset, h, n, mean_on, mean_base, hit_on FROM pattern_results"
                        " WHERE active=1 AND status IN ('SUPPORTED', 'SUGGESTIVE', 'NOT SUPPORTED', 'OPPOSITE')"
                        " ORDER BY CASE status WHEN 'SUPPORTED' THEN 0 WHEN 'SUGGESTIVE' THEN 1 ELSE 2 END"
                        " LIMIT ?", (limit,)).fetchall()
    total = conn.execute("SELECT COUNT(*) FROM pattern_results").fetchone()[0]
    out = []
    for claim, st, asset, h, n, on, base, hit in rows:
        out.append(f"[{st}] {claim}: {asset} next {h} days {on:+.1%} vs {base:+.1%} normally,"
                   f" up {hit:.0%} of {n} windows")
    if out:
        out.append(f"History from a ledger of {total} tested patterns, not a recommendation.")
    return out


def main_cli():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--show", action="store_true", help="print the stored ledger, no run")
    ap.add_argument("--refresh", action="store_true", help="fetch new data before the run")
    args = ap.parse_args()
    conn = sqlite3.connect(main.DB_PATH, timeout=main.DB_TIMEOUT_S)
    if args.refresh:
        equities.ensure_tables(conn)
        macro_data.ensure_tables(conn)
        macro_data.refresh(conn)
        equities.backfill(conn, equities.load_tiingo_credentials())
    if not args.show:
        run(conn)
    show(conn)


if __name__ == "__main__":
    main_cli()
