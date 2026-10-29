"""
Read-only dashboard API (P7.1). Standard library only (http.server), like the
rest of the repo, so it runs in plain python:3.12-slim with no image to build.

  python web.py                       # 0.0.0.0:8091, FORGE_DB read-only
  WEB_PORT=8092 python web.py

Endpoints (GET only; every request opens forge.db with SQLite mode=ro, so
nothing here can write):
  /api/strategies                     lineage rows + latest verdict + promotions
  /api/candidates?tf=&passed=&pair=&family=&reported=&limit=
                                      candidates with their latest adversary
                                      numbers (DSR, walk-forward, plateau)
  /api/reports/{id}[?equity=1]        one adversarial report; equity=1 re-runs
                                      the candidate on its pinned windows for
                                      the equity and drawdown curve
  /api/health                         decay-monitor rows, oldest first
  /api/paper/{strategy_id}            reconciliation, positions, daily summary
  /                                   the dashboard (static/index.html, P7.2)

Auth (P7.3): every /api/ request needs "Authorization: Bearer $WEB_TOKEN"
(constant-time compare; 401 otherwise). web.py will not start without a
WEB_TOKEN of at least 16 characters. The page at / carries no data and is
served without a token; it asks for the token on the first 401 and keeps it
in that browser's localStorage. LAN only: no Cloudflare route, no router
port forward; remote access is Tailscale.
"""

import hmac
import json
import math
import os
import re
import sqlite3
import sys
import threading
import traceback
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import engine
import main
import windows as W

PORT = int(os.environ.get("WEB_PORT", "8091"))
MIN_TOKEN_LEN = 16
BIND = os.environ.get("WEB_BIND", "0.0.0.0")
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com; "
       "style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; "
       "frame-ancestors 'none'")
DEFAULT_LIMIT = 1000
MAX_LIMIT = 10000


class HttpError(Exception):
    def __init__(self, status, msg):
        super().__init__(msg)
        self.status = status


def connect(db_path=None):
    """Read-only connection: a write raises sqlite3.OperationalError."""
    path = db_path or main.DB_PATH
    if not os.path.exists(path):
        raise HttpError(503, f"database not found: {path}")
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=main.DB_TIMEOUT_S,
                           check_same_thread=False)


def clean(x):
    """JSON-safe: inf / nan -> None (an infinite PF has no JSON spelling)."""
    if isinstance(x, float):
        return x if math.isfinite(x) else None
    if isinstance(x, dict):
        return {k: clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [clean(v) for v in x]
    return x


def rows(conn, sql, args=()):
    cur = conn.execute(sql, args)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def has_table(conn, name):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (name,)).fetchone() is not None


# ---------------------------------------------------------------- queries ---

def report_summary(report_json):
    """The numbers the candidate table sorts on, from one report's JSON."""
    try:
        r = json.loads(report_json)
    except (TypeError, ValueError):
        return {}
    a = r.get("attacks", {})
    return {"dsr": a.get("A2", {}).get("dsr_pair_tf"),
            "sharpe_per_trade": a.get("A2", {}).get("sharpe_per_trade"),
            "wf_consistency": a.get("A3", {}).get("consistency"),
            "plateau": a.get("A4", {}).get("plateau_score"),
            "mc_dd_p95_pct": a.get("A8", {}).get("dd_p95_pct_of_capital")}


def latest_reports(conn):
    """candidate_id -> its newest adversarial report row."""
    if not has_table(conn, "adversarial_reports"):
        return {}
    out = {}
    for r in rows(conn, "SELECT id, candidate_id, run_at, verdict, hard_fails, soft_fails,"
                        " report_json FROM adversarial_reports ORDER BY id"):
        out[r["candidate_id"]] = r
    return out


def strategies(conn):
    if not has_table(conn, "strategies"):
        return []
    reps = latest_reports(conn)
    promos = {}
    if has_table(conn, "promotions"):
        for p in rows(conn, "SELECT * FROM promotions ORDER BY id"):
            promos.setdefault(p["strategy_id"], []).append(p)
    out = []
    for s in rows(conn, "SELECT id, name, created_at, parent_id, candidate_id, pair, tf, family,"
                        " dna, status, note FROM strategies ORDER BY id"):
        rep = reps.get(s["candidate_id"])
        s["report_id"] = rep["id"] if rep else None
        s["verdict"] = rep["verdict"] if rep else None
        s["promotions"] = promos.get(s["id"], [])
        out.append(s)
    return out


def _flag(qs, name):
    v = qs.get(name)
    if v is None:
        return None
    if v in ("1", "true", "yes"):
        return 1
    if v in ("0", "false", "no"):
        return 0
    raise HttpError(400, f"{name} must be 0/1, got {v!r}")


def candidates(conn, qs):
    where, args = [], []
    for col in ("tf", "pair", "family"):
        if qs.get(col):
            where.append(f"{col}=?")
            args.append(qs[col])
    passed = _flag(qs, "passed")
    if passed is not None:
        where.append("passed=?")
        args.append(passed)
    try:
        limit = min(MAX_LIMIT, int(qs.get("limit", DEFAULT_LIMIT)))
    except ValueError:
        raise HttpError(400, "limit must be an integer") from None
    reps = latest_reports(conn)
    reported = _flag(qs, "reported")
    if reported == 1:
        if not reps:
            return []
        where.append(f"id IN ({','.join('?' * len(reps))})")
        args.extend(reps)
    sql = ("SELECT id, run_at, pair, tf, family, seed, trial, passed, is_trades, is_pf, is_net,"
           " is_sharpe, oos_trades, oos_pf, oos_net, oos_dd, oos_sharpe, is_start, split_ts, oos_end"
           " FROM candidates" + (" WHERE " + " AND ".join(where) if where else ""))
    out = rows(conn, sql, args)
    for c in out:
        rep = reps.get(c["id"])
        c["report_id"] = rep["id"] if rep else None
        c["verdict"] = rep["verdict"] if rep else None
        c.update(report_summary(rep["report_json"]) if rep else
                 {"dsr": None, "sharpe_per_trade": None, "wf_consistency": None, "plateau": None,
                  "mc_dd_p95_pct": None})
    if reported == 0:
        out = [c for c in out if c["report_id"] is None]
    # reported first (newest report), then by OOS profit factor
    out.sort(key=lambda c: (c["report_id"] is None, -(c["report_id"] or 0),
                            -(c["oos_pf"] if c["oos_pf"] is not None and math.isfinite(c["oos_pf"]) else 0)))
    return out[:limit]


def report(conn, rid, with_equity=False, db_path=None):
    if not has_table(conn, "adversarial_reports"):
        raise HttpError(404, "no adversarial_reports table")
    r = rows(conn, "SELECT id, run_at, candidate_id, pair, tf, verdict, hard_fails, soft_fails,"
                   " report_json FROM adversarial_reports WHERE id=?", (rid,))
    if not r:
        raise HttpError(404, f"report {rid} not found")
    r = r[0]
    r["report"] = json.loads(r.pop("report_json"))
    # the columns hold counts; the attack codes are in the report itself
    for k in ("hard_fails", "soft_fails"):
        r[f"n_{k}"] = r[k]
        r[k] = r["report"].get(k, [])
    if with_equity:
        r["equity"] = equity_for_candidate(db_path or main.DB_PATH, r["candidate_id"])
    return r


def equity_curve(trades, capital, start_ts):
    """Closed-trade equity and drawdown (% off the running peak), one point
    per trade exit plus the start."""
    eq, peak = float(capital), float(capital)
    pts = [{"ts": start_ts, "equity": eq, "dd_pct": 0.0}]
    for t in sorted(trades, key=lambda t: t["exit_ts"]):
        eq += t["pnl"]
        peak = max(peak, eq)
        pts.append({"ts": t["exit_ts"], "equity": eq,
                    "dd_pct": 100 * (eq / peak - 1) if peak > 0 else 0.0})
    return pts


_equity_lock = threading.Lock()


@lru_cache(maxsize=64)
def _equity_cached(db_path, cid):
    conn = connect(db_path)
    try:
        row = conn.execute("SELECT pair, tf, params_json FROM candidates WHERE id=?",
                           (cid,)).fetchone()
        if not row:
            raise HttpError(404, f"candidate {cid} not found")
        pair, tf, pj = row
        tf_s = W.tf_seconds(tf)
        try:
            is_start, split, end = W.for_candidate(conn, cid)
        except SystemExit as e:
            raise HttpError(409, str(e)) from None
        cfg = W.pin_cfg(json.loads(pj), is_start, tf_s)
        full = engine.backtest(conn, pair, cfg, is_start, end, tf_s)
        return {"pair": pair, "tf": tf, "is_start": is_start, "split": split, "end": end,
                "split_ts": W._ts(split), "n_trades": full["n_trades"], "net": full["net"],
                "points": equity_curve(full["trades"], cfg["initial_capital"], W._ts(is_start))}
    finally:
        conn.close()


def equity_for_candidate(db_path, cid):
    """Pinned windows (P2.1) make the curve deterministic: candles added
    later are outside them, so a computed curve is cached for good."""
    with _equity_lock:
        return _equity_cached(db_path, cid)


def health(conn):
    if not has_table(conn, "health"):
        return []
    return rows(conn, "SELECT h.*, s.name FROM health h LEFT JOIN strategies s ON s.id=h.strategy_id"
                      " ORDER BY h.strategy_id, h.day")


def paper(conn, sid):
    out = {"strategy_id": sid, "reconciliation": [], "positions": [], "summary": []}
    for key, table, order in (("reconciliation", "paper_trades", "bar_ts, id"),
                              ("positions", "paper_positions", "bar_ts, id"),
                              ("summary", "paper_summary", "day")):
        if has_table(conn, table):
            out[key] = rows(conn, f"SELECT * FROM {table} WHERE strategy_id=? ORDER BY {order}", (sid,))
    return out


# ----------------------------------------------------------------- server ---

ROUTES = [
    (re.compile(r"^/api/strategies$"), lambda c, m, qs, db: strategies(c)),
    (re.compile(r"^/api/candidates$"), lambda c, m, qs, db: candidates(c, qs)),
    (re.compile(r"^/api/reports/(\d+)$"),
     lambda c, m, qs, db: report(c, int(m.group(1)), _flag(qs, "equity") == 1, db)),
    (re.compile(r"^/api/health$"), lambda c, m, qs, db: health(c)),
    (re.compile(r"^/api/paper/(\d+)$"), lambda c, m, qs, db: paper(c, int(m.group(1)))),
]


class Handler(BaseHTTPRequestHandler):
    server_version = "strategyforge-web/1.0"
    db_path = None              # set by make_server
    token = None

    def authorized(self):
        got = self.headers.get("Authorization", "")
        want = f"Bearer {self.token}"
        return bool(self.token) and hmac.compare_digest(got.encode(), want.encode())

    def log_message(self, fmt, *args):
        main.log(f"web {self.address_string()} {fmt % args}")

    def send_json(self, status, obj):
        body = json.dumps(clean(obj), separators=(",", ":"), default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_page(self):
        with open(os.path.join(STATIC_DIR, "index.html"), "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def route(self):
        url = urlparse(self.path)
        qs = {k: v[-1] for k, v in parse_qs(url.query).items()}
        for pat, fn in ROUTES:
            m = pat.match(url.path)
            if m:
                conn = connect(self.db_path)
                try:
                    return fn(conn, m, qs, self.db_path or main.DB_PATH)
                finally:
                    conn.close()
        raise HttpError(404, f"no route {url.path}")

    def do_GET(self):
        if urlparse(self.path).path in ("/", "/index.html"):
            return self.send_page()
        if not self.authorized():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Bearer realm="strategyforge"')
            body = b'{"error":"bearer token required"}'
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
            return
        try:
            self.send_json(200, self.route())
        except HttpError as e:
            self.send_json(e.status, {"error": str(e)})
        except Exception as e:  # noqa: BLE001 — reported to the client and the log
            main.log(f"web 500 {self.path}: {traceback.format_exc()}")
            self.send_json(500, {"error": f"{type(e).__name__}: {e}"})

    do_HEAD = do_GET

    def refuse(self):
        self.send_json(405, {"error": "read-only API: GET only"})

    do_POST = do_PUT = do_PATCH = do_DELETE = refuse


def check_token(token):
    if not token or len(token) < MIN_TOKEN_LEN:
        raise SystemExit(f"WEB_TOKEN must be set to at least {MIN_TOKEN_LEN} characters "
                         "(e.g. python -c 'import secrets; print(secrets.token_urlsafe(32))')")
    return token


def make_server(bind=BIND, port=PORT, db_path=None, token=None):
    handler = type("BoundHandler", (Handler,), {"db_path": db_path, "token": check_token(token)})
    return ThreadingHTTPServer((bind, port), handler)


def main_cli():
    srv = make_server(token=os.environ.get("WEB_TOKEN"))
    main.log(f"web: read-only API on http://{BIND}:{PORT} (db {main.DB_PATH}, mode=ro)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main_cli())
