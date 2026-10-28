"""
The interface every strategy family implements (P5.2).

A family owns its SIGNAL logic only: which bars are long or short entries.
Everything that turns a signal into a trade — next-open fill, slippage,
commission, risk sizing, ATR stop / R:R target / trailing, reverse on
opposite, fill delay, feature filters — is engine.py's and is the same for
every family.

The execution keys engine.backtest reads from cfg (so a family's
default_cfg() must supply the ones it uses):
  atr_len stop_atr rr_ratio risk_pct use_risk_sizing initial_capital
  commission_pct slippage_ticks tick_size   and optionally
  use_trailing trail_atr (default off) fill_delay_bars history_start_ts filters
"""

import copy

# trade-logic-neutral execution assumptions: never part of DNA
NON_DNA_KEYS = {"initial_capital", "commission_pct", "slippage_ticks", "tick_size",
                "fill_delay_bars", "history_start_ts"}


class Strategy:
    name = None
    # keys of this family's cfg that change which trades are taken (DNA)
    dna_keys = ()
    # A4 plateau / validation.param_sensitivity nudges these; INT ones round
    sensitivity_params = ()
    int_params = frozenset()
    # analyze.py section 1 (survivor parameter structure)
    numeric_params = ()
    toggle_params = ()
    # ablation.py: toggles flipped one at a time, the ones pruning may switch
    # off (a direction such as use_shorts is not a filter), and trap
    # thresholds -> (value that never binds, gating toggle or None, what it guards)
    toggles = ()
    prunable_toggles = ()
    traps = {}

    def default_cfg(self):
        raise NotImplementedError

    def sample_cfg(self, rng):
        """One random draw for the optimizer from this family's ranges."""
        raise NotImplementedError

    def for_timeframe(self, cfg, tf_seconds):
        """Adjust a cfg for a non-native timeframe (V4.1: daily HTF at 4h)."""
        return cfg

    def cfg_ok(self, cfg):
        """False for a cfg that makes no sense (e.g. EMA lengths out of order);
        sensitivity nudges that produce one are skipped."""
        return True

    def signals(self, bars, cfg):
        """bars: dict of equal-length lists ts open high low close volume, plus
        atr (engine's ATR(atr_len), the one stops are sized from) and from_i,
        the first bar the engine will ask about. Returns a list, one
        (long, short) bool pair per bar; bars before from_i may be
        (False, False) unevaluated. A signal on bar i uses bars <= i only."""
        raise NotImplementedError

    def pine_template(self):
        """Pine v6 source as a str.format template: {cfg_key} for every cfg
        value (bools arrive as true/false), {title} {generated} {source_note},
        the extras from pine_fields(), and {{ }} for literal braces."""
        raise NotImplementedError

    def pine_fields(self, cfg):
        return {}

    def copy_cfg(self, cfg):
        return copy.deepcopy(cfg)
