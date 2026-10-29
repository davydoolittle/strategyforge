"""
StrategyForge portfolio — one candidate's cfg run as a book across every pair
(P6.1).

A candidate is selected on its HOME pair. This module asks what the same
parameter set does when it trades every pair in main.PAIRS at once, out of
one capital pool:

  - each pair runs on its own windows as they stood when the candidate was
    pinned (home: its stored windows; others: windows.compute at the
    candidate's oos_end and history start, exactly as adversary A7 cuts them)
    and at its own verified tick (main.TICK_SIZE). Pairs with no verified
    tick are excluded and listed, as in A7. The home pair keeps the tick in
    its stored cfg so it reproduces its stored results.
  - execution is engine.backtest's, unchanged. Signals, stops and exits do not
    depend on equity; only position size does. So every trade's return as a
    fraction of the equity it was sized from (validation.trade_returns) is
    the same in the book as when the pair trades alone, and the book re-sizes
    each trade from the POOL's realised equity at its entry:
        book pnl = trade return x pool equity at entry
    With one pair the book is that pair's own backtest, to the cent.
  - event order inside a bar: an exit at the open (reverse on opposite) is
    realised before entries at that open; a stop/target exit happens inside
    the bar, so after them. No trade is sized from a P&L realised later.

Reported per pair (standalone) and for the book: trades, net, PF, max DD,
per-trade Sharpe, annualised daily Sharpe (sqrt(365): crypto trades every
day), and the mean pairwise correlation of the pairs' daily returns.

Daily returns: each trade's P&L is spread over the UTC days it was open, pro
rata by time (validation.bucket_returns' convention), and divided by the
equity realised before that day. That is a smoothing, not mark-to-market.

A2 for the book: the selection happened once, on the home pair, so the
benchmark is E[max SR] of the candidate's own family's trials on its home
pair/tf, with that pair's across-trial SR variance. The statistic is the
book's per-trade Sharpe on its full-window trades (the trials' SRs are
per-trade, so a per-trade SR is what can be compared with them). Note that a
trade's return is the same in the book as alone, so the book's per-trade SR
is the pooled per-trade SR of the pairs: combining adds trades, not SR per
trade. The daily Sharpe is where diversification shows; it is reported with
its PSR against zero, information only, because there is no trial
distribution of daily Sharpes to deflate it against.

Nothing is written to the database.

Usage
  python portfolio.py --candidate 4027
  python portfolio.py --candidate 4027 --json
"""

import argparse
import json
import math
import os
import sqlite3

import engine
import main as collector
import strategies
import validation as V
import windows as W

DB_PATH = os.environ.get("FORGE_DB", "/data/forge.db")
MIN_DSR = 0.95          # adversary A2's bar
DAYS_PER_YEAR = 365


def legs_for(conn, cid):
    """The candidate's cfg run standalone on every pair with a verified tick.
    Returns (meta, legs, excluded); legs[pair] = {"windows", "full", "oos"}."""
    row = conn.execute("SELECT pair, tf, params_json FROM candidates WHERE id=?",
                       (cid,)).fetchone()
    if row is None:
        raise SystemExit(f"candidate {cid} not found")
    home, tf, pj = row
    tf_s = W.tf_seconds(tf)
    is_start, split, end = W.for_candidate(conn, cid)
    start_ts = W.history_start(is_start, tf_s)
    cfg = W.pin_cfg(json.loads(pj), is_start, tf_s)
    legs, excluded = {}, []
    for pair in collector.PAIRS:
        if pair == home:
            w, pcfg = (is_start, split, end), cfg
        else:
            tick = collector.TICK_SIZE.get(pair)
            if tick is None:
                excluded.append(pair)
                continue
            w = W.compute(conn, pair, tf_s, W._ts(end), start_ts)
            if w is None:
                excluded.append(pair)
                continue
            pcfg = {**cfg, "tick_size": tick}
        legs[pair] = {
            "windows": w, "tick_size": pcfg["tick_size"],
            "full": engine.backtest(conn, pair, pcfg, w[0], w[2], tf_s)["trades"],
            "oos": engine.backtest(conn, pair, pcfg, w[1], w[2], tf_s)["trades"],
        }
    meta = {"candidate_id": cid, "home": home, "tf": tf, "tf_seconds": tf_s,
            "family": strategies.family_of(cfg), "params": cfg,
            "initial_capital": cfg["initial_capital"], "risk_pct": cfg.get("risk_pct"),
            "windows": {"is_start": is_start, "split": split, "end": end}}
    return meta, legs, excluded


# ------------------------------------------------------------------ book ---

def book(trades_by_pair, initial_capital):
    """Re-size every pair's trades out of one pool. Returns the book's trades
    (pair, entry_ts, exit_ts, ret, pnl, equity_at_entry) in exit order, the
    realised equity path and the most positions open at once."""
    events = []
    for pair, trades in trades_by_pair.items():
        for t, r in zip(trades, V.trade_returns(trades, initial_capital)):
            leg = {"pair": pair, "dir": t["dir"], "entry_ts": t["entry_ts"],
                   "exit_ts": t["exit_ts"], "reason": t["reason"], "ret": r}
            events.append(((t["entry_ts"], 1, pair), "in", leg))
            events.append(((t["exit_ts"], 0 if t["reason"] == "opposite" else 2, pair),
                           "out", leg))
    events.sort(key=lambda e: e[0])
    equity = float(initial_capital)
    out, open_now, max_open = [], 0, 0
    for _, kind, leg in events:
        if kind == "in":
            leg["equity_at_entry"] = equity
            open_now += 1
            max_open = max(max_open, open_now)
        else:
            leg["pnl"] = leg["ret"] * leg["equity_at_entry"]
            equity += leg["pnl"]
            open_now -= 1
            out.append(leg)
    return {"trades": out, "final_equity": equity, "max_open": max_open}


def daily_returns(trades, initial_capital, start_ts, end_ts):
    """Book daily returns: P&L spread pro rata over the days each trade was
    open, over the equity realised before that day. trades need entry_ts,
    exit_ts, pnl."""
    d0 = start_ts // V.DAY
    n = end_ts // V.DAY - d0 + 1
    pnl = [0.0] * n
    realised = [0.0] * (n + 1)      # P&L of trades closed on day k, by index
    for t in trades:
        a, b = t["entry_ts"], max(t["exit_ts"], t["entry_ts"] + 1)
        x = a
        while x < b:
            k = x // V.DAY
            nxt = min(b, (k + 1) * V.DAY)
            if 0 <= k - d0 < n:
                pnl[k - d0] += t["pnl"] * (nxt - x) / (b - a)
            x = nxt
        k = t["exit_ts"] // V.DAY - d0
        realised[min(max(k, 0), n)] += t["pnl"]
    out, eq = [], float(initial_capital)
    for k in range(n):
        out.append(pnl[k] / eq if eq > 0 else 0.0)
        eq += realised[k]
    return out


def annualised(rets):
    return V.sharpe(rets) * math.sqrt(DAYS_PER_YEAR)


def stats(pnls, initial_capital):
    wins = sum(p for p in pnls if p > 0)
    losses = -sum(p for p in pnls if p <= 0)
    eq = peak = float(initial_capital)
    dd = 0.0
    for p in pnls:
        eq += p
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return {"n_trades": len(pnls), "net": sum(pnls),
            "pf": wins / losses if losses > 0 else None,
            "max_dd": dd, "max_dd_pct": dd / initial_capital * 100.0}


def mean_pairwise_corr(series_by_pair):
    """Mean Pearson correlation of daily returns over each pair of pairs'
    common days. series_by_pair[pair] = (first_day, [returns])."""
    pairs = sorted(series_by_pair)
    rhos = []
    for x, a in enumerate(pairs):
        for b in pairs[x + 1:]:
            da, sa = series_by_pair[a]
            db, sb = series_by_pair[b]
            lo, hi = max(da, db), min(da + len(sa), db + len(sb))
            if hi - lo < 30:
                continue
            m = V.correlation_matrix([sa[lo - da:hi - da], sb[lo - db:hi - db]])
            if m[0][1] is not None:
                rhos.append(((a, b), m[0][1]))
    return (sum(r for _, r in rhos) / len(rhos) if rhos else None), rhos


def evaluate(conn, cid):
    meta, legs, excluded = legs_for(conn, cid)
    cap = meta["initial_capital"]
    home = meta["home"]
    out = {**meta, "pairs": sorted(legs), "excluded_unverified_tick": excluded,
           "legs": {}, "book": {}}
    for window in ("full", "oos"):
        wi = 0 if window == "full" else 1
        per_pair_daily = {}
        for pair, leg in legs.items():
            tr = leg[window]
            rets = V.trade_returns(tr, cap)
            s_ts, e_ts = W._ts(leg["windows"][wi]), W._ts(leg["windows"][2])
            daily = V.bucket_returns(tr, cap, s_ts, e_ts)
            per_pair_daily[pair] = (s_ts // V.DAY, daily)
            out["legs"].setdefault(pair, {"windows": list(leg["windows"]),
                                          "tick_size": leg["tick_size"]})[window] = {
                **stats([t["pnl"] for t in tr], cap),
                "sr_per_trade": V.sharpe(rets), "sr_daily_ann": annualised(daily)}
        b = book({p: leg[window] for p, leg in legs.items()}, cap)
        s_ts = min(W._ts(leg["windows"][wi]) for leg in legs.values())
        e_ts = max(W._ts(leg["windows"][2]) for leg in legs.values())
        daily = daily_returns(b["trades"], cap, s_ts, e_ts)
        rets = [t["ret"] for t in b["trades"]]
        sk, ku = V.moments(daily)
        mean_rho, rhos = mean_pairwise_corr(per_pair_daily)
        out["book"][window] = {
            **stats([t["pnl"] for t in b["trades"]], cap),
            "sr_per_trade": V.sharpe(rets), "sr_daily_ann": annualised(daily),
            "n_days": len(daily), "psr_daily_vs_0": V.probabilistic_sharpe(
                V.sharpe(daily), len(daily), sk, ku, 0.0),
            "max_open": b["max_open"],
            "mean_pair_corr": mean_rho, "pair_corr": [[a, b2, r] for (a, b2), r in rhos],
            "trades_by_pair": {p: sum(1 for t in b["trades"] if t["pair"] == p) for p in legs},
            "net_by_pair": {p: sum(t["pnl"] for t in b["trades"] if t["pair"] == p) for p in legs},
        }
        if window == "full":
            out["book"]["full"]["a2"] = a2(conn, meta, rets)
            out["legs"][home]["full"]["a2"] = a2(conn, meta, V.trade_returns(legs[home]["full"], cap))
    return out


def a2(conn, meta, rets):
    """Deflated Sharpe of a per-trade return series against the home pair's
    own-family trials (the one selection that happened)."""
    sr = V.sharpe(rets)
    sk, ku = V.moments(rets)
    var, n_used, n_trials, _ = V.sharpe_variance_from_candidates(
        conn, meta["home"], meta["tf"], meta["family"])
    dsr, bench = V.deflated_sharpe(sr, len(rets), sk, ku, n_trials, var)
    return {"sr_per_trade": sr, "n_obs": len(rets), "skew": sk, "kurtosis": ku,
            "n_trials": n_trials, "sr_variance": var, "sr_var_sampled_from": n_used,
            "expected_max_sr": bench, "dsr": dsr,
            "psr_vs_0": V.probabilistic_sharpe(sr, len(rets), sk, ku, 0.0),
            "breakeven_trials": V.breakeven_trials(sr, len(rets), sk, ku, var, MIN_DSR),
            "min_dsr": MIN_DSR, "passed": dsr >= MIN_DSR}


# ------------------------------------------------------------------- CLI ---

def _f(x, nd=2):
    return "n/a" if x is None else f"{x:.{nd}f}"


def print_report(r):
    print("\n" + "=" * 78)
    print(f"PORTFOLIO — candidate #{r['candidate_id']} {r['family']} (home {r['home']} {r['tf']})"
          f", one pool, {r['risk_pct']}% risk per trade")
    print("=" * 78)
    if r["excluded_unverified_tick"]:
        print(f"excluded (no verified tick): {', '.join(r['excluded_unverified_tick'])}")
    for window in ("full", "oos"):
        print(f"\n--- {window.upper()} windows ---")
        print(f"  {'':10s} {'trades':>6s} {'net':>9s} {'pf':>5s} {'maxDD%':>7s} "
              f"{'SR/trade':>8s} {'SR daily ann':>12s}")
        for pair in r["pairs"]:
            s = r["legs"][pair][window]
            tag = " (home)" if pair == r["home"] else ""
            print(f"  {pair:10s} {s['n_trades']:6d} {s['net']:+9.0f} {_f(s['pf']):>5s} "
                  f"{s['max_dd_pct']:7.1f} {s['sr_per_trade']:8.3f} {s['sr_daily_ann']:12.2f}{tag}")
        b = r["book"][window]
        print(f"  {'BOOK':10s} {b['n_trades']:6d} {b['net']:+9.0f} {_f(b['pf']):>5s} "
              f"{b['max_dd_pct']:7.1f} {b['sr_per_trade']:8.3f} {b['sr_daily_ann']:12.2f}")
        print(f"  book: {b['n_days']} days, PSR(daily SR > 0) {b['psr_daily_vs_0']:.3f}, "
              f"at most {b['max_open']} positions open at once, "
              f"mean pairwise daily-return correlation {_f(b['mean_pair_corr'], 3)}")
        print("  book net by pair (pool-sized): " + "  ".join(
            f"{p} {v:+.0f}/{b['trades_by_pair'][p]}t" for p, v in sorted(b["net_by_pair"].items())))
    h, bk = r["legs"][r["home"]]["full"]["a2"], r["book"]["full"]["a2"]
    print(f"\n--- A2 against {h['n_trials']} {r['family']} trials on {r['home']} "
          f"(E[max SR] {h['expected_max_sr']:.3f}, SR variance {h['sr_variance']:.4f}) ---")
    for name, a in ((f"{r['home']} alone", h), ("book", bk)):
        print(f"  {name:14s} SR/trade {a['sr_per_trade']:.3f} on {a['n_obs']} trades  "
              f"PSR vs 0 {a['psr_vs_0']:.3f}  DSR {a['dsr']:.3f}  break-even N "
              f"{a['breakeven_trials']}  -> {'PASS' if a['passed'] else 'FAIL'}")
    print("\nRead: a trade's return is the same in the book as alone, so the book's SR per")
    print("trade is the pairs' pooled SR per trade; the book adds trades, not SR per trade.")
    print("If that SR is below E[max SR], more trades push DSR down, not up. The daily")
    print("Sharpe and the correlation show what diversification does to the equity curve;")
    print("they are not deflated, because no trial distribution of daily Sharpes exists.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidate", type=int, required=True)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    conn = sqlite3.connect(DB_PATH)
    r = evaluate(conn, args.candidate)
    conn.close()
    if args.json:
        print(json.dumps(r))
    else:
        print_report(r)


if __name__ == "__main__":
    main()
