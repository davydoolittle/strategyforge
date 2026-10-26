"""
StrategyForge engine v2 — SMRT Trap-Aware V4.1 as a parameterized library.

Same validated logic as v1 (73/87 exact trade matches vs TradingView,
$0.00 price deltas). Now: cfg is a parameter, timeframe resampling is
supported (1h native, 4h via aggregation), and backtest() returns results
instead of printing, so the optimizer can call it thousands of times.

CLI unchanged:
  python engine.py --pair BTC-USD --start 2024-09-02 --end 2026-09-01 [--trailing] [--tf 4h]
"""

import argparse
import json
import os
import sqlite3
from datetime import datetime, timezone

DB_PATH = os.environ.get("FORGE_DB", "/data/forge.db")
WARMUP_BARS = 300


def default_cfg():
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


# ------------------------------ indicators ---------------------------------

def sma(vals, n):
    out = [None] * len(vals)
    s = 0.0
    for i, v in enumerate(vals):
        s += v
        if i >= n:
            s -= vals[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def rma(vals, n):
    out = [None] * len(vals)
    seed = None
    s = 0.0
    for i, v in enumerate(vals):
        if v is None:
            continue
        if seed is None:
            s += v
            if i >= n - 1:
                seed = s / n
                out[i] = seed
        else:
            seed = (seed * (n - 1) + v) / n
            out[i] = seed
    return out


def ema(vals, n):
    out = [None] * len(vals)
    alpha = 2.0 / (n + 1)
    prev = None
    s = 0.0
    for i, v in enumerate(vals):
        if prev is None:
            s += v
            if i >= n - 1:
                prev = s / n
                out[i] = prev
        else:
            prev = alpha * v + (1 - alpha) * prev
            out[i] = prev
    return out


def rsi(close, n):
    gains = [max(close[i] - close[i - 1], 0.0) for i in range(1, len(close))]
    losses = [max(close[i - 1] - close[i], 0.0) for i in range(1, len(close))]
    ag = rma(gains, n)
    al = rma(losses, n)
    out = [None] * len(close)
    for i in range(1, len(close)):
        a, b = ag[i - 1], al[i - 1]
        if a is None or b is None:
            continue
        out[i] = 100.0 if b == 0 else 100.0 - 100.0 / (1.0 + a / b)
    return out


def true_range(h, l, c):
    out = [h[0] - l[0]]
    for i in range(1, len(h)):
        out.append(max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])))
    return out


def atr(h, l, c, n):
    return rma(true_range(h, l, c), n)


def dmi(h, l, c, di_len, adx_len):
    plus_dm = [0.0]
    minus_dm = [0.0]
    for i in range(1, len(h)):
        up = h[i] - h[i - 1]
        dn = l[i - 1] - l[i]
        plus_dm.append(up if (up > dn and up > 0) else 0.0)
        minus_dm.append(dn if (dn > up and dn > 0) else 0.0)
    tr_s = rma(true_range(h, l, c), di_len)
    pdm_s = rma(plus_dm, di_len)
    mdm_s = rma(minus_dm, di_len)
    dx = [None] * len(h)
    for i in range(len(h)):
        if tr_s[i] and pdm_s[i] is not None and mdm_s[i] is not None and tr_s[i] > 0:
            dip = 100.0 * pdm_s[i] / tr_s[i]
            dim = 100.0 * mdm_s[i] / tr_s[i]
            denom = dip + dim
            dx[i] = 0.0 if denom == 0 else 100.0 * abs(dip - dim) / denom
    return rma(dx, adx_len)


def donchian_prev(h, l, n):
    hi = [None] * len(h)
    lo = [None] * len(h)
    for i in range(n, len(h)):
        hi[i] = max(h[i - n:i])
        lo[i] = min(l[i - n:i])
    return hi, lo


def vwap_daily(ts, h, l, c, v):
    out = [None] * len(c)
    day = None
    pv = 0.0
    vv = 0.0
    for i in range(len(c)):
        d = datetime.fromtimestamp(ts[i], tz=timezone.utc).date()
        if d != day:
            day, pv, vv = d, 0.0, 0.0
        typ = (h[i] + l[i] + c[i]) / 3.0
        pv += typ * v[i]
        vv += v[i]
        out[i] = pv / vv if vv > 0 else None
    return out


# ------------------------------ data ---------------------------------------

def load_candles(conn, pair, end_ts, tf_seconds=3600, start_ts=None):
    rows = conn.execute(
        "SELECT ts, open, high, low, close, volume FROM candles"
        " WHERE pair = ? AND ts <= ? AND ts >= ? ORDER BY ts",
        (pair, end_ts, start_ts if start_ts is not None else 0),
    ).fetchall()
    if not rows:
        raise RuntimeError(f"no candles for {pair}")
    if tf_seconds == 3600:
        return ([r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows],
                [r[3] for r in rows], [r[4] for r in rows], [r[5] for r in rows])
    buckets = {}
    order = []
    for r in rows:
        b = r[0] // tf_seconds * tf_seconds
        if b not in buckets:
            buckets[b] = [r[1], r[2], r[3], r[4], r[5]]
            order.append(b)
        else:
            agg = buckets[b]
            agg[1] = max(agg[1], r[2])
            agg[2] = min(agg[2], r[3])
            agg[3] = r[4]
            agg[4] += r[5]
    ts = order
    return (ts,
            [buckets[b][0] for b in ts], [buckets[b][1] for b in ts],
            [buckets[b][2] for b in ts], [buckets[b][3] for b in ts],
            [buckets[b][4] for b in ts])


def htf_confirmed(ts, c, htf_seconds, ema_len):
    bucket_map = {}
    for i in range(len(ts)):
        bucket_map[ts[i] // htf_seconds] = c[i]
    keys = sorted(bucket_map)
    closes = [bucket_map[k] for k in keys]
    emas = ema(closes, ema_len)
    key_index = {k: i for i, k in enumerate(keys)}
    out_c = [None] * len(ts)
    out_e = [None] * len(ts)
    for i in range(len(ts)):
        b = ts[i] // htf_seconds - 1
        if b in key_index:
            j = key_index[b]
            out_c[i] = closes[j]
            out_e[i] = emas[j]
    return out_c, out_e


# ------------------------------ backtest -----------------------------------

def backtest(conn, pair, cfg, start_iso, end_iso, tf_seconds=3600):
    """Run the V4.1 strategy with the given cfg. Returns stats + trades."""
    start_ts = int(datetime.fromisoformat(start_iso).replace(tzinfo=timezone.utc).timestamp())
    end_ts = int(datetime.fromisoformat(end_iso).replace(tzinfo=timezone.utc).timestamp())
    # cfg["history_start_ts"] (default None = all history): ignore candles
    # before it, so indicator warmup is the same as when a candidate's windows
    # were pinned even after older candles are backfilled.
    ts, o, h, l, c, v = load_candles(conn, pair, end_ts, tf_seconds,
                                     cfg.get("history_start_ts"))

    n = len(ts)
    ema_f = ema(c, cfg["ema_fast"])
    ema_m = ema(c, cfg["ema_mid"])
    ema_s = ema(c, cfg["ema_slow"])
    rsi_v = rsi(c, cfg["rsi_len"])
    adx_v = dmi(h, l, c, cfg["adx_len"], cfg["adx_smooth"])
    dc_hi, dc_lo = donchian_prev(h, l, cfg["dc_len"])
    vwap_v = vwap_daily(ts, h, l, c, v)
    vol_ma = sma(v, cfg["vol_ma_len"])
    atr_v = atr(h, l, c, cfg["atr_len"])
    htf_c, htf_e = htf_confirmed(ts, c, cfg["htf_seconds"], cfg["htf_ema_len"])

    tick = cfg["tick_size"]
    slip = cfg["slippage_ticks"] * tick
    comm = cfg["commission_pct"] / 100.0
    trailing = cfg["use_trailing"]

    equity = cfg["initial_capital"]
    trades = []
    pos = None
    pending = None

    def bar_signals(i):
        need = [ema_f[i], ema_m[i], ema_s[i], rsi_v[i], adx_v[i], dc_hi[i],
                dc_lo[i], vwap_v[i], vol_ma[i], atr_v[i]]
        if any(x is None for x in need):
            return False, False
        if cfg["use_htf"] and (htf_c[i] is None or htf_e[i] is None):
            return False, False

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
        return long_sig, short_sig

    def close_position(exit_price, exit_ts, reason, is_market):
        nonlocal equity, pos
        px = exit_price
        if is_market or reason in ("stop", "trail"):
            px = px - slip if pos["dir"] == "long" else px + slip
        gross = (px - pos["entry"]) * pos["qty"] if pos["dir"] == "long" else (pos["entry"] - px) * pos["qty"]
        fees = (pos["entry"] * pos["qty"] + px * pos["qty"]) * comm
        pnl = gross - fees
        equity += pnl
        trades.append({
            "dir": pos["dir"], "qty": pos["qty"],
            "entry_ts": pos["entry_ts"], "entry": pos["entry"],
            "signal_ts": pos["signal_ts"],
            "exit_ts": exit_ts, "exit": px, "reason": reason, "pnl": pnl,
        })
        pos = None

    for i in range(WARMUP_BARS, n):
        bar_open_ts = ts[i]

        if pending is not None and pending.get("wait", 0) > 0:
            # validation.lookahead_shift: hold the fill back N extra bars.
            # cfg["fill_delay_bars"] defaults to 0 -> identical to v2 behaviour.
            pending["wait"] -= 1
        elif pending is not None:
            if pos is not None and pending.get("flip"):
                close_position(o[i], bar_open_ts, "opposite", True)
            if pos is None and pending["action"] in ("long", "short"):
                kind = pending["action"]
                entry_px = o[i] + slip if kind == "long" else o[i] - slip
                stop_dist = pending["atr"] * cfg["stop_atr"]
                risk_amt = equity * cfg["risk_pct"] / 100.0
                qty = risk_amt / max(stop_dist, tick) if cfg["use_risk_sizing"] else equity / entry_px
                pos = {
                    "dir": kind, "qty": qty, "entry": entry_px, "entry_ts": bar_open_ts,
                    "signal_ts": pending["signal_ts"], "atr": pending["atr"],
                    "stop": entry_px - stop_dist if kind == "long" else entry_px + stop_dist,
                    "limit": entry_px + stop_dist * cfg["rr_ratio"] if kind == "long"
                             else entry_px - stop_dist * cfg["rr_ratio"],
                    "trail_active": False, "peak": entry_px,
                    "trail_dist": pending["atr"] * cfg["trail_atr"],
                }
            pending = None

        if pos is not None:
            up_bar = c[i] >= o[i]
            path = [o[i], l[i], h[i], c[i]] if up_bar else [o[i], h[i], l[i], c[i]]
            for seg in range(3):
                a, b = path[seg], path[seg + 1]
                lo_seg, hi_seg = min(a, b), max(a, b)
                if pos["dir"] == "long":
                    if trailing:
                        pos["peak"] = max(pos["peak"], hi_seg)
                        if not pos["trail_active"] and pos["peak"] >= pos["entry"] + pos["trail_dist"]:
                            pos["trail_active"] = True
                        eff = max(pos["stop"], pos["peak"] - pos["trail_dist"]) if pos["trail_active"] else pos["stop"]
                        if lo_seg <= eff:
                            close_position(min(eff, a), ts[i], "trail" if pos["trail_active"] else "stop", False)
                            break
                    else:
                        if lo_seg <= pos["stop"]:
                            close_position(min(pos["stop"], a), ts[i], "stop", False)
                            break
                        if hi_seg >= pos["limit"]:
                            close_position(pos["limit"], ts[i], "target", False)
                            break
                else:
                    if trailing:
                        pos["peak"] = min(pos["peak"], lo_seg)
                        if not pos["trail_active"] and pos["peak"] <= pos["entry"] - pos["trail_dist"]:
                            pos["trail_active"] = True
                        eff = min(pos["stop"], pos["peak"] + pos["trail_dist"]) if pos["trail_active"] else pos["stop"]
                        if hi_seg >= eff:
                            close_position(max(eff, a), ts[i], "trail" if pos["trail_active"] else "stop", False)
                            break
                    else:
                        if hi_seg >= pos["stop"]:
                            close_position(max(pos["stop"], a), ts[i], "stop", False)
                            break
                        if lo_seg <= pos["limit"]:
                            close_position(pos["limit"], ts[i], "target", False)
                            break

        if ts[i] < start_ts:
            continue
        long_sig, short_sig = bar_signals(i)
        delay = cfg.get("fill_delay_bars", 0)
        if long_sig and (pos is None or pos["dir"] == "short"):
            pending = {"action": "long", "atr": atr_v[i], "flip": pos is not None,
                       "signal_ts": ts[i], "wait": delay}
        elif short_sig and (pos is None or pos["dir"] == "long"):
            pending = {"action": "short", "atr": atr_v[i], "flip": pos is not None,
                       "signal_ts": ts[i], "wait": delay}

    wins = [t for t in trades if t["pnl"] > 0]
    gross_p = sum(t["pnl"] for t in wins)
    gross_l = abs(sum(t["pnl"] for t in trades if t["pnl"] <= 0))
    pf = (gross_p / gross_l) if gross_l > 0 else (float("inf") if gross_p > 0 else 0.0)
    eq = cfg["initial_capital"]
    peak = eq
    max_dd = 0.0
    for t in trades:
        eq += t["pnl"]
        peak = max(peak, eq)
        max_dd = max(max_dd, peak - eq)

    return {
        "pair": pair, "tf_seconds": tf_seconds,
        "start": start_iso, "end": end_iso,
        "n_trades": len(trades), "n_wins": len(wins),
        "win_rate": 100.0 * len(wins) / len(trades) if trades else 0.0,
        "net": equity - cfg["initial_capital"],
        "pf": pf, "max_dd": max_dd,
        "trades": trades,
    }


# ------------------------------ CLI ----------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pair", default="BTC-USD")
    ap.add_argument("--start", default="2024-09-02")
    ap.add_argument("--end", default="2026-09-01")
    ap.add_argument("--tf", default="1h", choices=["1h", "4h"])
    ap.add_argument("--trailing", action="store_true")
    args = ap.parse_args()

    cfg = default_cfg()
    cfg["use_trailing"] = args.trailing
    tf_s = 3600 if args.tf == "1h" else 14400
    if args.tf == "4h":
        cfg["htf_seconds"] = 86400  # daily HTF for 4h charts

    conn = sqlite3.connect(DB_PATH)
    r = backtest(conn, args.pair, cfg, args.start, args.end, tf_s)

    print(f"\n=== SMRT V4.1 — {r['pair']} {args.tf} {r['start']} → {r['end']} "
          f"trailing={'ON' if args.trailing else 'OFF'} ===")
    if r["n_trades"] == 0:
        print("trades=0")
        return
    print(f"trades={r['n_trades']}  wins={r['n_wins']}  win_rate={r['win_rate']:.1f}%")
    print(f"net_profit={r['net']:.2f}  profit_factor={r['pf']:.2f}  max_drawdown={r['max_dd']:.2f}")
    print("\n--- trade list (compare with TradingView List of Trades) ---")
    for t in r["trades"]:
        e = datetime.fromtimestamp(t["entry_ts"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        x = datetime.fromtimestamp(t["exit_ts"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        print(f"{t['dir']:5s}  in {e} @ {t['entry']:.2f}   "
              f"out {x} @ {t['exit']:.2f}  [{t['reason']:8s}]  pnl {t['pnl']:+.2f}")

    conn.execute("""CREATE TABLE IF NOT EXISTS backtests (
        id INTEGER PRIMARY KEY AUTOINCREMENT, run_at TEXT, pair TEXT,
        start_iso TEXT, end_iso TEXT, trailing INTEGER, trades INTEGER,
        net REAL, pf REAL, max_dd REAL)""")
    conn.execute(
        "INSERT INTO backtests (run_at, pair, start_iso, end_iso, trailing, trades, net, pf, max_dd)"
        " VALUES (datetime('now'), ?, ?, ?, ?, ?, ?, ?, ?)",
        (args.pair, args.start, args.end, 1 if args.trailing else 0,
         r["n_trades"], r["net"], r["pf"] if r["pf"] != float("inf") else None, r["max_dd"]),
    )
    conn.commit()
    conn.close()


if __name__ == "__main__":
    main()
