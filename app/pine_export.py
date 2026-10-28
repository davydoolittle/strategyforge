"""
StrategyForge Pine export — turn a candidate's cfg into a Pine Script v6
strategy that mirrors engine.py's execution and the cfg's strategy family's
signal logic (the template is the family's pine_template(), P5.2).

This closes the loop: optimizer -> adversary -> lineage -> TradingView chart.

What it does
  * Emits a complete `strategy()` script with every cfg value baked in as the
    default of an `input.*` so you can still tweak on the chart.
  * Same fill model as the engine: signal on bar close, fill at next open,
    ATR stop + R:R limit (or ATR trailing), risk-% sizing, reverse on
    opposite signal, percent commission + tick slippage.
  * HTF filter uses request.security on the PREVIOUS completed HTF bar
    (lookahead_on + [1]) — the standard non-repainting idiom, and the same
    thing indicators.htf_confirmed() does with `ts // htf_seconds - 1` (V4.1).
  * Adds alert() calls that emit the JSON davesbridge expects.

What it does NOT do
  * It does not promise a bar-for-bar match. Pine's ta.ema seeds from the
    first value; engine.ema seeds from an SMA of the first n. Pine's daily
    VWAP anchor and Coinbase's UTC day usually agree but not always. The
    original V4.1 matched 73/87 trades under these same differences, so
    expect that order of agreement. ALWAYS run the TradingView List of
    Trades against `python engine.py --pair ... ` before trusting an export.
  * It does not publish to TradingView. There is no API for that. Paste it.

Usage
  python pine_export.py --candidate 1033                      # to stdout
  python pine_export.py --candidate 1033 --out /data/exports/  # writes .pine
  python pine_export.py --strategy 1 --out /data/exports/      # by lineage id
"""

import argparse
import json
import os
import sqlite3
from datetime import datetime, timezone

import strategies

DB_PATH = os.environ.get("FORGE_DB", "/data/forge.db")


def _b(v):
    return "true" if v else "false"


def render(cfg, title, source_note):
    """Fill the cfg's family's Pine template (strategies.<family>.pine_template)."""
    if cfg.get("filters"):
        # shadow features (P3.1) come from Python-side data (context feeds,
        # 90-day percentiles) that no template reproduces in Pine
        raise SystemExit(f"cannot export: cfg has feature filters {cfg['filters']}; "
                         f"the Pine template has no equivalent for them")
    fam = strategies.for_cfg(cfg)
    fields = {k: (_b(v) if isinstance(v, bool) else v) for k, v in cfg.items()}
    fields.update(title=title, source_note=source_note,
                  generated=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%MZ"),
                  slippage_int=int(cfg["slippage_ticks"]))
    fields.update(fam.pine_fields(cfg))
    return fam.pine_template().format(**fields)


def load_cfg(conn, candidate_id=None, strategy_id=None):
    if strategy_id is not None:
        row = conn.execute(
            "SELECT name, pair, tf, params_json, candidate_id, status FROM strategies WHERE id=?",
            (strategy_id,)).fetchone()
        if not row:
            raise SystemExit(f"strategy {strategy_id} not found")
        name, pair, tf, pj, cid, status = row
        return json.loads(pj), f"SF {name}", f"lineage strategy #{strategy_id} status={status} (candidate #{cid}, {pair} {tf})"
    row = conn.execute(
        "SELECT pair, tf, params_json, oos_pf, oos_trades FROM candidates WHERE id=?",
        (candidate_id,)).fetchone()
    if not row:
        raise SystemExit(f"candidate {candidate_id} not found")
    pair, tf, pj, oos_pf, oos_tr = row
    return (json.loads(pj), f"SF c{candidate_id} {pair} {tf}",
            f"optimizer candidate #{candidate_id} on {pair} {tf}, stored OOS pf={oos_pf:.2f} over {oos_tr} trades")


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--candidate", type=int)
    g.add_argument("--strategy", type=int)
    ap.add_argument("--out", help="directory to write <title>.pine into; omit for stdout")
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    cfg, title, note = load_cfg(conn, args.candidate, args.strategy)
    script = render(cfg, title, note)
    if args.out:
        os.makedirs(args.out, exist_ok=True)
        fn = os.path.join(args.out, title.replace(" ", "_").replace("/", "-") + ".pine")
        with open(fn, "w") as f:
            f.write(script)
        print(f"wrote {fn}  ({len(script.splitlines())} lines)")
        print("Next: TradingView -> Pine Editor -> paste -> Add to chart -> Strategy Tester ->")
        print("      List of Trades, compare with:  python engine.py --pair <PAIR> --start <IS> --end <END>")
    else:
        print(script)
    conn.close()


if __name__ == "__main__":
    main()
