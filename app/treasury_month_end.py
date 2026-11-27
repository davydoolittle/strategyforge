"""
P10.4 (V2_PROMPT 8d): the Treasury month-end rule F24 earned, cost-inclusive,
as registered and approved 2026-11-27. Run once per fund; read-only on snapshot
etf-2026-10-30.

Rule: hold the fund from the OPEN of the month's fifth-last trading day through
the CLOSE of its last trading day; otherwise cash earning the 3-month Treasury
yield (macro_series y3m, latest value usable that day), per trading day. The
on-days are known in advance from the calendar. Every entry and exit pays half
the quoted spread (instruments). Benchmark: buy-and-hold of the same fund from
the window's first open, same costs.

Window: F24's months, each fund's first to last full calendar month in the
snapshot (2002-08 to 2026-09). Pass per fund: PSR vs 0 >= 0.975 (0.95 over two
funds) on monthly returns of rule minus buy-and-hold.

Information only: CAGR, max drawdown, volatility, time in the market, 2x costs,
entry a day later (open of the fourth-last day), both halves, rule vs cash alone.

P10.5 (approved 2026-11-27), forward only: the same rule against cash alone,
from December 2026, on the live adjusted closes in `candles` (never the
snapshot). One verdict, on exactly the first 72 forward months (to 2032-11);
before that, --forward prints a reading for the record that cannot pass, fail
or end the test.

  python treasury_month_end.py              # P10.4, the historical test (run once)
  python treasury_month_end.py --forward    # P10.5 reading, or its verdict at 72 months
"""

import math
import sqlite3
from datetime import datetime, timezone

import etf_portfolio as P
import trend_switch as TS
import validation as V

SNAPSHOT = "etf-2026-10-30"
FUNDS = ("IEF", "TLT")
MIN_PSR = 1 - 0.05 / len(FUNDS)        # 0.975
ON_DAYS = 5
FORWARD_START = "2026-12"           # first forward month (P10.5)
FORWARD_MONTHS = 72                 # the registered size: one verdict, never earlier


def load(conn, symbol, snapshot=SNAPSHOT):
    """(dates, opens, closes, side cost) for full calendar months only."""
    dates, opens, closes, rate = TS.load_etf(conn, symbol, snapshot)
    first, last = dates[0][:7], dates[-1][:7]
    keep = [i for i, d in enumerate(dates) if d[:7] not in (first, last)]
    return [dates[i] for i in keep], [opens[i] for i in keep], [closes[i] for i in keep], rate


def load_forward(conn, symbol, today=None):
    """(dates, opens, closes, side cost) from live candles, full months from
    FORWARD_START on: the current month is left out until it has ended."""
    today = today or datetime.now(timezone.utc).date()
    rows = conn.execute("SELECT ts, open, close FROM candles WHERE pair=? AND session='us_equity' AND close > 0"
                        " ORDER BY ts", (symbol,)).fetchall()
    spread, frac = conn.execute("SELECT spread_bps, slippage_frac FROM instruments WHERE symbol=?",
                                (symbol,)).fetchone()
    cur = today.isoformat()[:7]
    out = [(datetime.fromtimestamp(t, timezone.utc).date().isoformat(), o, c) for t, o, c in rows]
    out = [r for r in out if FORWARD_START <= r[0][:7] < cur]
    return [r[0] for r in out], [r[1] for r in out], [r[2] for r in out], spread * frac / 10000.0


def first_months(data, n):
    """The data cut to its first n calendar months."""
    dates = data[0]
    months = sorted({d[:7] for d in dates})[:n]
    keep = [i for i, d in enumerate(dates) if d[:7] in months]
    return tuple([x[i] for i in keep] for x in data[:3]) + (data[3],)


def forward(conn, cy, symbol, today=None):
    """{"months": k, "verdict": None until FORWARD_MONTHS, then the PSR test}."""
    data = load_forward(conn, symbol, today)
    months = len({d[:7] for d in data[0]})
    out = {"symbol": symbol, "months": months, "verdict": None}
    if months == 0:
        return out
    if months >= FORWARD_MONTHS:
        data = first_months(data, FORWARD_MONTHS)
    on = on_flags(data[0])
    rule = simulate(*data, cy, on)
    cash = simulate(*data, cy, on, always_cash=True)
    vs = compare(data[0], rule, cash)
    out.update(psr=vs["psr"] if months > 2 else None, ann_sr=vs["ann_sr"] if months > 2 else None,
               first=data[0][0], last=data[0][-1])
    if months >= FORWARD_MONTHS:
        out["verdict"] = "PASS" if vs["psr"] >= MIN_PSR else "FAIL"
    return out


def on_flags(dates, n_on=ON_DAYS, skip_first=0):
    """True on the last n_on trading days of each month; skip_first drops the
    first of them (the delayed entry)."""
    by = {}
    for i, d in enumerate(dates):
        by.setdefault(d[:7], []).append(i)
    on = [False] * len(dates)
    for idx in by.values():
        for i in idx[-n_on:][skip_first:]:
            on[i] = True
    return on


def simulate(dates, opens, closes, rate, cy, on, cost_x=1.0, hold_always=False, always_cash=False):
    """Daily equity at each close. In the market on a day: bought at that day's
    open if not already in (paying the side cost), marked at the close; out the
    first day after the last on-day: sold at the previous close (side cost). Out
    of the market: cash grows by the day's yield."""
    fee = rate * cost_x
    eq, out, inside = 1.0, [], []
    held = False
    for t in range(len(dates)):
        want = (not always_cash) and (hold_always or on[t])
        if want and not held:
            eq *= (1 - fee) * closes[t] / opens[t]
            held = True
        elif want and held:
            eq *= closes[t] / closes[t - 1]
        else:
            eq *= 1 + TS.yield_on(cy, dates[t]) / 100.0 / 252
        last_on = held and not hold_always and (t + 1 == len(dates) or not on[t + 1])
        if last_on:
            eq *= 1 - fee                      # sold at this close
            held = False
        out.append(eq)
        inside.append(want)
    return {"days": list(range(len(dates))), "equity": out, "in": inside}


def monthly(dates, sim):
    ends = set(TS.month_ends(dates))
    rets, labels, prev = [], [], 1.0
    for d, e in zip(sim["days"], sim["equity"]):
        if d in ends:
            rets.append(e / prev - 1)
            labels.append(dates[d][:7])
            prev = e
    return rets, labels


def summary(sim):
    eq = sim["equity"]
    years = len(eq) / 252.0
    peak, dd = 1.0, 0.0
    for x in eq:
        peak, dd = max(peak, x), min(dd, x / max(peak, x) - 1)
    rets = [b / a - 1 for a, b in zip([1.0] + eq[:-1], eq)]
    return {"cagr": eq[-1] ** (1 / years) - 1, "max_dd": dd, "vol": V._std(rets) * math.sqrt(252),
            "in_market": sum(sim["in"]) / len(sim["in"])}


def compare(dates, a, b):
    ra, labels = monthly(dates, a)
    rb, _ = monthly(dates, b)
    ex = [x - y for x, y in zip(ra, rb)]
    return {"excess": ex, "months": labels, "psr": P.psr(ex)[0], "ann_sr": P.annual_sharpe(ex)}


def evaluate(dates, opens, closes, rate, cy, cost_x=1.0, skip_first=0):
    on = on_flags(dates, skip_first=skip_first)
    rule = simulate(dates, opens, closes, rate, cy, on, cost_x)
    hold = simulate(dates, opens, closes, rate, cy, on, cost_x, hold_always=True)
    cash = simulate(dates, opens, closes, rate, cy, on, cost_x, always_cash=True)
    vs_hold, vs_cash = compare(dates, rule, hold), compare(dates, rule, cash)
    return {"rule": rule, "hold": hold, "cash": cash, "vs_hold": vs_hold, "vs_cash": vs_cash,
            "passed": vs_hold["psr"] >= MIN_PSR, "first": dates[0], "last": dates[-1]}


def main_cli(argv=None):
    import argparse
    import main
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--forward", action="store_true", help="P10.5: the forward reading, or its verdict at 72 months")
    args = ap.parse_args(argv)
    conn = sqlite3.connect(f"file:{main.DB_PATH}?mode=ro", uri=True, timeout=main.DB_TIMEOUT_S)
    cy = TS.cash_yield(conn)
    if args.forward:
        print(f"P10.5 month-end Treasury exposure vs cash, forward from {FORWARD_START}; one verdict at"
              f" {FORWARD_MONTHS} months, PSR >= {MIN_PSR}\n")
        for sym in FUNDS:
            r = forward(conn, cy, sym)
            if r["months"] == 0:
                print(f"{sym}: no full forward month yet (the first is {FORWARD_START})")
            elif r["verdict"] is None:
                extra = (f", excess Sharpe so far {r['ann_sr']:+.3f}, PSR so far {r['psr']:.3f}"
                         if r["psr"] is not None else "")
                print(f"{sym}: month {r['months']} of {FORWARD_MONTHS} ({r['first']} .. {r['last']}){extra}."
                      " For the record only: no verdict before month 72.")
            else:
                print(f"{sym}: VERDICT {r['verdict']} on months 1-{FORWARD_MONTHS} ({r['first']} .. {r['last']}):"
                      f" excess Sharpe {r['ann_sr']:+.3f}, PSR {r['psr']:.4f}")
        return 0
    print(f"P10.4 Treasury month-end rule: snapshot {SNAPSHOT}; pass PSR vs 0 >= {MIN_PSR} per fund on monthly"
          " rule minus buy-and-hold\n")
    for sym in FUNDS:
        data = load(conn, sym)
        r = evaluate(*data, cy)
        a, h, c = summary(r["rule"]), summary(r["hold"]), summary(r["cash"])
        print(f"{sym}  {r['first']} .. {r['last']}  ({len(r['vs_hold']['excess'])} months)")
        for name, s in (("rule", a), ("buy-and-hold", h), ("cash only", c)):
            print(f"   {name:13} CAGR {s['cagr']:6.2%}  max DD {s['max_dd']:6.1%}  vol {s['vol']:5.1%}"
                  f"  in market {s['in_market']:4.0%}")
        vh = r["vs_hold"]
        print(f"   rule - buy-and-hold: excess Sharpe {vh['ann_sr']:+.3f}, PSR {vh['psr']:.4f}"
              f"  VERDICT {sym}: {'PASS: earns a forward test' if r['passed'] else 'FAIL'}")
        print(f"   information only:   rule - cash: excess Sharpe {r['vs_cash']['ann_sr']:+.3f},"
              f" PSR {r['vs_cash']['psr']:.4f}")
        for label, kw in (("2x costs", {"cost_x": 2.0}), ("entry a day later", {"skip_first": 1})):
            x = evaluate(*data, cy, **kw)
            print(f"   {label:19} vs hold Sharpe {x['vs_hold']['ann_sr']:+.3f} PSR {x['vs_hold']['psr']:.4f}"
                  f" | vs cash Sharpe {x['vs_cash']['ann_sr']:+.3f} PSR {x['vs_cash']['psr']:.4f}")
        half = len(vh["excess"]) // 2
        for name, part, part_c in (("first half", vh["excess"][:half], r["vs_cash"]["excess"][:half]),
                                   ("second half", vh["excess"][half:], r["vs_cash"]["excess"][half:])):
            print(f"   {name:19} vs hold Sharpe {P.annual_sharpe(part):+.3f} PSR {P.psr(part)[0]:.4f}"
                  f" | vs cash Sharpe {P.annual_sharpe(part_c):+.3f} PSR {P.psr(part_c)[0]:.4f}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
