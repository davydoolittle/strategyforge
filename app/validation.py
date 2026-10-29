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
  param_sensitivity()    perturb each numeric param +/-pct (several steps), PF stability
  cost_stress()          re-run with 2x and 3x commission+slippage
  lookahead_shift()      delay every signal by +1 bar; if results IMPROVE,
                         something is leaking
  bucket_returns()       a trade list as a calendar return series, so trials
                         can be correlated with each other
  effective_trials()     cluster trials by return correlation; N_eff =
                         number of clusters (information only, see A2)

Sharpe note: ratios are per-trade. Annualizing a 20-trade series is
meaningless and invites comparison with numbers it should not be compared to.
DSR / PSR are scale-free so per-trade is exactly what they want.

References
  Bailey & López de Prado (2014), "The Deflated Sharpe Ratio"
  López de Prado (2018), Advances in Financial ML, ch. 7 (purged/embargoed CV)
  López de Prado (2020), Machine Learning for Asset Managers, ch. 8 (effective
    number of trials by clustering, ONC); López de Prado & Lewis (2019),
    "Detection of false investment strategies using unsupervised learning"
"""

import copy
import json
import math
import operator
import random
from datetime import datetime, timezone

import engine
import strategies

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
                 embargo_bars=24, end_ts=None, start_ts=None):
    """Run cfg on every embargoed test window. cfg is FIXED (this validates a
    parameter set, it does not re-optimize per fold — that is the optimizer's
    job and would need nested CV). start_ts / end_ts bound the history (pass
    the candidate's pinned history start and oos_end so folds don't move as
    candles are added at either end). Returns per-fold stats + summary."""
    lo, hi = conn.execute(
        "SELECT MIN(ts), MAX(ts) FROM candles WHERE pair=? AND ts<=? AND ts>=?",
        (pair, end_ts if end_ts is not None else 2 ** 62,
         start_ts if start_ts is not None else 0)).fetchone()
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

def param_sensitivity(conn, pair, cfg, tf_seconds, start_iso, end_iso, pct=(0.10,)):
    """For each numeric param, nudge it +/-pct (one or several step sizes) and
    re-run. A robust parameter set lives on a plateau: neighbours keep PF > 1.
    A fragile one lives on a spike: neighbours collapse. Returns per-param
    results, plateau score = fraction of neighbours that stay profitable, and
    worst_pf = the lowest neighbour PF (0 if a neighbour takes no trades).
    A nudge that lands on a value already tried for that param is skipped.
    Which params, which are integers and which nudges are nonsense (e.g. EMA
    lengths out of order) is the cfg's family's call (sensitivity_params,
    int_params, cfg_ok)."""
    fam = strategies.for_cfg(cfg)
    pcts = (pct,) if isinstance(pct, (int, float)) else tuple(pct)
    base = engine.backtest(conn, pair, cfg, start_iso, end_iso, tf_seconds)
    base_pf = base["pf"] if base["pf"] != float("inf") else 99.0
    rows = []
    ok = 0
    total = 0
    for p in fam.sensitivity_params:
        if p not in cfg:
            continue
        seen = {cfg[p]}
        for step in pcts:
            for sign in (-1, +1):
                c2 = copy.deepcopy(cfg)
                v = cfg[p]
                nv = v * (1 + sign * step)
                if p in fam.int_params:
                    nv = max(2, int(round(nv)))
                    if nv == v:
                        nv = v + sign
                if nv in seen:
                    continue
                seen.add(nv)
                c2[p] = nv
                if not fam.cfg_ok(c2):
                    continue
                r = engine.backtest(conn, pair, c2, start_iso, end_iso, tf_seconds)
                pf = r["pf"] if r["pf"] != float("inf") else 99.0
                rows.append({"param": p, "pct": sign * step, "from": v, "to": nv, "pf": pf,
                             "n_trades": r["n_trades"], "net": r["net"]})
                total += 1
                ok += 1 if (r["net"] > 0 and r["n_trades"] > 0) else 0
    return {
        "base_pf": base_pf, "base_net": base["net"], "base_trades": base["n_trades"],
        "neighbours": rows, "plateau_score": ok / total if total else 0.0,
        "worst_pf": min((r["pf"] for r in rows), default=None),
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
# Per-trade return std below this many risk units (risk_pct of equity) means
# every trade ended the same way, e.g. 10 identical 1% stop-outs: SR = mean/std
# then measures cost noise, not the strategy (FINDINGS F11: #5518 SR -19.4,
# std 0.06 risk units; #6166 SR -32.2, 0.035). On the live table every other
# trial with |SR| >= 0.5 is at >= 0.41, so 0.1 sits an order of magnitude
# from both sides. The std is backed out of SR, which only works away from
# SR = 0, and near-identical returns only mislead when their mean is not ~0,
# i.e. when |SR| is large; so the test applies from |SR| >= 1.
MIN_RETURN_STD_RISK_UNITS = 0.1
DEGENERATE_MIN_ABS_SR = 1.0


def per_trade_return_std(sr, n_trades, net, initial_capital):
    """Std of a trial's per-trade returns, backed out of its stored SR =
    mean/std, with the mean taken as the geometric per-trade return of its
    IS net. That mean is off from the arithmetic one by about std^2/2, so the
    estimate is tight exactly where it is used (small std, large |SR|) and
    meaningless near SR = 0. None when it cannot be computed."""
    if not sr or not n_trades or net is None or not initial_capital:
        return None
    growth = 1 + net / initial_capital
    if growth <= 0:
        return None
    return abs((growth ** (1 / n_trades) - 1) / sr)


def is_degenerate_trial(sr, n_trades, net, params_json):
    """True when a trial's per-trade returns barely vary (see
    MIN_RETURN_STD_RISK_UNITS). Only risk-sized trials have a risk unit;
    any other, or one missing is_net, is never called degenerate."""
    cfg = json.loads(params_json or "{}")
    if abs(sr or 0.0) < DEGENERATE_MIN_ABS_SR:
        return False
    if not cfg.get("use_risk_sizing") or not cfg.get("risk_pct"):
        return False
    sd = per_trade_return_std(sr, n_trades, net, cfg.get("initial_capital"))
    return sd is not None and sd < MIN_RETURN_STD_RISK_UNITS * cfg["risk_pct"] / 100


def _variance_rows(conn, pair, tf, family):
    return conn.execute(
        "SELECT id, is_sharpe, is_trades, is_net, params_json FROM candidates"
        " WHERE pair=? AND tf=? AND family=?", (pair, tf, family)).fetchall()


def degenerate_trials(conn, pair, tf, family=strategies.DEFAULT_FAMILY):
    """Ids of the trials sharpe_variance_from_candidates leaves out as
    degenerate, so callers can report them."""
    return [cid for cid, sr, n, net, pj in _variance_rows(conn, pair, tf, family)
            if sr is not None and n is not None and n >= MIN_TRADES_FOR_SR_VARIANCE
            and is_degenerate_trial(sr, n, net, pj)]


def sharpe_variance_from_candidates(conn, pair, tf, family=strategies.DEFAULT_FAMILY):
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

    Trials whose per-trade returns barely vary are EXCLUDED too
    (is_degenerate_trial). The trade floor does not catch them: one 10-trade
    series of identical stop-outs (SR -19.4) inflated ETH squeeze's variance
    56x and its E[max SR] from 0.47 to 3.49 (FINDINGS F11). Like the trade
    floor, this only removes numbers that estimate nothing; they still count
    in n_trials.

    Only trials of the given strategy family count (P5.2): each family is
    its own search over its own parameter space, so a second family's 300
    trials neither inherit V4.1's 900 nor raise V4.1's bar.

    Returns (variance, n_used, n_total, n_missing). n_missing counts rows
    with no is_sharpe (not backfilled, or not reproducible on their windows);
    they are left out of the variance and should be reported, not hidden."""
    rows = _variance_rows(conn, pair, tf, family)
    n_total = len(rows)
    n_missing = sum(1 for _, sr, _, _, _ in rows if sr is None)
    srs = [sr for _, sr, n, net, pj in rows
           if sr is not None and n is not None and n >= MIN_TRADES_FOR_SR_VARIANCE
           and not is_degenerate_trial(sr, n, net, pj)]
    var = _std(srs) ** 2 if len(srs) > 1 else 0.0
    return var, len(srs), n_total, n_missing


# ------------------------------------------- effective number of trials ---
#
# expected_max_sharpe() assumes the N trials are INDEPENDENT. 900 draws of one
# strategy family on one price history are not: two parameter sets that
# differ by one EMA length take mostly the same trades. López de Prado (MLAM
# ch. 8) estimates the effective number of trials by clustering the trials'
# return series on correlation distance and counting the clusters.
#
# Deviations from the book, both forced by stdlib-only:
#   - clustering is average-linkage hierarchical (NN-chain, O(n^2)) rather
#     than repeated k-means; the number of clusters is still chosen by the
#     ONC criterion, the t-stat of the silhouette scores, mean(s) / std(s).
#   - the variance fed to E[max SR] stays the across-trial SR variance; the
#     book replaces it with the variance of cluster-level SRs.
# Trials whose series is flat (no trades in the window) are identical to each
# other and form one cluster of their own.

DAY = 86400


def bucket_returns(trades, initial_capital, start_ts, end_ts, bucket_secs=DAY):
    """Per-bucket (default: per UTC day) return series over [start_ts, end_ts].
    Each trade's return (trade_returns) is spread over the buckets it was open
    in, pro rata by time, so two trials holding the same position on the same
    days correlate even if their exits differ by a few hours."""
    rets = trade_returns(trades, initial_capital)
    b0 = start_ts // bucket_secs
    n = end_ts // bucket_secs - b0 + 1
    out = [0.0] * n
    for t, r in zip(trades, rets):
        a = t["entry_ts"]
        b = max(t["exit_ts"], a + 1)
        x = a
        while x < b:
            k = x // bucket_secs
            nxt = min(b, (k + 1) * bucket_secs)
            i = k - b0
            if 0 <= i < n:
                out[i] += r * (nxt - x) / (b - a)
            x = nxt
    return out


def rebucket(series, factor):
    """Sum consecutive groups of `factor` buckets (days -> weeks: factor=7)."""
    return [sum(series[i:i + factor]) for i in range(0, len(series), factor)]


def _zscores(x):
    n = len(x)
    m = sum(x) / n
    sd = math.sqrt(sum((v - m) ** 2 for v in x) / n)
    if sd < 1e-15:
        return None
    return [(v - m) / sd for v in x]


def correlation_matrix(series):
    """Pearson correlation of equal-length series. Rows for flat series are
    None (correlation undefined)."""
    zs = [_zscores(s) for s in series]
    n = len(series)
    length = len(series[0]) if series else 0
    corr = [[None] * n for _ in range(n)]
    for i in range(n):
        if zs[i] is None:
            continue
        corr[i][i] = 1.0
        for j in range(i + 1, n):
            if zs[j] is None:
                continue
            c = sum(map(operator.mul, zs[i], zs[j])) / length
            corr[i][j] = corr[j][i] = max(-1.0, min(1.0, c))
    return corr


def corr_distance(rho):
    """MLAM correlation distance sqrt((1 - rho) / 2): 0 identical, 1 opposite."""
    return math.sqrt(max(0.0, (1.0 - rho) / 2.0))


def average_linkage(dist):
    """Average-linkage agglomerative clustering by the nearest-neighbour-chain
    algorithm. dist: symmetric n x n list of lists. Returns the n-1 merges as
    (height, i, j) sorted by height; after a merge, slot i stands for i u j."""
    n = len(dist)
    d = [row[:] for row in dist]
    size = [1] * n
    active = set(range(n))
    merges = []
    chain = []
    seq = 0
    while len(active) > 1:
        if not chain:
            chain.append(min(active))
        a = chain[-1]
        prev = chain[-2] if len(chain) > 1 else None
        best = prev
        bd = d[a][prev] if prev is not None else float("inf")
        row = d[a]
        for c in active:
            if c != a and row[c] < bd:
                best, bd = c, row[c]
        if best == prev:
            chain.pop()
            chain.pop()
            i, j = min(a, prev), max(a, prev)
            merges.append((bd, seq, i, j))
            seq += 1
            ni, nj = size[i], size[j]
            for c in active:
                if c != i and c != j:
                    v = (ni * d[i][c] + nj * d[j][c]) / (ni + nj)
                    d[i][c] = d[c][i] = v
            size[i] = ni + nj
            active.remove(j)
        else:
            chain.append(best)
    merges.sort()
    return [(h, i, j) for h, _, i, j in merges]


def cut(merges, n, k):
    """Cluster labels (0..k-1) after applying the n-k lowest merges."""
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for _, i, j in merges[:max(0, n - k)]:
        parent[find(j)] = find(i)
    roots = {}
    return [roots.setdefault(find(x), len(roots)) for x in range(n)]


def silhouettes(dist, labels):
    """Silhouette score per observation (0 for members of singleton clusters)."""
    k = max(labels) + 1
    counts = [0] * k
    for lab in labels:
        counts[lab] += 1
    out = []
    for i, row in enumerate(dist):
        sums = [0.0] * k
        for j, dj in enumerate(row):
            sums[labels[j]] += dj
        own = labels[i]
        if counts[own] <= 1:
            out.append(0.0)
            continue
        a = sums[own] / (counts[own] - 1)
        b = min(sums[c] / counts[c] for c in range(k) if c != own and counts[c])
        out.append((b - a) / max(a, b) if max(a, b) > 0 else 0.0)
    return out


def _k_grid(n):
    ks = set(range(2, min(n, 41)))
    k = 40.0
    while k < n - 1:
        k *= 1.15
        ks.add(min(n - 1, int(round(k))))
    return sorted(x for x in ks if 2 <= x <= n - 1)


def effective_trials(series):
    """Effective number of independent trials among `series` (one return
    series per trial, all on the same calendar). Returns a dict:
      n_eff          clusters chosen by the ONC criterion, + 1 if any flat series
      n              trials
      n_flat         trials with a flat series (all in one cluster)
      n_distinct     non-flat trials left after merging exact duplicates
      quality        mean(silhouette) / std(silhouette) at the chosen k
      labels         cluster label per trial (flat trials get label -1)
      curve          [(k, quality)] over the k grid tried
      mean_abs_corr  average |rho| over all pairs of non-flat trials"""
    n = len(series)
    corr = correlation_matrix(series)
    live = [i for i in range(n) if corr[i][i] is not None]
    n_flat = n - len(live)
    labels = [-1] * n
    result = {"n": n, "n_flat": n_flat, "n_distinct": 0, "quality": None, "curve": [],
              "mean_abs_corr": None}
    if len(live) > 1:
        pairs = [abs(corr[a][b]) for x, a in enumerate(live) for b in live[x + 1:]]
        result["mean_abs_corr"] = sum(pairs) / len(pairs)
    # exact duplicates (two parameter sets that take identical trades) are one
    # trial; collapsing them first also lets N_eff be 1, which silhouette
    # scores cannot express
    reps, rep_of = [], {}
    for i in live:
        r = next((r for r in reps if corr[i][r] >= 1.0 - 1e-9), None)
        if r is None:
            reps.append(i)
            r = i
        rep_of[i] = r
    result["n_distinct"] = len(reps)
    if len(reps) <= 2:
        lab = list(range(len(reps)))
        q = None
    else:
        dist = [[corr_distance(corr[i][j]) for j in reps] for i in reps]
        merges = average_linkage(dist)
        best = None
        for k in _k_grid(len(reps)):
            lab_k = cut(merges, len(reps), k)
            s = silhouettes(dist, lab_k)
            sd = _std(s, ddof=0)
            qk = _mean(s) / sd if sd > 0 else 0.0
            result["curve"].append((k, qk))
            if best is None or qk > best[1]:
                best = (k, qk, lab_k)
        _, q, lab = best
    rep_label = dict(zip(reps, lab))
    for i in live:
        labels[i] = rep_label[rep_of[i]]
    k = len(set(lab))
    result.update(n_eff=k + (1 if n_flat else 0), quality=q, labels=labels)
    return result


def breakeven_trials(sr, n_obs, skew, kurt, sr_variance, min_dsr=0.95, cap=10 ** 6):
    """Largest number of independent trials N for which DSR >= min_dsr, i.e.
    how small the search would have had to be for this SR to pass. 0 if it
    fails even as a single trial (PSR vs 0 < min_dsr)."""
    if deflated_sharpe(sr, n_obs, skew, kurt, 1, sr_variance)[0] < min_dsr:
        return 0
    lo, hi = 1, 2
    while hi < cap and deflated_sharpe(sr, n_obs, skew, kurt, hi, sr_variance)[0] >= min_dsr:
        lo, hi = hi, hi * 2
    if hi >= cap:
        return cap
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if deflated_sharpe(sr, n_obs, skew, kurt, mid, sr_variance)[0] >= min_dsr:
            lo = mid
        else:
            hi = mid
    return lo
