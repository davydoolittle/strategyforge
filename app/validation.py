"""
StrategyForge validation — the statistics that stand between "backtest winner"
and "out-of-sample validated candidate".

Pure stdlib, same style as engine.py. Nothing here trades. Every function is
deterministic given its seed so a report can be reproduced bar-for-bar.

Contents
  trade_returns()        per-trade % returns on the capital at risk at entry
  sharpe() / sortino()   per-trade ratios (NOT annualized — see note below)
  moments()              skew + kurtosis of the return series
  probabilistic_sharpe() PSR: P(true SR > benchmark) given T trades, skew, kurt
  expected_max_sharpe()  E[max SR] of N independent trials with SR variance V
  deflated_sharpe()      DSR = PSR evaluated against expected_max_sharpe()
  walk_forward()         rolling, EMBARGOED folds — train on [a,b), test on
                         [b+embargo, c)
  monte_carlo()          resample trade order -> drawdown distribution
  param_sensitivity()    perturb each numeric param +/-pct, measure PF stability
  cost_stress()          re-run with 2x and 3x commission+slippage
  lookahead_shift()      delay every signal by +1 bar; if results IMPROVE,
                         something is leaking

Sharpe note: ratios are per-trade. Annualizing a 20-trade series is
meaningless and invites comparison with numbers it should not be compared to.
DSR / PSR are scale-free so per-trade is exactly what they want.

References
  Bailey & López de Prado (2014), "The Deflated Sharpe Ratio"
  López de Prado (2018), Advances in Financial ML, ch. 7 (purged/embargoed CV)
"""

import copy
import math
import random
from datetime import datetime, timezone

import engine

EULER_GAMMA = 0.5772156649015329


# ------------------------------------------------------------ basic stats ---

def trade_returns(trades, initial_capital):
    """Per-trade return as a fraction of running equity at entry."""
    eq = float(initial_capital)
    out = []
    for t in trades:
        if eq <= 0:
            out.append(0.0)
            continue
        out.append(t["pnl"] / eq)
        eq += t["pnl"]
    return out


def _mean(x):
    return sum(x) / len(x) if x else 0.0


def _std(x, ddof=1):
    n = len(x)
    if n - ddof <= 0:
        return 0.0
    m = _mean(x)
    return math.sqrt(sum((v - m) ** 2 for v in x) / (n - ddof))


def sharpe(rets):
    if len(rets) < 2:
        return 0.0
    sd = _std(rets)
    return _mean(rets) / sd if sd > 0 else 0.0


def sortino(rets):
    if len(rets) < 2:
        return 0.0
    downside = [min(r, 0.0) for r in rets]
    dd = math.sqrt(sum(d * d for d in downside) / len(rets))
    return _mean(rets) / dd if dd > 0 else 0.0


def moments(rets):
    """Returns (skew, kurtosis). Kurtosis is NON-excess (normal = 3)."""
    n = len(rets)
    if n < 3:
        return 0.0, 3.0
    m = _mean(rets)
    sd = _std(rets, ddof=0)
    if sd == 0:
        return 0.0, 3.0
    skew = sum(((r - m) / sd) ** 3 for r in rets) / n
    kurt = sum(((r - m) / sd) ** 4 for r in rets) / n
    return skew, kurt


def norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_ppf(p):
    """Inverse normal CDF, Acklam's algorithm (|rel err| < 1.15e-9)."""
    if p <= 0.0:
        return -float("inf")
    if p >= 1.0:
        return float("inf")
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    plow = 0.02425
    phigh = 1 - plow
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    if p <= phigh:
        q = p - 0.5
        r = q * q
        return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
               (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)
    q = math.sqrt(-2 * math.log(1 - p))
    return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
           ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)


# ---------------------------------------------------- deflated sharpe ratio ---

def probabilistic_sharpe(sr, n_obs, skew, kurt, sr_benchmark=0.0):
    """PSR = P(true SR > sr_benchmark | observed sr over n_obs returns).
    Bailey & López de Prado eq. (10). kurt is non-excess."""
    if n_obs < 2:
        return 0.0
    denom = 1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr * sr
    if denom <= 0:
        return 0.0
    z = (sr - sr_benchmark) * math.sqrt(n_obs - 1) / math.sqrt(denom)
    return norm_cdf(z)


def expected_max_sharpe(n_trials, sr_variance):
    """E[max SR] among n_trials independent strategies whose true SR is 0 and
    whose estimated SRs have variance sr_variance. BLdP eq. (12)."""
    if n_trials <= 1 or sr_variance <= 0:
        return 0.0
    n = float(n_trials)
    term = (1.0 - EULER_GAMMA) * norm_ppf(1.0 - 1.0 / n) \
        + EULER_GAMMA * norm_ppf(1.0 - 1.0 / (n * math.e))
    return math.sqrt(sr_variance) * term


def deflated_sharpe(sr, n_obs, skew, kurt, n_trials, sr_variance):
    """DSR = PSR with the benchmark set to the SR you'd expect the best of
    n_trials pure-noise strategies to show. > 0.95 is the usual bar."""
    bench = expected_max_sharpe(n_trials, sr_variance)
    return probabilistic_sharpe(sr, n_obs, skew, kurt, bench), bench


# ---------------------------------------------------------- walk-forward ---

def _iso(t):
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def make_folds(lo_ts, hi_ts, tf_seconds, n_folds, train_frac=0.6, embargo_bars=24,
               warmup_bars=engine.WARMUP_BARS):
    """Rolling walk-forward windows over [lo_ts, hi_ts].

    Each fold: train = [t0, t1), test = [t1 + embargo, t2).  The embargo gap
    (in bars) stops a trade opened on the last train bar from bleeding into
    the test window and stops indicator state built on the test window's
    first bars from being 'seen' twice.  Folds advance by one test-length.
    Returns list of dicts with train_start/train_end/test_start/test_end
    (unix seconds).
    """
    usable_lo = lo_ts + warmup_bars * tf_seconds
    span = hi_ts - usable_lo
    if span <= 0 or n_folds < 1:
        return []
    # each fold covers window_len; test portion is (1-train_frac) of it
    # folds overlap in train, never in test
    test_len = span / (n_folds + train_frac / (1.0 - train_frac))
    train_len = test_len * train_frac / (1.0 - train_frac)
    embargo = embargo_bars * tf_seconds
    folds = []
    for k in range(n_folds):
        t0 = usable_lo + k * test_len
        t1 = t0 + train_len
        t2 = t1 + test_len
        if t2 > hi_ts + 1:
            break
        folds.append({
            "fold": k,
            "train_start": int(t0), "train_end": int(t1),
            "test_start": int(t1 + embargo), "test_end": int(t2),
        })
    return folds


def walk_forward(conn, pair, cfg, tf_seconds, n_folds=5, train_frac=0.6,
                 embargo_bars=24, end_ts=None):
    """Run cfg on every embargoed test window. cfg is FIXED (this validates a
    parameter set, it does not re-optimize per fold — that is the optimizer's
    job and would need nested CV). end_ts caps the history (pass the
    candidate's pinned oos_end so folds don't move as candles arrive).
    Returns per-fold stats + summary."""
    if end_ts is None:
        lo, hi = conn.execute(
            "SELECT MIN(ts), MAX(ts) FROM candles WHERE pair=?", (pair,)).fetchone()
    else:
        lo, hi = conn.execute(
            "SELECT MIN(ts), MAX(ts) FROM candles WHERE pair=? AND ts<=?",
            (pair, end_ts)).fetchone()
    folds = make_folds(lo, hi, tf_seconds, n_folds, train_frac, embargo_bars)
    results = []
    for f in folds:
        r = engine.backtest(conn, pair, cfg, _iso(f["test_start"]), _iso(f["test_end"]), tf_seconds)
        rets = trade_returns(r["trades"], cfg["initial_capital"])
        results.append({
            **f,
            "n_trades": r["n_trades"], "net": r["net"],
            "pf": None if r["pf"] == float("inf") else r["pf"],
            "max_dd": r["max_dd"], "win_rate": r["win_rate"],
            "sharpe": sharpe(rets),
        })
    profitable = [x for x in results if x["net"] > 0]
    with_trades = [x for x in results if x["n_trades"] > 0]
    return {
        "folds": results,
        "n_folds": len(results),
        "folds_profitable": len(profitable),
        "folds_with_trades": len(with_trades),
        "consistency": len(profitable) / len(results) if results else 0.0,
        "total_trades": sum(x["n_trades"] for x in results),
        "total_net": sum(x["net"] for x in results),
    }


# ----------------------------------------------------------- monte carlo ---

def monte_carlo(trades, initial_capital, n_sims=2000, seed=7):
    """Two resamplings of the trade list:
      shuffle   — same trades, random ORDER. Final equity is unchanged; max
                  drawdown is not. The historical sequence is one draw from
                  this distribution, so dd_p95 is the drawdown to plan for.
      bootstrap — n trades drawn WITH replacement. Answers 'if the next n
                  trades look like these, how bad could the total be?'
                  final_p05 < initial_capital means a 1-in-20 losing run.
    Both are deterministic for a given seed."""
    if not trades:
        return {"n_sims": 0}
    pnls = [t["pnl"] for t in trades]
    n = len(pnls)
    rng = random.Random(seed)
    dds, finals = [], []
    ruin = 0
    for _ in range(n_sims):
        seq = pnls[:]
        rng.shuffle(seq)
        eq = initial_capital
        peak = eq
        dd = 0.0
        for p in seq:
            eq += p
            peak = max(peak, eq)
            dd = max(dd, peak - eq)
        dds.append(dd)
        boot = [pnls[rng.randrange(n)] for _ in range(n)]
        eq = initial_capital
        ruined = False
        for p in boot:
            eq += p
            if eq <= initial_capital * 0.5:
                ruined = True
        finals.append(eq)
        ruin += 1 if ruined else 0
    dds.sort()
    finals.sort()

    def pct(arr, q):
        return arr[min(len(arr) - 1, int(q * len(arr)))]

    return {
        "n_sims": n_sims, "seed": seed,
        "dd_p50": pct(dds, 0.50), "dd_p95": pct(dds, 0.95), "dd_p99": pct(dds, 0.99),
        "final_p05": pct(finals, 0.05), "final_p50": pct(finals, 0.50),
        "p_final_below_start": sum(1 for f in finals if f < initial_capital) / n_sims,
        "p_ruin_50pct": ruin / n_sims,
    }


# ---------------------------------------------------- param sensitivity ---

SENSITIVITY_PARAMS = ["ema_fast", "ema_mid", "ema_slow", "rsi_buy", "rsi_sell",
                      "adx_threshold", "min_ema_sep", "min_rel_vol",
                      "max_compression", "max_dist_ema", "stop_atr", "rr_ratio"]
INT_PARAMS = {"ema_fast", "ema_mid", "ema_slow", "rsi_len", "adx_len", "dc_len"}


def param_sensitivity(conn, pair, cfg, tf_seconds, start_iso, end_iso, pct=0.10):
    """For each numeric param, nudge it +/-pct and re-run. A robust parameter
    set lives on a plateau: neighbours keep PF > 1. A fragile one lives on a
    spike: neighbours collapse. Returns per-param results + plateau score =
    fraction of neighbours that stay profitable."""
    base = engine.backtest(conn, pair, cfg, start_iso, end_iso, tf_seconds)
    base_pf = base["pf"] if base["pf"] != float("inf") else 99.0
    rows = []
    ok = 0
    total = 0
    for p in SENSITIVITY_PARAMS:
        if p not in cfg:
            continue
        for sign in (-1, +1):
            c2 = copy.deepcopy(cfg)
            v = cfg[p]
            nv = v * (1 + sign * pct)
            if p in INT_PARAMS:
                nv = max(2, int(round(nv)))
                if nv == v:
                    nv = v + sign
            c2[p] = nv
            # keep EMA ordering sane
            if c2["ema_fast"] >= c2["ema_mid"] or c2["ema_mid"] >= c2["ema_slow"]:
                continue
            r = engine.backtest(conn, pair, c2, start_iso, end_iso, tf_seconds)
            pf = r["pf"] if r["pf"] != float("inf") else 99.0
            rows.append({"param": p, "from": v, "to": nv, "pf": pf,
                         "n_trades": r["n_trades"], "net": r["net"]})
            total += 1
            ok += 1 if (r["net"] > 0 and r["n_trades"] > 0) else 0
    return {
        "base_pf": base_pf, "base_net": base["net"], "base_trades": base["n_trades"],
        "neighbours": rows, "plateau_score": ok / total if total else 0.0,
    }


# ------------------------------------------------------------ cost stress ---

def cost_stress(conn, pair, cfg, tf_seconds, start_iso, end_iso, multipliers=(1.0, 2.0, 3.0)):
    out = []
    for m in multipliers:
        c2 = copy.deepcopy(cfg)
        c2["commission_pct"] = cfg["commission_pct"] * m
        c2["slippage_ticks"] = cfg["slippage_ticks"] * m
        r = engine.backtest(conn, pair, c2, start_iso, end_iso, tf_seconds)
        out.append({"cost_x": m, "net": r["net"],
                    "pf": None if r["pf"] == float("inf") else r["pf"],
                    "n_trades": r["n_trades"]})
    return out


# --------------------------------------------------------- look-ahead test ---

def lookahead_shift(conn, pair, cfg, tf_seconds, start_iso, end_iso, delay_bars=1):
    """Hold every fill back delay_bars extra bars (cfg['fill_delay_bars'],
    engine default 0). A real edge DECAYS as execution gets later. If net
    profit goes UP when the strategy acts on staler information, the signal
    is almost certainly leaking something from the future (or is pure
    noise). Returns base vs delayed."""
    base = engine.backtest(conn, pair, cfg, start_iso, end_iso, tf_seconds)
    c2 = copy.deepcopy(cfg)
    c2["fill_delay_bars"] = delay_bars
    late = engine.backtest(conn, pair, c2, start_iso, end_iso, tf_seconds)
    return {
        "delay_bars": delay_bars,
        "base_net": base["net"], "delayed_net": late["net"],
        "base_trades": base["n_trades"], "delayed_trades": late["n_trades"],
        "improves_when_delayed": late["net"] > base["net"] * 1.10 and late["net"] > 0,
    }


# ---------------------------------------------------- trial-variance helper ---

MIN_TRADES_FOR_SR_VARIANCE = 10


def sharpe_variance_from_candidates(conn, pair, tf):
    """DSR needs the variance of SR across the trials that were run. Since
    P2.3 every candidate row carries its own IS Sharpe (is_sharpe, computed
    on its pinned IS window by the optimizer or `optimizer.py
    --backfill-sharpe`), so this uses ALL trials, not a re-run sample. The
    old sr_trials cache is no longer read or written.

    Trials with < MIN_TRADES_FOR_SR_VARIANCE trades are EXCLUDED from the
    variance: a 2-trade series can post SR = 7 and that number estimates
    nothing. Including them inflated the variance 500x on the real table
    (54.8 vs 0.105) and made the benchmark absurd rather than strict.
    n_trials is still the full count — every draw was a chance to get lucky.

    Returns (variance, n_used, n_total, n_missing). n_missing counts rows
    with no is_sharpe (not backfilled, or not reproducible on their windows);
    they are left out of the variance and should be reported, not hidden."""
    rows = conn.execute(
        "SELECT is_sharpe, is_trades FROM candidates WHERE pair=? AND tf=?",
        (pair, tf)).fetchall()
    n_total = len(rows)
    n_missing = sum(1 for sr, _ in rows if sr is None)
    srs = [sr for sr, n in rows
           if sr is not None and n is not None and n >= MIN_TRADES_FOR_SR_VARIANCE]
    var = _std(srs) ** 2 if len(srs) > 1 else 0.0
    return var, len(srs), n_total, n_missing
