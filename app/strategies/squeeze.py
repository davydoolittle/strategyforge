"""
squeeze — a volatility-compression breakout, built from FINDINGS F6, not from
V4.1 (P6.2).

F6 measured what comes before a big 24-hour move (>= 8 hourly ATRs) across
BTC, ETH, SOL and XRP since 2021: low volatility (bb_bw_pctile AUC 0.36,
Bonferroni-significant), and, for the direction, a start near the 30-day
extreme the move goes toward (up-moves nearer the 30-day high, down-moves
nearer the low). This family trades exactly that, with nothing else.

Entry, long (short is the mirror image), on the close of bar i:
  compressed  bb_bw_pctile <= compression_pctile, where bb_bw_pctile is the
              percentile of Bollinger(20, 2) bandwidth at bar i among the
              previous 90 days of bars, computed exactly as features.py's
              shadow feature (so a squeeze trade's trade_features row shows
              the value the entry used). Needs 90 days of history: no signal
              before that, whatever the threshold.
  breakout    close > highest high of the previous breakout_len bars
  near the high
              position in the 30-day range (bar i included, as features.py's
              dist_30d_high/low) = (close - low30) / (high30 - low30)
              >= extreme_bias. extreme_bias 0.5 = "nearer the high than the
              low"; the sampler draws 0.5..0.95. Shorts: <= 1 - extreme_bias.
Exit: fixed ATR(14) stop at stop_atr and a target at rr_ratio x the stop
distance (engine.py). Reverse on an opposite signal, as every family.

The percentile is taken at the signal bar, the breakout bar itself, because
that is what features.py computes and F6's AUC was measured on the window's
first bar. A breakout bar widens the bands, so a compressed reading at i
means the squeeze was still on as price broke out.

Searched (sample_cfg): compression_pctile breakout_len extreme_bias stop_atr
rr_ratio use_shorts = 6 parameters. Fixed: use_longs on, ATR 14, Bollinger
20/2, 90-day percentile window, 30-day range, 1% risk sizing.
"""

import bisect
import math

from strategies import register
from strategies.base import Strategy

import indicators as ind

BB_LEN = 20
PCTILE_DAYS = 90
RANGE_DAYS = 30
_CACHE = {}          # bb_bw_pctile per price history; it does not depend on cfg
_CACHE_MAX = 16


def _bar_seconds(ts):
    diffs = [b - a for a, b in zip(ts[:50], ts[1:51]) if b > a]
    return min(diffs) if diffs else 3600


def bb_bandwidth(c):
    """Bollinger(20, 2) bandwidth in %, as features.FeatureComputer computes it
    (sample std of the last 20 closes, same operation order, so equal floats)."""
    out = [None] * len(c)
    basis = ind.sma(c, BB_LEN)
    for i in range(BB_LEN - 1, len(c)):
        w = c[i - BB_LEN + 1:i + 1]
        m = sum(w) / len(w)
        sd = math.sqrt(sum((x - m) ** 2 for x in w) / (len(w) - 1))
        if basis[i]:
            out[i] = 4.0 * sd / basis[i] * 100.0
    return out


def bb_bw_pctile(c, bars_per_day):
    """features.py's bb_bw_pctile for every bar: the share (%) of the previous
    90 days' bandwidths that are <= bar i's. None before 90 days of bars.
    A sorted window is rolled forward instead of re-scanning 2,160 bars."""
    bw = bb_bandwidth(c)
    lb = PCTILE_DAYS * bars_per_day
    out = [None] * len(c)
    win = []
    for i in range(1, len(c)):
        if bw[i - 1] is not None:
            bisect.insort(win, bw[i - 1])
        k = i - lb - 1
        if k >= 0 and bw[k] is not None:
            del win[bisect.bisect_left(win, bw[k])]
        if i - lb >= 0 and bw[i] is not None and win:
            out[i] = 100.0 * bisect.bisect_right(win, bw[i]) / len(win)
    return out


def pctile_for(bars):
    ts, c = bars["ts"], bars["close"]
    key = (len(c), ts[0], ts[-1], c[0], c[-1])
    if key not in _CACHE:
        if len(_CACHE) >= _CACHE_MAX:
            _CACHE.pop(next(iter(_CACHE)))
        _CACHE[key] = bb_bw_pctile(c, 86400 // _bar_seconds(ts))
    return _CACHE[key]


@register
class Squeeze(Strategy):
    name = "squeeze"

    dna_keys = ("use_longs", "use_shorts", "compression_pctile", "breakout_len",
                "extreme_bias", "atr_len", "stop_atr", "rr_ratio", "risk_pct",
                "use_risk_sizing")
    sensitivity_params = ("compression_pctile", "breakout_len", "extreme_bias",
                          "stop_atr", "rr_ratio")
    int_params = frozenset({"breakout_len"})
    numeric_params = sensitivity_params
    toggle_params = ("use_shorts",)
    toggles = ("use_shorts",)
    prunable_toggles = ()
    traps = {
        "compression_pctile": (100.0, None, "compression: BB bandwidth 90-day percentile"),
        "extreme_bias":       (0.0, None, "extreme bias: position in the 30-day range"),
    }

    def default_cfg(self):
        return {
            "family": self.name,
            "use_longs": True, "use_shorts": True,
            "compression_pctile": 25.0, "breakout_len": 24, "extreme_bias": 0.7,
            "atr_len": 14, "stop_atr": 2.5, "rr_ratio": 2.4,
            "risk_pct": 1.0, "use_risk_sizing": True,
            "initial_capital": 10000.0,
            "commission_pct": 0.1, "slippage_ticks": 5, "tick_size": 0.01,
        }

    def sample_cfg(self, rng):
        # the draw order is fixed: changing it changes every seeded run's trials
        cfg = self.default_cfg()
        cfg["compression_pctile"] = round(rng.uniform(5, 50), 1)
        cfg["breakout_len"] = rng.randint(10, 72)
        cfg["extreme_bias"] = round(rng.uniform(0.5, 0.95), 2)
        cfg["stop_atr"] = round(rng.uniform(1.5, 3.5), 2)
        cfg["rr_ratio"] = round(rng.uniform(1.5, 3.5), 2)
        cfg["use_shorts"] = rng.random() < 0.5
        return cfg

    def cfg_ok(self, cfg):
        return (0.0 < cfg["compression_pctile"] <= 100.0 and cfg["breakout_len"] >= 2
                and 0.0 <= cfg["extreme_bias"] <= 1.0)

    def signals(self, bars, cfg):
        h, l, c = bars["high"], bars["low"], bars["close"]
        n = len(c)
        pct = pctile_for(bars)
        atr_v = bars["atr"]
        n_bo = cfg["breakout_len"]
        rng30 = RANGE_DAYS * (86400 // _bar_seconds(bars["ts"]))
        comp, bias = cfg["compression_pctile"], cfg["extreme_bias"]

        out = [(False, False)] * n
        for i in range(max(bars["from_i"], n_bo), n):
            if pct[i] is None or pct[i] > comp or atr_v[i] is None:
                continue
            long_bo = cfg["use_longs"] and c[i] > max(h[i - n_bo:i])
            short_bo = cfg["use_shorts"] and c[i] < min(l[i - n_bo:i])
            if not (long_bo or short_bo):
                continue
            lb = max(0, i - rng30)
            hi30, lo30 = max(h[lb:i + 1]), min(l[lb:i + 1])
            if hi30 <= lo30:
                continue
            pos = (c[i] - lo30) / (hi30 - lo30)
            out[i] = (long_bo and pos >= bias, short_bo and pos <= 1.0 - bias)
        return out

    def pine_template(self):
        return PINE


# Written for this family; the order/exit block is the core template's.
PINE = '''//@version=6
// {title}
// Generated by StrategyForge pine_export.py on {generated}
// {source_note}
// Logic mirrors strategyforge/app/strategies/squeeze.py (squeeze family) and engine.py.
// Verify against the engine's trade list before trusting it.
strategy("{title}", overlay=true,
     initial_capital={initial_capital:.0f},
     default_qty_type=strategy.cash, default_qty_value=0,
     commission_type=strategy.commission.percent, commission_value={commission_pct},
     slippage={slippage_int},
     pyramiding=0, calc_on_every_tick=false, process_orders_on_close=false,
     max_bars_back=2500)

// ---------------------------------------------------------------- inputs ---
useLongs      = input.bool({use_longs},  "Enable longs",  group="Direction")
useShorts     = input.bool({use_shorts}, "Enable shorts", group="Direction")

compPct       = input.float({compression_pctile}, "Max BB bandwidth percentile (90d)", group="Squeeze")
breakoutLen   = input.int({breakout_len}, "Breakout lookback (bars)", group="Squeeze")
extremeBias   = input.float({extreme_bias}, "Min position in 30d range (long)", group="Squeeze")

atrLen        = input.int({atr_len}, "ATR length", group="Risk")
stopAtr       = input.float({stop_atr}, "Stop = ATR x", group="Risk")
rrRatio       = input.float({rr_ratio}, "Reward : risk", group="Risk")
riskPct       = input.float({risk_pct}, "Risk % of equity per trade", group="Risk")
useRiskSizing = input.bool({use_risk_sizing}, "Risk-based sizing", group="Risk")

webhookSecret = input.string("CHANGE_ME", "davesbridge webhook secret", group="Alerts")

// ------------------------------------------------------------ indicators ---
barsPerDay = int(86400 / timeframe.in_seconds())
pctLen     = 90 * barsPerDay          // previous 90 days, bar i excluded (features.py)
rangeLen   = 30 * barsPerDay + 1      // 30 days, bar i included (features.py)
bbBasis    = ta.sma(close, 20)
bbSd       = ta.stdev(close, 20, false)   // sample std, as features.py
bbBw       = bbBasis != 0 ? 4.0 * bbSd / bbBasis * 100.0 : na
bwPct      = ta.percentrank(bbBw, pctLen) // % of previous pctLen values <= current
dcHi       = ta.highest(high, breakoutLen)[1]
dcLo       = ta.lowest(low, breakoutLen)[1]
hi30       = ta.highest(high, rangeLen)
lo30       = ta.lowest(low, rangeLen)
atrV       = ta.atr(atrLen)

// --------------------------------------------------------------- signals ---
// no signal until 90 days of bandwidth history exist, as in the engine
compressed = bar_index >= pctLen and not na(bwPct) and bwPct <= compPct
rangePos   = hi30 > lo30 ? (close - lo30) / (hi30 - lo30) : na
longSig  = useLongs  and compressed and close > dcHi and not na(rangePos) and rangePos >= extremeBias
shortSig = useShorts and compressed and close < dcLo and not na(rangePos) and rangePos <= 1.0 - extremeBias

// ---------------------------------------------------------------- orders ---
// signal on this bar's close -> strategy.entry fills at NEXT bar's open,
// exactly like engine.py's `pending` mechanism.
var float stopDist  = na
float riskAmt = strategy.equity * riskPct / 100.0

if longSig and strategy.position_size <= 0
    stopDist  := atrV * stopAtr
    float qty = useRiskSizing ? riskAmt / math.max(stopDist, syminfo.mintick) : strategy.equity / close
    strategy.entry("L", strategy.long, qty=qty)
    alert('{{"secret":"' + webhookSecret + '","strategy":"{title}","side":"long","symbol":"' + syminfo.ticker + '","tf":"' + timeframe.period + '","price":' + str.tostring(close) + '}}', alert.freq_once_per_bar_close)

if shortSig and strategy.position_size >= 0
    stopDist  := atrV * stopAtr
    float qty = useRiskSizing ? riskAmt / math.max(stopDist, syminfo.mintick) : strategy.equity / close
    strategy.entry("S", strategy.short, qty=qty)
    alert('{{"secret":"' + webhookSecret + '","strategy":"{title}","side":"short","symbol":"' + syminfo.ticker + '","tf":"' + timeframe.period + '","price":' + str.tostring(close) + '}}', alert.freq_once_per_bar_close)

// exits attach once the position exists (avg price is the real fill)
if strategy.position_size > 0
    strategy.exit("L-X", "L", stop=strategy.position_avg_price - stopDist,
         limit=strategy.position_avg_price + stopDist * rrRatio)
if strategy.position_size < 0
    strategy.exit("S-X", "S", stop=strategy.position_avg_price + stopDist,
         limit=strategy.position_avg_price - stopDist * rrRatio)

// ------------------------------------------------------------------ plots ---
plot(hi30, "30d high", color=color.new(color.green, 60))
plot(lo30, "30d low",  color=color.new(color.red, 60))
bgcolor(compressed ? color.new(color.yellow, 90) : na, title="Compressed")
plotshape(longSig,  "Long signal",  shape.triangleup,   location.belowbar, color=color.green, size=size.small)
plotshape(shortSig, "Short signal", shape.triangledown, location.abovebar, color=color.red,   size=size.small)
'''
