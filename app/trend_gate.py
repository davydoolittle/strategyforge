"""
Phase 16.1 (V2_PROMPT 8k): trend-gated children of the three forward tests.

Each child is its parent's pinned cfg plus cfg["trend_gate"] = {"market":
"BTC-USD"}: a long entry only while the dashboard's crypto trend is Bull, a
short only while it is Bear, no new entry while Flat (engine.gated). Exits,
stops and targets are unchanged. The parents are not touched.

Registering a child adds a candidates row (filter_of = parent, same pinned
windows), which A2 counts as one more trial of the family, and a lineage child.
The registered pass is the full adversary verdict on the child: run
`adversary.py --candidate <child>` after this. Run on a scratch copy first,
as the registration says; a passing child is then registered on the live DB
as a new forward test.

  python trend_gate.py                      # report parents vs children, write nothing
  python trend_gate.py --register           # also store the children
"""

import argparse
import json
import sqlite3

import ablation
import engine
import lineage
import market_trend as MT
import windows as W

PARENTS = (3214, 4027, 5833)
GATE = {"market": "BTC-USD"}
NOTE = "trend gate: BTC-USD dashboard trend, longs in Bull, shorts in Bear, none in Flat (Phase 16.1)"


def child_cfg(parent_cfg):
    return {**parent_cfg, "trend_gate": dict(GATE)}


def _stats(r, cap):
    pf = None if r["pf"] == float("inf") else r["pf"]
    import optimizer
    sr = optimizer.trade_stats(r["trades"], cap)[0] if r["trades"] else None
    return {"n": r["n_trades"], "net": r["net"], "pf": pf, "sr": sr,
            "long": sum(t["dir"] == "long" for t in r["trades"]),
            "short": sum(t["dir"] == "short" for t in r["trades"])}


def state_shares(conn, start, end):
    """Share of the window's days in each BTC trend state (the gate's input)."""
    st = MT.states(MT.daily_closes(conn, GATE["market"], "crypto"))
    days = [s[4] for s in st if start[:10] <= s[0] < end[:10]]
    return {k: (days.count(k) / len(days) if days else 0.0) for k in ("Bull", "Flat", "Bear")}


def compare(conn, cid):
    pair, tf, pj = conn.execute("SELECT pair, tf, params_json FROM candidates WHERE id=?", (cid,)).fetchone()
    tf_s = W.tf_seconds(tf)
    win = W.for_candidate(conn, cid)
    parent = W.pin_cfg(json.loads(pj), win[0], tf_s)
    child = child_cfg(parent)
    p_raw = ablation.run_cfg(conn, pair, parent, win, tf_s)
    c_raw = ablation.run_cfg(conn, pair, child, win, tf_s)
    cap = parent["initial_capital"]
    return {"candidate_id": cid, "pair": pair, "tf": tf,
            "windows": {"is_start": win[0], "split": win[1], "end": win[2]},
            "parent": {"is": _stats(p_raw[0], cap), "oos": _stats(p_raw[1], cap)},
            "child": {"is": _stats(c_raw[0], cap), "oos": _stats(c_raw[1], cap)},
            "oos_states": state_shares(conn, win[1], win[2]),
            "params": {k: v for k, v in child.items() if k != "history_start_ts"}}, c_raw


def register(conn, report, raw):
    import filters
    filters.ensure_tables(conn)          # adds candidates.filter_of on a DB that never ran filters.py
    cid = report["candidate_id"]
    new_id, _ = ablation.insert_child(conn, cid, report["pair"], report["tf"], report["params"],
                                      report["windows"], raw, "filter_of")
    lineage.ensure_tables(conn)
    got = conn.execute("SELECT id FROM strategies WHERE candidate_id=?", (new_id,)).fetchone()
    if got:
        return new_id, got[0]
    psid = conn.execute("SELECT id FROM strategies WHERE candidate_id=? ORDER BY id LIMIT 1", (cid,)).fetchone()
    psid = psid[0] if psid else lineage.register(conn, cid, note="parent of a trend-gated child")
    return new_id, lineage.register(conn, new_id, parent_id=psid, note=NOTE)


def fmt(s):
    pf = "inf" if s["pf"] is None else f"{s['pf']:.2f}"
    sr = "-" if s["sr"] is None else f"{s['sr']:+.3f}"
    return f"{s['n']:4d} trades ({s['long']}L/{s['short']}S)  net {s['net']:+9.0f}  PF {pf:>5}  SR/trade {sr}"


def main_cli(argv=None):
    import main
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--register", action="store_true")
    args = ap.parse_args(argv)
    conn = sqlite3.connect(main.DB_PATH, timeout=main.DB_TIMEOUT_S)
    for cid in PARENTS:
        rep, raw = compare(conn, cid)
        sh = rep["oos_states"]
        print(f"#{cid} {rep['pair']} {rep['tf']}  OOS {rep['windows']['split'][:10]} .. {rep['windows']['end'][:10]}"
              f"  (BTC trend: Bull {sh['Bull']:.0%}, Flat {sh['Flat']:.0%}, Bear {sh['Bear']:.0%})")
        for side in ("is", "oos"):
            print(f"   {side.upper():3} parent {fmt(rep['parent'][side])}")
            print(f"   {side.upper():3} child  {fmt(rep['child'][side])}")
        if args.register:
            new_id, sid = register(conn, rep, raw)
            print(f"   registered child: candidate #{new_id}, lineage strategy #{sid}")
    print("\nRead: the verdict is the adversary's on each child (adversary.py --candidate N), thresholds"
          " unchanged; a child counts as one more trial of its family.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
