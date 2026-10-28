"""
StrategyForge engine — the EXECUTION half of a backtest (P5.2).

Which bars are long/short entries is a strategy family's job
(app/strategies/, picked by cfg["family"], default SMRT Trap-Aware V4.1).
Everything that turns a signal into a trade is here and is the same for
every family: signal on bar close, fill at the next open (+ fill_delay_bars),
tick slippage, percent commission, risk-% sizing, ATR stop + R:R target or
ATR trailing, reverse on opposite, history_start_ts warmup pinning, forced
signals= (paper.py), the cfg["filters"] feature hook, and the entries /
open_position / pending_entry outputs. 1h native, 4h by aggregation.

V4.1 is still validated at 73/87 exact trade matches vs TradingView, and
tests/test_regression.py holds every stored result to the cent.

CLI:
  python engine.py --pair BTC-USD --start 2024-09-02 --end 2026-09-01 [--trailing] [--tf 4h]
  python engine.py --family core ...
"""

import argparse
import json
import os
import sqlite3
from datetime import datetime, timezone

import strategies
# indicator maths lives in indicators.py; re-exported so engine.ema() etc. keep working
from indicators import (atr, dmi, donchian_prev, ema, htf_confirmed, rma, rsi,  # noqa: F401
                        sma, true_range, vwap_daily)

DB_PATH = os.environ.get("FORGE_DB", "/data/forge.db")
WARMUP_BARS = 300


def default_cfg(family=strategies.DEFAULT_FAMILY):
    return strategies.get(family).default_cfg()


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


# ------------------------------ backtest -----------------------------------

def backtest(conn, pair, cfg, start_iso, end_iso, tf_seconds=3600, signals=None):
    """Run cfg's strategy family with the given cfg. Returns stats + trades.

    signals (default None = the strategy's own): {bar_ts: "long"|"short"}
    replaces the entry signals, so paper.py can drive positions from the
    TradingView alerts. Fills, sizing, stops, targets, trailing and flips are
    unchanged; a signal on the side already held is ignored, as in Pine."""
    fam = strategies.for_cfg(cfg)    # an unknown family fails before any work
    start_ts = int(datetime.fromisoformat(start_iso).replace(tzinfo=timezone.utc).timestamp())
    end_ts = int(datetime.fromisoformat(end_iso).replace(tzinfo=timezone.utc).timestamp())
    # cfg["history_start_ts"] (default None = all history): ignore candles
    # before it, so indicator warmup is the same as when a candidate's windows
    # were pinned even after older candles are backfilled.
    ts, o, h, l, c, v = load_candles(conn, pair, end_ts, tf_seconds,
                                     cfg.get("history_start_ts"))

    n = len(ts)
    # stops and the trailing distance are sized from this ATR for every family
    atr_v = atr(h, l, c, cfg["atr_len"])
    # first bar a signal is asked for: past warmup and inside [start, end]
    from_i = WARMUP_BARS
    while from_i < n and ts[from_i] < start_ts:
        from_i += 1
    if signals is None:
        sig = fam.signals({"ts": ts, "open": o, "high": h, "low": l, "close": c, "volume": v,
                           "atr": atr_v, "from_i": from_i}, cfg)

    tick = cfg["tick_size"]
    slip = cfg["slippage_ticks"] * tick
    comm = cfg["commission_pct"] / 100.0
    trailing = cfg.get("use_trailing", False)
    trail_atr = cfg.get("trail_atr", 0.0)

    equity = cfg["initial_capital"]
    trades = []
    # every entry DECISION (signal on bar close + position allows it), filled
    # or not by the end of the run. This is the bar where the Pine export
    # calls strategy.entry() and alert(); paper.py reconciles against it.
    entries = []
    pos = None
    pending = None

    # cfg["filters"] (P3.1, default none): shadow features that must hold at
    # the signal bar. Features are only computed when a signal would fire.
    filters = cfg.get("filters") or []
    fc = None

    def filters_pass(i):
        nonlocal fc
        import features  # here, not at top: features imports engine
        if fc is None:
            features.validate_filters(filters)
            fc = features.FeatureComputer(conn, pair, tf_seconds,
                                          cfg.get("history_start_ts"), end_ts)
        return features.check_filters(fc, filters, ts[i])

    def bar_signals(i):
        long_sig, short_sig = sig[i]
        if filters and (long_sig or short_sig) and not filters_pass(i):
            return False, False
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
                    "trail_dist": pending["atr"] * trail_atr,
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
        if signals is None:
            long_sig, short_sig = bar_signals(i)
        else:
            long_sig, short_sig = signals.get(ts[i]) == "long", signals.get(ts[i]) == "short"
        delay = cfg.get("fill_delay_bars", 0)
        if long_sig and (pos is None or pos["dir"] == "short"):
            pending = {"action": "long", "atr": atr_v[i], "flip": pos is not None,
                       "signal_ts": ts[i], "wait": delay}
            entries.append({"ts": ts[i], "side": "long", "close": c[i]})
        elif short_sig and (pos is None or pos["dir"] == "long"):
            pending = {"action": "short", "atr": atr_v[i], "flip": pos is not None,
                       "signal_ts": ts[i], "wait": delay}
            entries.append({"ts": ts[i], "side": "short", "close": c[i]})

    # still open at the last bar: marked at its close as if closed at market
    open_position = None
    if pos is not None:
        px = c[-1] - slip if pos["dir"] == "long" else c[-1] + slip
        gross = (px - pos["entry"]) * pos["qty"] if pos["dir"] == "long" else (pos["entry"] - px) * pos["qty"]
        open_position = {k: pos[k] for k in ("dir", "qty", "entry_ts", "entry", "signal_ts",
                                             "stop", "limit", "trail_active")}
        open_position.update(mark_ts=ts[-1], mark=c[-1],
                             unrealised=gross - (pos["entry"] + px) * pos["qty"] * comm)
    # signalled on the last bar, fills at the next bar's open
    pending_entry = ({"side": pending["action"], "signal_ts": pending["signal_ts"]}
                     if pending is not None else None)

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
        "entries": entries,
        "open_position": open_position,
        "pending_entry": pending_entry,
    }


# ------------------------------ CLI ----------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pair", default="BTC-USD")
    ap.add_argument("--start", default="2024-09-02")
    ap.add_argument("--end", default="2026-09-01")
    ap.add_argument("--tf", default="1h", choices=["1h", "4h"])
    ap.add_argument("--trailing", action="store_true")
    ap.add_argument("--family", default=strategies.DEFAULT_FAMILY, choices=strategies.names())
    args = ap.parse_args()

    fam = strategies.get(args.family)
    cfg = fam.default_cfg()
    cfg["use_trailing"] = args.trailing
    tf_s = 3600 if args.tf == "1h" else 14400
    cfg = fam.for_timeframe(cfg, tf_s)

    conn = sqlite3.connect(DB_PATH)
    r = backtest(conn, args.pair, cfg, args.start, args.end, tf_s)

    print(f"\n=== {fam.name} — {r['pair']} {args.tf} {r['start']} → {r['end']} "
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
