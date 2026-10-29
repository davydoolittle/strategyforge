"""
TSP signals: daily regime, major-shift flags, their track record, and the
move-budget score.

One forward pass over the dataset (compute()). Day t is computed from
ds.view(t) only, so it cannot see later data, and a flag's hit rate at day
t counts only earlier flags whose evaluation window had CLOSED by day t.
signals[t] is therefore exactly what a person would have seen after day t's
close. tests/test_tsp.py checks that by truncating the data.

Regime (explainable): each input adds or removes points and says why.
  risk_on  score >= risk_on_min_score
  neutral  in between
  risk_off score <= risk_off_max_score
  crisis   score <= crisis_max_score, or VIX >= vix_crisis with a drawdown
           >= drawdown_crisis

Flags (each with direction, a target allocation and a counterfactual):
  regime_change_up/down  a new regime held regime_persist_days in a row
                      (up and down are scored separately: opposite bets)
  vix_spike_revert    VIX spiked >= vix_spike and has fallen back below
                      vix_revert_frac of the spike's peak
  ma200_cross_up/down trend fund crossed its 200-day MA by ma200_band_pct
  fast_drawdown       trend fund fell >= fast_dd_pct in fast_dd_days
  leadership_change   the C/S/I leader by leadership_lookback momentum
                      changed, by >= leadership_margin_pct

Evaluation: a flag on day t is acted on at the fill close t+lag. It is a
hit if its target beat the counterfactual over the next eval_horizon_days.
Edge = target return - counterfactual return, in percent.
"""

import math

import validation as V

REGIMES = ("crisis", "risk_off", "neutral", "risk_on")
RISK_OFF_TYPES = {"regime_change_down", "ma200_cross_down", "fast_drawdown"}
RANK = {r: i for i, r in enumerate(REGIMES)}


# ------------------------------------------------------------------ helpers ---

def _sma(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _ret(view, name, days):
    a, b = view.value(name, days), view.value(name)
    return None if a is None or b is None or a <= 0 else b / a - 1


def _realized_vol(view, name, days):
    px = [p for p in view.tail(name, days + 1) if p is not None]
    if len(px) < days + 1:
        return None
    r = [b / a - 1 for a, b in zip(px, px[1:])]
    return V._std(r) * math.sqrt(252) * 100


def _ma(view, name, days):
    xs = view.tail(name, days)
    if len(xs) < days or any(x is None for x in xs):
        return None
    return sum(xs) / days


# ------------------------------------------------------------------- regime ---

def regime_at(view, rc):
    """(regime, score, drivers). drivers: list of (input, reading, points, why)."""
    f = rc["trend_fund"]
    px = view.value(f)
    drivers = []

    def add(inp, reading, pts, why):
        drivers.append((inp, reading, pts, why))

    ma_f, ma_s = _ma(view, f, rc["ma_fast"]), _ma(view, f, rc["ma_slow"])
    if px is None or ma_s is None:
        return None, 0, [("trend", "warming up", 0, f"needs {rc['ma_slow']} days of {f}")]
    if px > ma_s and px > ma_f:
        add("trend", f"{f} {100 * (px / ma_s - 1):+.1f}% vs MA{rc['ma_slow']}", 2,
            f"above MA{rc['ma_fast']} and MA{rc['ma_slow']}")
    elif px > ma_s:
        add("trend", f"{f} {100 * (px / ma_s - 1):+.1f}% vs MA{rc['ma_slow']}", 1,
            f"above MA{rc['ma_slow']}, below MA{rc['ma_fast']}")
    elif ma_f < ma_s:
        add("trend", f"{f} {100 * (px / ma_s - 1):+.1f}% vs MA{rc['ma_slow']}", -2,
            f"below MA{rc['ma_slow']}, MA{rc['ma_fast']} under MA{rc['ma_slow']}")
    else:
        add("trend", f"{f} {100 * (px / ma_s - 1):+.1f}% vs MA{rc['ma_slow']}", -1,
            f"below MA{rc['ma_slow']}")

    vix = view.value("vix") if view.has("vix") else None
    if vix is not None:
        if vix >= rc["vix_crisis"]:
            add("vix", f"{vix:.1f}", -3, f">= {rc['vix_crisis']:g} (crisis level)")
        elif vix >= rc["vix_high"]:
            add("vix", f"{vix:.1f}", -1, f">= {rc['vix_high']:g}")
        elif vix < 15:
            add("vix", f"{vix:.1f}", 1, "< 15 (calm)")
        else:
            add("vix", f"{vix:.1f}", 0, "normal")
    v3 = view.value("vix3m") if view.has("vix3m") else None
    if vix is not None and v3:
        term = vix / v3
        if term > rc["vix_term_stress"]:
            add("vix_term", f"VIX/VIX3M {term:.2f}", -2, "backwardation: near-term fear above 3-month")
        elif term < 0.9:
            add("vix_term", f"VIX/VIX3M {term:.2f}", 1, "steep contango (calm)")
        else:
            add("vix_term", f"VIX/VIX3M {term:.2f}", 0, "flat")

    rv = _realized_vol(view, f, rc["realized_vol_days"])
    if rv is not None:
        if rv >= rc["realized_vol_extreme"]:
            add("realized_vol", f"{rv:.0f}%", -2, f">= {rc['realized_vol_extreme']:g}% annualised")
        elif rv >= rc["realized_vol_high"]:
            add("realized_vol", f"{rv:.0f}%", -1, f">= {rc['realized_vol_high']:g}% annualised")
        elif rv < 0.6 * rc["realized_vol_high"]:
            add("realized_vol", f"{rv:.0f}%", 1, "low")
        else:
            add("realized_vol", f"{rv:.0f}%", 0, "normal")

    hist = [p for p in view.tail(f, rc["drawdown_lookback"]) if p is not None]
    dd = 100 * (1 - px / max(hist)) if hist else 0.0
    if dd >= rc["drawdown_crisis"]:
        add("drawdown", f"-{dd:.1f}%", -3, f">= {rc['drawdown_crisis']:g}% off the 1-year high")
    elif dd >= rc["drawdown_warn"]:
        add("drawdown", f"-{dd:.1f}%", -1, f">= {rc['drawdown_warn']:g}% off the 1-year high")
    elif dd < 3:
        add("drawdown", f"-{dd:.1f}%", 1, "near the 1-year high")
    else:
        add("drawdown", f"-{dd:.1f}%", 0, "shallow")

    above = []
    for g in ("C", "S", "I"):
        m = _ma(view, g, rc["ma_slow"]) if view.has(g) else None
        p = view.value(g) if view.has(g) else None
        if m and p:
            above.append(g if p > m else None)
    if above:
        n_up = sum(1 for a in above if a)
        pts = 1 if n_up == len(above) else -1 if n_up == 0 else 0
        add("breadth", f"{n_up}/{len(above)} of C/S/I above MA{rc['ma_slow']}", pts,
            "all up" if pts > 0 else "all down" if pts < 0 else "mixed")

    if view.has("hy_oas") and view.value("hy_oas") is not None:
        o = view.value("hy_oas")
        add("credit", f"HY OAS {o:.2f}%", -2 if o >= rc["hy_oas_stress"] else 0,
            f">= {rc['hy_oas_stress']:g}% (stress)" if o >= rc["hy_oas_stress"] else "normal")
    elif view.has("hyg") and view.has("lqd"):
        ratio = [h / l if h and l else None for h, l in
                 zip(view.tail("hyg", rc["credit_ma"]), view.tail("lqd", rc["credit_ma"]))]
        if len(ratio) == rc["credit_ma"] and all(ratio):
            dev = 100 * (ratio[-1] / (sum(ratio) / len(ratio)) - 1)
            if dev <= -rc["credit_stress_pct"]:
                add("credit", f"HYG/LQD {dev:+.1f}% vs MA{rc['credit_ma']}", -2, "junk lagging quality (stress)")
            elif dev > 0:
                add("credit", f"HYG/LQD {dev:+.1f}% vs MA{rc['credit_ma']}", 1, "junk leading quality")
            else:
                add("credit", f"HYG/LQD {dev:+.1f}% vs MA{rc['credit_ma']}", 0, "mild")

    curve = None
    if view.has("t10y2y") and view.value("t10y2y") is not None:
        curve, label = view.value("t10y2y"), "2s10s"
    elif view.has("y10") and view.has("y3m") and view.value("y10") is not None and view.value("y3m") is not None:
        curve, label = view.value("y10") - view.value("y3m"), "10y-3m"
    if curve is not None:
        add("curve", f"{label} {curve:+.2f}", -1 if curve < 0 else 0,
            "inverted (slow recession warning)" if curve < 0 else "positive")

    score = sum(d[2] for d in drivers)
    if score <= rc["crisis_max_score"] or (vix is not None and vix >= rc["vix_crisis"]
                                           and dd >= rc["drawdown_crisis"]):
        reg = "crisis"
    elif score <= rc["risk_off_max_score"]:
        reg = "risk_off"
    elif score >= rc["risk_on_min_score"]:
        reg = "risk_on"
    else:
        reg = "neutral"
    return reg, score, drivers


# -------------------------------------------------------------------- flags ---

def _leader(view, lookback):
    moms = {}
    for g in ("C", "S", "I"):
        if view.has(g):
            r = _ret(view, g, lookback)
            if r is not None:
                moms[g] = 100 * r
    if len(moms) < 2:
        return None, None, moms
    ranked = sorted(moms, key=moms.get, reverse=True)
    return ranked[0], moms[ranked[0]] - moms[ranked[1]], moms


def tilt_to(base, fund, risk_funds):
    """Base allocation with its whole risk share moved into one fund."""
    risk = sum(p for f, p in base.items() if f in risk_funds)
    out = {f: p for f, p in base.items() if f not in risk_funds}
    out[fund] = out.get(fund, 0) + risk
    return out


class _FlagState:
    def __init__(self):
        self.reg_hist = []
        self.side = None            # trend fund above/below its MA200 band
        self.leader = None
        self.dd_armed = True
        self.vix_armed = False


def flags_at(view, state, regime, cfg):
    """Flags raised on day t. Each: dict(type, direction, target, counter, why)."""
    sc, rc = cfg["shifts"], cfg["regime"]
    base = cfg["base_allocation"]
    reg_alloc = cfg["strategies"]["regime_switch"]["allocations"]
    out = []
    f = rc["trend_fund"]

    # regime change, confirmed after regime_persist_days
    state.reg_hist.append(regime)
    k = sc["regime_persist_days"]
    h = state.reg_hist
    if regime and len(h) > k and all(r == regime for r in h[-k:]) and h[-k - 1] != regime \
            and h[-k - 1] is not None:
        prev = h[-k - 1]
        up = RANK[regime] > RANK[prev]
        out.append({"type": "regime_change_up" if up else "regime_change_down",
                    "direction": "risk_on" if up else "risk_off",
                    "target": dict(reg_alloc[regime]),
                    "counter": dict(reg_alloc[prev]),
                    "why": f"regime {prev} -> {regime}, held {k} days"})

    # VIX spike then mean reversion
    if view.has("vix"):
        win = [v for v in view.tail("vix", sc["vix_spike_window"]) if v is not None]
        vix, prev_vix = view.value("vix"), view.value("vix", 1)
        if win and vix is not None and prev_vix is not None:
            peak = max(win)
            lvl = sc["vix_revert_frac"] * peak
            if peak >= sc["vix_spike"] and vix <= lvl < prev_vix:
                out.append({"type": "vix_spike_revert", "direction": "risk_on",
                            "target": dict(base), "counter": {"G": 100},
                            "why": f"VIX peaked {peak:.1f} in {sc['vix_spike_window']} days, "
                                   f"now {vix:.1f} (< {sc['vix_revert_frac']:.0%} of peak)"})

    # 200-day MA cross with a band
    ma = _ma(view, f, rc["ma_slow"])
    px = view.value(f)
    if ma and px:
        band = sc["ma200_band_pct"] / 100
        side = "up" if px > ma * (1 + band) else "down" if px < ma * (1 - band) else state.side
        if state.side and side != state.side:
            if side == "up":
                out.append({"type": "ma200_cross_up", "direction": "risk_on", "target": dict(base),
                            "counter": {"G": 100},
                            "why": f"{f} {100 * (px / ma - 1):+.1f}% above MA{rc['ma_slow']}"})
            else:
                out.append({"type": "ma200_cross_down", "direction": "risk_off", "target": {"G": 100},
                            "counter": dict(base),
                            "why": f"{f} {100 * (px / ma - 1):+.1f}% below MA{rc['ma_slow']}"})
        state.side = side

    # fast drawdown (re-arms once the move is back above half the threshold)
    r = _ret(view, f, sc["fast_dd_days"])
    if r is not None:
        if state.dd_armed and r <= -sc["fast_dd_pct"] / 100:
            out.append({"type": "fast_drawdown", "direction": "risk_off", "target": {"G": 100},
                        "counter": dict(base),
                        "why": f"{f} {100 * r:+.1f}% in {sc['fast_dd_days']} days"})
            state.dd_armed = False
        elif r > -sc["fast_dd_pct"] / 200:
            state.dd_armed = True

    # leadership change among C/S/I
    lead, gap, moms = _leader(view, sc["leadership_lookback"])
    if lead and gap >= sc["leadership_margin_pct"] and lead != state.leader:
        if state.leader is not None:
            out.append({"type": "leadership_change", "direction": "rotate",
                        "target": tilt_to(base, lead, cfg["risk_funds"]),
                        "counter": tilt_to(base, state.leader, cfg["risk_funds"]),
                        "why": f"{lead} leads {state.leader} by {gap:.1f} pts over "
                               f"{sc['leadership_lookback']} days ("
                               + ", ".join(f"{g} {m:+.1f}%" for g, m in sorted(moms.items())) + ")"})
        state.leader = lead
    return out


# ------------------------------------------------------- evaluation and pass ---

def alloc_return(ds, alloc, a, b):
    """Buy-and-hold return of `alloc` (percents) from close a to close b."""
    tot = 0.0
    for f, p in alloc.items():
        s = ds.series(f) if ds.has(f) else None
        if s is None or s[a] is None or s[b] is None:
            return None
        tot += p / 100 * s[b] / s[a]
    return tot - 1


class FlagStats:
    """Expanding record per flag type: occurrences seen so far, and the
    resolved ones (evaluation window closed) with hit and edge."""

    def __init__(self):
        self.seen = {}              # type -> count of occurrences up to now
        self.resolved = {}          # type -> list of (hit, edge_pct)

    def summary(self, ftype, min_n):
        res = self.resolved.get(ftype, [])
        n = len(res)
        hits = sum(1 for h, _ in res if h)
        mean_edge = sum(e for _, e in res) / n if n else 0.0
        conf = (hits + 1) / (n + 2)
        return {"n": n, "hits": hits, "hit_rate": hits / n if n else None,
                "confidence": conf if n >= min_n else min(conf, 0.5),
                "mean_edge_pct": mean_edge, "enough_history": n >= min_n,
                "seen": self.seen.get(ftype, 0)}

    def copy(self):
        c = FlagStats()
        c.seen = dict(self.seen)
        c.resolved = {k: list(v) for k, v in self.resolved.items()}
        return c


def compute(ds, cfg, lag=None):
    """signals[t] = {date, regime, score, drivers, flags (each with stats),
    stats (FlagStats snapshot summary per type), opportunity}."""
    lag = cfg["rules"]["fill_lag_days"] if lag is None else lag
    sc = cfg["shifts"]
    H, min_n = sc["eval_horizon_days"], sc["min_history_events"]
    state, stats = _FlagState(), FlagStats()
    pending = []                    # (resolve_at, type, target, counter, a, b)
    out = []
    for t in range(len(ds)):
        view = ds.view(t)
        # resolve evaluations whose window closed today (uses closes <= t)
        still = []
        for ev in pending:
            if ev[0] <= t:
                _, ftype, target, counter, a, b = ev
                rt, rc_ = alloc_return(ds, target, a, b), alloc_return(ds, counter, a, b)
                if rt is not None and rc_ is not None:
                    stats.resolved.setdefault(ftype, []).append((rt > rc_, 100 * (rt - rc_)))
            else:
                still.append(ev)
        pending = still

        reg, score, drivers = regime_at(view, cfg["regime"])
        flags = flags_at(view, state, reg, cfg)
        for fl in flags:
            stats.seen[fl["type"]] = stats.seen.get(fl["type"], 0) + 1
            fl.update(stats.summary(fl["type"], min_n))
            a = t + lag
            pending.append((a + H, fl["type"], fl["target"], fl["counter"], a, a + H))
        out.append({"t": t, "date": ds.dates[t], "regime": reg, "score": score,
                    "drivers": drivers, "flags": flags,
                    "opportunity": opportunity(stats, t + 1, min_n)})
    return out


def opportunity(stats, days_seen, min_n):
    """How often a flag worth a flexible move has come up so far, and what it
    was worth: rate per business day and mean edge of flexible-move flag types
    (risk_on and rotate) with a positive track record."""
    good, n_events = [], 0
    for ftype, seen in stats.seen.items():
        if ftype in RISK_OFF_TYPES:
            continue            # de-risking can always go to G: never needs a flexible move
        s = stats.summary(ftype, min_n)
        if s["enough_history"] and s["mean_edge_pct"] > 0:
            good.append(s["mean_edge_pct"])
            n_events += seen
    rate = n_events / days_seen if days_seen else 0.0
    return {"rate_per_day": rate, "mean_edge_pct": sum(good) / len(good) if good else 0.0}


# --------------------------------------------------------- budget optimizer ---

def poisson_at_least(k, lam):
    if k <= 0:
        return 1.0
    p, term = 0.0, math.exp(-lam)
    for i in range(k):
        p += term
        term *= lam / (i + 1)
    return max(0.0, 1.0 - p)


def score_move(flag, moves_left, days_left, g_only, opp):
    """Expected edge of acting now minus the expected value of keeping the
    move. Returns dict(score, edge, reserve_cost, why).

    - edge: the flag type's mean historical edge (pct over the evaluation
      horizon), 0 while it has too little history.
    - reserve cost: spending a flexible move when k are left forfeits the
      chance to act on a k-th good flag later this month:
      mean edge of such flags x P(N >= k), N ~ Poisson(rate x days_left).
    - A move into G once the budget is spent costs nothing (the G escape
      hatch): it uses no flexible move."""
    edge = flag["mean_edge_pct"] if flag.get("enough_history") else 0.0
    if moves_left <= 0:
        cost = 0.0 if g_only else math.inf
        why = ("budget spent; a move into G is still allowed (escape hatch)" if g_only
               else "budget spent and this move is not into G only: not allowed")
    else:
        p = poisson_at_least(moves_left, opp["rate_per_day"] * days_left)
        cost = opp["mean_edge_pct"] * p
        why = (f"spending 1 of {moves_left} flexible move(s) gives up "
               f"{opp['mean_edge_pct']:.2f}% x P(>= {moves_left} more good flags in "
               f"{days_left} business days) {p:.0%}")
    return {"score": edge - cost, "edge": edge, "reserve_cost": cost, "why": why}
