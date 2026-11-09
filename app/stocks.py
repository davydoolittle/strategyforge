"""
Point-in-time S&P 500 universe on free data (P12, V2_PROMPT 8i).

Everything lives in its own database, stocks.db next to forge.db (STOCKS_DB
overrides), so millions of stock bars never share a lock with the collector.

  members   fja05680/sp500 on GitHub: one row per change date, each the full
            member list from that date until the next row. Turned into
            intervals per ticker (pit_members).
  check     Wikipedia's "Historical components of the S&P 500" change log: each
            dated addition and removal since CHECK_FROM must show up in the
            intervals within CHECK_TOL_DAYS; and the per-date member count must
            stay inside COUNT_RANGE from 2005 on.
  cik       each member interval gets its company's SEC CIK: Wikipedia's current
            table (it has a CIK column), then SEC's current ticker file for a
            ticker no later company took, then a name match against SEC's
            company list using the names in Wikipedia's change log. What stays
            unmatched is listed, never guessed.

  python stocks.py --members        # fetch, build intervals, run the checks
  python stocks.py --cik            # map intervals to CIKs, list the unmatched
  python stocks.py --report

Network: GitHub and Wikipedia with a generic client name; SEC with the owner's
declared User-Agent from sec.env next to forge.db, at most SEC_RATE requests a
second. Nothing is sent anywhere else.
"""

import argparse
import csv
import html
import io
import json
import os
import re
import sqlite3
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

import main

FJA_URL = ("https://raw.githubusercontent.com/fja05680/sp500/master/"
           + urllib.parse.quote("S&P 500 Historical Components & Changes (Updated).csv"))
WIKI_API = "https://en.wikipedia.org/w/api.php"
WIKI_CHANGES = "Historical_components_of_the_S&P_500"
WIKI_CURRENT = "List_of_S&P_500_companies"
SEC_TICKERS = "https://www.sec.gov/files/company_tickers.json"
SEC_NAMES = "https://www.sec.gov/Archives/edgar/cik-lookup-data.txt"
UA = "strategyforge/1.0 (research)"
SEC_RATE = 8                    # requests a second; SEC allows 10
FROM = "2004-01-01"             # 2005 events need ~250 days of history for beta
CHECK_FROM = "2005-01-01"
CHECK_TOL_DAYS = 7
COUNT_RANGE = (490, 510)
BATCH_ROWS = 100


def db_path():
    return os.environ.get("STOCKS_DB") or os.path.join(
        os.path.dirname(os.path.abspath(main.DB_PATH)), "stocks.db")


def connect(path=None, ro=False):
    path = path or db_path()
    if ro:
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=main.DB_TIMEOUT_S)
    return sqlite3.connect(path, timeout=main.DB_TIMEOUT_S)


def ensure_tables(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS pit_members (
        ticker TEXT NOT NULL, start TEXT NOT NULL, end TEXT,      -- end exclusive; NULL = still a member
        cik TEXT, cik_source TEXT, name TEXT,
        PRIMARY KEY (ticker, start))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS pit_counts (
        date TEXT PRIMARY KEY, n INTEGER)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS wiki_changes (
        date TEXT NOT NULL, added TEXT, added_name TEXT, removed TEXT, removed_name TEXT, reason TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS stocks_meta (k TEXT PRIMARY KEY, v TEXT)""")
    conn.commit()


def meta(conn, k, v=None):
    if v is None:
        r = conn.execute("SELECT v FROM stocks_meta WHERE k=?", (k,)).fetchone()
        return r[0] if r else None
    conn.execute("INSERT OR REPLACE INTO stocks_meta VALUES (?, ?)", (k, v))
    conn.commit()


# ------------------------------------------------------------------- http ---

def http_get(url, headers=None, timeout=120):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def sec_user_agent(path=None):
    path = path or os.path.join(os.path.dirname(os.path.abspath(main.DB_PATH)), "sec.env")
    if not os.path.exists(path):
        raise SystemExit(f"missing {path}: needs SEC_USER_AGENT (a name and an email, as SEC requires)")
    with open(path) as f:
        for line in f:
            k, sep, v = line.strip().partition("=")
            if sep and k.strip() == "SEC_USER_AGENT" and v.strip():
                return v.strip()
    raise SystemExit(f"{path} has no SEC_USER_AGENT")


class Sec:
    """SEC fetches with the declared User-Agent, at most SEC_RATE a second."""

    def __init__(self, ua=None, get=http_get, sleep=time.sleep, clock=time.monotonic):
        self.ua, self.get, self.sleep, self.clock = ua or sec_user_agent(), get, sleep, clock
        self.last = float("-inf")

    def __call__(self, url):
        wait = 1.0 / SEC_RATE - (self.clock() - self.last)
        if wait > 0:
            self.sleep(wait)
        self.last = self.clock()
        return self.get(url, {"User-Agent": self.ua, "Accept-Encoding": "identity"})


def wiki_html(page, get=http_get):
    q = urllib.parse.urlencode({"action": "parse", "page": page, "prop": "text", "format": "json",
                                "formatversion": 2})
    return json.loads(get(f"{WIKI_API}?{q}"))["parse"]["text"]


# ---------------------------------------------------------------- members ---

def parse_fja(text):
    """[(date, set of tickers)] oldest first."""
    rows = []
    for r in csv.DictReader(io.StringIO(text)):
        tickers = {t.strip().upper() for t in r["tickers"].split(",") if t.strip()}
        rows.append((r["date"], tickers))
    rows.sort()
    if not rows:
        raise ValueError("fja05680 file is empty: format changed?")
    return rows


def intervals(rows, since=FROM):
    """[(ticker, start, end)] with end exclusive (None while still a member).
    The row in force on `since` counts from `since`."""
    first = max((i for i, (d, _) in enumerate(rows) if d <= since), default=0)
    rows = [(max(d, since), t) for d, t in rows[first:]]
    out, open_ = [], {}
    for d, tickers in rows:
        for t in sorted(tickers - set(open_)):
            open_[t] = d
        for t in sorted(set(open_) - tickers):
            out.append((t, open_.pop(t), d))
    out += [(t, s, None) for t, s in sorted(open_.items())]
    return sorted(out, key=lambda x: (x[0], x[1]))


def extend_with_wiki(ivs, changes, after):
    """Apply Wikipedia's changes dated after fja05680's last row: an addition
    opens an interval, a removal closes the open one. Returns (intervals, applied)."""
    ivs = [list(x) for x in ivs]
    applied = []
    for d, add, _, rem, _, _ in sorted((c for c in changes if c[0] > after), key=lambda c: c[0]):
        if rem:
            for iv in ivs:
                if _norm(iv[0]) == _norm(rem) and iv[2] is None:
                    iv[2] = d
                    applied.append((d, "removed", rem))
        if add and not any(_norm(iv[0]) == _norm(add) and iv[2] is None for iv in ivs):
            ivs.append([add, d, None])
            applied.append((d, "added", add))
    return sorted((tuple(x) for x in ivs), key=lambda x: (x[0], x[1])), applied


def counts(rows, since=CHECK_FROM):
    return [(d, len(t)) for d, t in rows if d >= since]


def _cells(row_html):
    cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row_html, re.S)
    return [html.unescape(re.sub(r"\s+", " ", re.sub(r"<sup.*?</sup>|<[^>]+>", " ", c))).strip()
            for c in cells]


def parse_wiki_changes(page_html):
    """[(date iso, added, added_name, removed, removed_name, reason)]."""
    table = re.search(r"<table[^>]*>(.*?)</table>", page_html, re.S).group(1)
    out = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", table, re.S)[2:]:
        c = _cells(row)
        if len(c) < 6:
            continue
        try:
            d = datetime.strptime(c[0], "%B %d, %Y").date().isoformat()
        except ValueError:
            continue
        out.append((d, c[1].upper() or None, c[2] or None, c[3].upper() or None, c[4] or None, c[5]))
    return out


def parse_wiki_current(page_html):
    """[(ticker, name, cik)] from the constituents table."""
    table = re.search(r"<table[^>]*>(.*?)</table>", page_html, re.S).group(1)
    out = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", table, re.S)[1:]:
        c = _cells(row)
        ciks = [x for x in c if re.fullmatch(r"\d{10}", x)]
        if c and ciks:
            out.append((c[0].upper(), c[1], ciks[0]))
    return out


def _days(a, b):
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def cross_check(ivs, changes, since=CHECK_FROM, tol=CHECK_TOL_DAYS):
    """Each Wikipedia addition (removal) since `since` must match an interval
    start (end) of that ticker within tol days. Returns (n checked, misses)."""
    starts, ends = {}, {}
    for t, s, e in ivs:
        starts.setdefault(t, []).append(s)
        if e:
            ends.setdefault(t, []).append(e)
    checked, misses = 0, []
    for d, add, _, rem, _, _ in changes:
        if d < since:
            continue
        for kind, t, pool in (("added", add, starts), ("removed", rem, ends)):
            if not t:
                continue
            checked += 1
            if not any(abs(_days(x, d)) <= tol for x in pool.get(_norm(t), [])):
                misses.append((d, kind, t))
    return checked, misses


def _norm(t):
    """fja uses 'BRK.B'; Wikipedia sometimes 'BRK.B' too: keep one spelling."""
    return t.replace("-", ".").upper()


def store_members(conn, ivs, cnt, changes):
    ensure_tables(conn)
    conn.execute("DELETE FROM pit_counts")
    conn.execute("DELETE FROM wiki_changes")
    keep = {(r[0], r[1]): r[2:] for r in conn.execute(
        "SELECT ticker, start, cik, cik_source, name FROM pit_members")}
    conn.execute("DELETE FROM pit_members")
    rows = [(t, s, e, *keep.get((t, s), (None, None, None))) for t, s, e in ivs]
    for i in range(0, len(rows), BATCH_ROWS):
        conn.executemany("INSERT INTO pit_members VALUES (?, ?, ?, ?, ?, ?)", rows[i:i + BATCH_ROWS])
    conn.executemany("INSERT INTO pit_counts VALUES (?, ?)", cnt)
    conn.executemany("INSERT INTO wiki_changes VALUES (?, ?, ?, ?, ?, ?)", changes)
    conn.commit()


# -------------------------------------------------------------------- cik ---

SUFFIX_RE = re.compile(r"\b(incorporated|inc|corporation|corp|company|co|ltd|limited|plc|holdings?|group|"
                       r"the|n\.?v|s\.?a|llc|lp|l\.p|trust|de|new|cl [ab]|class [ab])\b\.?")


def norm_name(name):
    n = html.unescape(name or "").lower().replace("&", " and ")
    n = re.sub(r"\(.*?\)", " ", n)
    n = re.sub(r"[^a-z0-9 ]", " ", n.replace(".com", "com"))
    n = SUFFIX_RE.sub(" ", n)
    return re.sub(r"\s+", " ", n).strip()


def parse_cik_lookup(text):
    """SEC's cik-lookup-data.txt: 'NAME:CIK:' per line -> {normalised name: {cik}}."""
    out = {}
    for line in text.splitlines():
        name, _, rest = line.rpartition(":")[0].rpartition(":")
        if not name:
            continue
        cik = rest.strip().zfill(10)
        out.setdefault(norm_name(name), set()).add(cik)
    return out


PAIR_DAYS = 10                  # a change-log row this close to an interval's edge may be it
MAX_NAME_CIKS = 5               # a name filed under more CIKs than this is too common to use
RENAME_DAYS = 3                 # one interval ending this close to another's start: a ticker change
VERIFY_PAD_DAYS = 120           # a periodic report this close to the interval counts


def candidates(ivs, current, sec_tickers, changes, names_index):
    """{(ticker, start): [(cik, source, name)]}, best source first:
      wikipedia-current  a current member in Wikipedia's table, with its CIK
      sec-ticker         SEC's current ticker file (fja05680 tends to use a
                         company's latest ticker); verified later, since tickers get reused
      name-ticker        the change log names this ticker near the interval's edges
      name-date          a change-log row dated within PAIR_DAYS of the interval's
                         edge, for a ticker fja05680 spells differently
    Every name is matched to SEC's company list, unique matches only."""
    cur = {_norm(t): (cik, name) for t, name, cik in current}
    sect = {_norm(t): str(c).zfill(10) for t, c in sec_tickers.items()}
    by_ticker, by_date = {}, []
    for d, add, add_name, rem, rem_name, _ in changes:
        for kind, t, n in (("added", add, add_name), ("removed", rem, rem_name)):
            if t and n:
                by_ticker.setdefault(_norm(t), []).append((d, n))
                by_date.append((d, kind, n))

    def by_name(n):
        """Every CIK filed under this name (at most MAX_NAME_CIKS): the
        filing-history check decides between them."""
        c = sorted(names_index.get(norm_name(n), set()))
        return c if 0 < len(c) <= MAX_NAME_CIKS else []

    out = {}
    for t, s, e in ivs:
        k, got = _norm(t), []
        if e is None and k in cur:
            got.append((cur[k][0], "wikipedia-current", cur[k][1]))
        if k in sect:
            got.append((sect[k], "sec-ticker", None))
        for d, n in by_ticker.get(k, []):
            if any(abs(_days(d, x)) <= PAIR_DAYS for x in (s, e) if x):
                got += [(c, "name-ticker", n) for c in by_name(n)]
        for d, kind, n in by_date:
            edge = s if kind == "added" else e
            if edge and abs(_days(d, edge)) <= PAIR_DAYS:
                got += [(c, "name-date", n) for c in by_name(n)]
        seen, uniq = set(), []
        for c in got:
            if c[0] not in seen:
                seen.add(c[0])
                uniq.append(c)
        if uniq:
            out[(t, s)] = uniq
    return out


def verified(periodic, s, e, today=None):
    """The company filed a 10-K or 10-Q within VERIFY_PAD_DAYS of the interval."""
    lo = (date.fromisoformat(s) - timedelta(days=VERIFY_PAD_DAYS)).isoformat()
    hi = (date.fromisoformat(e or (today or date.today()).isoformat())
          + timedelta(days=VERIFY_PAD_DAYS)).isoformat()
    return any(lo <= d <= hi for d in periodic) and any(d >= s for d in periodic)


def name_agrees(a, b):
    """Two company names for the same firm: one contains the other, or they
    share their first word (3+ letters)."""
    a, b = norm_name(a), norm_name(b)
    if not a or not b:
        return False
    if a in b or b in a:
        return True
    wa, wb = a.split()[0], b.split()[0]
    return len(wa) >= 3 and wa == wb


def ticker_fits(ticker, name):
    """The ticker's letters appear in order in the name, starting with its
    first letter: BSC fits Bear Stearns, not Ambac."""
    t = re.sub(r"[^a-z]", "", ticker.lower())
    n = re.sub(r"[^a-z]", "", norm_name(name))
    if not t or not n or t[0] != n[0]:
        return False
    it = iter(n)
    return all(ch in it for ch in t)


def _coverage(periodic, s, e):
    hi = e or "9999"
    return sum(1 for d in periodic if s <= d <= hi)


def choose(ivs, cands, periodic_of, changes=(), sec_names=None):
    """{(ticker, start): (cik, source, name)} and the ambiguous intervals.
    Sources in order, the first that yields one verified candidate wins:
      wikipedia-current  taken as is
      name-ticker        the change log's name for this ticker near its edges
      sec-ticker         SEC's current ticker, and if the change log names the
                         ticker, SEC's name or a former name must agree with it
      name-date          a change-log name from the interval's edge date that
                         the ticker fits (ticker_fits)
    Several verified candidates of one source: the one with the most 10-K/10-Q
    filings inside the interval; a tie leaves it unmatched."""
    sec_names = sec_names or {}
    out, ambiguous = {}, []
    for t, s, e in ivs:
        cs = cands.get((t, s), [])
        wiki_names = [c[2] for c in cs if c[1] == "name-ticker" and c[2]]
        pick = next((c for c in cs if c[1] == "wikipedia-current"), None)

        def allowed(c):
            if c[1] == "sec-ticker" and wiki_names:
                return any(name_agrees(w, n) for w in wiki_names for n in sec_names.get(c[0], []))
            if c[1] == "name-date":
                return ticker_fits(t, c[2])
            return True
        for src in ("name-ticker", "sec-ticker", "name-date"):
            if pick:
                break
            ok = [c for c in cs if c[1] == src and allowed(c) and verified(periodic_of(c[0]), s, e)]
            if not ok:
                continue
            ok.sort(key=lambda c: -_coverage(periodic_of(c[0]), s, e))
            if len(ok) == 1 or _coverage(periodic_of(ok[0][0]), s, e) > _coverage(periodic_of(ok[1][0]), s, e):
                pick = ok[0]
            else:
                ambiguous.append((t, s, e, sorted({c[2] or c[0] for c in ok})))
                break
        if pick:
            out[(t, s)] = pick
    # link_renames() is not used: Wikipedia's change log is selective, so a date
    # with no logged change does not prove a rename (spot-check 2026-11-09:
    # it mapped Bear Stearns to Intuitive Surgical). Unmatched intervals go to
    # the hand-checked list instead (cik_overrides).
    return out, ambiguous


def link_renames(ivs, out, periodic_of, changes):
    """An unmatched interval that ends within RENAME_DAYS of a mapped one's
    start (or starts as one ends), on a date with no index change at all in
    Wikipedia's log, is the same company under a new ticker: it takes that CIK
    if the company's filings cover it. Repeated until nothing changes."""
    change_dates = [c[0] for c in changes]

    def index_change(t, d):
        """Any index change near d, for any ticker: then two intervals meeting
        on d are an addition and a removal, not a rename."""
        return any(abs(_days(x, d)) <= RENAME_DAYS for x in change_dates)

    moved = True
    while moved:
        moved = False
        for t, s, e in ivs:
            if (t, s) in out:
                continue
            for t2, s2, e2 in ivs:
                m = out.get((t2, s2))
                if not m or t2 == t:
                    continue
                pairs = []
                if e and abs(_days(e, s2)) <= RENAME_DAYS:
                    pairs.append(e)
                if e2 and abs(_days(s, e2)) <= RENAME_DAYS:
                    pairs.append(s)
                for d in pairs:
                    if not index_change(t, d) and not index_change(t2, d) and verified(periodic_of(m[0]), s, e):
                        out[(t, s)] = (m[0], "rename", m[2])
                        moved = True
                        break
                if (t, s) in out:
                    break


def store_ciks(conn, mapped):
    rows = [(cik, src, name, t, s) for (t, s), (cik, src, name) in mapped.items()]
    for i in range(0, len(rows), BATCH_ROWS):
        conn.executemany("UPDATE pit_members SET cik=?, cik_source=?, name=COALESCE(?, name)"
                         " WHERE ticker=? AND start=?", rows[i:i + BATCH_ROWS])
        conn.commit()


# ------------------------------------------------------------------- cli ---

def run_members(conn, get=http_get):
    rows = parse_fja(get(FJA_URL).decode())
    changes = parse_wiki_changes(wiki_html(WIKI_CHANGES, get))
    ivs, applied = extend_with_wiki(intervals(rows), changes, rows[-1][0])
    cnt = counts(rows)
    checked, misses = cross_check(ivs, changes)
    store_members(conn, ivs, cnt, changes)
    meta(conn, "members_fetched_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    meta(conn, "members_last_date", rows[-1][0])
    lo = min(n for _, n in cnt)
    hi = max(n for _, n in cnt)
    bad = [(d, n) for d, n in cnt if not COUNT_RANGE[0] <= n <= COUNT_RANGE[1]]
    print(f"fja05680: {len(rows)} change dates through {rows[-1][0]}; {len(applied)} later Wikipedia changes"
          f" applied; {len(ivs)} member intervals from {FROM}, {len({t for t, _, _ in ivs})} tickers")
    print(f"members per date from {CHECK_FROM}: {lo}-{hi}; outside {COUNT_RANGE}: {len(bad)} dates"
          + (f" (e.g. {bad[:3]})" if bad else ""))
    print(f"Wikipedia cross-check since {CHECK_FROM}: {checked - len(misses)}/{checked} changes matched"
          f" within {CHECK_TOL_DAYS} days; {len(misses)} misses")
    for m in misses[:40]:
        print("  miss", *m)
    return {"checked": checked, "misses": misses, "bad_counts": bad, "intervals": len(ivs)}


def run_cik(conn, sec=None, get=http_get):
    import filings
    sec = sec or Sec()
    ivs = conn.execute("SELECT ticker, start, end FROM pit_members").fetchall()
    changes = conn.execute("SELECT * FROM wiki_changes").fetchall()
    current = parse_wiki_current(wiki_html(WIKI_CURRENT, get))
    tick = {v["ticker"]: v["cik_str"] for v in json.loads(sec(SEC_TICKERS)).values()}
    names_index = parse_cik_lookup(sec(SEC_NAMES).decode("latin-1"))
    cands = candidates(ivs, current, tick, changes, names_index)
    need = {c[0] for cs in cands.values() for c in cs}
    print(f"{len(cands)}/{len(ivs)} intervals have candidates; fetching SEC history for {len(need)} CIKs")
    done, failed = filings.fetch_all(conn, need, sec)
    sec_names = {cik: [n] + json.loads(f or "[]") for cik, n, f in
                 conn.execute("SELECT cik, name, former_names FROM sec_companies")}
    conn.execute("UPDATE pit_members SET cik=NULL, cik_source=NULL WHERE cik_source<>'manual' OR cik_source IS NULL")
    conn.commit()
    mapped, ambiguous = choose(ivs, cands, lambda cik: filings.periodic_dates(conn, cik), changes, sec_names)
    store_ciks(conn, mapped)
    for a in ambiguous[:20]:
        print("  ambiguous", *a)
    by = {}
    for _, src, _ in mapped.values():
        by[src] = by.get(src, 0) + 1
    un = [r for r in ivs if (r[0], r[1]) not in mapped]
    print(f"CIK map: {len(mapped)}/{len(ivs)} intervals mapped {by}; {len(un)} unmatched")
    for t, s, e in un[:60]:
        print(f"  unmatched {t:6} {s} .. {e or 'now'}")
    return {"mapped": len(mapped), "unmatched": un}


def report(conn):
    n, cik = conn.execute("SELECT COUNT(*), COUNT(cik) FROM pit_members").fetchone()
    print(f"pit_members: {n} intervals, {cik} with a CIK; members data through {meta(conn, 'members_last_date')}")


def main_cli(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--members", action="store_true")
    ap.add_argument("--cik", action="store_true")
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args(argv)
    conn = connect()
    ensure_tables(conn)
    if args.members:
        run_members(conn)
    if args.cik:
        run_cik(conn)
    report(conn)
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
