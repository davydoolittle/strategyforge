"""
Phase 16.2 (V2_PROMPT 8k): trend switching per market against buy-and-hold.

Rule, as registered: hold the market while its dashboard trend (market_trend.py,
the fixed 200-day rule) is Bull, otherwise hold cash. Decided on each day's
close, traded at the next open (one day later for the delay check), paying
half the quoted spread per side from `instruments`. Cash earns the 3-month
Treasury yield (macro_series 'y3m', the latest value usable on that day), per
trading day. Benchmark: buy-and-hold of the same market from the same first
fill, same costs.

Markets: SPY, IEF, GLD, UUP from snapshot etf-2026-10-30. Pass, per market:
PSR vs 0 >= 0.9875 (0.95, Bonferroni over four) on monthly returns of the
switched book minus buy-and-hold. Bitcoin (BTC-USD from candles, from 2021-05)
is information only, at 10 bps a side.

Read-only: nothing is written.

  python trend_switch.py
"""

import argparse
import math
import sqlite3
from bisect import bisect_right
from datetime import datetime, timezone

import etf_portfolio as P
import market_trend as MT
import validation as V

SNAPSHOT = "etf-2026-10-30"
MARKETS = ("SPY", "IEF", "GLD", "UUP")
MIN_PSR = 1 - 0.05 / len(MARKETS)          # 0.9875
INFO_MARKET = "BTC-USD"
INFO_SIDE_COST = 0.0010


def load_etf(conn, symbol, snapshot=SNAPSHOT):
    """(dates, opens, closes, side cost rate) from the frozen snapshot."""
    rows = conn.execute("SELECT ts, open, close FROM bar_snapshots WHERE snapshot=? AND pair=? ORDER BY ts",
                        (snapshot, symbol)).fetchall()
    if not rows:
        raise ValueError(f"{symbol}: no bars in snapshot {snapshot}")
    spread, frac = conn.execute("SELECT spread_bps, slippage_frac FROM instruments WHERE symbol=?",
                                (symbol,)).fetchone()
    dates = [datetime.fromtimestamp(t, timezone.utc).date().isoformat() for t, _, _ in rows]
    return dates, [r[1] for r in rows], [r[2] for r in rows], spread * frac / 10000.0


def load_btc(conn):
    """BTC daily: close = last hourly close of the UTC day, open = previous close."""
    d = MT.daily_closes(conn, INFO_MARKET, "crypto")
    dates, closes = [x for x, _ in d], [c for _, c in d]
    return dates, [closes[0]] + closes[:-1], closes, INFO_SIDE_COST


def cash_yield(conn):
    """(usable dates, annual % values) for y3m, sorted by usable date."""
    rows = conn.execute("SELECT usable, value FROM macro_series WHERE series='y3m' AND value IS NOT NULL"
                        " ORDER BY usable, date").fetchall()
    return [r[0] for r in rows], [r[1] for r in rows]


def yield_on(cy, day):
    days, vals = cy
    k = bisect_right(days, day) - 1
    return vals[k] if k >= 0 else 0.0


def simulate(dates, opens, closes, rate, cy, periods=252, delay=0, cost_x=1.0, hold_always=False):
    """Daily book. Returns {"days": [index], "equity": [close equity], "in": [bool], "switches": n}.
    The first decision is the first day with a trend state; equity starts at 1.0
    there, in cash. hold_always=True is the benchmark (in from the first fill)."""
    st = MT.states(list(zip(dates, closes)))
    state = {d: s[4] for d, s in zip([x[0] for x in st], st)}
    k0 = dates.index(st[0][0])
    want = {}                                   # fill day index -> target (1 in, 0 out)
    for t in range(k0, len(dates)):
        target = 1 if hold_always or state.get(dates[t]) == "Bull" else 0
        f = t + 1 + delay
        if f < len(dates):
            want[f] = target
    cash, qty, pos = 1.0, 0.0, 0
    days, eq, inside, switches = [], [], [], 0
    for t in range(k0 + 1, len(dates)):
        if t in want and want[t] != pos:
            value = cash + qty * opens[t]
            fee = value * rate * cost_x
            if want[t] == 1:
                qty, cash = (value - fee) / opens[t], 0.0
            else:
                cash, qty = value - fee, 0.0
            pos = want[t]
            switches += 1
        if pos == 0:
            cash *= 1 + yield_on(cy, dates[t]) / 100.0 / periods
        days.append(t)
        eq.append(cash + qty * closes[t])
        inside.append(pos == 1)
    return {"days": days, "equity": eq, "in": inside, "switches": switches}


def month_ends(dates):
    return [i for i in range(len(dates)) if i == len(dates) - 1 or dates[i][:7] != dates[i + 1][:7]]


def monthly(dates, sim):
    ends = set(month_ends(dates))
    out, labels, prev = [], [], 1.0
    for d, e in zip(sim["days"], sim["equity"]):
        if d in ends:
            out.append(e / prev - 1)
            labels.append(dates[d][:7])
            prev = e
    return out, labels


def summary(sim, periods=252):
    eq = sim["equity"]
    years = len(eq) / periods
    peak, dd = 1.0, 0.0
    for x in eq:
        peak = max(peak, x)
        dd = min(dd, x / peak - 1)
    rets = [b / a - 1 for a, b in zip([1.0] + eq[:-1], eq)]
    return {"cagr": eq[-1] ** (1 / years) - 1, "max_dd": dd, "vol": V._std(rets) * math.sqrt(periods),
            "in_market": sum(sim["in"]) / len(sim["in"]), "switches": sim["switches"]}


def evaluate(dates, opens, closes, rate, cy, periods=252, **kw):
    book = simulate(dates, opens, closes, rate, cy, periods, **kw)
    bench = simulate(dates, opens, closes, rate, cy, periods, hold_always=True, **kw)
    rb, labels = monthly(dates, book)
    rm, _ = monthly(dates, bench)
    excess = [a - b for a, b in zip(rb, rm)]
    pr = P.psr(excess)[0] if len(excess) > 2 else 0.0
    return {"book": book, "bench": bench, "excess": excess, "months": labels, "psr": pr,
            "ann_sr": P.annual_sharpe(excess) if len(excess) > 2 else 0.0, "passed": pr >= MIN_PSR,
            "first": dates[book["days"][0]], "last": dates[book["days"][-1]]}


def line(label, r, periods):
    b, m = summary(r["book"], periods), summary(r["bench"], periods)
    return (f"{label:5} {r['first']}..{r['last']}  switched CAGR {b['cagr']:6.2%} DD {b['max_dd']:6.1%} "
            f"vol {b['vol']:5.1%} in {b['in_market']:4.0%} switches {b['switches']:3d} | hold CAGR {m['cagr']:6.2%} "
            f"DD {m['max_dd']:6.1%} vol {m['vol']:5.1%} | excess Sharpe {r['ann_sr']:+.3f} PSR {r['psr']:.4f}")


def main_cli(argv=None):
    import main
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.parse_args(argv)
    conn = sqlite3.connect(f"file:{main.DB_PATH}?mode=ro", uri=True, timeout=main.DB_TIMEOUT_S)
    cy = cash_yield(conn)
    print(f"Phase 16.2: snapshot {SNAPSHOT}; pass PSR >= {MIN_PSR:.4f} per market (Bonferroni over {len(MARKETS)})\n")
    verdicts = {}
    for sym in MARKETS:
        data = load_etf(conn, sym)
        r = evaluate(*data, cy)
        verdicts[sym] = r["passed"]
        print(line(sym, r, 252))
        print(f"      VERDICT {sym}: {'PASS: earns a forward test' if r['passed'] else 'FAIL'}")
        for label, kw in (("2x costs", {"cost_x": 2.0}), ("fills a day later", {"delay": 1})):
            x = evaluate(*data, cy, **kw)
            print(f"      {label:18} excess Sharpe {x['ann_sr']:+.3f}  PSR {x['psr']:.4f}")
        half = len(r["excess"]) // 2
        for name, part in (("first half", r["excess"][:half]), ("second half", r["excess"][half:])):
            print(f"      {name:18} {r['months'][0 if name == 'first half' else half]}.. excess Sharpe "
                  f"{P.annual_sharpe(part):+.3f}  PSR {P.psr(part)[0]:.4f}")
    print("\nInformation only (not part of the verdict):")
    r = evaluate(*load_btc(conn), cy, periods=365)
    print(line("BTC", r, 365))
    print(f"\nRead: {sum(verdicts.values())} of {len(MARKETS)} markets pass. PSR is the probability that the"
          " true excess Sharpe of switching over holding is above 0, given the monthly sample.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
