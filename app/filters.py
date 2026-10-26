"""
StrategyForge filters (P3.1) — put a shadow feature in front of a candidate.

A filter is {"feature": name, "op": ">=" | "<=", "value": x} in
cfg["filters"]; engine.backtest lets a signal through only if every filter
holds at the signal bar (features.check_filters). This CLI takes a candidate,
adds one or more filters, and runs parent and child on the parent's PINNED IS
and OOS windows (P2.1), side by side.

With --register the child becomes:
  * a candidates row (filter_of = parent id, same pinned windows), so
    adversary.py can attack it and A2 counts it as one more trial, and
  * a lineage strategy whose parent_id is the parent candidate's strategy
    (registered first if it is not yet), note "filter: vol_of_vol >= 12".

Which feature, which direction and which threshold is not decided here:
feature_evals.py (P3.2) says when a feature has earned a trial. Choosing the
threshold after looking at the data is itself a search; say how many values
you tried.

Usage
  python filters.py --candidate 2716 --filter "vol_of_vol>=12"
  python filters.py --candidate 2716 --filter "vol_of_vol>=12" --register
"""

import argparse
import copy
import json
import os
import re
import sqlite3

import ablation
import features
import lineage
import optimizer
import windows as W

DB_PATH = os.environ.get("FORGE_DB", "/data/forge.db")

_FILTER_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*(>=|<=)\s*(-?[0-9.]+(?:[eE][-+]?[0-9]+)?)\s*$")


def ensure_tables(conn):
    have = {r[1] for r in conn.execute("PRAGMA table_info(candidates)")}
    if have and "filter_of" not in have:
        conn.execute("ALTER TABLE candidates ADD COLUMN filter_of INTEGER")
    conn.commit()


def parse_filter(text):
    m = _FILTER_RE.match(text)
    if not m:
        raise ValueError(f"filter must look like 'vol_of_vol>=12', got {text!r}")
    return {"feature": m.group(1), "op": m.group(2), "value": float(m.group(3))}


def with_filters(cfg, new):
    c2 = copy.deepcopy(cfg)
    have = c2.get("filters") or []
    c2["filters"] = have + [f for f in new if f not in have]
    features.validate_filters(c2["filters"])
    return c2


def _stats(r):
    return {"n_trades": r["n_trades"], "net": r["net"],
            "pf": None if r["pf"] == float("inf") else r["pf"], "max_dd": r["max_dd"]}


def compare(conn, cid, new_filters):
    row = conn.execute("SELECT pair, tf, params_json FROM candidates WHERE id=?",
                       (cid,)).fetchone()
    if not row:
        raise SystemExit(f"candidate {cid} not found")
    pair, tf, pj = row
    tf_s = W.tf_seconds(tf)
    win = W.for_candidate(conn, cid)
    parent = W.pin_cfg(json.loads(pj), win[0], tf_s)
    child = with_filters(parent, new_filters)
    p_raw = ablation.run_cfg(conn, pair, parent, win, tf_s)
    c_raw = ablation.run_cfg(conn, pair, child, win, tf_s)
    blocked = {}
    for name, (p, c) in zip(("is", "oos"), zip(p_raw, c_raw)):
        kept = {t["signal_ts"] for t in c["trades"]}
        gone = [t for t in p["trades"] if t["signal_ts"] not in kept]
        blocked[name] = {"n": len(gone), "net": sum(t["pnl"] for t in gone),
                         "n_wins": sum(1 for t in gone if t["pnl"] > 0)}
    report = {
        "candidate_id": cid, "pair": pair, "tf": tf,
        "windows": {"is_start": win[0], "split": win[1], "end": win[2]},
        "filters": child["filters"],
        "parent": {"is": _stats(p_raw[0]), "oos": _stats(p_raw[1])},
        "child": {"is": _stats(c_raw[0]), "oos": _stats(c_raw[1])},
        "blocked": blocked,
        "params": {k: v for k, v in child.items() if k != "history_start_ts"},
    }
    return report, c_raw


def register(conn, report, raw):
    """candidates row + lineage child. Returns (candidate id, strategy id)."""
    cid = report["candidate_id"]
    new_id, created = ablation.insert_child(conn, cid, report["pair"], report["tf"],
                                            report["params"], report["windows"], raw, "filter_of")
    lineage.ensure_tables(conn)
    got = conn.execute("SELECT id FROM strategies WHERE candidate_id=? ORDER BY id LIMIT 1",
                       (new_id,)).fetchone()
    if got:
        return new_id, got[0]
    prow = conn.execute("SELECT id FROM strategies WHERE candidate_id=? ORDER BY id LIMIT 1",
                        (cid,)).fetchone()
    psid = prow[0] if prow else lineage.register(conn, cid, note="parent of a filter child")
    note = "filter: " + ", ".join(features.describe(f) for f in report["filters"])
    sid = lineage.register(conn, new_id, parent_id=psid, note=note)
    return new_id, sid


def _pf(x):
    return "inf" if x is None else f"{x:.2f}"


def print_report(r):
    print("\n" + "=" * 74)
    print(f"FILTER — candidate #{r['candidate_id']} {r['pair']} {r['tf']}   "
          + ", ".join(features.describe(f) for f in r["filters"]))
    print(f"IS {r['windows']['is_start']} -> {r['windows']['split']} | OOS -> {r['windows']['end']}")
    print("=" * 74)
    for w in ("is", "oos"):
        p, c, b = r["parent"][w], r["child"][w], r["blocked"][w]
        print(f"  {w.upper():3s} parent {p['n_trades']:4d}t pf {_pf(p['pf']):>5s} net {p['net']:+8.0f}"
              f"   child {c['n_trades']:4d}t pf {_pf(c['pf']):>5s} net {c['net']:+8.0f}"
              f"   blocked {b['n']}t ({b['n_wins']} winners) net {b['net']:+.0f}")
    print("\nRead: 'blocked' are the parent's trades whose signal the filter stopped.")
    print("A filter that helps blocks trades with negative net in BOTH windows; one")
    print("that only helps IS was fitted. Fewer trades also means a wider PF error")
    print("bar, and the child is one more trial for A2. Nothing here is validated")
    print("until adversary.py has attacked the registered child.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidate", type=int, required=True)
    ap.add_argument("--filter", action="append", required=True,
                    help="e.g. 'vol_of_vol>=12'; repeat for several (all must hold)")
    ap.add_argument("--register", action="store_true",
                    help="store the child as a candidates row and a lineage child")
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    optimizer.ensure_tables(conn)
    ablation.ensure_tables(conn)
    ensure_tables(conn)
    report, raw = compare(conn, args.candidate, [parse_filter(f) for f in args.filter])
    print_report(report)
    if args.register:
        new_id, sid = register(conn, report, raw)
        print(f"\n  child is candidate #{new_id} (filter_of={args.candidate}), lineage strategy "
              f"#{sid}. Attack it with:\n    python adversary.py --candidate {new_id}")
    conn.close()


if __name__ == "__main__":
    main()
