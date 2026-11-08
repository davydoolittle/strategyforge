"""
Pre-registered cross-sectional momentum (Phase 14, V2_PROMPT 8h). One fixed rule,
N = 1: no search, and nothing here may be tuned after the first run.

Universe, fixed by launch cohort: the 9 original Select Sector SPDRs and the 17
original iShares MSCI country funds (equities.XSMOM_SECTORS / XSMOM_COUNTRIES).

Each month's last trading day, momentum = adjusted close one month-end back
over the close 12 month-ends back, minus 1 ("12-1": the latest month is
skipped). The book holds the top 3 sectors and the top 6 countries, ties broken
by symbol, 50% per sleeve, equal weight inside a sleeve: long only, fully
invested, no leverage. It rebalances to those weights at the next trading day's
open, paying half the quoted spread per side (5 bps sectors, 15 bps countries,
from `instruments`).

Benchmark: 50% equal weight of the 9 sectors plus 50% equal weight of the 17
countries, rebalanced on the same days at the same costs. The test is PSR vs 0
>= 0.95 on monthly book minus benchmark. A pass earns a forward test on data
after the snapshot, not a promotion.

The simulator, month ends, monthly returns and PSR are Phase 10's
(etf_portfolio.py), so the mechanics are the ones F17 already used.

Read-only: every bar comes from one frozen snapshot, and nothing is written.

  python xsmom.py                    # the registered test, on SNAPSHOT
  python xsmom.py --snapshot NAME
"""

import argparse
import math
import sqlite3
from datetime import datetime, timezone

import equities
import etf_portfolio as P
import main
import validation as V

SNAPSHOT = "xsmom-2026-11-06"
START = "1998-12-22"            # the sector funds' first day; the countries are older
LOOKBACK_MONTHS = 12            # the far end of 12-1
SKIP_MONTHS = 1                 # the near end of 12-1
MIN_PSR = 0.95
# (name, symbols, weight, number held)
SLEEVES = (("sectors", tuple(equities.XSMOM_SECTORS), 0.5, 3),
           ("countries", tuple(equities.XSMOM_COUNTRIES), 0.5, 6))
HALVES = (("2000-2012", None, "2013-01-01"), ("2013-2026", "2013-01-01", None))

assert LOOKBACK_MONTHS == P.LOOKBACK_MONTHS, "the shared simulator starts at month-end 12"


def load(conn, snapshot=SNAPSHOT, start=START, symbols=None):
    """(trading days, {symbol: opens}, {symbol: closes}, {symbol: side cost}).
    Every symbol must have a bar on every day of the calendar."""
    symbols = symbols or [s for _, syms, _, _ in SLEEVES for s in syms]
    start_ts = int(datetime.fromisoformat(start).replace(tzinfo=timezone.utc).timestamp())
    cal = [r[0] for r in conn.execute("SELECT ts FROM bar_snapshots WHERE snapshot=? AND pair=?"
                                      " AND ts>=? ORDER BY ts", (snapshot, symbols[0], start_ts))]
    if not cal:
        raise ValueError(f"snapshot {snapshot} has no bars for {symbols[0]}")
    opens, closes, rate = {}, {}, {}
    for s in symbols:
        rows = conn.execute("SELECT ts, open, close FROM bar_snapshots WHERE snapshot=? AND pair=?"
                            " AND ts>=? ORDER BY ts", (snapshot, s, start_ts)).fetchall()
        if [r[0] for r in rows] != cal:
            missing = sorted(set(cal) - {r[0] for r in rows})[:3]
            raise ValueError(f"{s} does not match the trading calendar (e.g. missing "
                             f"{[equities._date(t) for t in missing]})")
        opens[s], closes[s] = [r[1] for r in rows], [r[2] for r in rows]
        spread, frac = conn.execute("SELECT spread_bps, slippage_frac FROM instruments WHERE symbol=?",
                                    (s,)).fetchone()
        rate[s] = spread * frac / 10000.0
    return cal, opens, closes, rate


def momentum(c, far, near):
    return c[near] / c[far] - 1


def rank(closes, syms, far, near):
    """Symbols best first by 12-1 momentum, ties by symbol."""
    return sorted(syms, key=lambda s: (-momentum(closes[s], far, near), s))


def make_targets(cal, sleeves=SLEEVES, pick="top"):
    """A targets(closes, sig, prev) for etf_portfolio.simulate. prev is the
    month-end 12 back (the far end); the near end is the month-end before sig.
    pick: 'top' (the rule), 'bottom' (information only) or 'all' (benchmark)."""
    ends = P.month_ends(cal)
    pos = {d: k for k, d in enumerate(ends)}

    def targets(closes, sig, prev):
        near = ends[pos[sig] - SKIP_MONTHS]
        out = {}
        for _, syms, weight, n in sleeves:
            if pick == "all":
                held = list(syms)
            else:
                order = rank(closes, syms, prev, near)
                held = order[:n] if pick == "top" else order[-n:]
            for s in held:
                out[s] = out.get(s, 0.0) + weight / len(held)
        return out
    return targets


def selections(cal, closes, sleeves=SLEEVES):
    """[(signal day index, {sleeve: held symbols})] for every rebalance."""
    ends = P.month_ends(cal)
    out = []
    for k in range(LOOKBACK_MONTHS, len(ends)):
        far, near = ends[k - LOOKBACK_MONTHS], ends[k - SKIP_MONTHS]
        out.append((ends[k], {name: rank(closes, syms, far, near)[:n] for name, syms, _, n in sleeves}))
    return out


def turnover(sel, sleeves=SLEEVES):
    """Mean share of the book whose name changes at a rebalance (drift ignored)."""
    w = {name: weight / n for name, _, weight, n in sleeves}
    changes = []
    for (_, a), (_, b) in zip(sel, sel[1:]):
        changes.append(sum(w[name] * len(set(b[name]) - set(a[name])) for name in w))
    return sum(changes) / len(changes) if changes else 0.0


def month_labels(cal, sim):
    ends = set(P.month_ends(cal))
    return [equities._date(cal[d]) for d in sim["days"] if d in ends]


def evaluate(cal, opens, closes, rate, sleeves=SLEEVES, **kw):
    book = P.simulate(cal, opens, closes, rate, make_targets(cal, sleeves, "top"), **kw)
    bench = P.simulate(cal, opens, closes, rate, make_targets(cal, sleeves, "all"), **kw)
    rb, rm = P.monthly_returns(cal, book), P.monthly_returns(cal, bench)
    excess = [a - b for a, b in zip(rb, rm)]
    pr = P.psr(excess)[0]
    return {"book": book, "bench": bench, "rb": rb, "rm": rm, "excess": excess,
            "months": month_labels(cal, book), "psr": pr, "ann_sr": P.annual_sharpe(excess),
            "passed": pr >= MIN_PSR}


def split(months, xs, start=None, end=None):
    return [x for m, x in zip(months, xs) if (start is None or m >= start) and (end is None or m < end)]


def vol(sim):
    eq = sim["equity"]
    rets = [b / a - 1 for a, b in zip(eq, eq[1:])]
    return V._std(rets) * math.sqrt(252)


def main_cli(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--snapshot", default=SNAPSHOT)
    args = ap.parse_args(argv)
    conn = sqlite3.connect(f"file:{main.DB_PATH}?mode=ro", uri=True, timeout=main.DB_TIMEOUT_S)
    cal, opens, closes, rate = load(conn, args.snapshot)
    r = evaluate(cal, opens, closes, rate)
    b, m = P.summary(r["book"]), P.summary(r["bench"])
    years = len(r["book"]["days"]) / 252.0
    first = equities._date(cal[r["book"]["days"][0]])
    print(f"Phase 14: snapshot {args.snapshot}, first fill {first}, last bar {equities._date(cal[-1])}, "
          f"{years:.1f} years, {len(r['excess'])} months, N = 1\n")
    print(f"{'':28}{'CAGR':>8}{'max DD':>9}{'vol':>7}{'Sharpe':>8}")
    print(f"{'book (top 3 + top 6)':28}{b['cagr']:8.2%}{b['max_dd']:9.1%}{vol(r['book']):7.1%}"
          f"{P.annual_sharpe(r['rb']):8.2f}")
    print(f"{'equal-weight benchmark':28}{m['cagr']:8.2%}{m['max_dd']:9.1%}{vol(r['bench']):7.1%}"
          f"{P.annual_sharpe(r['rm']):8.2f}")
    print(f"\nexcess vs benchmark: annualised Sharpe {r['ann_sr']:+.3f}, PSR vs 0 {r['psr']:.3f}"
          f" (needs {MIN_PSR})")
    print(f"VERDICT: {'PASS: earns a forward test on data after the snapshot, not a promotion' if r['passed'] else 'FAIL'}\n")

    print("Information only, the verdict above does not use these:")
    sel = selections(cal, closes)
    print(f"  turnover: {turnover(sel):.0%} of the book changes name per month on average")
    for name, syms, _, n in SLEEVES:
        x = evaluate(cal, opens, closes, rate, sleeves=((name, syms, 1.0, n),))
        print(f"  {name + ' sleeve alone':28} book CAGR {P.summary(x['book'])['cagr']:6.2%} vs "
              f"{P.summary(x['bench'])['cagr']:6.2%}  excess Sharpe {x['ann_sr']:+.3f}  PSR {x['psr']:.3f}")
    for label, kw in (("2x costs", {"cost_x": 2.0}), ("fills one day later", {"delay": 1})):
        x = evaluate(cal, opens, closes, rate, **kw)
        print(f"  {label:28} book CAGR {P.summary(x['book'])['cagr']:6.2%}  excess Sharpe "
              f"{x['ann_sr']:+.3f}  PSR {x['psr']:.3f}")
    for label, start, end in HALVES:
        ex = split(r["months"], r["excess"], start, end)
        print(f"  {label:28} {len(ex):3d} months  excess Sharpe {P.annual_sharpe(ex):+.3f}  "
              f"PSR {P.psr(ex)[0]:.3f}")
    bottom = P.simulate(cal, opens, closes, rate, make_targets(cal, SLEEVES, "bottom"))
    spread = [a - b for a, b in zip(r["rb"], P.monthly_returns(cal, bottom))]
    print(f"  {'top third minus bottom third':28} mean {12 * sum(spread) / len(spread):+.2%} a year, "
          f"Sharpe {P.annual_sharpe(spread):+.3f}, PSR {P.psr(spread)[0]:.3f}")
    print("\nRead: PSR is the probability that the true excess Sharpe is above 0, given the monthly"
          " sample, its skew and kurtosis. The rule was fixed in V2_PROMPT 8h before any data was"
          " fetched; a FAIL ends this hypothesis, and a variant needs its own registration.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
