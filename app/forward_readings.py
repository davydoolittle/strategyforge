"""
Quarterly readings of every running forward test, for the record. Display only:
nothing here can pass, fail, stop or re-tune a test.

  strategies  the decay monitor's latest row for each strategy marked
              forward_test (#3214, #4027, #5833): forward trades so far, CUSUM
              and whether the alert is on (health.py runs this daily)
  P10.5       month-end Treasury exposure vs cash, IEF and TLT: forward months
              so far of the registered 72 (treasury_month_end.forward), which
              refuses a verdict before month 72

The collector runs it every 91 days (job `forward_readings`) and sends the text
via ntfy (NTFY_TOPIC, default priority), so the owner sees how far each test has
come. The verdicts themselves are read only at each test's registered size.

  python forward_readings.py            # print
  python forward_readings.py --notify   # print and send via ntfy
"""

import argparse
import os
import sqlite3

import health
import treasury_month_end as TM
import trend_switch as TS

TITLE = "StrategyForge forward tests: quarterly reading"
FOOT = "For the record only: no test is judged before its registered size."


def strategy_lines(conn):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='health'").fetchone():
        return ["decay monitor: no rows yet"]
    rows = conn.execute(
        "SELECT s.name, h.day, h.n_forward, h.cusum, h.alert, h.source FROM health h JOIN strategies s"
        " ON s.id = h.strategy_id WHERE s.forward_test = 1 AND h.day = (SELECT MAX(day) FROM health"
        " WHERE strategy_id = h.strategy_id) ORDER BY s.id").fetchall()
    out = []
    for name, day, n, cusum, alert, source in rows:
        c = "-" if cusum is None else f"{cusum:+.2f}"
        out.append(f"{name}: {n or 0} forward trades ({source}), CUSUM {c}"
                   f"{', DECAY ALERT' if alert else ''} (as of {day})")
    return out or ["no strategy is marked forward_test"]


def treasury_lines(conn):
    cy = TS.cash_yield(conn)
    out = []
    for sym in TM.FUNDS:
        r = TM.forward(conn, cy, sym)
        if r["months"] == 0:
            out.append(f"P10.5 {sym}: no full forward month yet (first is {TM.FORWARD_START})")
        elif r["verdict"] is None:
            so_far = f", excess Sharpe so far {r['ann_sr']:+.2f}" if r.get("ann_sr") is not None else ""
            out.append(f"P10.5 {sym}: month {r['months']} of {TM.FORWARD_MONTHS}{so_far}")
        else:
            out.append(f"P10.5 {sym}: {TM.FORWARD_MONTHS} months reached: run treasury_month_end.py --forward"
                       " for the verdict")
    return out


def reading(conn):
    return "\n".join(strategy_lines(conn) + treasury_lines(conn) + [FOOT])


def main_cli(argv=None):
    import main
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--notify", action="store_true")
    ap.add_argument("--ntfy-topic", default=os.environ.get("NTFY_TOPIC", ""))
    args = ap.parse_args(argv)
    conn = sqlite3.connect(f"file:{main.DB_PATH}?mode=ro", uri=True, timeout=main.DB_TIMEOUT_S)
    text = reading(conn)
    print(text)
    if args.notify:
        if not args.ntfy_topic:
            print("!! NTFY_TOPIC not set: reading NOT sent")
        elif health.send_ntfy(args.ntfy_topic, TITLE, text, priority="default", tags="memo"):
            print("sent via ntfy")
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
