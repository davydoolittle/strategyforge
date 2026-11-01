"""
Insider activity from Edgar Alert, for stocks and crypto-linked companies.

Pulls Edgar's read-only /api/export into forge.db, then sends one ntfy digest
of what is new. Stage 1 of the plan: this reports what insiders did and makes no
prediction. Every line says UNTESTED until a pre-registered study earns more.

Nothing here writes to Edgar. Sales are stored but not sent: most large-company
selling is routine. If Edgar cannot be reached, or has stopped polling, the
digest says so, because it once sat dead for seven weeks without anyone knowing.

The topic is NTFY_TOPIC in insider_ntfy.env next to forge.db, kept apart from the
TSP topic.

  python insider_feed.py             # pull and print the digest, send nothing
  python insider_feed.py --notify    # pull and send (the daily job)
"""

import argparse
import json
import os
import sqlite3
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

import main

EDGAR_URL = os.environ.get("EDGAR_URL", "http://192.168.68.71:8433")
NTFY_SERVER = "https://ntfy.sh"
PAGE = 1000
BATCH_ROWS = 100
MIN_BUY_USD = 100_000       # an open-market buy below this is not worth a message
MAX_AGE_DAYS = 14           # older activity is history, recorded but not sent
MAX_LINES = 12
STALE_POLL_S = 6 * 3600
BUY_CODE = "P"              # open-market purchase; awards and exercises are not buys
# Crypto tokens have no insiders. These listed companies are the only link.
CRYPTO_LINKED = {"COIN", "MSTR", "MARA", "RIOT", "CLSK"}
TAG = "UNTESTED, no prediction"

TXN_COLS = ("id", "accession", "ticker", "owner_name", "owner_title", "is_director", "is_officer",
            "is_ten_pct", "txn_date", "code", "acq_disp", "shares", "price", "value", "direction",
            "is_signal", "filing_date")


def ensure_tables(conn):
    conn.execute(f"""CREATE TABLE IF NOT EXISTS insider_txns (
        id INTEGER PRIMARY KEY,         -- Edgar's id, the pull cursor
        {", ".join(c + " TEXT" if c in ("accession", "ticker", "owner_name", "owner_title",
                                        "txn_date", "code", "acq_disp", "direction", "filing_date")
                   else c + " REAL" for c in TXN_COLS[1:])},
        notified INTEGER DEFAULT 0
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS insider_clusters (
        cluster_key TEXT PRIMARY KEY, ticker TEXT, window_start TEXT, window_end TEXT,
        insider_count INTEGER, buyers TEXT, buy_count INTEGER, total_value REAL, created_at INTEGER,
        notified INTEGER DEFAULT 0
    )""")
    conn.commit()


def http_get(url):
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.loads(resp.read())


def pull(conn, base=EDGAR_URL, get=http_get):
    """Fetch every page first, then write in batches. Returns the last page's
    payload, which carries Edgar's health and the full cluster list."""
    after = conn.execute("SELECT COALESCE(MAX(id), 0) FROM insider_txns").fetchone()[0]
    rows = []
    while True:
        page = get(f"{base}/api/export?" + urllib.parse.urlencode({"after": after, "limit": PAGE}))
        rows += page["txns"]
        if len(page["txns"]) < PAGE:
            break
        after = page["txns"][-1]["id"]
    cols = ", ".join(TXN_COLS)
    marks = ", ".join("?" * len(TXN_COLS))
    data = [tuple(r.get(c) for c in TXN_COLS) for r in rows]
    for i in range(0, len(data), BATCH_ROWS):
        conn.executemany(f"INSERT OR IGNORE INTO insider_txns ({cols}) VALUES ({marks})",
                         data[i:i + BATCH_ROWS])
        conn.commit()
    for c in page["clusters"]:
        conn.execute("INSERT OR IGNORE INTO insider_clusters (cluster_key, ticker, window_start,"
                     " window_end, insider_count, buyers, buy_count, total_value, created_at)"
                     " VALUES (:cluster_key, :ticker, :window_start, :window_end, :insider_count,"
                     " :buyers, :buy_count, :total_value, :created_at)", c)
    conn.commit()
    return page


def _name(ticker):
    return f"{ticker} (crypto-linked)" if ticker in CRYPTO_LINKED else ticker


def _money(x):
    return f"${x / 1e6:.1f}M" if x >= 1e6 else f"${x / 1e3:.0f}K"


def digest(conn, today=None):
    """Lines for what is new and recent. Anything older than MAX_AGE_DAYS is only
    marked, so a backfill can never send old news."""
    today = today or datetime.now(timezone.utc).date()
    since = (today - timedelta(days=MAX_AGE_DAYS)).isoformat()
    conn.execute("UPDATE insider_txns SET notified=1 WHERE notified=0 AND COALESCE(filing_date, txn_date)<?",
                 (since,))
    conn.execute("UPDATE insider_clusters SET notified=1 WHERE notified=0 AND window_end<?", (since,))
    conn.commit()
    clusters = conn.execute("SELECT cluster_key, ticker, insider_count, total_value, window_end"
                            " FROM insider_clusters WHERE notified=0 ORDER BY total_value DESC").fetchall()
    buys = conn.execute("SELECT id, ticker, owner_name, owner_title, value, txn_date FROM insider_txns"
                        " WHERE notified=0 AND code=? AND value>=? ORDER BY value DESC",
                        (BUY_CODE, MIN_BUY_USD)).fetchall()
    lines = [f"CLUSTER {_name(t)}: {n} insiders bought {_money(v or 0)}, through {end}"
             for _, t, n, v, end in clusters]
    for _, t, owner, title, v, d in buys:
        who = f"{owner} ({title})" if title else owner
        lines.append(f"{_name(t)}: {who} bought {_money(v)} on {d}")
    more = len(lines) - MAX_LINES
    lines = lines[:MAX_LINES] + ([f"...and {more} more"] if more > 0 else [])
    return lines


def mark_sent(conn):
    """Everything in the window counts as seen, sent or below the bar."""
    conn.execute("UPDATE insider_txns SET notified=1 WHERE notified=0")
    conn.execute("UPDATE insider_clusters SET notified=1 WHERE notified=0")
    conn.commit()


def health_problem(page, now_ms=None):
    last = page.get("last_poll_ok")
    now_ms = now_ms if now_ms is not None else datetime.now(timezone.utc).timestamp() * 1000
    if last is None:
        return "Edgar has no successful poll on record"
    hours = (now_ms - last) / 3_600_000
    return f"Edgar last polled {hours:.0f} hours ago" if hours * 3600 > STALE_POLL_S else None


def load_topic(path=None):
    path = path or os.path.join(os.path.dirname(os.path.abspath(main.DB_PATH)), "insider_ntfy.env")
    if not os.path.exists(path):
        raise SystemExit(f"missing {path}: needs NTFY_TOPIC")
    with open(path) as f:
        for line in f:
            k, _, v = line.strip().partition("=")
            if k == "NTFY_TOPIC" and v:
                return v
    raise SystemExit(f"{path} has no NTFY_TOPIC")


def send(topic, title, body):
    req = urllib.request.Request(f"{NTFY_SERVER}/{topic}", data=body.encode(),
                                 headers={"Title": title, "User-Agent": "strategyforge/1.0"})
    urllib.request.urlopen(req, timeout=15)


def run(conn, notify, base=EDGAR_URL, get=http_get, push=send, topic=None):
    ensure_tables(conn)
    try:
        page = pull(conn, base, get)
    except OSError as e:
        msg = f"Edgar could not be reached at {base}: {e}"
        print(msg)
        if notify:
            push(topic or load_topic(), "Edgar is down", msg)
        return msg
    problem = health_problem(page)
    lines = digest(conn)
    body = "\n".join(lines + ([problem] if problem else []) + ([TAG] if lines else []))
    print(body or "Nothing new. Read: no recent clusters or large open-market buys.")
    if notify and body:
        push(topic or load_topic(), "Insider activity" if lines else "Edgar problem", body)
    if notify:
        mark_sent(conn)
    return body


def main_cli():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--notify", action="store_true", help="send the digest and mark it sent")
    args = ap.parse_args()
    conn = sqlite3.connect(main.DB_PATH, timeout=main.DB_TIMEOUT_S)
    run(conn, args.notify)


if __name__ == "__main__":
    main_cli()
