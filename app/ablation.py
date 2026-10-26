"""
StrategyForge ablation (P2.5) — which parts of a parameter set earn their place.

Random search asks "which of 900 parameter sets looks best?" and pays for it
in the deflated-Sharpe bar. Ablation asks a narrower question about ONE
parameter set: take each filter away (or add it, if it is off) and see
whether the result gets worse. 14 variants, not 900 draws.

Variants (each one change from the candidate's own cfg):
  8 toggles flipped    use_shorts use_htf use_vwap use_volume avoid_chop
                       avoid_late avoid_fake use_trailing
  6 trap thresholds    set to a value that never binds (see TRAPS). A
  removed              threshold whose gating toggle is off changes nothing
                       and is reported 'inactive'.

Every variant runs on the candidate's PINNED IS and OOS windows (P2.1).
Verdict per feature, comparing the run WITH it to the run WITHOUT it:
  earns       PF with > PF without on IS AND on OOS, and the full-window
              (IS+OOS trades) PF gap is >= EARN_MIN_PF_DELTA
  hurts       the reverse: the strategy is better without it, both windows
  decoration  anything else — changes trades but not in a consistent direction
  inactive    identical trades in both windows (the feature never fires)

Then every feature that is on but does not earn its place is switched off
(trap thresholds set to non-binding) in one 'pruned' cfg, which is run too.
Simplicity wins ties (V2_PROMPT §2), so the pruned cfg is the one to feed to
the adversary if it holds up: `--register` stores it as a new candidates row
(ablation_of = the parent id, same pinned windows) and prints the
adversary.py command. A registered row counts as one more trial, as it should.

Caveat: IS PF is not independent of the search — the parent was selected on
it. The OOS half of each comparison is the one that carries information.
DSR here uses n_trials = number of variants run (the ablation's own search),
NOT the 900 trials that found the parent; report it next to the adversary's.

Usage
  python ablation.py --candidate 2716
  python ablation.py --candidate 2716,3214 [--register] [--json]
"""

import argparse
import copy
import json
import os
import sqlite3
import time

import engine
import lineage
import optimizer
import validation as V
import windows as W

DB_PATH = os.environ.get("FORGE_DB", "/data/forge.db")

TOGGLES = ["use_shorts", "use_htf", "use_vwap", "use_volume", "avoid_chop",
           "avoid_late", "avoid_fake", "use_trailing"]
# Turning these OFF is the simpler cfg. use_shorts is a direction, not a
# filter: switching it off throws away trades, so pruning never touches it.
PRUNABLE_TOGGLES = [t for t in TOGGLES if t != "use_shorts"]

# trap threshold -> (value that never binds, toggle that gates it or None, what it guards)
TRAPS = {
    "min_rel_vol":     (0.0, "use_volume", "volume trap: bar volume vs its MA"),
    "max_compression": (0.0, "avoid_chop", "chop trap: EMA-compression arm"),
    "max_dist_ema":    (1e9, "avoid_late", "late trap: distance-from-EMA arm"),
    "max_big_bars":    (10 ** 9, "avoid_late", "late trap: big-bar-count arm"),
    "min_ema_sep":     (0.0, None, "trend gate: fast/slow EMA separation"),
    "adx_threshold":   (0.0, None, "trend gate: ADX strength (+ chop ADX arm)"),
}

EARN_MIN_PF_DELTA = 0.05
PF_CAP = 99.0


def ensure_tables(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS ablation_reports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_at TEXT, candidate_id INTEGER, pair TEXT, tf TEXT,
        report_json TEXT
    )""")
    have = {r[1] for r in conn.execute("PRAGMA table_info(candidates)")}
    if have and "ablation_of" not in have:
        conn.execute("ALTER TABLE candidates ADD COLUMN ablation_of INTEGER")
    conn.commit()


def variants(cfg):
    """[(name, kind, on_in_base, cfg)] — 8 toggle flips + 6 trap removals.
    on_in_base: whether the feature is active in the parent cfg. For a trap
    that means its gating toggle is on (and it is not already non-binding)."""
    out = []
    for t in TOGGLES:
        c2 = copy.deepcopy(cfg)
        c2[t] = not cfg[t]
        out.append((t, "toggle", bool(cfg[t]), c2))
    for p, (off, gate, _) in TRAPS.items():
        c2 = copy.deepcopy(cfg)
        c2[p] = off
        on = cfg[p] != off and (gate is None or bool(cfg[gate]))
        out.append((p, "trap", on, c2))
    return out


def _pf(trades):
    gp = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    gl = -sum(t["pnl"] for t in trades if t["pnl"] <= 0)
    if gl <= 0:
        return PF_CAP if gp > 0 else 0.0
    return min(gp / gl, PF_CAP)


def _sig(trades):
    return tuple((t["entry_ts"], t["exit_ts"], t["dir"]) for t in trades)


def summarize(is_trades, oos_trades, cap, n_trials, sr_var):
    """IS / OOS / full stats for one cfg. full = the IS and OOS trades pooled
    (each window compounds from initial capital), which is what the
    optimizer's two backtests saw — not a single continuous backtest."""
    out = {}
    for name, tr in (("is", is_trades), ("oos", oos_trades)):
        rets = V.trade_returns(tr, cap)
        out[name] = {"n_trades": len(tr), "pf": _pf(tr),
                     "net": sum(t["pnl"] for t in tr), "sr": V.sharpe(rets)}
    rets = V.trade_returns(is_trades, cap) + V.trade_returns(oos_trades, cap)
    skew, kurt = V.moments(rets)
    sr = V.sharpe(rets)
    dsr, bench = V.deflated_sharpe(sr, len(rets), skew, kurt, n_trials, sr_var)
    out["full"] = {"n_trades": len(rets), "pf": _pf(is_trades + oos_trades),
                   "net": out["is"]["net"] + out["oos"]["net"], "sr": sr,
                   "psr": V.probabilistic_sharpe(sr, len(rets), skew, kurt),
                   "dsr": dsr, "expected_max_sr": bench}
    out["sig"] = (_sig(is_trades), _sig(oos_trades))
    return out


def classify(base, var, on_in_base, min_delta=EARN_MIN_PF_DELTA):
    """Verdict on a feature from two summaries: the parent (base) and the
    one-change variant (var). on_in_base says which of them HAS the feature."""
    if base["sig"] == var["sig"]:
        return "inactive"
    with_, without = (base, var) if on_in_base else (var, base)
    d_is = with_["is"]["pf"] - without["is"]["pf"]
    d_oos = with_["oos"]["pf"] - without["oos"]["pf"]
    d_full = with_["full"]["pf"] - without["full"]["pf"]
    if d_is > 0 and d_oos > 0 and d_full >= min_delta:
        return "earns"
    if d_is < 0 and d_oos < 0 and d_full <= -min_delta:
        return "hurts"
    return "decoration"


def prune(cfg, rows):
    """Switch off every feature that is on in cfg and did not earn its place.
    Returns (pruned cfg, list of what was switched off)."""
    c2 = copy.deepcopy(cfg)
    removed = []
    for r in rows:
        if not r["on_in_base"] or r["verdict"] == "earns":
            continue
        if r["kind"] == "toggle" and r["name"] in PRUNABLE_TOGGLES:
            c2[r["name"]] = False
            removed.append(r["name"])
        elif r["kind"] == "trap":
            c2[r["name"]] = TRAPS[r["name"]][0]
            removed.append(r["name"])
    return c2, removed


def active_toggles(cfg):
    return sum(1 for t in PRUNABLE_TOGGLES if cfg.get(t))


def run_cfg(conn, pair, cfg, win, tf_s):
    is_start, split, end = win
    is_r = engine.backtest(conn, pair, cfg, is_start, split, tf_s)
    oos_r = engine.backtest(conn, pair, cfg, split, end, tf_s)
    return is_r, oos_r


def ablate(conn, cid, verbose=True):
    row = conn.execute("SELECT pair, tf, params_json FROM candidates WHERE id=?",
                       (cid,)).fetchone()
    if not row:
        raise SystemExit(f"candidate {cid} not found")
    pair, tf, pj = row
    tf_s = W.tf_seconds(tf)
    win = W.for_candidate(conn, cid)
    cfg = W.pin_cfg(json.loads(pj), win[0], tf_s)
    cap = cfg["initial_capital"]
    sr_var = V.sharpe_variance_from_candidates(conn, pair, tf)[0]
    vs = variants(cfg)
    n_trials = len(vs) + 2          # parent + variants + pruned
    t0 = time.time()

    def run(c):
        is_r, oos_r = run_cfg(conn, pair, c, win, tf_s)
        return summarize(is_r["trades"], oos_r["trades"], cap, n_trials, sr_var), (is_r, oos_r)

    base, _ = run(cfg)
    rows = []
    for name, kind, on, c2 in vs:
        s, _ = run(c2)
        rows.append({"name": name, "kind": kind, "on_in_base": on,
                     "verdict": classify(base, s, on), "stats": s})
        if verbose:
            print(f"  ...{name:16s} {rows[-1]['verdict']}")

    pruned_cfg, removed = prune(cfg, rows)
    pruned = None
    if removed:
        s, raw = run(pruned_cfg)
        pruned = {"removed": removed, "stats": s, "cfg": pruned_cfg, "raw": raw}

    report = {
        "candidate_id": cid, "pair": pair, "tf": tf,
        "windows": {"is_start": win[0], "split": win[1], "end": win[2]},
        "n_trials": n_trials, "sr_variance": sr_var,
        "base": base, "base_active_toggles": active_toggles(cfg),
        "rows": rows,
        "pruned": None if pruned is None else {
            "removed": removed, "stats": pruned["stats"],
            "active_toggles": active_toggles(pruned_cfg),
            "params": {k: v for k, v in pruned_cfg.items() if k != "history_start_ts"}},
        "elapsed_s": round(time.time() - t0, 1),
    }
    ensure_tables(conn)
    conn.execute("INSERT INTO ablation_reports (run_at, candidate_id, pair, tf, report_json)"
                 " VALUES (datetime('now'), ?, ?, ?, ?)",
                 (cid, pair, tf, json.dumps(_strip_sigs(report))))
    conn.commit()
    return report, (pruned["raw"] if pruned else None)


def _strip_sigs(report):
    r = copy.deepcopy(report)
    r["base"].pop("sig", None)
    for x in r["rows"]:
        x["stats"].pop("sig", None)
    if r["pruned"]:
        r["pruned"]["stats"].pop("sig", None)
    return r


def register(conn, parent_id, report, raw):
    """Store the pruned cfg as a candidates row so adversary.py can attack
    it. Returns the new id, or the id of an existing row with the same DNA."""
    return insert_child(conn, parent_id, report["pair"], report["tf"],
                        report["pruned"]["params"], report["windows"], raw, "ablation_of")


CHILD_LINKS = {"ablation_of", "filter_of"}


def insert_child(conn, parent_id, pair, tf, params, w, raw, link_col):
    """Insert a cfg derived from candidate parent_id (pruned by ablation, or
    with a filter added) as a candidates row: same pair/tf/pinned windows as
    the parent, passed by the optimizer's gates, link_col = parent_id.
    raw = (IS result, OOS result) already run on those windows. Returns
    (id, created); an existing row with the same DNA is returned instead."""
    if link_col not in CHILD_LINKS:
        raise ValueError(f"link_col must be one of {sorted(CHILD_LINKS)}")
    dna = lineage.dna_of(params)
    seen = conn.execute("SELECT id FROM candidates WHERE dna=? AND pair=? AND tf=?",
                        (dna, pair, tf)).fetchone()
    if seen:
        return seen[0], False
    is_r, oos_r = raw
    cap = params["initial_capital"]
    is_sr, is_skew, is_kurt = optimizer.trade_stats(is_r["trades"], cap)
    oos_sr = optimizer.trade_stats(oos_r["trades"], cap)[0]
    passed = int(is_r["n_trades"] >= optimizer.IS_MIN_TRADES and is_r["pf"] >= optimizer.IS_MIN_PF
                 and oos_r["n_trades"] >= optimizer.OOS_MIN_TRADES
                 and oos_r["pf"] >= optimizer.OOS_MIN_PF and oos_r["net"] > 0)
    backfilled = conn.execute("SELECT windows_backfilled FROM candidates WHERE id=?",
                              (parent_id,)).fetchone()[0]

    def pf(x):
        return None if x == float("inf") else x

    cur = conn.execute(
        f"""INSERT INTO candidates (run_at, pair, tf, seed, trial, params_json,
           is_trades, is_net, is_pf, is_dd, is_wr,
           oos_trades, oos_net, oos_pf, oos_dd, oos_wr, passed,
           is_start, split_ts, oos_end, windows_backfilled, base_seed, dna,
           is_sharpe, is_skew, is_kurt, oos_sharpe, {link_col})
           VALUES (datetime('now'), ?, ?, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                   ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)""",
        (pair, tf, json.dumps(params),
         is_r["n_trades"], is_r["net"], pf(is_r["pf"]), is_r["max_dd"], is_r["win_rate"],
         oos_r["n_trades"], oos_r["net"], pf(oos_r["pf"]), oos_r["max_dd"], oos_r["win_rate"],
         passed, w["is_start"], w["split"], w["end"], backfilled, dna,
         is_sr, is_skew, is_kurt, oos_sr, parent_id))
    conn.commit()
    return cur.lastrowid, True


def _line(label, s, base=None):
    def d(k, w):
        if base is None:
            return ""
        return f"({s[w][k] - base[w][k]:+.2f})"
    return (f"  {label:22s} IS pf {s['is']['pf']:5.2f}{d('pf', 'is'):8s} n={s['is']['n_trades']:<4d}"
            f" OOS pf {s['oos']['pf']:5.2f}{d('pf', 'oos'):8s} n={s['oos']['n_trades']:<4d}"
            f" full pf {s['full']['pf']:5.2f} SR {s['full']['sr']:.3f}")


def print_report(r):
    print("\n" + "=" * 100)
    print(f"ABLATION — candidate #{r['candidate_id']} {r['pair']} {r['tf']}   "
          f"IS {r['windows']['is_start']} -> {r['windows']['split']} | OOS -> {r['windows']['end']}")
    print("=" * 100)
    b = r["base"]
    print(_line("parent", b))
    print(f"  {'':22s} PSR(SR>0) {b['full']['psr']:.3f}   DSR {b['full']['dsr']:.3f} vs "
          f"E[max SR | {r['n_trials']} variants] = {b['full']['expected_max_sr']:.2f}"
          f"   active toggles {r['base_active_toggles']}")
    print()
    for x in r["rows"]:
        state = "on " if x["on_in_base"] else "off"
        act = ("remove" if x["on_in_base"] else "add") if x["kind"] == "toggle" else "remove"
        label = f"{act} {x['name']}"
        print(_line(label, x["stats"], b) + f"  [{state}] {x['verdict'].upper()}")
    print()
    by = {}
    for x in r["rows"]:
        by.setdefault(x["verdict"], []).append(x["name"] + ("" if x["on_in_base"] else "(off)"))
    for v in ("earns", "hurts", "decoration", "inactive"):
        print(f"  {v:10s}: {', '.join(by.get(v, [])) or '-'}")
    if r["pruned"]:
        p = r["pruned"]
        print()
        print(_line("pruned", p["stats"], b))
        print(f"  {'':22s} switched off: {', '.join(p['removed'])}   active toggles "
              f"{r['base_active_toggles']} -> {p['active_toggles']}   "
              f"PSR {p['stats']['full']['psr']:.3f}  DSR {p['stats']['full']['dsr']:.3f}")
    print(f"\n({r['elapsed_s']}s)")
    print("Read: 'earns' means the strategy is worse without that feature on BOTH the IS and")
    print("the OOS window. 'decoration' changes which trades are taken but not in a consistent")
    print("direction — it is fitted, not structural. IS PF was selected on, so weight OOS. A")
    print("feature marked (off) was tested by ADDING it; 'earns' there is a new hypothesis that")
    print("cost one more trial, not a result. DSR uses only this ablation's trials; the")
    print("adversary's A2 still charges the parent's full search.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidate", required=True, help="id or comma list of ids")
    ap.add_argument("--register", action="store_true",
                    help="store the pruned cfg as a candidates row for adversary.py")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    optimizer.ensure_tables(conn)
    ensure_tables(conn)
    for cid in [int(x) for x in args.candidate.split(",")]:
        rep, raw = ablate(conn, cid, verbose=not args.json)
        if args.json:
            print(json.dumps(_strip_sigs(rep)))
        else:
            print_report(rep)
        if args.register:
            if rep["pruned"] is None:
                print("  nothing to prune: every active feature earns its place; not registered")
                continue
            new_id, created = register(conn, cid, rep, raw)
            print(f"  pruned cfg {'registered' if created else 'already in candidates'} "
                  f"as #{new_id} (ablation_of={cid}). Attack it with:")
            print(f"    python adversary.py --candidate {new_id}")
    conn.close()


if __name__ == "__main__":
    main()
