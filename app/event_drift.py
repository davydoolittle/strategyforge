"""
P13 evaluator (V2_PROMPT 8i): drift after a material agreement. This first part
selects the events; it is shared by the coverage check (P12.3) and, later, the
one registered run (P13.3).

Events, exactly as registered:
  - an 8-K (amendments, 8-K/A, are not new events) reporting item 1.01, entry
    into a material definitive agreement;
  - accepted 2005-01-01 or later;
  - by a company (CIK) that is an S&P 500 member on the acceptance date (New
    York time), point in time from pit_members;
  - not also reporting 2.02 (results) or 2.03 (a new debt obligation);
  - not within DEDUP_DAYS trading days after the same company's previous item
    1.01 8-K. "Its previous one" is read as any earlier 1.01 8-K, excluded or
    not, so a run of agreement filings counts once. (Reading fixed 2026-11-10,
    before any price or outcome was looked at.)

Timing: acceptance time is UTC (verified 2026-11-09); 09:30 New York is the
cut. Accepted before the open on a trading day: d0 is that day; otherwise the
next trading day.
"""

from bisect import bisect_left, bisect_right
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
START = "2005-01-01"
ITEM = "1.01"
EXCLUDE_ITEMS = ("2.02", "2.03")
DEDUP_DAYS = 20


def items_of(s):
    return {x.strip() for x in (s or "").split(",") if x.strip()}


def ny_time(accepted_utc):
    return datetime.strptime(accepted_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).astimezone(NY)


def reaction_day(accepted_utc, cal):
    """Index in cal (sorted ISO trading dates) of d0, or None past its end."""
    t = ny_time(accepted_utc)
    d = t.date().isoformat()
    before_open = (t.hour, t.minute) < (9, 30)
    i = bisect_left(cal, d)
    if i < len(cal) and cal[i] == d and not before_open:
        i += 1
    return i if i < len(cal) else None


def member_on(intervals, cik, day):
    """intervals: {cik: [(start, end)]} (end exclusive, None = still a member)."""
    return any(s <= day and (e is None or day < e) for s, e in intervals.get(cik, []))


def select_events(filings, intervals, cal, start=START, end=None):
    """filings: [(accession, cik, form, accepted_utc, items)].
    Returns (events, counts): events = [(accession, cik, accepted_utc, ny_date)],
    counts = why the other 1.01 filings were left out."""
    counts = {"amendment": 0, "before_start": 0, "not_member": 0, "excluded_item": 0,
              "duplicate": 0, "after_end": 0}
    by_cik = {}
    for acc, cik, form, accepted, items in filings:
        if not accepted or ITEM not in items_of(items):
            continue
        if form != "8-K":
            counts["amendment"] += 1
            continue
        by_cik.setdefault(cik, []).append((accepted, acc, items))
    events = []
    for cik, rows in by_cik.items():
        rows.sort()
        prev_i = None
        for accepted, acc, items in rows:
            day = ny_time(accepted).date().isoformat()
            i = bisect_right(cal, day) - 1                  # trading-day index on or before
            dup = prev_i is not None and i - prev_i < DEDUP_DAYS
            prev_i = i
            if day < start:
                counts["before_start"] += 1
            elif end and day > end:
                counts["after_end"] += 1
            elif not member_on(intervals, cik, day):
                counts["not_member"] += 1
            elif items_of(items) & set(EXCLUDE_ITEMS):
                counts["excluded_item"] += 1
            elif dup:
                counts["duplicate"] += 1
            else:
                events.append((acc, cik, accepted, day))
    events.sort(key=lambda e: e[2])
    return events, counts


def load_inputs(conn):
    """(filings, intervals by CIK, trading calendar from SPY bars) from stocks.db."""
    filings = conn.execute("SELECT accession, cik, form, accepted, items FROM sec_filings"
                           " WHERE form IN ('8-K', '8-K/A') AND items LIKE '%1.01%'").fetchall()
    ivs = {}
    for cik, s, e in conn.execute("SELECT cik, start, end FROM pit_members WHERE cik IS NOT NULL"):
        ivs.setdefault(cik, []).append((s, e))
    cal = [datetime.fromtimestamp(t, tz=timezone.utc).date().isoformat() for (t,) in conn.execute(
        "SELECT ts FROM stock_bars WHERE ticker='SPY' ORDER BY ts")]
    return filings, ivs, cal


# ----------------------------------------------------- coverage (P12.3) ---

BEFORE_DAYS = 175       # calendar days of bars needed before d0: ~120 trading days for beta
AFTER_DAYS = 90         # and after it: the 60-trading-day hold


def event_tickers(conn, cik, day):
    """Tickers that may carry this company's prices on `day`: the member
    interval's own ticker and SEC's current tickers for the CIK (Tiingo
    spelling: dots become dashes)."""
    import json
    out = [t for (t,) in conn.execute("SELECT ticker FROM pit_members WHERE cik=? AND start<=?"
                                      " AND (end IS NULL OR end>?)", (cik, day, day))]
    r = conn.execute("SELECT tickers FROM sec_companies WHERE cik=?", (cik,)).fetchone()
    out += json.loads(r[0] or "[]") if r else []
    return list(dict.fromkeys(t.upper().replace(".", "-") for t in out))


def priced(spans, tickers, day, newest=None):
    """spans: {ticker: [(first, last)]} from Tiingo's list. True if one ticker
    covers BEFORE_DAYS before to AFTER_DAYS after the event day."""
    d = date.fromisoformat(day)
    lo = (d - timedelta(days=BEFORE_DAYS)).isoformat()
    hi = (d + timedelta(days=AFTER_DAYS)).isoformat()
    newest = newest or max((l for v in spans.values() for _, l in v if l), default=hi_default())
    hi = min(hi, newest)              # a recent event needs bars only to the list's newest date
    return any(f and l and f <= lo and l >= hi for t in tickers for f, l in spans.get(t, []))


def hi_default():
    return date.today().isoformat()


def coverage(conn, spans, end=None):
    """The P12.3 numbers the stop rule needs, from filings and Tiingo's ticker
    list alone (no prices): eligible events, how many fall at companies Tiingo
    cannot price, and events presumably lost at members with no CIK."""
    filings, ivs, cal = load_inputs(conn)
    events, counts = select_events(filings, ivs, cal, end=end)
    newest = max((l for v in spans.values() for _, l in v if l), default=hi_default())
    unpriced = [e for e in events if not priced(spans, event_tickers(conn, e[1], e[3]), e[3], newest)]
    # members without a CIK: their events are invisible; estimate them from the
    # mapped members' rate per member-year
    def years(rows):
        tot = 0.0
        for s, e in rows:
            s = max(s, START)
            e = e or hi_default()
            if e > s:
                tot += (date.fromisoformat(e) - date.fromisoformat(s)).days / 365.25
        return tot
    mapped_years = years([iv for v in ivs.values() for iv in v])
    unmapped_years = years(conn.execute("SELECT start, end FROM pit_members WHERE cik IS NULL").fetchall())
    rate = len(events) / mapped_years if mapped_years else 0.0
    return {"events": len(events), "unpriced": len(unpriced), "counts": counts,
            "unmapped_member_years": unmapped_years, "est_lost": rate * unmapped_years,
            "unpriced_sample": unpriced[:20]}
