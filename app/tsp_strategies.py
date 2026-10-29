"""
TSP allocation strategies: what to hold, decided after each close.

A separate registry from strategies/: those families emit long/short entry
signals for engine.backtest on one instrument; these choose whole-percent
allocations across funds under the IFT rules, and tsp_backtest runs them.

Interface (all reads go through the View, so no strategy can see past day t):
  initial(view, sig)   allocation on the first day (initial funding, not an IFT)
  decide(view, ctx)    None, or {"target", "reason", "confidence", "evidence"}
ctx: weights (pct, drifted, at t's close), held (the last target set),
  moves_left / days_left (for the month of the fill date), fill_date,
  signal (tsp_signals.compute()[t], or None when not computed).
"""

import tsp_rules as R
import tsp_signals as S

REGISTRY = {}


def register(cls):
    REGISTRY[cls.type] = cls
    return cls


def build(name, cfg, overrides=None):
    spec = dict(cfg["strategies"][name])
    spec.update(overrides or {})
    return REGISTRY[spec["type"]](name, spec, cfg)


def library(cfg, data_mode):
    """Every configured strategy that can run on this data."""
    return [n for n, s in cfg["strategies"].items()
            if not (s.get("tsp_data_only") and data_mode != "tsp")]


def differs(a, b):
    return any(abs(a.get(f, 0) - b.get(f, 0)) >= 1 for f in set(a) | set(b))


def new_month(view):
    return view.t == 0 or view.at_date(view.t - 1).month != view.date.month


class AllocStrategy:
    type = None
    uses_signals = False

    def __init__(self, name, params, cfg):
        self.name, self.p, self.cfg = name, params, cfg

    def initial(self, view, sig):
        return dict(self.cfg["base_allocation"])

    def decide(self, view, ctx):
        return None

    def feasible(self, target, ctx):
        """target; or, with the budget spent, its G-only version if the target
        cuts total C/S/I exposure (the escape hatch is for de-risking only);
        None if nothing allowed is a change."""
        if ctx["moves_left"] <= 0 and not R.is_g_only_move(ctx["weights"], target):
            risk = self.cfg["risk_funds"]
            if sum(target.get(f, 0) for f in risk) >= sum(ctx["held"].get(f, 0) for f in risk):
                return None
            target = R.g_only_version(ctx["weights"], target, ctx["held"])
            if target is None:
                return None
        return target if differs(target, ctx["held"]) else None


@register
class BuyHold(AllocStrategy):
    type = "buy_hold"

    def initial(self, view, sig):
        return R.validate_allocation(self.p["allocation"])


@register
class DualMomentum(AllocStrategy):
    """First business day of each month: the C/S/I fund with the best
    lookback return, if it beat G over the lookback; else the best of the
    safe funds (F, then G)."""
    type = "dual_momentum"

    def pick(self, view):
        lb = self.p["lookback_days"]
        rets = {f: S._ret(view, f, lb) for f in self.cfg["risk_funds"] + list(self.p["safe"])}
        if any(rets[f] is None for f in rets):
            return None, rets
        best = max(self.cfg["risk_funds"], key=lambda f: rets[f])
        if rets[best] > rets["G"]:
            return best, rets
        return max(self.p["safe"], key=lambda f: rets[f]), rets

    def initial(self, view, sig):
        f, _ = self.pick(view)
        return {f or "G": 100}

    def decide(self, view, ctx):
        if not new_month(view):
            return None
        f, rets = self.pick(view)
        if f is None:
            return None
        target = self.feasible({f: 100}, ctx)
        if not target:
            return None
        return {"target": target, "confidence": None,
                "reason": f"monthly check: {f} has the best {self.p['lookback_days']}-day return ("
                          + ", ".join(f"{k} {100 * v:+.1f}%" for k, v in rets.items()) + ")"}


@register
class TrendFilter(AllocStrategy):
    """Hold the fund while it is above its MA (by band_pct), else G.
    check: 'monthly' (first business day) or 'daily'. Exits to G are always
    allowed; re-entries need a flexible move."""
    type = "trend_filter"

    def state(self, view):
        ma = S._ma(view, self.p["fund"], self.p["ma_days"])
        px = view.value(self.p["fund"])
        if not ma or not px:
            return None, None
        band = self.p["band_pct"] / 100
        return ("in" if px > ma * (1 + band) else "out" if px < ma * (1 - band) else None,
                100 * (px / ma - 1))

    def initial(self, view, sig):
        s, _ = self.state(view)
        return {self.p["fund"]: 100} if s == "in" else {"G": 100}

    def decide(self, view, ctx):
        if self.p["check"] == "monthly" and not new_month(view):
            return None
        s, dist = self.state(view)
        if s is None:
            return None
        target = self.feasible({self.p["fund"]: 100} if s == "in" else {"G": 100}, ctx)
        if not target:
            return None
        return {"target": target, "confidence": None,
                "reason": f"{self.p['fund']} {dist:+.1f}% vs MA{self.p['ma_days']} "
                          f"(band {self.p['band_pct']:g}%): {'risk on' if s == 'in' else 'to G'}"}


@register
class RegimeSwitch(AllocStrategy):
    """Hold the configured allocation of the regime once it has held
    confirm_days in a row."""
    type = "regime_switch"
    uses_signals = True

    def initial(self, view, sig):
        return dict(self.p["allocations"].get(sig["regime"] or "neutral"))

    def decide(self, view, ctx):
        hist = ctx["regime_hist"][-self.p["confirm_days"]:]
        reg = hist[-1] if hist else None
        if not reg or len(hist) < self.p["confirm_days"] or any(r != reg for r in hist):
            return None
        target = self.feasible(dict(self.p["allocations"][reg]), ctx)
        if not target:
            return None
        drivers = ", ".join(f"{d[0]} {d[1]} ({d[2]:+d})" for d in ctx["signal"]["drivers"] if d[2])
        return {"target": target, "confidence": None,
                "reason": f"regime {reg} for {self.p['confirm_days']} days (score "
                          f"{ctx['signal']['score']:+d}: {drivers})"}


@register
class ShiftCapture(AllocStrategy):
    """Hold the base allocation; spend a move only on a shift flag whose
    confidence is high enough and whose move-budget score is positive."""
    type = "shift_capture"
    uses_signals = True

    def decide(self, view, ctx):
        sig = ctx["signal"]
        best = None
        for fl in sorted(sig["flags"], key=lambda f: -f["confidence"]):
            if fl["confidence"] < self.p["min_confidence"]:
                continue
            target = self.feasible(dict(fl["target"]), ctx)
            if not target:
                continue
            g_only = R.is_g_only_move(ctx["weights"], target)
            sc = S.score_move(fl, ctx["moves_left"], ctx["days_left"], g_only, sig["opportunity"])
            if sc["score"] <= self.cfg["budget_optimizer"]["min_score"]:
                continue
            if best is None or sc["score"] > best[1]["score"]:
                best = (fl, sc, target)
        if best is None:
            return None
        fl, sc, target = best
        return {"target": target, "confidence": fl["confidence"],
                "reason": f"{fl['type']}: {fl['why']}",
                "evidence": f"hit rate {fl['hits']}/{fl['n']} earlier flags, mean edge "
                            f"{fl['mean_edge_pct']:+.2f}% over {self.cfg['shifts']['eval_horizon_days']}d; "
                            f"score {sc['score']:+.2f} = edge {sc['edge']:+.2f} - reserve "
                            f"{sc['reserve_cost']:.2f} ({sc['why']})"}
