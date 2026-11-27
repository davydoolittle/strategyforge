"""
Forward-trade autopsy (governance item 4): a descriptive row for every forward
trade of the two original forward tests, #3214 and #4027. Display only.

**Not an input to any candidate.** Reading winners and losers and then building
a strategy from what they share is the selection bias V2_PROMPT section 9
declines ("descendants derived from autopsies"). No module but web.py may
import this one; a test enforces it.

Per forward trade (the same list health.py's decay monitor uses,
health.forward_trades):
  regime    the daily regime tag at the signal (features.RegimeTagger: trend_up,
            trend_down or chop on the last completed day), the tag F7 and F8 used
  vix_band  the equity regime's VIX band on the latest equity day at or before
            the signal (<50, 50-90, >90: percentile of the past year), if known
  weekday   of the signal, UTC
  hold      hours from entry to exit, or to the newest candle while open
  outcome   win / loss, return on capital at entry, exit reason

  python autopsy.py
"""

import sqlite3
from datetime import datetime, timezone

import features
import health
import paper

STRATEGIES = ("smrt-v41-eth-3214", "core-btc-4027")
NOTE = ("Descriptive only: what each forward trade looked like. Not an input to any candidate, "
        "and too few trades to read anything into (see the forward-test rule, FINDINGS F13).")


def vix_band(conn, day):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='equity_regime'").fetchone():
        return None
    r = conn.execute("SELECT band FROM equity_regime WHERE date<=? ORDER BY date DESC LIMIT 1", (day,)).fetchone()
    return r[0] if r else None


def rows_for(conn, name):
    strat = paper.strategy_by_name(conn, name)
    if not strat:
        return {"strategy": name, "missing": True, "source": None, "trades": []}
    source, _, fwd = health.forward_trades(conn, strat)
    tagger = features.RegimeTagger(conn, strat["pair"])
    newest = conn.execute("SELECT MAX(ts) FROM candles WHERE pair=?", (strat["pair"],)).fetchone()[0]
    out = []
    for t, x in fwd:
        sig = datetime.fromtimestamp(t["signal_ts"], timezone.utc)
        end = t.get("exit_ts") or newest
        out.append({"signal": sig.strftime("%Y-%m-%d %H:%M"), "dir": t["dir"],
                    "regime": tagger.at(t["signal_ts"]), "vix_band": vix_band(conn, sig.date().isoformat()),
                    "weekday": sig.strftime("%a"), "hold_h": round((end - t["entry_ts"]) / 3600, 1),
                    "ret_pct": x,               # health.returns: % of equity before the trade
                    "outcome": "win" if x > 0 else "loss",
                    "reason": t.get("reason") or "open"})
    return {"strategy": name, "missing": False, "source": source, "pair": strat["pair"], "trades": out}


def panel(conn):
    return {"note": NOTE, "strategies": [rows_for(conn, n) for n in STRATEGIES]}


if __name__ == "__main__":
    import main
    conn = sqlite3.connect(f"file:{main.DB_PATH}?mode=ro", uri=True, timeout=main.DB_TIMEOUT_S)
    p = panel(conn)
    for s in p["strategies"]:
        print(f"{s['strategy']} ({s['source']}): {len(s['trades'])} forward trades")
        for r in s["trades"]:
            print("  ", r)
    print(p["note"])
