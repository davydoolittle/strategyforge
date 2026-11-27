"""
P10.3 (V2_PROMPT 8d): calendar effects on the research ten, as registered and
approved 2026-11-27. Run once; read-only on snapshot etf-2026-10-30.

Daily close-to-close returns of adjusted closes, on each ETF's own trading days.
Each ETF's first and last calendar month are left out of every test (a partial
month cannot say which days are its first three or last five).

  C1 turn of month    last trading day + first three of the month    one-sided, higher
  C2 day of week      each weekday vs the other four, two-sided; p = smallest x 5
  C3 opex week        Mon-Fri week holding the month's third Friday   one-sided, higher
  C4 month end        last five trading days of the month             one-sided, higher

Statistic: Welch's t on mean daily return, on days vs off days, normal tail.
Pass per test: p < 0.05 / 40 and the same sign of (on - off) in both halves of
that ETF's sample (for one-sided tests, that sign must be positive). F18
already tested C1 on SPY: that row is reported as a repeat and is not scored.

Information only: uncorrected p, halves, and the effect after charging 2x the
ETF's quoted spread on each entry into and exit from a run of on-days.

  python calendar_effects.py
"""

import math
import sqlite3
from datetime import date, datetime, timedelta, timezone

import equities

SNAPSHOT = "etf-2026-10-30"
ALPHA = 0.05
N_TESTS = 40
BAR = ALPHA / N_TESTS
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri")
REPEAT = {("SPY", "C1")}            # (ETF, hypothesis)


def load(conn, symbol, snapshot=SNAPSHOT):
    rows = conn.execute("SELECT ts, close FROM bar_snapshots WHERE snapshot=? AND pair=? ORDER BY ts",
                        (snapshot, symbol)).fetchall()
    return [(datetime.fromtimestamp(t, timezone.utc).date(), c) for t, c in rows]


def third_friday(y, m):
    d = date(y, m, 1)
    d += timedelta(days=(4 - d.weekday()) % 7)
    return d + timedelta(days=14)


def flags(days):
    """{name: [bool per day]} for C1, C3, C4 and each weekday, from the ETF's
    own trading calendar."""
    by_month = {}
    for i, d in enumerate(days):
        by_month.setdefault((d.year, d.month), []).append(i)
    n = len(days)
    c1, c3, c4 = [False] * n, [False] * n, [False] * n
    for (y, m), idx in by_month.items():
        for i in idx[:3] + idx[-1:]:
            c1[i] = True
        for i in idx[-5:]:
            c4[i] = True
        tf = third_friday(y, m)
        mon = tf - timedelta(days=4)
        for i in idx:
            if mon <= days[i] <= tf:
                c3[i] = True
    out = {"C1": c1, "C3": c3, "C4": c4}
    for k, w in enumerate(WEEKDAYS):
        out[w] = [d.weekday() == k for d in days]
    return out


def welch(on, off):
    """(difference in means, t) or (None, None)."""
    if len(on) < 2 or len(off) < 2:
        return None, None
    m1, m0 = sum(on) / len(on), sum(off) / len(off)
    v1 = sum((x - m1) ** 2 for x in on) / (len(on) - 1)
    v0 = sum((x - m0) ** 2 for x in off) / (len(off) - 1)
    se = math.sqrt(v1 / len(on) + v0 / len(off))
    return m1 - m0, ((m1 - m0) / se if se > 0 else 0.0)


def p_upper(t):
    return 0.5 * math.erfc(t / math.sqrt(2))


def p_two(t):
    return math.erfc(abs(t) / math.sqrt(2))


def sample(bars):
    """(days, returns) with each ETF's first and last calendar month left out."""
    rets = [(bars[i][0], bars[i][1] / bars[i - 1][1] - 1) for i in range(1, len(bars))]
    first, last = (bars[0][0].year, bars[0][0].month), (bars[-1][0].year, bars[-1][0].month)
    keep = [(d, r) for d, r in rets if (d.year, d.month) not in (first, last)]
    return [d for d, _ in keep], [r for _, r in keep]


def split(mask, rets):
    return [r for r, f in zip(rets, mask) if f], [r for r, f in zip(rets, mask) if not f]


def runs(mask):
    """Number of runs of consecutive on-days (each one entry and one exit)."""
    return sum(1 for i, f in enumerate(mask) if f and (i == 0 or not mask[i - 1]))


def test_one(days, rets, mask, one_sided):
    on, off = split(mask, rets)
    diff, t = welch(on, off)
    if diff is None:
        return None
    half = len(rets) // 2
    halves = [welch(*split(mask[a:b], rets[a:b]))[0] for a, b in ((0, half), (half, len(rets)))]
    p = p_upper(t) if one_sided else p_two(t)
    same = all(h is not None for h in halves) and (
        all(h > 0 for h in halves) if one_sided else (all(h > 0 for h in halves) or all(h < 0 for h in halves)))
    return {"diff": diff, "t": t, "p": p, "n_on": len(on), "n_off": len(off), "halves": halves, "same_sign": same}


def run_symbol(symbol, bars, spread_bps):
    days, rets = sample(bars)
    f = flags(days)
    years = len(days) / 252.0
    out = {}
    for h in ("C1", "C3", "C4"):
        r = test_one(days, rets, f[h], True)
        r["runs_per_year"] = runs(f[h]) / years
        r["on_per_year"] = r["n_on"] / years
        out[h] = r
    wk = {w: test_one(days, rets, f[w], False) for w in WEEKDAYS}
    best = min(wk, key=lambda w: wk[w]["p"])
    c2 = dict(wk[best])
    c2["weekday"] = best
    c2["p_raw"] = c2["p"]
    c2["p"] = min(1.0, 5 * c2["p"])
    c2["runs_per_year"] = runs(f[best]) / years
    c2["on_per_year"] = c2["n_on"] / years
    out["C2"] = c2
    cost = 2 * spread_bps / 10000.0                     # 2x the quoted spread per entry and per exit
    for h, r in out.items():
        r["pass"] = (symbol, h) not in REPEAT and r["p"] < BAR and r["same_sign"]
        r["repeat"] = (symbol, h) in REPEAT
        gross = r["diff"] * r["on_per_year"]
        r["gross_pct_year"] = 100 * gross
        r["net_pct_year"] = 100 * (gross - 2 * cost * r["runs_per_year"])
    return {"symbol": symbol, "first": days[0].isoformat(), "last": days[-1].isoformat(), "n": len(days), "tests": out}


def main_cli():
    import main
    conn = sqlite3.connect(f"file:{main.DB_PATH}?mode=ro", uri=True, timeout=main.DB_TIMEOUT_S)
    print(f"P10.3 calendar effects: snapshot {SNAPSHOT}; pass p < {BAR:.5f} (0.05/{N_TESTS}) and the same sign in"
          " both halves\n")
    print(f"{'ETF':5} {'test':4} {'detail':5} {'on-off bp/day':>13} {'p (corr.)':>10} {'p raw':>9} {'halves bp':>15}"
          f" {'gross %/yr':>10} {'net %/yr':>9}  verdict")
    passes, scored = [], 0
    for sym in equities.UNIVERSE:
        bars = load(conn, sym)
        spread = conn.execute("SELECT spread_bps FROM instruments WHERE symbol=?", (sym,)).fetchone()[0]
        res = run_symbol(sym, bars, spread)
        for h in ("C1", "C2", "C3", "C4"):
            r = res["tests"][h]
            det = r.get("weekday", "")
            hv = "/".join("-" if x is None else f"{1e4 * x:+.1f}" for x in r["halves"])
            verdict = "repeat (F18)" if r["repeat"] else ("PASS" if r["pass"] else "fail")
            if not r["repeat"]:
                scored += 1
            if r["pass"]:
                passes.append((sym, h))
            print(f"{sym:5} {h:4} {det:5} {1e4 * r['diff']:+13.2f} {r['p']:10.4f} {r.get('p_raw', r['p']):9.4f} {hv:>15}"
                  f" {r['gross_pct_year']:+10.2f} {r['net_pct_year']:+9.2f}  {verdict}")
        print(f"      sample {res['first']} .. {res['last']}, {res['n']} days")
    print(f"\n{len(passes)} of {scored} scored tests pass: {passes or 'none'}")
    print("Read: p is the chance of an on-day difference this large if the calendar did not matter; the bar"
          " corrects for 40 tests. A pass is a calendar fact, not a strategy.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
