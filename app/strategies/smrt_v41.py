"""
SMRT Trap-Aware V4.1 — the original family, moved out of engine.py in P5.2.

Signal logic, parameter ranges, ablation/sensitivity lists and Pine template
are exactly what engine.py / optimizer.py / ablation.py / validation.py /
pine_export.py held before the split; tests/test_regression.py checks every
stored V4.1 result still reproduces to the cent.

A V4.1 cfg carries no "family" key (strategies.family_of treats a missing key
as this family), so stored params and their DNA are unchanged.
"""

import indicators as ind
from strategies import register
from strategies.base import Strategy


@register
class SmrtV41(Strategy):
    name = "smrt_v41"

    dna_keys = (
        "use_longs", "use_shorts", "rsi_len", "rsi_buy", "rsi_sell",
        "ema_fast", "ema_mid", "ema_slow", "adx_len", "adx_smooth", "adx_threshold",
        "dc_len", "min_ema_sep", "htf_seconds", "htf_ema_len", "use_htf",
        "use_vwap", "use_volume", "vol_ma_len", "min_rel_vol",
        "avoid_chop", "max_compression", "avoid_late", "max_dist_ema",
        "lookback_big", "big_bar_atr", "max_big_bars",
        "avoid_fake", "require_retest", "retest_bars",
        "atr_len", "stop_atr", "rr_ratio", "risk_pct", "use_risk_sizing",
        "close_on_opposite", "use_trailing", "trail_atr",
    )
    sensitivity_params = ("ema_fast", "ema_mid", "ema_slow", "rsi_buy", "rsi_sell",
                          "adx_threshold", "min_ema_sep", "min_rel_vol",
                          "max_compression", "max_dist_ema", "stop_atr", "rr_ratio")
    int_params = frozenset({"ema_fast", "ema_mid", "ema_slow", "rsi_len", "adx_len", "dc_len"})
    numeric_params = ("ema_fast", "ema_mid", "ema_slow", "rsi_buy", "rsi_sell",
                      "adx_threshold", "min_ema_sep", "min_rel_vol",
                      "max_compression", "max_dist_ema", "stop_atr", "rr_ratio",
                      "trail_atr")
    toggle_params = ("use_trailing", "use_shorts", "use_htf", "use_vwap",
                     "use_volume", "avoid_chop", "avoid_late", "avoid_fake")
    toggles = ("use_shorts", "use_htf", "use_vwap", "use_volume", "avoid_chop",
               "avoid_late", "avoid_fake", "use_trailing")
    prunable_toggles = tuple(t for t in toggles if t != "use_shorts")
    traps = {
        "min_rel_vol":     (0.0, "use_volume", "volume trap: bar volume vs its MA"),
        "max_compression": (0.0, "avoid_chop", "chop trap: EMA-compression arm"),
        "max_dist_ema":    (1e9, "avoid_late", "late trap: distance-from-EMA arm"),
        "max_big_bars":    (10 ** 9, "avoid_late", "late trap: big-bar-count arm"),
        "min_ema_sep":     (0.0, None, "trend gate: fast/slow EMA separation"),
        "adx_threshold":   (0.0, None, "trend gate: ADX strength (+ chop ADX arm)"),
    }

    def default_cfg(self):
        return {
            "use_longs": True, "use_shorts": True,
            "rsi_len": 14, "rsi_buy": 56.0, "rsi_sell": 44.0,
            "ema_fast": 9, "ema_mid": 21, "ema_slow": 50,
            "adx_len": 14, "adx_smooth": 14, "adx_threshold": 25.0,
            "dc_len": 20, "min_ema_sep": 1.5,
            "htf_seconds": 4 * 3600, "htf_ema_len": 50, "use_htf": True,
            "use_vwap": True, "use_volume": True,
            "vol_ma_len": 20, "min_rel_vol": 1.1,
            "avoid_chop": True, "max_compression": 1.5,
            "avoid_late": True, "max_dist_ema": 3.0,
            "lookback_big": 4, "big_bar_atr": 1.2, "max_big_bars": 2,
            "avoid_fake": True, "require_retest": False, "retest_bars": 3,
            "atr_len": 14, "stop_atr": 2.5, "rr_ratio": 2.4,
            "risk_pct": 1.0, "use_risk_sizing": True,
            "close_on_opposite": True,
            "use_trailing": False, "trail_atr": 1.6,
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
        cfg["rsi_buy"] = round(rng.uniform(52, 60), 1)
        cfg["rsi_sell"] = round(rng.uniform(40, 48), 1)
        cfg["adx_threshold"] = round(rng.uniform(18, 30), 1)
        cfg["min_ema_sep"] = round(rng.uniform(0.5, 2.5), 2)
        cfg["min_rel_vol"] = round(rng.uniform(0.8, 1.5), 2)
        cfg["max_compression"] = round(rng.uniform(0.8, 2.0), 2)
        cfg["max_dist_ema"] = round(rng.uniform(2.0, 4.5), 2)
        cfg["stop_atr"] = round(rng.uniform(1.5, 3.5), 2)
        cfg["rr_ratio"] = round(rng.uniform(1.5, 3.5), 2)
        cfg["use_trailing"] = rng.random() < 0.5
        cfg["trail_atr"] = round(rng.uniform(1.2, 2.5), 2)
        cfg["use_shorts"] = rng.random() < 0.5
        for flag in ("use_htf", "use_vwap", "use_volume", "avoid_chop",
                     "avoid_late", "avoid_fake"):
            cfg[flag] = rng.random() < 0.7   # keep filters mostly on
        return cfg

    def for_timeframe(self, cfg, tf_seconds):
        if tf_seconds == 4 * 3600:
            cfg["htf_seconds"] = 86400   # daily HTF for 4h charts
        return cfg

    def cfg_ok(self, cfg):
        return cfg["ema_fast"] < cfg["ema_mid"] < cfg["ema_slow"]

    def signals(self, bars, cfg):
        ts, o, h, l, c, v = (bars[k] for k in ("ts", "open", "high", "low", "close", "volume"))
        atr_v = bars["atr"]
        n = len(ts)
        ema_f = ind.ema(c, cfg["ema_fast"])
        ema_m = ind.ema(c, cfg["ema_mid"])
        ema_s = ind.ema(c, cfg["ema_slow"])
        rsi_v = ind.rsi(c, cfg["rsi_len"])
        adx_v = ind.dmi(h, l, c, cfg["adx_len"], cfg["adx_smooth"])
        dc_hi, dc_lo = ind.donchian_prev(h, l, cfg["dc_len"])
        vwap_v = ind.vwap_daily(ts, h, l, c, v)
        vol_ma = ind.sma(v, cfg["vol_ma_len"])
        htf_c, htf_e = ind.htf_confirmed(ts, c, cfg["htf_seconds"], cfg["htf_ema_len"])

        out = [(False, False)] * n
        for i in range(bars["from_i"], n):
            need = [ema_f[i], ema_m[i], ema_s[i], rsi_v[i], adx_v[i], dc_hi[i],
                    dc_lo[i], vwap_v[i], vol_ma[i], atr_v[i]]
            if any(x is None for x in need):
                continue
            if cfg["use_htf"] and (htf_c[i] is None or htf_e[i] is None):
                continue

            bull_stack = ema_f[i] > ema_m[i] > ema_s[i]
            bear_stack = ema_f[i] < ema_m[i] < ema_s[i]
            bull_rsi = rsi_v[i] > (cfg["rsi_buy"] + 1)
            bear_rsi = rsi_v[i] < (cfg["rsi_sell"] - 1)
            strong_long = adx_v[i] > (cfg["adx_threshold"] + 2)
            strong_short = adx_v[i] > (cfg["adx_threshold"] + 5)
            rel_vol = v[i] / vol_ma[i] if vol_ma[i] > 0 else 0.0
            vol_ok = rel_vol >= cfg["min_rel_vol"]
            bull_break = c[i] > dc_hi[i] and c[i] > o[i]
            bear_break = c[i] < dc_lo[i] and c[i] < o[i]
            sep = abs(ema_f[i] - ema_s[i]) / ema_s[i] * 100.0 if ema_s[i] else 0.0
            strong_sep = sep >= cfg["min_ema_sep"]
            slope_long = ema_s[i] > ema_s[i - 1]
            slope_short = ema_s[i] < ema_s[i - 1]
            trending = adx_v[i] > 22 and sep > 1.0
            mom_long = c[i] > c[i - 1] > c[i - 2]
            mom_short = c[i] < c[i - 1] < c[i - 2] and c[i] < ema_f[i] and adx_v[i] > 30
            choppy = adx_v[i] <= cfg["adx_threshold"] or sep < cfg["max_compression"]
            dist = abs(c[i] - ema_f[i]) / ema_f[i] * 100.0 if ema_f[i] else 0.0
            big_bars = 0
            for k in range(max(0, i - cfg["lookback_big"] + 1), i + 1):
                if atr_v[k] is not None and abs(c[k] - o[k]) > atr_v[k] * cfg["big_bar_atr"]:
                    big_bars += 1
            late = dist > (cfg["max_dist_ema"] * 0.8) or big_bars > cfg["max_big_bars"]
            conf_bull = bull_break and c[i] > h[i - 1]
            conf_bear = bear_break and c[i] < l[i - 1]
            htf_long = (not cfg["use_htf"]) or (htf_c[i] > htf_e[i])
            htf_short = (not cfg["use_htf"]) or (htf_c[i] < htf_e[i])
            trend_long = strong_long and bull_stack and strong_sep and adx_v[i] > 26 and slope_long
            trend_short = strong_short and bear_stack and strong_sep and adx_v[i] > 28 and slope_short

            long_sig = (cfg["use_longs"] and trending and trend_long and bull_rsi and mom_long
                        and htf_long
                        and ((not cfg["use_vwap"]) or c[i] > vwap_v[i])
                        and ((not cfg["use_volume"]) or vol_ok)
                        and ((not cfg["avoid_chop"]) or not choppy)
                        and ((not cfg["avoid_late"]) or not late)
                        and ((not cfg["avoid_fake"]) or conf_bull))
            short_sig = (cfg["use_shorts"] and trending and trend_short and bear_rsi and mom_short
                         and htf_short
                         and ((not cfg["use_vwap"]) or c[i] < vwap_v[i])
                         and ((not cfg["use_volume"]) or vol_ok)
                         and ((not cfg["avoid_chop"]) or not choppy)
                         and ((not cfg["avoid_late"]) or not late)
                         and ((not cfg["avoid_fake"]) or conf_bear))
            out[i] = (long_sig, short_sig)
        return out

    def pine_fields(self, cfg):
        return {"htf": _tf_minutes(cfg["htf_seconds"])}

    def pine_template(self):
        return PINE


def _tf_minutes(seconds):
    m = int(seconds) // 60
    if m % 1440 == 0:
        return f"{m // 1440}D"
    return str(m)


PINE = '''//@version=6
// {title}
// Generated by StrategyForge pine_export.py on {generated}
// {source_note}
// Logic mirrors strategyforge/app/engine.py (SMRT Trap-Aware V4.1).
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

rsiLen        = input.int({rsi_len},   "RSI length",     group="RSI")
rsiBuy        = input.float({rsi_buy},  "RSI buy level",  group="RSI")
rsiSell       = input.float({rsi_sell}, "RSI sell level", group="RSI")

emaFastLen    = input.int({ema_fast}, "EMA fast", group="EMA stack")
emaMidLen     = input.int({ema_mid},  "EMA mid",  group="EMA stack")
emaSlowLen    = input.int({ema_slow}, "EMA slow", group="EMA stack")
minEmaSep     = input.float({min_ema_sep}, "Min EMA separation %", group="EMA stack")

adxLen        = input.int({adx_len},    "DI length",     group="ADX")
adxSmooth     = input.int({adx_smooth}, "ADX smoothing", group="ADX")
adxThresh     = input.float({adx_threshold}, "ADX threshold", group="ADX")

dcLen         = input.int({dc_len}, "Donchian length", group="Breakout")

useHTF        = input.bool({use_htf}, "Use HTF EMA filter", group="Higher timeframe")
htfTF         = input.timeframe("{htf}", "HTF", group="Higher timeframe")
htfEmaLen     = input.int({htf_ema_len}, "HTF EMA length", group="Higher timeframe")

useVWAP       = input.bool({use_vwap},   "Require VWAP side", group="Filters")
useVolume     = input.bool({use_volume}, "Require volume",    group="Filters")
volMaLen      = input.int({vol_ma_len}, "Volume MA length", group="Filters")
minRelVol     = input.float({min_rel_vol}, "Min relative volume", group="Filters")

avoidChop     = input.bool({avoid_chop}, "Avoid chop", group="Trap filters")
maxCompress   = input.float({max_compression}, "Max compression (EMA sep %)", group="Trap filters")
avoidLate     = input.bool({avoid_late}, "Avoid late entries", group="Trap filters")
maxDistEma    = input.float({max_dist_ema}, "Max distance from fast EMA %", group="Trap filters")
lookbackBig   = input.int({lookback_big}, "Big-bar lookback", group="Trap filters")
bigBarAtr     = input.float({big_bar_atr}, "Big bar = body > ATR x", group="Trap filters")
maxBigBars    = input.int({max_big_bars}, "Max big bars in lookback", group="Trap filters")
avoidFake     = input.bool({avoid_fake}, "Require breakout confirmation", group="Trap filters")

atrLen        = input.int({atr_len}, "ATR length", group="Risk")
stopAtr       = input.float({stop_atr}, "Stop = ATR x", group="Risk")
rrRatio       = input.float({rr_ratio}, "Reward : risk", group="Risk")
riskPct       = input.float({risk_pct}, "Risk % of equity per trade", group="Risk")
useRiskSizing = input.bool({use_risk_sizing}, "Risk-based sizing", group="Risk")
useTrailing   = input.bool({use_trailing}, "ATR trailing stop instead of fixed target", group="Risk")
trailAtr      = input.float({trail_atr}, "Trail = ATR x", group="Risk")

webhookSecret = input.string("CHANGE_ME", "davesbridge webhook secret", group="Alerts")

// ------------------------------------------------------------ indicators ---
emaF  = ta.ema(close, emaFastLen)
emaM  = ta.ema(close, emaMidLen)
emaS  = ta.ema(close, emaSlowLen)
rsiV  = ta.rsi(close, rsiLen)
[diP, diM, adxV] = ta.dmi(adxLen, adxSmooth)
dcHi  = ta.highest(high, dcLen)[1]
dcLo  = ta.lowest(low, dcLen)[1]
vwapV = ta.vwap(hlc3)
volMa = ta.sma(volume, volMaLen)
atrV  = ta.atr(atrLen)

// previous COMPLETED higher-timeframe bar (non-repainting)
htfClose = request.security(syminfo.tickerid, htfTF, close[1], lookahead=barmerge.lookahead_on)
htfEma   = request.security(syminfo.tickerid, htfTF, ta.ema(close, htfEmaLen)[1], lookahead=barmerge.lookahead_on)

// --------------------------------------------------------------- signals ---
bullStack  = emaF > emaM and emaM > emaS
bearStack  = emaF < emaM and emaM < emaS
bullRsi    = rsiV > rsiBuy + 1
bearRsi    = rsiV < rsiSell - 1
strongLong = adxV > adxThresh + 2
strongShort= adxV > adxThresh + 5
relVol     = volMa > 0 ? volume / volMa : 0.0
volOk      = relVol >= minRelVol
bullBreak  = close > dcHi and close > open
bearBreak  = close < dcLo and close < open
sep        = emaS != 0 ? math.abs(emaF - emaS) / emaS * 100.0 : 0.0
strongSep  = sep >= minEmaSep
slopeLong  = emaS > emaS[1]
slopeShort = emaS < emaS[1]
trending   = adxV > 22 and sep > 1.0
momLong    = close > close[1] and close[1] > close[2]
momShort   = close < close[1] and close[1] < close[2] and close < emaF and adxV > 30
choppy     = adxV <= adxThresh or sep < maxCompress
dist       = emaF != 0 ? math.abs(close - emaF) / emaF * 100.0 : 0.0
bigBar     = math.abs(close - open) > atrV * bigBarAtr
bigBars    = math.sum(bigBar ? 1 : 0, lookbackBig)
late       = dist > maxDistEma * 0.8 or bigBars > maxBigBars
confBull   = bullBreak and close > high[1]
confBear   = bearBreak and close < low[1]
htfLong    = not useHTF or htfClose > htfEma
htfShort   = not useHTF or htfClose < htfEma
trendLong  = strongLong and bullStack and strongSep and adxV > 26 and slopeLong
trendShort = strongShort and bearStack and strongSep and adxV > 28 and slopeShort

longSig  = useLongs and trending and trendLong and bullRsi and momLong and htfLong
     and (not useVWAP or close > vwapV)
     and (not useVolume or volOk)
     and (not avoidChop or not choppy)
     and (not avoidLate or not late)
     and (not avoidFake or confBull)
shortSig = useShorts and trending and trendShort and bearRsi and momShort and htfShort
     and (not useVWAP or close < vwapV)
     and (not useVolume or volOk)
     and (not avoidChop or not choppy)
     and (not avoidLate or not late)
     and (not avoidFake or confBear)

// ---------------------------------------------------------------- orders ---
// signal on this bar's close -> strategy.entry fills at NEXT bar's open,
// exactly like engine.py's `pending` mechanism.
var float stopDist  = na
var float trailDist = na
float riskAmt = strategy.equity * riskPct / 100.0

if longSig and strategy.position_size <= 0
    stopDist  := atrV * stopAtr
    trailDist := atrV * trailAtr
    float qty = useRiskSizing ? riskAmt / math.max(stopDist, syminfo.mintick) : strategy.equity / close
    strategy.entry("L", strategy.long, qty=qty)
    alert('{{"secret":"' + webhookSecret + '","strategy":"{title}","side":"long","symbol":"' + syminfo.ticker + '","tf":"' + timeframe.period + '","price":' + str.tostring(close) + '}}', alert.freq_once_per_bar_close)

if shortSig and strategy.position_size >= 0
    stopDist  := atrV * stopAtr
    trailDist := atrV * trailAtr
    float qty = useRiskSizing ? riskAmt / math.max(stopDist, syminfo.mintick) : strategy.equity / close
    strategy.entry("S", strategy.short, qty=qty)
    alert('{{"secret":"' + webhookSecret + '","strategy":"{title}","side":"short","symbol":"' + syminfo.ticker + '","tf":"' + timeframe.period + '","price":' + str.tostring(close) + '}}', alert.freq_once_per_bar_close)

// exits attach once the position exists (avg price is the real fill)
if strategy.position_size > 0
    if useTrailing
        strategy.exit("L-X", "L", stop=strategy.position_avg_price - stopDist,
             trail_points=trailDist / syminfo.mintick, trail_offset=trailDist / syminfo.mintick)
    else
        strategy.exit("L-X", "L", stop=strategy.position_avg_price - stopDist,
             limit=strategy.position_avg_price + stopDist * rrRatio)
if strategy.position_size < 0
    if useTrailing
        strategy.exit("S-X", "S", stop=strategy.position_avg_price + stopDist,
             trail_points=trailDist / syminfo.mintick, trail_offset=trailDist / syminfo.mintick)
    else
        strategy.exit("S-X", "S", stop=strategy.position_avg_price + stopDist,
             limit=strategy.position_avg_price - stopDist * rrRatio)

// ------------------------------------------------------------------ plots ---
plot(emaF, "EMA fast", color=color.new(color.aqua, 0))
plot(emaM, "EMA mid",  color=color.new(color.orange, 0))
plot(emaS, "EMA slow", color=color.new(color.red, 0))
plotshape(longSig,  "Long signal",  shape.triangleup,   location.belowbar, color=color.green, size=size.small)
plotshape(shortSig, "Short signal", shape.triangledown, location.abovebar, color=color.red,   size=size.small)
'''
