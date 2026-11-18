"""
Phase 15 data (V2_PROMPT 8j, P15.1): everything the seven-factor battery reads,
fetched once and frozen. Into stocks.db, never forge.db.

  f15_symbols  one row per S&P 500 member interval overlapping the window: the
               Alpaca symbol chosen for it, and how many of its member days
               that symbol prices
  f15_bars     daily bars of every chosen symbol, 2016-01-04 to 2026-10-30:
               adjusted open and close (splits and dividends) and the raw close
  f15_facts    the XBRL company facts the signals need, each with its filing date
  f15_text     word counts of each member's 10-K main documents filed from 2015,
               zlib-compressed JSON; the text itself is never kept

Which symbol prices a member (checked 2026-11-18): Alpaca files a live
company's whole history under its current symbol (META back to 2016, though FB
was the ticker until 2022), and old tickers get reused (FB is now another fund;
FI was Frank's International before Fiserv took it). So the company's current
SEC tickers come first, then the interval's own ticker, and the candidate that
prices the most member days wins. A fallback to the interval ticker for an
interval starting after the window opens is listed for a manual check, since
its earlier bars may belong to the ticker's previous owner.

Restartable: every step skips what is already stored. Fetch first, then write
in batches (section 10.9). SEC requests carry the owner's declared contact
(stocks.Sec) and nothing else.

  python factor_data.py --bars        # Alpaca, adjusted then raw (minutes)
  python factor_data.py --facts       # SEC company facts, one request a company
  python factor_data.py --text        # SEC 10-K main documents (about an hour)
  python factor_data.py --coverage    # the price stop rule; computes no return
"""

import argparse
import html
import json
import re
import urllib.parse
import zlib
from collections import Counter
from datetime import date, datetime, timezone

import equities as E
import stocks as S

SNAPSHOT = "f15-2026-10-30"
START, END = "2016-01-04", "2026-10-30"
FIRST_FORMATION = "2017-01-31"
LAST_FORMATION = "2026-09-30"
TEXT_FROM = "2015-01-01"              # a 2016 formation needs the 10-K before the latest one
BENCHMARK = "SPY"
BATCH_SYMBOLS = 100
BATCH_ROWS = 500
PRICE_STOP = 0.02                     # registered: unpriced member-months at formation
JUMP = 0.40                           # a fallback ticker this far off at its interval start had another owner
JUMP_DAYS = 3

FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik}.json"
EXTRA = "https://data.sec.gov/submissions/{name}"
DOC_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}"
# us-gaap tags read by the signals, in the order a signal prefers them, plus shares
TAGS = {
    "revenue": ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
                "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet"),
    "cost_of_revenue": ("CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold",
                        "CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization"),
    "assets": ("Assets",),
    "net_income": ("NetIncomeLoss",),
    "equity": ("StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"),
}
SHARE_TAGS = (("dei", "EntityCommonStockSharesOutstanding"), ("us-gaap", "CommonStockSharesOutstanding"))


def ensure_tables(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS f15_symbols (
        ticker TEXT, start TEXT, cik TEXT, symbol TEXT, member_days INTEGER, priced_days INTEGER,
        via TEXT, PRIMARY KEY (ticker, start))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS f15_bars (
        symbol TEXT, day TEXT, open REAL, close REAL, raw_close REAL, PRIMARY KEY (symbol, day))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS f15_facts (
        cik TEXT, tag TEXT, start TEXT, end TEXT, val REAL, form TEXT, filed TEXT, accn TEXT)""")
    conn.execute("CREATE INDEX IF NOT EXISTS f15_facts_cik ON f15_facts (cik, tag, filed)")
    conn.execute("CREATE TABLE IF NOT EXISTS f15_facts_done (cik TEXT PRIMARY KEY, n INTEGER, note TEXT)")
    conn.execute("""CREATE TABLE IF NOT EXISTS f15_text (
        accession TEXT PRIMARY KEY, cik TEXT, filed TEXT, n_words INTEGER, counts BLOB, note TEXT)""")
    conn.commit()


# ------------------------------ members and symbols --------------------------

def intervals(conn):
    """Member intervals overlapping the window: (ticker, start, end or None, cik)."""
    return conn.execute("SELECT ticker, start, end, cik FROM pit_members WHERE start<=? AND"
                        " COALESCE(end, '9999-12-31')>? ORDER BY ticker, start", (END, START)).fetchall()


def alpaca_symbol(t):
    """SEC writes share classes BRK-B, the member list BRK.B, Alpaca BRK.B."""
    return t.replace("-", ".").upper()


def candidates(ticker, sec_tickers):
    """The company's current common tickers first, then the interval's own."""
    out = [alpaca_symbol(t) for t in sec_tickers if re.fullmatch(r"[A-Z]{1,5}([.-][A-Z])?", t)]
    if alpaca_symbol(ticker) not in out:
        out.append(alpaca_symbol(ticker))
    return out


def member_days(cal, start, end):
    """Trading days in the window on which the interval is a member (end exclusive)."""
    return [d for d in cal if max(start, START) <= d < (end or "9999-12-31") and d <= END]


def choose(cands, days, have):
    """The candidate pricing the most member days; ties go to the earlier one.
    have: {symbol: set of days with a bar}. Returns (symbol, priced) or (None, 0)."""
    best, n = None, -1
    for s in cands:
        k = sum(1 for d in days if d in have.get(s, ()))
        if k > n:
            best, n = s, k
    return (best, n) if n > 0 else (None, 0)


def sec_tickers(conn, cik):
    r = conn.execute("SELECT tickers FROM sec_companies WHERE cik=?", (cik,)).fetchone() if cik else None
    return json.loads(r[0]) if r and r[0] else []


# ------------------------------ Alpaca bars ----------------------------------

def alpaca_closes(symbols, adjustment, headers, get=E._get, sleep=None):
    """{symbol: {day: (open, close)}} for the window, many symbols a request."""
    import time
    sleep = sleep or time.sleep
    out, syms = {}, sorted(set(symbols))
    for i in range(0, len(syms), BATCH_SYMBOLS):
        token = None
        while True:
            q = {"symbols": ",".join(syms[i:i + BATCH_SYMBOLS]), "timeframe": "1Day", "start": START,
                 "end": f"{END}T23:59:59Z", "adjustment": adjustment, "feed": E.FEED, "limit": 10000,
                 "sort": "asc"}
            if token:
                q["page_token"] = token
            page = get(f"{E.DATA_URL}?{urllib.parse.urlencode(q)}", headers)
            for sym, bars in (page.get("bars") or {}).items():
                d = out.setdefault(sym, {})
                for b in bars:
                    d[b["t"][:10]] = (b["o"], b["c"])
            token = page.get("next_page_token")
            sleep(E.PAGE_SLEEP_S)
            if not token:
                break
    return out


def fetch_bars(conn, headers=None, get=E._get, sleep=None, log=print):
    """Adjusted bars for every candidate, the choice per interval, then raw closes
    for the chosen symbols. Network first, then one write pass."""
    headers = headers or E.load_credentials()
    ivs = intervals(conn)
    cands = {(t, s): candidates(t, sec_tickers(conn, c)) for t, s, _, c in ivs}
    wanted = {x for v in cands.values() for x in v} | {BENCHMARK}
    log(f"intervals {len(ivs)}, candidate symbols {len(wanted)}")
    adj = alpaca_closes(wanted, "all", headers, get, sleep)
    cal = sorted(adj.get(BENCHMARK, {}))
    if not cal:
        raise SystemExit(f"no {BENCHMARK} bars: cannot build the trading calendar")
    have = {s: set(v) for s, v in adj.items()}
    rows = []
    for t, s, e, c in ivs:
        days = member_days(cal, s, e)
        sym, n = choose(cands[(t, s)], days, have)
        current = [alpaca_symbol(x) for x in sec_tickers(conn, c)]
        via = "none" if sym is None else ("sec-current" if sym in current else "interval")
        rows.append((t, s, c, sym, len(days), n, via))
    chosen = sorted({r[3] for r in rows if r[3]} | {BENCHMARK})
    raw = alpaca_closes(chosen, "raw", headers, get, sleep)
    bars = [(sym, d, o, cl, raw.get(sym, {}).get(d, (None, None))[1])
            for sym in chosen for d, (o, cl) in sorted(adj[sym].items())]
    conn.execute("DELETE FROM f15_symbols")
    conn.executemany("INSERT INTO f15_symbols VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    conn.execute("DELETE FROM f15_bars")
    conn.commit()
    for i in range(0, len(bars), BATCH_ROWS):
        conn.executemany("INSERT INTO f15_bars VALUES (?, ?, ?, ?, ?)", bars[i:i + BATCH_ROWS])
        conn.commit()
    S.meta(conn, "f15_snapshot", SNAPSHOT)
    S.meta(conn, "f15_bars_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    log(f"bars: {len(chosen)} symbols, {len(bars)} rows; intervals without a symbol: "
        f"{sum(1 for r in rows if not r[3])}")
    return rows


def drop_previous_owner(conn, log=print):
    """For a fallback ticker whose interval starts in the window, a close-to-close
    move above JUMP within JUMP_DAYS of the start means the earlier bars belong to
    the ticker's previous owner (EVHC on 2016-12-02, CTRA on 2021-10-04): they are
    deleted. Decided by hand review before any return was computed."""
    dropped = []
    for t, s, sym in conn.execute("SELECT ticker, start, symbol FROM f15_symbols WHERE via='interval'"
                                  " AND start>? AND symbol IS NOT NULL", (START,)).fetchall():
        bars = conn.execute("SELECT day, raw_close FROM f15_bars WHERE symbol=? ORDER BY day", (sym,)).fetchall()
        days = [d for d, _ in bars]
        i = next((k for k, d in enumerate(days) if d >= s), len(days))
        near = range(max(1, i - JUMP_DAYS), min(len(bars), i + JUMP_DAYS + 1))
        if any(bars[k - 1][1] and abs(bars[k][1] / bars[k - 1][1] - 1) > JUMP for k in near):
            conn.execute("DELETE FROM f15_bars WHERE symbol=? AND day<?", (sym, s))
            conn.commit()
            dropped.append((t, s, sym, i))
            log(f"  {t}: dropped {i} bars before {s} (previous owner of {sym})")
    return dropped


def review_list(conn):
    """Intervals priced by their own ticker that start after the window opens: the
    ticker's earlier bars may belong to a previous owner."""
    return conn.execute("SELECT ticker, start, cik, symbol FROM f15_symbols WHERE via='interval' AND start>?"
                        " ORDER BY start", (START,)).fetchall()


# ------------------------------ the price stop rule --------------------------

def formation_dates(cal):
    """The last trading day of each month, FIRST_FORMATION to LAST_FORMATION."""
    ends = [d for i, d in enumerate(cal[:-1]) if cal[i + 1][:7] != d[:7]]   # the data's last day is not known to end a month
    return [d for d in ends if FIRST_FORMATION <= d <= LAST_FORMATION]


def coverage(conn):
    """Share of member-months with no usable price at formation. Computes no return."""
    cal = [r[0] for r in conn.execute("SELECT day FROM f15_bars WHERE symbol=? ORDER BY day", (BENCHMARK,))]
    priced = {}
    for sym, d in conn.execute("SELECT symbol, day FROM f15_bars"):
        priced.setdefault(sym, set()).add(d)
    syms = {(t, s): sym for t, s, sym in conn.execute("SELECT ticker, start, symbol FROM f15_symbols")}
    ivs = intervals(conn)
    total = missing = 0
    worst = Counter()
    for f in formation_dates(cal):
        for t, s, e, _ in ivs:
            if s <= f < (e or "9999-12-31"):
                total += 1
                sym = syms.get((t, s))
                if not sym or f not in priced.get(sym, ()):
                    missing += 1
                    worst[t] += 1
    share = missing / total if total else 1.0
    return {"member_months": total, "unpriced": missing, "share": share, "limit": PRICE_STOP,
            "passes": share <= PRICE_STOP, "months": len(formation_dates(cal)),
            "most_missing": worst.most_common(12)}


# ------------------------------ SEC company facts ----------------------------

def parse_facts(cik, payload):
    """Rows for f15_facts: our tag names, values with their filing dates."""
    out = []
    gaap = payload.get("facts", {}).get("us-gaap", {})
    for name, tags in TAGS.items():
        for rank, tag in enumerate(tags):
            for unit, obs in gaap.get(tag, {}).get("units", {}).items():
                if unit != "USD":
                    continue
                for o in obs:
                    out.append((cik, f"{name}:{rank}", o.get("start"), o["end"], o["val"], o.get("form"),
                                o["filed"], o.get("accn")))
    for tax, tag in SHARE_TAGS:
        for unit, obs in payload.get("facts", {}).get(tax, {}).get(tag, {}).get("units", {}).items():
            if unit == "shares":
                out += [(cik, f"shares:{tax}", o.get("start"), o["end"], o["val"], o.get("form"), o["filed"],
                         o.get("accn")) for o in obs]
    return out


def fetch_facts(conn, sec=None, log=print):
    sec = sec or S.Sec()
    ciks = sorted({c for _, _, _, c in intervals(conn) if c})
    done = {r[0] for r in conn.execute("SELECT cik FROM f15_facts_done")}
    todo = [c for c in ciks if c not in done]
    log(f"company facts: {len(ciks)} companies, {len(todo)} to fetch")
    for k, cik in enumerate(todo):
        note = None
        try:
            rows = parse_facts(cik, json.loads(sec(FACTS_URL.format(cik=cik))))
        except Exception as e:                      # noqa: BLE001 — recorded, not hidden
            rows, note = [], f"{type(e).__name__}: {e}"[:200]
        for i in range(0, len(rows), BATCH_ROWS):
            conn.executemany("INSERT INTO f15_facts VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows[i:i + BATCH_ROWS])
        conn.execute("INSERT OR REPLACE INTO f15_facts_done VALUES (?, ?, ?)", (cik, len(rows), note))
        conn.commit()
        if k % 100 == 99:
            log(f"  {k + 1}/{len(todo)}")


# ------------------------------ 10-K word counts -----------------------------

IX_HEADER = re.compile(r"<ix:header>.*?</ix:header>", re.S | re.I)
NOISE = re.compile(r"<(script|style)\b.*?</\1>", re.S | re.I)
TAG = re.compile(r"<[^>]+>")
WORD = re.compile(r"[a-z]+")


def word_counts(doc):
    """Words of letters only, lower-cased, from the main document's visible text:
    inline-XBRL data, scripts, styles and markup removed."""
    text = TAG.sub(" ", NOISE.sub(" ", IX_HEADER.sub(" ", doc)))
    return Counter(WORD.findall(html.unescape(text).lower()))


def pack(counts):
    return zlib.compress(json.dumps(counts, separators=(",", ":")).encode(), 9)


def unpack(blob):
    return json.loads(zlib.decompress(blob))


def ten_ks(sec, cik):
    """[(accession, filed, primary document)] of the company's 10-Ks from TEXT_FROM."""
    d = json.loads(sec(SUBMISSIONS.format(cik=cik)))
    blocks = [d["filings"]["recent"]] + [json.loads(sec(EXTRA.format(name=f["name"])))
                                         for f in d["filings"].get("files", [])]
    out = []
    for b in blocks:
        for acc, form, filed, doc in zip(b["accessionNumber"], b["form"], b["filingDate"],
                                         b.get("primaryDocument", [""] * len(b["form"]))):
            if form == "10-K" and TEXT_FROM <= filed <= END and doc:
                out.append((acc, filed, doc))
    return sorted(set(out), key=lambda r: r[1])


def fetch_text(conn, sec=None, log=print):
    sec = sec or S.Sec()
    ciks = sorted({c for _, _, _, c in intervals(conn) if c})
    have = {r[0] for r in conn.execute("SELECT accession FROM f15_text")}
    log(f"10-K text: {len(ciks)} companies")
    for k, cik in enumerate(ciks):
        try:
            docs = ten_ks(sec, cik)
        except Exception as e:                      # noqa: BLE001 — recorded, not hidden
            log(f"  {cik}: submissions failed ({e})")
            continue
        for acc, filed, doc in docs:
            if acc in have:
                continue
            url = DOC_URL.format(cik=int(cik), acc=acc.replace("-", ""), doc=doc)
            try:
                counts = word_counts(sec(url).decode("utf-8", "replace"))
                row = (acc, cik, filed, sum(counts.values()), pack(counts), None)
            except Exception as e:                  # noqa: BLE001
                row = (acc, cik, filed, 0, None, f"{type(e).__name__}: {e}"[:200])
            conn.execute("INSERT OR REPLACE INTO f15_text VALUES (?, ?, ?, ?, ?, ?)", row)
            conn.commit()
        if k % 50 == 49:
            log(f"  {k + 1}/{len(ciks)} companies, {conn.execute('SELECT COUNT(*) FROM f15_text').fetchone()[0]} 10-Ks")


def main_cli(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--clean", action="store_true", help="drop previous owners' bars (part of --bars)")
    for f in ("bars", "facts", "text", "coverage"):
        ap.add_argument(f"--{f}", action="store_true")
    args = ap.parse_args(argv)
    conn = S.connect()
    S.ensure_tables(conn)
    ensure_tables(conn)
    if args.bars:
        fetch_bars(conn)
        drop_previous_owner(conn)
        for row in review_list(conn):
            print("  check by hand (interval ticker, starts in window):", row)
    if args.clean:
        drop_previous_owner(conn)
    if args.facts:
        fetch_facts(conn)
    if args.text:
        fetch_text(conn)
    if args.coverage or args.bars:
        c = coverage(conn)
        print(f"price coverage: {c['unpriced']} of {c['member_months']} member-months unpriced at formation"
              f" ({c['share']:.2%}, limit {c['limit']:.0%}) over {c['months']} months:"
              f" {'PASSES' if c['passes'] else 'STOP: not testable'}")
        print(f"  most missing: {c['most_missing']}")


if __name__ == "__main__":
    main_cli()
