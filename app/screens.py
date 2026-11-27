"""
P9.4 tracker screens: which S&P 100 names the Tracker panel shows besides the
owner's own picks. Daily, deterministic, display only: a screen picks names to
look at, never a trade. Thresholds are declared in docs/RESEARCH_BACKLOG.md
("Tracker screens") and shown on screen; a change is a dated DECISION_LOG line.

  momentum         top decile of the S&P 100 by 12-month return / 12-month
                   realised volatility (screen.py's measures), excluding names
                   in the top 10% by 1-month return (reversal risk)
  value_quality    Piotroski F-score >= 7, trailing P/E below the median of its
                   2-digit SIC group in the S&P 100, FCF yield > 0
  insider_cluster  >= 3 open-market purchases (Form 4 code P) by >= 2 distinct
                   insiders, by transaction date, in the last 30 days (Edgar
                   Alert's data, already in insider_txns via insider_feed.py)

Each screen keeps at most CAP names. A name that qualifies gets an active
watchlist row (added_by = the screen) with the values that put it there; an
active screen row whose condition no longer holds is marked lapsed. Rows are
never deleted, and a user's rows are never touched.

  python screens.py
"""

import json
import sqlite3
import statistics
from datetime import date, datetime, timedelta, timezone

import fundamentals as F
import screen as SC
import tracker
import watchlist as WL

CAP = 10
MOMENTUM_DECILE = 0.10
REVERSAL_PCT = 0.10
F_MIN = 7
INSIDER_MIN_BUYS = 3
INSIDER_MIN_PEOPLE = 2
INSIDER_DAYS = 30
SCREENS = {
    "momentum": f"top {MOMENTUM_DECILE:.0%} of the S&P 100 by 12-month return / 12-month volatility, "
                f"excluding the top {REVERSAL_PCT:.0%} by 1-month return; at most {CAP}",
    "value_quality": f"Piotroski F >= {F_MIN}, P/E below its SEC sector's median in the S&P 100, "
                     f"free-cash-flow yield above 0; at most {CAP}",
    "insider_cluster": f">= {INSIDER_MIN_BUYS} open-market purchases by >= {INSIDER_MIN_PEOPLE} different insiders "
                       f"in the last {INSIDER_DAYS} days (transaction dates); at most {CAP}",
}


def momentum(conn, symbols):
    """[(symbol, trigger values)] best first."""
    need = max(SC.HORIZONS.values()) + 1
    scored = []
    for s in symbols:
        m = SC.metrics(SC.closes(conn, s, need))
        if m and m.get("vol_adj_12m") is not None:
            scored.append((s, m))
    if not scored:
        return []
    n = len(scored)
    k = max(1, round(n * MOMENTUM_DECILE))
    top = sorted(scored, key=lambda x: (-x[1]["vol_adj_12m"], x[0]))[:k]
    hot = {s for s, _ in sorted(scored, key=lambda x: (-x[1]["ret_1m"], x[0]))[:max(1, round(n * REVERSAL_PCT))]}
    return [(s, {"score": round(m["vol_adj_12m"], 3), "ret_12m": round(m["ret_12m"], 4),
                 "vol_12m": round(m["vol_12m"], 4), "ret_1m": round(m["ret_1m"], 4), "decile_of": n})
            for s, m in top if s not in hot][:CAP]


def value_quality(metrics):
    """metrics {symbol: fundamentals.derive(...)}; [(symbol, trigger values)]."""
    by_sector = {}
    for m in metrics.values():
        if m.get("pe") is not None and m.get("sector2"):
            by_sector.setdefault(m["sector2"], []).append(m["pe"])
    out = []
    for s, m in metrics.items():
        pes = by_sector.get(m.get("sector2"), [])
        if (m.get("piotroski_f") is None or m["piotroski_f"] < F_MIN or m.get("pe") is None or len(pes) < 2
                or m.get("fcf_yield") is None or m["fcf_yield"] <= 0):
            continue
        med = statistics.median(pes)
        if m["pe"] < med:
            out.append((s, {"piotroski_f": m["piotroski_f"], "pe": round(m["pe"], 2),
                            "sector_median_pe": round(med, 2), "sector": m.get("sic_desc"),
                            "fcf_yield": round(m["fcf_yield"], 4)}))
    return sorted(out, key=lambda x: (-x[1]["piotroski_f"], -x[1]["fcf_yield"], x[0]))[:CAP]


def insider_cluster(conn, symbols, today=None):
    today = today or datetime.now(timezone.utc).date()
    since = (today - timedelta(days=INSIDER_DAYS)).isoformat()
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='insider_txns'").fetchone():
        return []
    want = {s.replace(".", "-"): s for s in symbols}
    rows = conn.execute("SELECT ticker, COUNT(*), COUNT(DISTINCT owner_name), SUM(value), MIN(txn_date), MAX(txn_date)"
                        " FROM insider_txns WHERE code='P' AND txn_date>=? AND txn_date<=? GROUP BY ticker",
                        (since, today.isoformat())).fetchall()
    out = [(want[t], {"buys": n, "insiders": p, "value": round(v or 0.0), "first": a, "last": b})
           for t, n, p, v, a, b in rows if t in want and n >= INSIDER_MIN_BUYS and p >= INSIDER_MIN_PEOPLE]
    return sorted(out, key=lambda x: (-x[1]["buys"], -x[1]["value"], x[0]))[:CAP]


def apply(conn, name, picks, today):
    """Add new picks, lapse active rows of this screen no longer picked."""
    picked = {s for s, _ in picks}
    added = []
    for s, trig in picks:
        rid, created = WL.add(conn, s, name, trig, today=today)
        if created:
            added.append(s)
        else:
            conn.execute("UPDATE watchlist SET trigger_values=? WHERE id=?", (json.dumps(trig), rid))
    lapsed = []
    for rid, t in conn.execute("SELECT id, ticker FROM watchlist WHERE added_by=? AND status='active'",
                               (name,)).fetchall():
        if t not in picked:
            WL.set_status(conn, rid, "lapsed", today)
            lapsed.append(t)
    conn.commit()
    return added, lapsed


def run(conn, today=None, log=print):
    today = today or date.today()
    WL.ensure_watch_table(conn)
    symbols = tracker.active(conn)
    picks = {"momentum": momentum(conn, symbols),
             "value_quality": value_quality(F.latest_metrics(conn)),
             "insider_cluster": insider_cluster(conn, symbols, today)}       # compute first, then write
    for name, p in picks.items():
        added, lapsed = apply(conn, name, p, today.isoformat())
        log(f"screen {name}: {len(p)} names {[s for s, _ in p]}; added {added or '-'}; lapsed {lapsed or '-'}")
    return picks


if __name__ == "__main__":
    import main
    run(sqlite3.connect(main.DB_PATH, timeout=main.DB_TIMEOUT_S))
