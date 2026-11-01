"""
Pre-registered ETF portfolio test (Phase 10, V2_PROMPT 8d). One fixed rule,
N = 1: no search, and nothing here may be tuned after the first run.

Each month's last trading day, an ETF is held if its 12-month adjusted return
is above 0, otherwise its sleeve sits in cash. A held sleeve (1/10 of equity)
targets 10% annualised volatility on 60 days of returns, capped at 2x, so total
notional cannot pass 2x. Trades fill at the next trading day's open at the
instrument's costs. Cash earns nothing.

Benchmark: equal-weight buy-and-hold of the same ten, rebalanced on the same
days at the same costs. The test is PSR vs 0 >= 0.95 on monthly book minus
benchmark. A pass earns a forward test, not a promotion.

The rule does not charge for borrowing when notional is above 1x. borrow_rate
prices that for the report only; the verdict uses the rule as written.

Read-only: every bar comes from one frozen snapshot, and nothing is written.

  python etf_portfolio.py
"""

import math
import sqlite3
from datetime import datetime, timezone

import equities
import main
import validation as V

SNAPSHOT = "etf-2026-10-30"
START = "2007-03-01"
LOOKBACK_MONTHS = 12
VOL_DAYS = 60
TARGET_VOL = 0.10
MAX_SLEEVE_X = 2.0
MIN_PSR = 0.95
INFO_BORROW_RATE = 0.03     # information only, a rough average short rate


def load(conn, snapshot=SNAPSHOT, start=START):
    """(trading days, {symbol: opens}, {symbol: closes}, {symbol: side cost}).
    Every symbol must have a bar on every day: a sleeve cannot be priced otherwise."""
    start_ts = int(datetime.fromisoformat(start).replace(tzinfo=timezone.utc).timestamp())
    syms = list(equities.UNIVERSE)
    cal = [r[0] for r in conn.execute("SELECT ts FROM bar_snapshots WHERE snapshot=? AND pair=?"
                                      " AND ts>=? ORDER BY ts", (snapshot, syms[0], start_ts))]
    if not cal:
        raise ValueError(f"snapshot {snapshot} has no bars")
    opens, closes, rate = {}, {}, {}
    for s in syms:
        rows = conn.execute("SELECT ts, open, close FROM bar_snapshots WHERE snapshot=? AND pair=?"
                            " AND ts>=? ORDER BY ts", (snapshot, s, start_ts)).fetchall()
        if [r[0] for r in rows] != cal:
            raise ValueError(f"{s} does not match the trading calendar")
        opens[s], closes[s] = [r[1] for r in rows], [r[2] for r in rows]
        spread, frac = conn.execute("SELECT spread_bps, slippage_frac FROM instruments WHERE symbol=?",
                                    (s,)).fetchone()
        rate[s] = spread * frac / 10000.0
    return cal, opens, closes, rate


def month_ends(cal):
    """Index of each month's last trading day."""
    def month(t):
        d = datetime.fromtimestamp(t, tz=timezone.utc)
        return d.year, d.month
    return [i for i in range(len(cal)) if i == len(cal) - 1 or month(cal[i]) != month(cal[i + 1])]


def realised_vol(closes, i):
    rets = [closes[k] / closes[k - 1] - 1 for k in range(i - VOL_DAYS + 1, i + 1)]
    mean = sum(rets) / len(rets)
    return math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)) * math.sqrt(252)


def book_targets(closes, sig, prev):
    """Weights decided at the close of day `sig`; prev is the day 12 month-ends earlier."""
    out = {}
    n = len(closes)
    for s, c in closes.items():
        if c[sig] / c[prev] - 1 > 0:
            vol = realised_vol(c, sig)
            out[s] = min(TARGET_VOL / vol, MAX_SLEEVE_X) / n if vol > 0 else 0.0
    return out


def bench_targets(closes, sig, prev):
    return {s: 1.0 / len(closes) for s in closes}


def simulate(cal, opens, closes, rate, targets, delay=0, cost_x=1.0, borrow_rate=0.0):
    """Daily book from the first fill. Returns {"days", "equity", "sleeve_pnl",
    "gross", "fills"}; equity[k] is the close of days[k], starting from 1.0."""
    ends = month_ends(cal)
    fills = {}
    for k in range(LOOKBACK_MONTHS, len(ends)):
        d = ends[k] + 1 + delay
        if d < len(cal):
            fills[d] = targets(closes, ends[k], ends[k - LOOKBACK_MONTHS])
    syms = list(closes)
    qty = {s: 0.0 for s in syms}
    pnl = {s: 0.0 for s in syms}
    cash, days, equity, gross = 1.0, [], [], []
    for d in range(min(fills), len(cal)):
        for s in syms:
            pnl[s] += qty[s] * (opens[s][d] - closes[s][d - 1])
        if d in fills:
            eq_open = cash + sum(qty[s] * opens[s][d] for s in syms)
            for s in syms:
                new = fills[d].get(s, 0.0) * eq_open / opens[s][d]
                traded = (new - qty[s]) * opens[s][d]
                fee = abs(traded) * rate[s] * cost_x
                cash -= traded + fee
                pnl[s] -= fee
                qty[s] = new
        if borrow_rate and cash < 0:
            cash += cash * borrow_rate / 252.0
        for s in syms:
            pnl[s] += qty[s] * (closes[s][d] - opens[s][d])
        eq = cash + sum(qty[s] * closes[s][d] for s in syms)
        days.append(d)
        equity.append(eq)
        gross.append(sum(qty[s] * closes[s][d] for s in syms) / eq)
    return {"days": days, "equity": equity, "sleeve_pnl": pnl, "gross": gross, "fills": len(fills)}


def monthly_returns(cal, sim):
    ends = set(month_ends(cal))
    out, prev = [], 1.0
    for d, eq in zip(sim["days"], sim["equity"]):
        if d in ends:
            out.append(eq / prev - 1)
            prev = eq
    return out


def summary(sim):
    eq = sim["equity"]
    years = len(eq) / 252.0
    peak, dd = 1.0, 0.0
    for x in eq:
        peak = max(peak, x)
        dd = min(dd, x / peak - 1)
    return {"cagr": eq[-1] ** (1 / years) - 1, "max_dd": dd, "final": eq[-1]}


def annual_sharpe(rets):
    return V.sharpe(rets) * math.sqrt(12)


def psr(excess):
    """(PSR vs 0 of the monthly series, its monthly Sharpe)."""
    sr = V.sharpe(excess)
    skew, kurt = V.moments(excess)
    return V.probabilistic_sharpe(sr, len(excess), skew, kurt), sr


def evaluate(cal, opens, closes, rate, **kw):
    book = simulate(cal, opens, closes, rate, book_targets, **kw)
    bench_kw = {k: v for k, v in kw.items() if k != "borrow_rate"}
    bench = simulate(cal, opens, closes, rate, bench_targets, **bench_kw)
    rb, rm = monthly_returns(cal, book), monthly_returns(cal, bench)
    excess = [a - b for a, b in zip(rb, rm)]
    return {"book": book, "bench": bench, "rb": rb, "rm": rm, "excess": excess,
            "psr": psr(excess)[0], "ann_sr": annual_sharpe(excess), "passed": psr(excess)[0] >= MIN_PSR}


def main_cli():
    conn = sqlite3.connect(f"file:{main.DB_PATH}?mode=ro", uri=True, timeout=main.DB_TIMEOUT_S)
    cal, opens, closes, rate = load(conn)
    r = evaluate(cal, opens, closes, rate)
    b, m = summary(r["book"]), summary(r["bench"])
    years = len(r["book"]["days"]) / 252.0
    first = equities._date(cal[r["book"]["days"][0]])
    print(f"Phase 10: snapshot {SNAPSHOT}, first fill {first}, {years:.1f} years, "
          f"{len(r['excess'])} months, N = 1\n")
    print(f"{'':32}{'CAGR':>8}{'max DD':>9}{'Sharpe':>8}")
    print(f"{'book':32}{b['cagr']:8.2%}{b['max_dd']:9.1%}{annual_sharpe(r['rb']):8.2f}")
    print(f"{'equal-weight benchmark':32}{m['cagr']:8.2%}{m['max_dd']:9.1%}{annual_sharpe(r['rm']):8.2f}")
    print(f"\nexcess vs benchmark: annualised Sharpe {r['ann_sr']:+.3f}, PSR vs 0 {r['psr']:.3f}"
          f" (needs {MIN_PSR})")
    print(f"VERDICT: {'PASS: earns a forward test, not a promotion' if r['passed'] else 'FAIL'}\n")

    gross = r["book"]["gross"]
    print(f"mean notional {sum(gross) / len(gross):.2f}x, max {max(gross):.2f}x, "
          f"days above 1x {sum(g > 1 for g in gross) / len(gross):.0%}")
    sd = lambda x: V._std(x)   # noqa: E731
    k = sd(r["rb"]) / sd(r["rm"])
    matched = [x * k for x in r["rm"]]
    mx = [a - b for a, b in zip(r["rb"], matched)]
    print(f"\nInformation only, the verdict above does not use these:")
    print(f"  benchmark scaled {k:.2f}x to the book's volatility: excess Sharpe "
          f"{annual_sharpe(mx):+.3f}, PSR {psr(mx)[0]:.3f}, benchmark CAGR "
          f"{math.exp(sum(math.log(1 + x) for x in matched) / years) - 1:.2%}")
    for label, kw in (("2x costs", {"cost_x": 2.0}), ("fills one day later", {"delay": 1}),
                      (f"borrowing at {INFO_BORROW_RATE:.0%} above 1x", {"borrow_rate": INFO_BORROW_RATE})):
        x = evaluate(cal, opens, closes, rate, **kw)
        print(f"  {label:28} book CAGR {summary(x['book'])['cagr']:6.2%}  excess Sharpe "
              f"{x['ann_sr']:+.3f}  PSR {x['psr']:.3f}")
    print("\nsleeve share of the book's profit:")
    pnl = r["book"]["sleeve_pnl"]
    total = sum(pnl.values())
    for s, v in sorted(pnl.items(), key=lambda kv: -kv[1]):
        print(f"  {s:5}{v / total:7.1%}")
    print("\nby period, annualised book vs benchmark:")
    ends = [i for i in month_ends(cal) if i >= r["book"]["days"][0]]
    years_of = [datetime.fromtimestamp(cal[i], tz=timezone.utc).year for i in ends]
    for lo, hi in ((2008, 2009), (2010, 2019), (2020, 2026)):
        idx = [j for j, y in enumerate(years_of) if lo <= y <= hi]
        comp = lambda rets: math.exp(sum(math.log(1 + rets[j]) for j in idx) * 12 / len(idx)) - 1  # noqa: E731
        print(f"  {lo}-{hi}  {comp(r['rb']):7.2%}  {comp(r['rm']):7.2%}")
    print("\nRead: past, simulated, after modelled costs and before taxes. The verdict is one test of "
          "one fixed rule.")


if __name__ == "__main__":
    main_cli()
