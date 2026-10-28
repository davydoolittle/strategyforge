"""
core — the parts of V4.1 that earned their place in the F2 ablation (P5.3).

Entry, long (short is the mirror image), on the close of bar i:
  EMA stack       ema_fast > ema_mid > ema_slow
  trend strength  ADX(14, 14) > adx_threshold
  volume          volume / SMA(volume, vol_ma_len) >= min_rel_vol
  not late        |close - ema_fast| / ema_fast * 100 <= max_dist_ema
  breakout, confirmed
                  close > highest high of the previous 20 bars, close > open,
                  close > previous bar's high
Exit: fixed ATR(14) stop at stop_atr and a target at rr_ratio x the stop
distance (engine.py). Reverse on an opposite signal, as every family.

Gone, compared with V4.1: VWAP, the chop trap, the big-bar arm of the late
trap, trailing exits, the HTF filter, RSI, the 3-bar momentum rule, the
EMA-separation gate, the EMA-slope gate, and V4.1's hardcoded extras
(ADX > 22/26/28/30, separation > 1.0, shorts needing ADX threshold + 5).
Longs and shorts are symmetric.

Searched (sample_cfg): ema_fast ema_mid ema_slow adx_threshold vol_ma_len
min_rel_vol max_dist_ema stop_atr rr_ratio use_shorts = 10 parameters.
Fixed: use_longs on, ATR/ADX lengths 14, Donchian 20, 1% risk sizing.
Each cfg carries "family": "core", so its DNA can never equal a V4.1 DNA.
"""

import indicators as ind
from strategies import register
from strategies.base import Strategy

ADX_LEN = 14
DC_LEN = 20


@register
class Core(Strategy):
    name = "core"

    dna_keys = ("use_longs", "use_shorts", "ema_fast", "ema_mid", "ema_slow",
                "adx_threshold", "vol_ma_len", "min_rel_vol", "max_dist_ema",
                "atr_len", "stop_atr", "rr_ratio", "risk_pct", "use_risk_sizing")
    sensitivity_params = ("ema_fast", "ema_mid", "ema_slow", "adx_threshold",
                          "vol_ma_len", "min_rel_vol", "max_dist_ema", "stop_atr", "rr_ratio")
    int_params = frozenset({"ema_fast", "ema_mid", "ema_slow", "vol_ma_len"})
    numeric_params = sensitivity_params
    toggle_params = ("use_shorts",)
    toggles = ("use_shorts",)
    prunable_toggles = ()
    traps = {
        "min_rel_vol":   (0.0, None, "volume trap: bar volume vs its MA"),
        "max_dist_ema":  (1e9, None, "late trap: distance from the fast EMA"),
        "adx_threshold": (0.0, None, "trend gate: ADX strength"),
    }

    def default_cfg(self):
        return {
            "family": self.name,
            "use_longs": True, "use_shorts": True,
            "ema_fast": 9, "ema_mid": 21, "ema_slow": 50,
            "adx_threshold": 25.0,
            "vol_ma_len": 20, "min_rel_vol": 1.1,
            "max_dist_ema": 2.4,
            "atr_len": 14, "stop_atr": 2.5, "rr_ratio": 2.4,
            "risk_pct": 1.0, "use_risk_sizing": True,
            "initial_capital": 10000.0,
            "commission_pct": 0.1, "slippage_ticks": 5, "tick_size": 0.01,
        }

    def sample_cfg(self, rng):
        # the draw order is fixed: changing it changes every seeded run's trials
        cfg = self.default_cfg()
        f = rng.randint(5, 13)
        m = rng.randint(max(f + 4, 15), 35)
        s = rng.randint(max(m + 10, 40), 80)
        cfg["ema_fast"], cfg["ema_mid"], cfg["ema_slow"] = f, m, s
        cfg["adx_threshold"] = round(rng.uniform(15, 35), 1)
        cfg["vol_ma_len"] = rng.randint(10, 40)
        cfg["min_rel_vol"] = round(rng.uniform(0.8, 1.8), 2)
        cfg["max_dist_ema"] = round(rng.uniform(1.0, 4.0), 2)
        cfg["stop_atr"] = round(rng.uniform(1.5, 3.5), 2)
        cfg["rr_ratio"] = round(rng.uniform(1.5, 3.5), 2)
        cfg["use_shorts"] = rng.random() < 0.5
        return cfg

    def cfg_ok(self, cfg):
        return cfg["ema_fast"] < cfg["ema_mid"] < cfg["ema_slow"]

    def signals(self, bars, cfg):
        o, h, l, c, v = (bars[k] for k in ("open", "high", "low", "close", "volume"))
        n = len(c)
        ema_f = ind.ema(c, cfg["ema_fast"])
        ema_m = ind.ema(c, cfg["ema_mid"])
        ema_s = ind.ema(c, cfg["ema_slow"])
        adx_v = ind.dmi(h, l, c, ADX_LEN, ADX_LEN)
        dc_hi, dc_lo = ind.donchian_prev(h, l, DC_LEN)
        vol_ma = ind.sma(v, cfg["vol_ma_len"])
        atr_v = bars["atr"]

        out = [(False, False)] * n
        for i in range(bars["from_i"], n):
            if any(x is None for x in (ema_f[i], ema_m[i], ema_s[i], adx_v[i],
                                       dc_hi[i], vol_ma[i], atr_v[i])):
                continue
            strong = adx_v[i] > cfg["adx_threshold"]
            vol_ok = (v[i] / vol_ma[i] if vol_ma[i] > 0 else 0.0) >= cfg["min_rel_vol"]
            dist = abs(c[i] - ema_f[i]) / ema_f[i] * 100.0 if ema_f[i] else 0.0
            if not (strong and vol_ok and dist <= cfg["max_dist_ema"]):
                continue
            long_sig = (cfg["use_longs"] and ema_f[i] > ema_m[i] > ema_s[i]
                        and c[i] > dc_hi[i] and c[i] > o[i] and c[i] > h[i - 1])
            short_sig = (cfg["use_shorts"] and ema_f[i] < ema_m[i] < ema_s[i]
                         and c[i] < dc_lo[i] and c[i] < o[i] and c[i] < l[i - 1])
            out[i] = (long_sig, short_sig)
        return out

    def pine_fields(self, cfg):
        return {"adx_len": ADX_LEN, "dc_len": DC_LEN}

    def pine_template(self):
        return PINE


# Derived from the V4.1 template (smrt_v41.PINE) with the removed parts gone.
PINE = '''//@version=6
// {title}
// Generated by StrategyForge pine_export.py on {generated}
// {source_note}
// Logic mirrors strategyforge/app/strategies/core.py (core family) and engine.py.
// Verify against the engine's trade list before trusting it.
strategy("{title}", overlay=true,
     initial_capital={initial_capital:.0f},
     default_qty_type=strategy.cash, default_qty_value=0,
     commission_type=strategy.commission.percent, commission_value={commission_pct},
     slippage={slippage_int},
     pyramiding=0, calc_on_every_tick=false, process_orders_on_close=false)

// ---------------------------------------------------------------- inputs ---
useLongs      = input.bool({use_longs},  "Enable longs",  group="Direction")
useShorts     = input.bool({use_shorts}, "Enable shorts", group="Direction")

emaFastLen    = input.int({ema_fast}, "EMA fast", group="EMA stack")
emaMidLen     = input.int({ema_mid},  "EMA mid",  group="EMA stack")
emaSlowLen    = input.int({ema_slow}, "EMA slow", group="EMA stack")

adxLen        = input.int({adx_len}, "DI length / ADX smoothing", group="ADX")
adxThresh     = input.float({adx_threshold}, "ADX threshold", group="ADX")

dcLen         = input.int({dc_len}, "Donchian length", group="Breakout")

volMaLen      = input.int({vol_ma_len}, "Volume MA length", group="Filters")
minRelVol     = input.float({min_rel_vol}, "Min relative volume", group="Filters")
maxDistEma    = input.float({max_dist_ema}, "Max distance from fast EMA %", group="Filters")

atrLen        = input.int({atr_len}, "ATR length", group="Risk")
stopAtr       = input.float({stop_atr}, "Stop = ATR x", group="Risk")
rrRatio       = input.float({rr_ratio}, "Reward : risk", group="Risk")
riskPct       = input.float({risk_pct}, "Risk % of equity per trade", group="Risk")
useRiskSizing = input.bool({use_risk_sizing}, "Risk-based sizing", group="Risk")

webhookSecret = input.string("CHANGE_ME", "davesbridge webhook secret", group="Alerts")

// ------------------------------------------------------------ indicators ---
emaF  = ta.ema(close, emaFastLen)
emaM  = ta.ema(close, emaMidLen)
emaS  = ta.ema(close, emaSlowLen)
[diP, diM, adxV] = ta.dmi(adxLen, adxLen)
dcHi  = ta.highest(high, dcLen)[1]
dcLo  = ta.lowest(low, dcLen)[1]
volMa = ta.sma(volume, volMaLen)
atrV  = ta.atr(atrLen)

// --------------------------------------------------------------- signals ---
bullStack  = emaF > emaM and emaM > emaS
bearStack  = emaF < emaM and emaM < emaS
strong     = adxV > adxThresh
relVol     = volMa > 0 ? volume / volMa : 0.0
volOk      = relVol >= minRelVol
dist       = emaF != 0 ? math.abs(close - emaF) / emaF * 100.0 : 0.0
notLate    = dist <= maxDistEma
confBull   = close > dcHi and close > open and close > high[1]
confBear   = close < dcLo and close < open and close < low[1]

longSig  = useLongs and bullStack and strong and volOk and notLate and confBull
shortSig = useShorts and bearStack and strong and volOk and notLate and confBear

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
plot(emaF, "EMA fast", color=color.new(color.aqua, 0))
plot(emaM, "EMA mid",  color=color.new(color.orange, 0))
plot(emaS, "EMA slow", color=color.new(color.red, 0))
plotshape(longSig,  "Long signal",  shape.triangleup,   location.belowbar, color=color.green, size=size.small)
plotshape(shortSig, "Short signal", shape.triangledown, location.abovebar, color=color.red,   size=size.small)
'''
