"""
P9.4 fundamentals: SEC XBRL company facts for every equity instrument (today's
S&P 100), and trailing ratios derived from them. Display only: facts and
traditional readings, never a validated signal.

Sources, both SEC, with the owner's declared User-Agent (stocks.Sec, at most
8 requests a second; SEC allows 10):
  ticker -> CIK  https://www.sec.gov/files/company_tickers.json (BRK.B -> BRK-B)
  facts          https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json
  sector         https://data.sec.gov/submissions/CIK##########.json: SIC code

Stored (forge.db), each fact with its filing date:
  fund_companies  symbol, cik, name, sic, sic_desc, fetched_at (a company fetched
                  today is skipped: the cache, and the restart marker)
  fund_facts      cik, tag, unit, start, end, fy, fp, form, filed, accn, val
                  (annual and quarterly; only the TAGS below; changed rows only)
  fund_metrics    date, symbol, metrics_json: the derived ratios of that day

Trailing only. Flow items (revenue, income, cash flow) are the last four
quarters: SEC facts carry quarterly values for Q1-Q3 and the fiscal year, so Q4
is the year minus Q1-Q3. Balance-sheet items are the latest instant. Price is
the latest daily close in `candles`. A ratio whose facts are missing, or whose
denominator is not positive, is None and shows as a dash, never estimated.

Not available, and shown as such: forward P/E, PEG and analyst estimates need
paid estimates data.

  python fundamentals.py              # refresh facts, derive today's ratios
  python fundamentals.py --derive     # derive only, from what is stored
"""

import argparse
import json
import sqlite3
from datetime import date, datetime, timedelta, timezone

import stocks as S

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
REFRESH_DAYS = 1
BATCH_ROWS = 500
NOT_AVAILABLE = "not available (paid estimates)"
# Per-share facts in a different class than the tracked price: Berkshire reports
# per class-A share, the tracker prices class B (about 1/1,500 of A). Price-based
# ratios are left out rather than converted by an assumed ratio.
PRICE_CLASS_MISMATCH = {"BRK.B": "per-share facts are per class-A share; the price is class B"}

# concept -> (taxonomy, [tags in order of preference], unit)
CONCEPTS = {
    "revenue": ("us-gaap", ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet",
                            "RevenuesNetOfInterestExpense"], "USD"),
    "cost_of_revenue": ("us-gaap", ["CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold"], "USD"),
    "gross_profit": ("us-gaap", ["GrossProfit"], "USD"),
    "operating_income": ("us-gaap", ["OperatingIncomeLoss"], "USD"),
    "net_income": ("us-gaap", ["NetIncomeLoss", "ProfitLoss"], "USD"),
    "eps_diluted": ("us-gaap", ["EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted"], "USD/shares"),
    "cfo": ("us-gaap", ["NetCashProvidedByUsedInOperatingActivities"], "USD"),
    "capex": ("us-gaap", ["PaymentsToAcquirePropertyPlantAndEquipment"], "USD"),
    "dna": ("us-gaap", ["DepreciationDepletionAndAmortization", "DepreciationAndAmortization",
                        "DepreciationAmortizationAndAccretionNet", "DepreciationAmortizationAndOther",
                        "DepreciationAndAmortizationIncludingDiscontinuedOperations", "Depreciation"], "USD"),
    "interest_expense": ("us-gaap", ["InterestExpense", "InterestExpenseNonoperating"], "USD"),
    "assets": ("us-gaap", ["Assets"], "USD"),
    "liabilities": ("us-gaap", ["Liabilities"], "USD"),
    "equity": ("us-gaap", ["StockholdersEquity",
                           "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"], "USD"),
    "current_assets": ("us-gaap", ["AssetsCurrent"], "USD"),
    "current_liabilities": ("us-gaap", ["LiabilitiesCurrent"], "USD"),
    "cash": ("us-gaap", ["CashAndCashEquivalentsAtCarryingValue",
                         "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"], "USD"),
    "lt_debt": ("us-gaap", ["LongTermDebtNoncurrent", "LongTermDebt"], "USD"),
    "st_debt": ("us-gaap", ["LongTermDebtCurrent", "DebtCurrent", "ShortTermBorrowings"], "USD"),
    "retained_earnings": ("us-gaap", ["RetainedEarningsAccumulatedDeficit"], "USD"),
    "diluted_shares": ("us-gaap", ["WeightedAverageNumberOfDilutedSharesOutstanding"], "shares"),
    "shares_out": ("dei", ["EntityCommonStockSharesOutstanding"], "shares"),
}
FLOWS = {"revenue", "cost_of_revenue", "gross_profit", "operating_income", "net_income", "cfo", "capex", "dna",
         "interest_expense"}
TAGS = {(tax, t) for tax, tags, _ in CONCEPTS.values() for t in tags}


def ensure_tables(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS fund_companies (
        symbol TEXT PRIMARY KEY, cik TEXT, name TEXT, sic TEXT, sic_desc TEXT, fetched_at TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS fund_facts (
        cik TEXT NOT NULL, tag TEXT NOT NULL, unit TEXT NOT NULL, start TEXT, end TEXT NOT NULL,
        fy INTEGER, fp TEXT, form TEXT, filed TEXT, accn TEXT NOT NULL, val REAL,
        PRIMARY KEY (cik, tag, unit, start, end, accn))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS fund_metrics (
        date TEXT NOT NULL, symbol TEXT NOT NULL, metrics_json TEXT, PRIMARY KEY (date, symbol))""")
    conn.commit()


# ------------------------------------------------------------------ fetch ---

def sec_symbol(sym):
    return sym.upper().replace(".", "-")


def parse_facts(cik, payload):
    """fund_facts rows for the tags this module reads."""
    out = []
    for tax, block in (payload.get("facts") or {}).items():
        for tag, body in block.items():
            if (tax, tag) not in TAGS:
                continue
            for unit, vals in (body.get("units") or {}).items():
                for v in vals:
                    if v.get("val") is None or not v.get("end") or not v.get("accn"):
                        continue
                    out.append((cik, f"{tax}:{tag}", unit, v.get("start"), v["end"], v.get("fy"), v.get("fp"),
                                v.get("form"), v.get("filed"), v["accn"], float(v["val"])))
    return out


def store_facts(conn, cik, rows):
    """Write only rows not already stored, in batches; returns how many."""
    have = {r for r in conn.execute("SELECT tag, unit, start, end, accn, val FROM fund_facts WHERE cik=?", (cik,))}
    new = [r for r in rows if (r[1], r[2], r[3], r[4], r[9], r[10]) not in have]
    for i in range(0, len(new), BATCH_ROWS):
        conn.executemany("INSERT OR REPLACE INTO fund_facts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         new[i:i + BATCH_ROWS])
        conn.commit()
    return len(new)


def fresh(conn, sym, now):
    r = conn.execute("SELECT fetched_at FROM fund_companies WHERE symbol=?", (sym,)).fetchone()
    return bool(r and r[0]) and now - datetime.fromisoformat(r[0]) < timedelta(days=REFRESH_DAYS)


def fetch_all(conn, symbols, sec=None, now=None, log=print):
    """Facts and SIC for every symbol not fetched today. Network first per
    company, then its writes; a failure is reported and retried next run."""
    ensure_tables(conn)
    sec = sec or S.Sec()
    now = now or datetime.now(timezone.utc)
    todo = [s for s in symbols if not fresh(conn, s, now)]
    if not todo:
        return 0, []
    tick = {v["ticker"].upper(): str(v["cik_str"]).zfill(10) for v in json.loads(sec(TICKERS_URL)).values()}
    done, failed = 0, []
    for sym in todo:
        cik = tick.get(sec_symbol(sym))
        if not cik:
            failed.append((sym, "no CIK in SEC's ticker file"))
            continue
        try:
            facts = json.loads(sec(FACTS_URL.format(cik=cik)))
            sub = json.loads(sec(SUBMISSIONS_URL.format(cik=cik)))
        except Exception as e:  # noqa: BLE001  reported, retried next run
            failed.append((sym, str(e)[:80]))
            continue
        store_facts(conn, cik, parse_facts(cik, facts))
        conn.execute("INSERT OR REPLACE INTO fund_companies VALUES (?, ?, ?, ?, ?, ?)",
                     (sym, cik, sub.get("name") or facts.get("entityName"), str(sub.get("sic") or ""),
                      sub.get("sicDescription"), now.isoformat(timespec="seconds")))     # last: the marker
        conn.commit()
        done += 1
    return done, failed


# ---------------------------------------------------------------- derive ---

def _days(a, b):
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def series(conn, cik, concept):
    """The facts of the concept's tag with the most recent data, deduplicated by
    period (the latest filing of a period wins): [(start, end, fy, fp, form,
    filed, val)]. Companies switch tags over the years (Revenues, then
    RevenueFromContractWithCustomer... from 2018), so the first tag that has
    any rows can be a stale one; a tie in recency goes to the earlier-listed tag."""
    tax, tags, unit = CONCEPTS[concept]
    best = None
    for k, t in enumerate(tags):
        rows = conn.execute("SELECT start, end, fy, fp, form, filed, val FROM fund_facts WHERE cik=? AND tag=?"
                            " AND unit=? ORDER BY filed", (cik, f"{tax}:{t}", unit)).fetchall()
        if not rows:
            continue
        by = {}
        for r in rows:
            by[(r[0], r[1])] = r
        got = sorted(by.values(), key=lambda r: (r[1], r[0] or ""))
        if best is None or got[-1][1] > best[-1][1]:
            best = got
    return best or []


def quarters(rows):
    """{end: value} of ~3-month periods, with Q4 derived as the fiscal year
    minus the three quarters that end inside it."""
    q = {e: v for s, e, *_, v in rows if s and 80 <= _days(s, e) <= 100}
    for s, e, *_, v in rows:
        if s and 350 <= _days(s, e) <= 380 and e not in q:
            inside = [x for x in q if s < x < e and _days(s, x) >= 60]
            if len(inside) == 3:
                q[e] = v - sum(q[x] for x in inside)
    return q


def ttm(rows):
    """Sum of the latest four consecutive quarters, or the latest fiscal year
    if quarters are not available; (value, period end) or (None, None)."""
    q = quarters(rows)
    ends = sorted(q)
    if len(ends) >= 4:
        last4 = ends[-4:]
        if _days(last4[0], last4[-1]) <= 300:
            return sum(q[e] for e in last4), last4[-1]
    annual = [(e, v) for s, e, *_, v in rows if s and 350 <= _days(s, e) <= 380]
    return (annual[-1][1], annual[-1][0]) if annual else (None, None)


def annual(rows):
    """{fiscal year end: value} for full-year periods (flows) or year-end instants."""
    out = {}
    for s, e, fy, fp, form, filed, v in rows:
        if form and form.startswith("10-K") and (s is None or 350 <= _days(s, e) <= 380):
            out[e] = v
    return out


def latest(rows):
    return (rows[-1][6], rows[-1][1]) if rows else (None, None)


def div(a, b):
    return a / b if a is not None and b not in (None, 0) and b > 0 else None


def last_close(conn, symbol):
    r = conn.execute("SELECT close, ts FROM candles WHERE pair=? AND session='us_equity' ORDER BY ts DESC LIMIT 1",
                     (symbol,)).fetchone()
    return (r[0], datetime.fromtimestamp(r[1], timezone.utc).date().isoformat()) if r else (None, None)


def growth_3y(a):
    ends = sorted(a)
    if len(ends) < 4:
        return None
    v0, v3 = a[ends[-4]], a[ends[-1]]
    if v0 is None or v3 is None or v0 <= 0 or v3 <= 0:
        return None
    return (v3 / v0) ** (1 / 3) - 1


def piotroski(fa):
    """Piotroski F-score (0-9) from the last two fiscal years, or None when a
    year's core facts are missing. fa: {concept: {fy end: value}}."""
    ends = sorted(set(fa.get("assets", {})) & set(fa.get("net_income", {})))
    if len(ends) < 3:
        return None, {}
    y0, y1, yp = ends[-1], ends[-2], ends[-3]

    def g(c, y):
        return fa.get(c, {}).get(y)
    roa1, roa0 = div(g("net_income", y1), g("assets", yp)), div(g("net_income", y0), g("assets", y1))
    cfo0 = g("cfo", y0)
    lev0, lev1 = div(g("lt_debt", y0) or 0.0, g("assets", y0)), div(g("lt_debt", y1) or 0.0, g("assets", y1))
    cr0 = div(g("current_assets", y0), g("current_liabilities", y0))
    cr1 = div(g("current_assets", y1), g("current_liabilities", y1))
    sh0, sh1 = g("diluted_shares", y0), g("diluted_shares", y1)

    def gm(y):
        r, gp, c = g("revenue", y), g("gross_profit", y), g("cost_of_revenue", y)
        gp = gp if gp is not None else (r - c if r is not None and c is not None else None)
        return div(gp, r)
    at0, at1 = div(g("revenue", y0), g("assets", y1)), div(g("revenue", y1), g("assets", yp))
    tests = {
        "positive ROA": roa0 is not None and roa0 > 0,
        "positive operating cash flow": cfo0 is not None and cfo0 > 0,
        "ROA improved": None not in (roa0, roa1) and roa0 > roa1,
        "cash flow above net income": None not in (cfo0, g("net_income", y0)) and cfo0 > g("net_income", y0),
        "less long-term leverage": None not in (lev0, lev1) and lev0 < lev1,
        "current ratio improved": None not in (cr0, cr1) and cr0 > cr1,
        "no new shares": None not in (sh0, sh1) and sh0 <= sh1,
        "gross margin improved": None not in (gm(y0), gm(y1)) and gm(y0) > gm(y1),
        "asset turnover improved": None not in (at0, at1) and at0 > at1,
    }
    return sum(tests.values()), tests


def altman_z(sic, wc, ta, re_, ebit, mve, tl, sales):
    """Altman's original Z (manufacturers); None for financials (SIC 6000-6999),
    where it does not apply, or when a fact is missing."""
    if sic and sic.isdigit() and 6000 <= int(sic) <= 6999:
        return None
    if None in (wc, ta, re_, ebit, mve, tl, sales) or ta <= 0 or tl <= 0:
        return None
    return 1.2 * wc / ta + 1.4 * re_ / ta + 3.3 * ebit / ta + 0.6 * mve / tl + 1.0 * sales / ta


def derive(conn, symbol):
    """The ratio set for one company, from stored facts and the latest close."""
    co = conn.execute("SELECT cik, name, sic, sic_desc FROM fund_companies WHERE symbol=?", (symbol,)).fetchone()
    price, price_day = last_close(conn, symbol)
    out = {"symbol": symbol, "price": price, "price_date": price_day,
           "forward_pe": NOT_AVAILABLE, "peg": NOT_AVAILABLE, "estimates": NOT_AVAILABLE}
    if not co:
        return {**out, "missing": "no SEC facts yet"}
    cik, name, sic, sic_desc = co
    out.update(name=name, sic=sic, sic_desc=sic_desc, sector2=(sic[:2] if sic else None))
    rows = {c: series(conn, cik, c) for c in CONCEPTS}
    t = {c: ttm(rows[c])[0] for c in FLOWS}
    b = {c: latest(rows[c])[0] for c in CONCEPTS if c not in FLOWS}
    fa = {c: annual(rows[c]) for c in CONCEPTS}
    # diluted weighted shares are the company's total count; the dei cover-page
    # count is per class for multi-class companies (Alphabet, Meta)
    shares = b["diluted_shares"] or b["shares_out"]
    mcap = price * shares if price and shares and symbol not in PRICE_CLASS_MISMATCH else None
    if symbol in PRICE_CLASS_MISMATCH:
        out["note"] = PRICE_CLASS_MISMATCH[symbol]
    if b["liabilities"] is None and None not in (b["assets"], b["equity"]):
        b["liabilities"] = b["assets"] - b["equity"]           # many filers never tag total liabilities
    debt = (b["lt_debt"] or 0.0) + (b["st_debt"] or 0.0) if (b["lt_debt"] is not None or b["st_debt"] is not None) else None
    gp = t["gross_profit"] if t["gross_profit"] is not None else (
        t["revenue"] - t["cost_of_revenue"] if None not in (t["revenue"], t["cost_of_revenue"]) else None)
    ebitda = t["operating_income"] + t["dna"] if None not in (t["operating_income"], t["dna"]) else None
    ev = mcap + debt - (b["cash"] or 0.0) if None not in (mcap, debt) else None
    fcf = t["cfo"] - t["capex"] if None not in (t["cfo"], t["capex"]) else None
    sh_a = fa["diluted_shares"]
    sh_ends = sorted(sh_a)
    f, f_tests = piotroski(fa)
    wc = (b["current_assets"] - b["current_liabilities"]) if None not in (b["current_assets"], b["current_liabilities"]) else None
    out.update({
        "market_cap": mcap,
        "pe": div(mcap, t["net_income"]), "ps": div(mcap, t["revenue"]), "pb": div(mcap, b["equity"]),
        "ev_ebitda": div(ev, ebitda),
        "gross_margin": div(gp, t["revenue"]), "operating_margin": div(t["operating_income"], t["revenue"]),
        "net_margin": div(t["net_income"], t["revenue"]),
        "roe": div(t["net_income"], b["equity"]), "roa": div(t["net_income"], b["assets"]),
        "debt_to_equity": div(debt, b["equity"]),
        "net_debt_ebitda": div(debt - (b["cash"] or 0.0), ebitda) if debt is not None else None,
        "interest_coverage": div(t["operating_income"], t["interest_expense"]),
        "current_ratio": div(b["current_assets"], b["current_liabilities"]),
        "fcf": fcf, "fcf_yield": div(fcf, mcap),
        "revenue_growth_3y": growth_3y(fa["revenue"]), "eps_growth_3y": growth_3y(fa["eps_diluted"]),
        "share_change_1y": (sh_a[sh_ends[-1]] / sh_a[sh_ends[-2]] - 1) if len(sh_ends) >= 2 and sh_a[sh_ends[-2]] else None,
        "piotroski_f": f, "piotroski_tests": f_tests,
        "altman_z": altman_z(sic, wc, b["assets"], b["retained_earnings"], t["operating_income"], mcap,
                             b["liabilities"], t["revenue"]),
        "facts_through": max((r[-1][1] for r in rows.values() if r), default=None),
    })
    return out


PERCENTILE_KEYS = ("pe", "ps", "pb", "ev_ebitda", "gross_margin", "operating_margin", "net_margin", "roe", "roa",
                   "debt_to_equity", "net_debt_ebitda", "interest_coverage", "current_ratio", "fcf_yield",
                   "revenue_growth_3y", "eps_growth_3y", "share_change_1y", "piotroski_f", "altman_z")
MIN_SECTOR_PEERS = 3


def percentile(vals, x):
    """Share of the other values strictly below x, as 0-100, or None."""
    others = [v for v in vals if v is not None]
    if x is None or len(others) < 2:
        return None
    return 100.0 * sum(1 for v in others if v < x) / (len(others) - 1) if len(others) > 1 else None


def add_percentiles(metrics):
    """Each ratio's percentile within the whole list and within its 2-digit SIC
    group (when that group has MIN_SECTOR_PEERS or more members)."""
    for k in PERCENTILE_KEYS:
        allv = [m.get(k) for m in metrics]
        for m in metrics:
            m.setdefault("pct_all", {})[k] = percentile(allv, m.get(k))
            peers = [x.get(k) for x in metrics if x.get("sector2") and x.get("sector2") == m.get("sector2")]
            m.setdefault("pct_sector", {})[k] = (percentile(peers, m.get(k))
                                                 if len([p for p in peers if p is not None]) >= MIN_SECTOR_PEERS else None)
    for m in metrics:
        m["sector_peers"] = sum(1 for x in metrics if x.get("sector2") and x.get("sector2") == m.get("sector2"))
    return metrics


def add_outside_percentiles(m, members):
    """An added name's percentiles against the S&P 100 members only: it is
    ranked among them without becoming part of the comparison group."""
    m["pct_all"], m["pct_sector"] = {}, {}
    peers = [x for x in members if x.get("sector2") and x.get("sector2") == m.get("sector2")]
    for k in PERCENTILE_KEYS:
        m["pct_all"][k] = percentile([x.get(k) for x in members] + [m.get(k)], m.get(k))
        pv = [x.get(k) for x in peers]
        m["pct_sector"][k] = (percentile(pv + [m.get(k)], m.get(k))
                              if len([p for p in pv if p is not None]) >= MIN_SECTOR_PEERS else None)
    m["sector_peers"] = len(peers)
    m["sp100_member"] = False
    return m


def derive_all(conn, symbols, today=None, members=None):
    """members: the S&P 100 symbols the percentiles are taken within; any other
    symbol is ranked against them (add_outside_percentiles). Default: all."""
    today = today or date.today().isoformat()
    members = set(symbols if members is None else members)
    inside = add_percentiles([derive(conn, s) for s in symbols if s in members])
    for m in inside:
        m["sp100_member"] = True
    outside = [add_outside_percentiles(derive(conn, s), inside) for s in symbols if s not in members]
    metrics = inside + outside
    rows = [(today, m["symbol"], json.dumps(m)) for m in metrics]          # compute first, then write
    for i in range(0, len(rows), BATCH_ROWS):
        conn.executemany("INSERT OR REPLACE INTO fund_metrics VALUES (?, ?, ?)", rows[i:i + BATCH_ROWS])
        conn.commit()
    return metrics


def latest_metrics(conn):
    """{symbol: metrics} of the newest derived day."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='fund_metrics'").fetchone():
        return {}
    d = conn.execute("SELECT MAX(date) FROM fund_metrics").fetchone()[0]
    return {s: json.loads(j) for s, j in conn.execute("SELECT symbol, metrics_json FROM fund_metrics WHERE date=?", (d,))}


def refresh(conn, symbols=None, sec=None, log=print):
    import tracker
    ensure_tables(conn)
    symbols = symbols or tracker.tracked(conn)
    done, failed = fetch_all(conn, symbols, sec, log=log)
    metrics = derive_all(conn, symbols, members=tracker.active(conn))
    with_pe = sum(1 for m in metrics if m.get("pe") is not None)
    log(f"fundamentals: {done} companies fetched, {len(failed)} failed {failed[:5]}; ratios for {len(metrics)},"
        f" P/E for {with_pe}")
    return metrics


def main_cli(argv=None):
    import main
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--derive", action="store_true")
    args = ap.parse_args(argv)
    conn = sqlite3.connect(main.DB_PATH, timeout=main.DB_TIMEOUT_S)
    ensure_tables(conn)
    if args.derive:
        import tracker
        derive_all(conn, tracker.tracked(conn), members=tracker.active(conn))
    else:
        refresh(conn)
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
