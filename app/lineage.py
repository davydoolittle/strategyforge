"""
StrategyForge lineage — strategy DNA, family tree, and the promotion ladder.

A *candidate* is one row the optimizer produced.  A *strategy* is a candidate
that someone decided to keep track of: it gets a name, a parent, a DNA hash
and a status that can only move UP the ladder when evidence is attached.

Ladder (from the APEX spec, section 12):
    hypothesis -> backtest_winner -> oos_validated -> paper_validated -> approved

Rules enforced here, not by convention:
  * A promotion needs an evidence pointer (an adversarial_reports id, a
    paper-trade log id, or free text for 'approved' which is a HUMAN act).
  * oos_validated requires an adversarial report with verdict SURVIVED for
    the SAME params hash.  REJECTED/FRAGILE cannot be promoted past
    backtest_winner no matter what.
  * approved is never set by code paths that run unattended.  The CLI
    requires --i-am-a-human on that promotion.
  * Nothing is ever deleted; retired strategies keep their history.

DNA = sha256 of the canonical params JSON (sorted keys, engine-relevant keys
only, capital/costs excluded).  Two candidates with identical trading logic
share a DNA even if found by different optimizer runs on different pairs.

Usage
  python lineage.py register --candidate 1033 --name "smrt-v41-btc-1033"
  python lineage.py register --candidate 1333 --parent 1 --note "ETH retune"
  python lineage.py promote --strategy 1 --to oos_validated --evidence report:7
  python lineage.py promote --strategy 1 --to approved --evidence "Dave 2026-10-20" --i-am-a-human
  python lineage.py retire --strategy 1 --note "edge decayed Q4"
  python lineage.py tree
  python lineage.py diff --strategy 2          # vs parent
"""

import argparse
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone

DB_PATH = os.environ.get("FORGE_DB", "/data/forge.db")

LADDER = ["hypothesis", "backtest_winner", "oos_validated", "paper_validated", "approved"]
TERMINAL = "retired"

# keys that change trading behaviour; everything else (capital, fees) is
# an execution assumption and does not change DNA
NON_DNA_KEYS = {"initial_capital", "commission_pct", "slippage_ticks", "tick_size",
                "fill_delay_bars"}


def dna_of(params):
    core = {k: v for k, v in params.items() if k not in NON_DNA_KEYS}
    return hashlib.sha256(json.dumps(core, sort_keys=True).encode()).hexdigest()[:16]


def ensure_tables(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS strategies (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE,
        created_at TEXT,
        parent_id INTEGER,             -- NULL for a root
        candidate_id INTEGER,          -- optimizer row it came from, if any
        pair TEXT, tf TEXT,
        dna TEXT,
        params_json TEXT,
        status TEXT,
        note TEXT
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS promotions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        at TEXT, strategy_id INTEGER,
        from_status TEXT, to_status TEXT,
        evidence TEXT,                 -- 'report:<adversarial_reports.id>' | 'paper:<id>' | text
        by TEXT                        -- 'cli' | 'human' | job name
    )""")
    conn.commit()


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------- register ---

def register(conn, candidate_id, name=None, parent_id=None, note=""):
    row = conn.execute(
        "SELECT pair, tf, params_json, passed FROM candidates WHERE id=?",
        (candidate_id,)).fetchone()
    if not row:
        raise SystemExit(f"candidate {candidate_id} not found")
    pair, tf, pj, passed = row
    params = json.loads(pj)
    dna = dna_of(params)
    name = name or f"c{candidate_id}-{pair.split('-')[0].lower()}-{tf}"
    status = "backtest_winner" if passed else "hypothesis"
    if parent_id is not None:
        if not conn.execute("SELECT 1 FROM strategies WHERE id=?", (parent_id,)).fetchone():
            raise SystemExit(f"parent strategy {parent_id} not found")
    dup = conn.execute("SELECT id, name FROM strategies WHERE dna=?", (dna,)).fetchone()
    if dup:
        print(f"note: DNA {dna} already registered as strategy #{dup[0]} '{dup[1]}' "
              f"(same trading logic). Registering anyway as a separate lineage node.")
    cur = conn.execute(
        "INSERT INTO strategies (name, created_at, parent_id, candidate_id, pair, tf, dna,"
        " params_json, status, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (name, now(), parent_id, candidate_id, pair, tf, dna, json.dumps(params), status, note))
    sid = cur.lastrowid
    conn.execute(
        "INSERT INTO promotions (at, strategy_id, from_status, to_status, evidence, by)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (now(), sid, None, status, f"candidate:{candidate_id} passed={passed}", "cli"))
    conn.commit()
    print(f"registered strategy #{sid} '{name}'  dna={dna}  status={status}")
    return sid


# -------------------------------------------------------------- promote ---

def _latest_report(conn, params_json):
    """Most recent adversarial report whose params match this strategy's DNA."""
    want = dna_of(json.loads(params_json))
    rows = conn.execute(
        "SELECT id, verdict, report_json FROM adversarial_reports ORDER BY id DESC").fetchall()
    for rid, verdict, rj in rows:
        rep = json.loads(rj)
        if dna_of(rep["params"]) == want:
            return rid, verdict
    return None, None


def promote(conn, strategy_id, to_status, evidence, human=False, by="cli"):
    row = conn.execute(
        "SELECT status, params_json, name FROM strategies WHERE id=?", (strategy_id,)).fetchone()
    if not row:
        raise SystemExit(f"strategy {strategy_id} not found")
    cur_status, pj, name = row
    if cur_status == TERMINAL:
        raise SystemExit(f"strategy #{strategy_id} is retired; promotions are closed")
    if to_status not in LADDER:
        raise SystemExit(f"unknown status {to_status}; ladder is {LADDER}")
    if LADDER.index(to_status) != LADDER.index(cur_status) + 1:
        raise SystemExit(f"can only move one rung: {cur_status} -> {LADDER[LADDER.index(cur_status) + 1]}")
    if not evidence:
        raise SystemExit("promotion requires --evidence")

    if to_status == "oos_validated":
        if not evidence.startswith("report:"):
            raise SystemExit("oos_validated needs evidence 'report:<adversarial_reports.id>'")
        rid = int(evidence.split(":", 1)[1])
        rep = conn.execute(
            "SELECT verdict, report_json FROM adversarial_reports WHERE id=?", (rid,)).fetchone()
        if not rep:
            raise SystemExit(f"adversarial report {rid} not found")
        verdict, rj = rep
        if dna_of(json.loads(rj)["params"]) != dna_of(json.loads(pj)):
            raise SystemExit(f"report {rid} is for different params (DNA mismatch)")
        if verdict != "SURVIVED":
            raise SystemExit(f"report {rid} verdict is {verdict}; only SURVIVED can promote")
    if to_status == "paper_validated" and not evidence.startswith("paper:"):
        raise SystemExit("paper_validated needs evidence 'paper:<id>' from the paper-trade log")
    if to_status == "approved":
        if not human:
            raise SystemExit("approved is a human decision: re-run with --i-am-a-human")
        by = "human"

    conn.execute("UPDATE strategies SET status=? WHERE id=?", (to_status, strategy_id))
    conn.execute(
        "INSERT INTO promotions (at, strategy_id, from_status, to_status, evidence, by)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (now(), strategy_id, cur_status, to_status, evidence, by))
    conn.commit()
    print(f"strategy #{strategy_id} '{name}': {cur_status} -> {to_status}  (evidence {evidence})")


def retire(conn, strategy_id, note):
    row = conn.execute("SELECT status, name FROM strategies WHERE id=?", (strategy_id,)).fetchone()
    if not row:
        raise SystemExit(f"strategy {strategy_id} not found")
    conn.execute("UPDATE strategies SET status=?, note=COALESCE(note,'') || ? WHERE id=?",
                 (TERMINAL, f" | retired: {note}", strategy_id))
    conn.execute(
        "INSERT INTO promotions (at, strategy_id, from_status, to_status, evidence, by)"
        " VALUES (?, ?, ?, ?, ?, ?)", (now(), strategy_id, row[0], TERMINAL, note, "cli"))
    conn.commit()
    print(f"strategy #{strategy_id} '{row[1]}' retired: {note}")


# ---------------------------------------------------------------- views ---

def tree(conn):
    rows = conn.execute(
        "SELECT id, name, parent_id, pair, tf, dna, status, candidate_id FROM strategies"
        " ORDER BY id").fetchall()
    if not rows:
        print("no strategies registered yet. Try: python lineage.py register --candidate <ID>")
        return
    kids = {}
    for r in rows:
        kids.setdefault(r[2], []).append(r)

    def walk(parent, depth):
        for r in kids.get(parent, []):
            sid, name, _, pair, tf, dna, status, cid = r
            rid, verdict = _latest_report(conn, conn.execute(
                "SELECT params_json FROM strategies WHERE id=?", (sid,)).fetchone()[0])
            adv = f"adv#{rid}={verdict}" if rid else "no adversarial report"
            print(f"{'  ' * depth}#{sid} {name}  [{pair} {tf}]  {status:16s} dna={dna}  "
                  f"from c{cid}  {adv}")
            walk(sid, depth + 1)

    walk(None, 0)


def diff(conn, strategy_id):
    row = conn.execute(
        "SELECT name, parent_id, params_json FROM strategies WHERE id=?", (strategy_id,)).fetchone()
    if not row:
        raise SystemExit(f"strategy {strategy_id} not found")
    name, pid, pj = row
    if pid is None:
        print(f"#{strategy_id} '{name}' is a root; nothing to diff against")
        return
    prow = conn.execute("SELECT name, params_json FROM strategies WHERE id=?", (pid,)).fetchone()
    a, b = json.loads(prow[1]), json.loads(pj)
    print(f"#{strategy_id} '{name}'  vs parent #{pid} '{prow[0]}'")
    changed = 0
    for k in sorted(set(a) | set(b)):
        if a.get(k) != b.get(k):
            print(f"  {k:18s} {a.get(k)!s:>10s} -> {b.get(k)!s}")
            changed += 1
    print(f"  ({changed} parameters differ)")


def history(conn, strategy_id):
    for at, fs, ts, ev, by in conn.execute(
            "SELECT at, from_status, to_status, evidence, by FROM promotions"
            " WHERE strategy_id=? ORDER BY id", (strategy_id,)):
        print(f"  {at}  {fs or '-':16s} -> {ts:16s}  {by:6s}  {ev}")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("register")
    r.add_argument("--candidate", type=int, required=True)
    r.add_argument("--name")
    r.add_argument("--parent", type=int)
    r.add_argument("--note", default="")
    p = sub.add_parser("promote")
    p.add_argument("--strategy", type=int, required=True)
    p.add_argument("--to", required=True, choices=LADDER)
    p.add_argument("--evidence", required=True)
    p.add_argument("--i-am-a-human", action="store_true")
    x = sub.add_parser("retire")
    x.add_argument("--strategy", type=int, required=True)
    x.add_argument("--note", required=True)
    sub.add_parser("tree")
    d = sub.add_parser("diff")
    d.add_argument("--strategy", type=int, required=True)
    h = sub.add_parser("history")
    h.add_argument("--strategy", type=int, required=True)
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    ensure_tables(conn)
    if args.cmd == "register":
        register(conn, args.candidate, args.name, args.parent, args.note)
    elif args.cmd == "promote":
        promote(conn, args.strategy, args.to, args.evidence, human=args.i_am_a_human)
    elif args.cmd == "retire":
        retire(conn, args.strategy, args.note)
    elif args.cmd == "tree":
        tree(conn)
    elif args.cmd == "diff":
        diff(conn, args.strategy)
    elif args.cmd == "history":
        history(conn, args.strategy)
    conn.close()


if __name__ == "__main__":
    main()
