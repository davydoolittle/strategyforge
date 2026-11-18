"""
Phase 15 evaluator (V2_PROMPT 8j, P15.2): the seven-factor battery on the
point-in-time S&P 500, 2017-2026. Read-only on the P15.1 snapshot in stocks.db.

The stop rules run first: the price rule over the whole battery, then each
statement or text signal's 80% data rule. No return is computed for anything a
stop rule closes.

Each month's last trading day (the formation) ranks the priced members on each
signal, using data up to that close only. The top fifth, equal weight, is bought
at the next trading day's open and held to the next rebalance's open; the last
holding ends at the snapshot's final close, 2026-10-30 (the registered window
has no later bar). The benchmark is every priced member, equal weight, on the
same days. A series that ends inside a holding returns its last close, then -30%,
for strategy and benchmark alike. Costs are 10 bps a side on the weight traded.

The verdict per strategy: PSR vs 0 of monthly (strategy - benchmark) at or above
0.99286 (0.95, Bonferroni over seven). Everything else printed is information.

  python stock_factors.py               # the registered run (P15.3): once
  python stock_factors.py --coverage    # stop rules only, no returns
"""

import argparse
import bisect
import math
import statistics
from collections import defaultdict
from datetime import date

import factor_data as F
import stocks as S
import validation as V

SIGNALS = ("momentum", "high52", "lowvol", "reversal", "profitability", "value", "unchanged_10k")
DATA_SIGNALS = ("profitability", "value", "unchanged_10k")
TOP = 0.20
COST = 0.0010                    # a side
DELIST = -0.30                   # Shumway's return after the last bar
MIN_PSR = 1 - 0.05 / len(SIGNALS)
DATA_STOP = 0.80
LOOK = 252                       # trading days in a year of history
MONTH = 21
ANNUAL_DAYS = (350, 380)         # a 10-K duration fact counts as annual within this


# ------------------------------ data access ----------------------------------

class Snapshot:
    """Bars, membership and statements, all from P15.1's tables."""

    def __init__(self, conn):
        self.cal = [r[0] for r in conn.execute("SELECT day FROM f15_bars WHERE symbol=? ORDER BY day",
                                               (F.BENCHMARK,))]
        self.bars = defaultdict(dict)
        for sym, d, o, c, raw in conn.execute("SELECT symbol, day, open, close, raw_close FROM f15_bars"):
            self.bars[sym][d] = (o, c, raw)
        self.days = {s: sorted(v) for s, v in self.bars.items()}
        self.members = [(t, s, e, c, sym) for (t, s, e, c), sym in (
            ((t, s, e, c), self._sym(conn, t, s)) for t, s, e, c in F.intervals(conn))]
        self.facts = defaultdict(lambda: index_facts([]))
        rows = defaultdict(list)
        for cik, tag, st, en, val, form, filed in conn.execute(
                "SELECT cik, tag, start, end, val, form, filed FROM f15_facts"):
            rows[cik].append((tag, st, en, val, form, filed))
        for cik, r in rows.items():
            self.facts[cik] = index_facts(r)
        self.text = defaultdict(list)
        for acc, cik, filed, blob in conn.execute(
                "SELECT accession, cik, filed, counts FROM f15_text WHERE counts IS NOT NULL ORDER BY filed"):
            self.text[cik].append((filed, acc, blob))

    @staticmethod
    def _sym(conn, t, s):
        r = conn.execute("SELECT symbol FROM f15_symbols WHERE ticker=? AND start=?", (t, s)).fetchone()
        return r[0] if r else None

    def at(self, f):
        """Members priced on formation day f: [(ticker, cik, symbol)]."""
        return [(t, c, sym) for t, s, e, c, sym in self.members
                if s <= f < (e or "9999-12-31") and sym and f in self.bars[sym]]

    def closes_to(self, sym, f, n):
        """The last n adjusted closes up to and including f, or None if short."""
        ds = self.days[sym]
        i = bisect.bisect_right(ds, f)
        if i < n:
            return None
        return [self.bars[sym][d][1] for d in ds[i - n:i]]


# ------------------------------ signals --------------------------------------

def momentum(px):
    return px[-MONTH - 1] / px[0] - 1                    # 12 months back to 1 month back


def high52(px):
    return px[-1] / max(px[-LOOK:])


def lowvol(px):
    rets = [px[k] / px[k - 1] - 1 for k in range(len(px) - LOOK, len(px))]
    return -statistics.stdev(rets)                       # higher is better: lower volatility


def reversal(px):
    return -(px[-1] / px[-MONTH - 1] - 1)               # last month's losers first


def index_facts(rows):
    """One company's facts, indexed once: annual 10-K durations by line, balance
    sheet values by (line, period end), shares by filing date. Each list is in
    filing order so a formation reads only what was filed by then."""
    annual, instant, shares = defaultdict(list), defaultdict(list), []
    for tag, st, en, val, form, filed in rows:
        name, rank = tag.split(":")
        if name == "shares":
            if val:
                shares.append((filed, en, rank == "dei", val))
            continue
        rank = -int(rank)                                    # tag 0 is preferred
        instant[(name, en)].append((filed, rank, val))
        if st and (form or "").startswith("10-K"):
            dur = (date.fromisoformat(en) - date.fromisoformat(st)).days
            if ANNUAL_DAYS[0] <= dur <= ANNUAL_DAYS[1]:
                annual[name].append((filed, en, rank, val))
    for d in (annual, instant):
        for v in d.values():
            v.sort()
    shares.sort()
    return {"annual": annual, "instant": instant, "shares": shares}


def _annual(facts, name, f):
    """Latest annual value of a statement line filed by f, preferred tag at that
    period end: (end, value) or None."""
    best = None
    for filed, en, rank, val in facts["annual"].get(name, ()):
        if filed > f:
            break
        key = (en, rank, filed)
        if best is None or key > best[0]:
            best = (key, val)
    return (best[0][0], best[1]) if best else None


def _instant(facts, name, end, f):
    """A balance-sheet value at exactly `end`, filed by f, preferred tag first."""
    best = None
    for filed, rank, val in facts["instant"].get((name, end), ()):
        if filed > f:
            break
        if best is None or (rank, filed) > best[0]:
            best = ((rank, filed), val)
    return best[1] if best else None


def _shares(facts, f):
    """Latest reported shares outstanding filed by f (the cover page's figure
    wins a tie)."""
    best = None
    for filed, en, dei, val in facts["shares"]:
        if filed > f:
            break
        if best is None or (filed, en, dei) > best[0]:
            best = ((filed, en, dei), val)
    return best[1] if best else None


def profitability(facts, f):
    rev = _annual(facts, "revenue", f)
    cogs = _annual(facts, "cost_of_revenue", f)
    if not rev or not cogs or rev[0] != cogs[0]:
        return None
    assets = _instant(facts, "assets", rev[0], f)
    return (rev[1] - cogs[1]) / assets if assets else None


def value_parts(facts, f, raw_close):
    """(earnings yield, book to market) or None. Both need the same fiscal year."""
    ni = _annual(facts, "net_income", f)
    shares = _shares(facts, f)
    if not ni or not shares or not raw_close:
        return None
    equity = _instant(facts, "equity", ni[0], f)
    if equity is None:
        return None
    mv = raw_close * shares
    return ni[1] / mv, equity / mv


def cosine(a, b):
    dot = sum(v * b.get(k, 0) for k, v in a.items())
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    return dot / (na * nb) if na and nb else None


def unchanged_10k(text, f, cache):
    """Similarity of the latest 10-K filed within 365 days before f to the one before it."""
    filed = [x for x in text if x[0] <= f]
    if len(filed) < 2 or (date.fromisoformat(f) - date.fromisoformat(filed[-1][0])).days > 365:
        return None
    key = (filed[-2][1], filed[-1][1])
    if key not in cache:
        cache[key] = cosine(F.unpack(filed[-2][2]), F.unpack(filed[-1][2]))
    return cache[key]


def pct_ranks(scores):
    """{k: percentile rank in [0, 1]}, higher score higher rank."""
    order = sorted(scores, key=lambda k: scores[k])
    n = len(order)
    return {k: (i / (n - 1) if n > 1 else 0.5) for i, k in enumerate(order)}


def signal_scores(snap, f, members, cache):
    """{signal: {symbol: score}}, higher is better for every signal."""
    out = {s: {} for s in SIGNALS}
    ep, bm = {}, {}
    for t, cik, sym in members:
        px = snap.closes_to(sym, f, LOOK + 1)
        if px:
            out["momentum"][sym] = momentum(px)
            out["high52"][sym] = high52(px)
            out["lowvol"][sym] = lowvol(px)
            out["reversal"][sym] = reversal(px)
        facts = snap.facts[cik] if cik in snap.facts else index_facts([])
        p = profitability(facts, f)
        if p is not None:
            out["profitability"][sym] = p
        v = value_parts(facts, f, snap.bars[sym][f][2])
        if v:
            ep[sym], bm[sym] = v
        u = unchanged_10k(snap.text.get(cik, []), f, cache)
        if u is not None:
            out["unchanged_10k"][sym] = u
    if ep:
        r1 = pct_ranks({k: (x if x > 0 else -1e9) for k, x in ep.items()})
        r2 = pct_ranks({k: (x if x > 0 else -1e9) for k, x in bm.items()})
        out["value"] = {k: (r1[k] + r2[k]) / 2 for k in ep}
    return out


# ------------------------------ portfolio arithmetic -------------------------

def hold_return(snap, sym, entry_day, exit_day, f, worst_case=True):
    """Open-to-open return from entry_day to exit_day ('end': the last close of
    the window). A bar missing on the exit day with later bars is a gap, priced at
    the next open; a series with no bar on or after it has ended, and returns its
    last close, then DELIST (no DELIST without the worst case)."""
    bars, ds = snap.bars[sym], snap.days[sym]
    entry = bars[entry_day][0] if entry_day in bars else bars[f][1]
    stop = snap.cal[-1] if exit_day == "end" else exit_day
    k = bisect.bisect_left(ds, stop)
    if k < len(ds):
        d = ds[k]
        return (bars[d][1] if exit_day == "end" else bars[d][0]) / entry - 1
    last = bars[ds[-1]][1] / entry
    return last * (1 + DELIST) - 1 if worst_case else last - 1


def run_book(snap, months, picks, worst_case=True):
    """Monthly net returns of an equal-weight book. months: [(f, entry, exit)];
    picks: {f: [symbols]}. Costs on the weight traded, against drifted weights."""
    rets, held = [], {}
    for f, entry, exit_day in months:
        names = picks.get(f, [])
        if not names:
            rets.append(0.0)
            held = {}
            continue
        target = {s: 1 / len(names) for s in names}
        turnover = sum(abs(target.get(s, 0) - held.get(s, 0)) for s in set(target) | set(held))
        r = {s: hold_return(snap, s, entry, exit_day, f, worst_case) for s in names}
        gross = sum(target[s] * r[s] for s in names)
        rets.append(gross - turnover * COST)
        total = 1 + gross
        held = {s: target[s] * (1 + r[s]) / total for s in names}
    return rets


def schedule(snap):
    """[(formation, entry day, exit day)] for every registered formation."""
    forms = F.formation_dates(snap.cal)
    nxt = {d: snap.cal[i + 1] for i, d in enumerate(snap.cal[:-1])}
    out = []
    for k, f in enumerate(forms):
        entry = nxt[f]
        exit_day = nxt[forms[k + 1]] if k + 1 < len(forms) else "end"
        out.append((f, entry, exit_day))
    return out


def top(scores, frac=TOP):
    k = max(1, round(len(scores) * frac))
    return [s for s, _ in sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[:k]]


def bottom(scores, frac=TOP):
    k = max(1, round(len(scores) * frac))
    return [s for s, _ in sorted(scores.items(), key=lambda kv: (kv[1], kv[0]))[:k]]


# ------------------------------ the run --------------------------------------

def stats(excess):
    sr = V.sharpe(excess)
    skew, kurt = V.moments(excess)
    return {"months": len(excess), "mean_annual": statistics.fmean(excess) * 12,
            "sharpe_annual": sr * math.sqrt(12), "psr": V.probabilistic_sharpe(sr, len(excess), skew, kurt)}


def evaluate(snap, log=print):
    """Stop rules, then every signal once. Returns the full result."""
    months = schedule(snap)
    members = {f: snap.at(f) for f, _, _ in months}
    total = sum(1 for f, _, _ in months for m in snap.members if m[1] <= f < (m[2] or "9999-12-31"))
    priced = sum(len(v) for v in members.values())
    price_share = 1 - priced / total if total else 1.0
    out = {"price_unpriced_share": price_share, "price_passes": price_share <= F.PRICE_STOP, "signals": {}}
    log(f"price stop rule: {price_share:.2%} of {total} member-months unpriced (limit {F.PRICE_STOP:.0%})")
    if not out["price_passes"]:
        log("STOP: the battery is not testable on free data; no return computed.")
        return out
    cache = {}
    scores = {f: signal_scores(snap, f, members[f], cache) for f, _, _ in months}
    for sig in SIGNALS:
        have = sum(len(scores[f][sig]) for f, _, _ in months)
        out["signals"][sig] = {"coverage": have / priced}
    for sig in DATA_SIGNALS:
        c = out["signals"][sig]["coverage"]
        out["signals"][sig]["testable"] = c >= DATA_STOP
        log(f"{sig}: data for {c:.1%} of member-months (limit {DATA_STOP:.0%})"
            + ("" if c >= DATA_STOP else ": NOT TESTABLE, counts as a trial"))
    bench = run_book(snap, months, {f: [s for _, _, s in members[f]] for f, _, _ in months})
    bench_nw = run_book(snap, months, {f: [s for _, _, s in members[f]] for f, _, _ in months}, False)
    out["benchmark_annual"] = statistics.fmean(bench) * 12
    for sig in SIGNALS:
        info = out["signals"][sig]
        if info.get("testable") is False:
            continue
        book = run_book(snap, months, {f: top(scores[f][sig]) for f, _, _ in months})
        excess = [a - b for a, b in zip(book, bench)]
        info.update(stats(excess))
        info["passed"] = info["psr"] >= MIN_PSR
        low = run_book(snap, months, {f: bottom(scores[f][sig]) for f, _, _ in months})
        half = len(excess) // 2
        no_worst = run_book(snap, months, {f: top(scores[f][sig]) for f, _, _ in months}, False)
        ex2020 = [x for (f, _, _), x in zip(months, excess) if not f.startswith("2020")]
        info["info"] = {
            "top_minus_bottom_annual": statistics.fmean([a - b for a, b in zip(book, low)]) * 12,
            "first_half": stats(excess[:half]), "second_half": stats(excess[half:]),
            "without_2020": stats(ex2020),
            "without_worst_case": stats([a - b for a, b in zip(no_worst, bench_nw)]),
            "book_annual": statistics.fmean(book) * 12,
        }
    return out


def report(res):
    print(f"\nPhase 15: seven factors, point-in-time S&P 500, pass PSR >= {MIN_PSR:.5f} (Bonferroni over 7)")
    if not res["price_passes"]:
        return
    print(f"benchmark (equal-weight members) {res['benchmark_annual']:+.2%}/yr")
    print(f"{'signal':16}{'coverage':>9}{'excess/yr':>11}{'Sharpe':>8}{'PSR':>8}  verdict   first half / second half Sharpe")
    for sig, i in res["signals"].items():
        if i.get("testable") is False:
            print(f"{sig:16}{i['coverage']:9.1%}  NOT TESTABLE (data below {DATA_STOP:.0%}); counts as a trial")
            continue
        h1, h2 = i["info"]["first_half"], i["info"]["second_half"]
        print(f"{sig:16}{i['coverage']:9.1%}{i['mean_annual']:+11.2%}{i['sharpe_annual']:8.2f}{i['psr']:8.3f}"
              f"  {'PASS' if i['passed'] else 'FAIL':8}  {h1['sharpe_annual']:+.2f} / {h2['sharpe_annual']:+.2f}")
    print("\nRead: past, simulated, after costs; the verdict column is the registered test, the rest information.")


def main_cli(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--coverage", action="store_true", help="stop rules only, no returns")
    args = ap.parse_args(argv)
    snap = Snapshot(S.connect(ro=True))
    if args.coverage:
        months = schedule(snap)
        members = {f: snap.at(f) for f, _, _ in months}
        cache, priced, have = {}, 0, defaultdict(int)
        for f, _, _ in months:
            sc = signal_scores(snap, f, members[f], cache)
            priced += len(members[f])
            for sig in DATA_SIGNALS:
                have[sig] += len(sc[sig])
        for sig in DATA_SIGNALS:
            print(f"{sig}: {have[sig] / priced:.1%} of {priced} priced member-months")
        return
    report(evaluate(snap))


if __name__ == "__main__":
    main_cli()
