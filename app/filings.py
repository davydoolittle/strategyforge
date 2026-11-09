"""
SEC filings for the P12/P13 universe (V2_PROMPT 8i), from SEC's free
submissions JSON (data.sec.gov), with the owner's declared User-Agent.

For each CIK: the company's name, its current tickers and former names, and
every filing of the forms the study reads (FORMS), with EDGAR's acceptance
time and, for 8-Ks, the items reported. Older filings beyond the first page
come from the extra files the JSON lists, so the history is complete.

  sec_companies   cik, name, tickers (JSON), former names (JSON), fetched_at
  sec_filings     accession, cik, form, filed (date), accepted (UTC ISO), items

Restartable: a CIK fetched within REFRESH_DAYS is skipped. Fetch first, then
write in batches (section 10.9). Into stocks.db, never forge.db.

  python filings.py --ciks 0000320193,0000789019     # specific companies
  python filings.py --members                        # every CIK in pit_members
"""

import argparse
import json
from datetime import datetime, timedelta, timezone

import stocks as S

SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik}.json"
EXTRA = "https://data.sec.gov/submissions/{name}"
FORMS = {"8-K", "8-K/A", "10-K", "10-K/A", "10-Q", "10-Q/A"}
REFRESH_DAYS = 1
BATCH_ROWS = 100


def ensure_tables(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS sec_companies (
        cik TEXT PRIMARY KEY, name TEXT, tickers TEXT, former_names TEXT, fetched_at TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS sec_filings (
        accession TEXT PRIMARY KEY, cik TEXT NOT NULL, form TEXT, filed TEXT, accepted TEXT, items TEXT)""")
    conn.execute("CREATE INDEX IF NOT EXISTS sec_filings_cik ON sec_filings (cik, form, filed)")
    conn.commit()


def _rows(block, cik):
    """Filing rows from one 'recent'-style column block."""
    n = len(block.get("accessionNumber", []))
    items = block.get("items") or [""] * n
    out = []
    for i in range(n):
        form = block["form"][i]
        if form not in FORMS:
            continue
        acc = block["acceptanceDateTime"][i] if block.get("acceptanceDateTime") else None
        out.append((block["accessionNumber"][i], cik, form, block["filingDate"][i],
                    _utc(acc) if acc else None, items[i] or ""))
    return out


def _utc(s):
    """EDGAR gives '2008-09-15T08:12:34.000Z'; keep ISO UTC to the second."""
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def fetch_company(sec, cik, recent_only=False):
    """(company row, filing rows) for one CIK; all network first. recent_only
    reads just the first page (the last ~1,000 filings): enough for a daily
    update of a company whose history is already stored."""
    cik = str(cik).zfill(10)
    d = json.loads(sec(SUBMISSIONS.format(cik=cik)))
    rows = _rows(d["filings"]["recent"], cik)
    for f in ([] if recent_only else d["filings"].get("files", [])):
        rows += _rows(json.loads(sec(EXTRA.format(name=f["name"]))), cik)
    former = [x.get("name") for x in d.get("formerNames", [])]
    company = (cik, d.get("name"), json.dumps(d.get("tickers", [])), json.dumps(former),
               datetime.now(timezone.utc).isoformat(timespec="seconds"))
    return company, rows


def store_company(conn, company, rows):
    for i in range(0, len(rows), BATCH_ROWS):
        conn.executemany("INSERT OR REPLACE INTO sec_filings VALUES (?, ?, ?, ?, ?, ?)", rows[i:i + BATCH_ROWS])
        conn.commit()
    conn.execute("INSERT OR REPLACE INTO sec_companies VALUES (?, ?, ?, ?, ?)", company)   # last: the marker
    conn.commit()


def fresh(conn, cik, now=None):
    r = conn.execute("SELECT fetched_at FROM sec_companies WHERE cik=?", (cik,)).fetchone()
    if not r:
        return False
    now = now or datetime.now(timezone.utc)
    return now - datetime.fromisoformat(r[0]) < timedelta(days=REFRESH_DAYS)


def fetch_all(conn, ciks, sec=None, log=print, recent_only=False):
    """recent_only applies only to companies already stored: a new one always
    gets its full history."""
    sec = sec or S.Sec()
    ensure_tables(conn)
    done, failed = 0, []
    for cik in sorted({str(c).zfill(10) for c in ciks}):
        if fresh(conn, cik):
            continue
        known = conn.execute("SELECT 1 FROM sec_companies WHERE cik=?", (cik,)).fetchone()
        try:
            company, rows = fetch_company(sec, cik, recent_only=recent_only and bool(known))
        except Exception as e:  # noqa: BLE001  reported, retried next run
            failed.append((cik, str(e)[:80]))
            continue
        store_company(conn, company, rows)
        done += 1
        if done % 50 == 0:
            log(f"  {done} companies fetched")
    return done, failed


def periodic_dates(conn, cik):
    """Filing dates of the company's 10-K and 10-Q reports, oldest first."""
    return [r[0] for r in conn.execute("SELECT filed FROM sec_filings WHERE cik=? AND form IN"
                                       " ('10-K', '10-Q', '10-K/A', '10-Q/A') ORDER BY filed", (cik,))]


def main_cli(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--ciks", help="comma-separated CIKs")
    ap.add_argument("--members", action="store_true", help="every CIK in pit_members")
    args = ap.parse_args(argv)
    conn = S.connect()
    S.ensure_tables(conn)
    ensure_tables(conn)
    ciks = []
    if args.ciks:
        ciks += args.ciks.split(",")
    if args.members:
        ciks += [r[0] for r in conn.execute("SELECT DISTINCT cik FROM pit_members WHERE cik IS NOT NULL")]
    done, failed = fetch_all(conn, ciks)
    n = conn.execute("SELECT COUNT(*) FROM sec_filings").fetchone()[0]
    print(f"fetched {done} companies; {len(failed)} failed; sec_filings now {n} rows")
    for f in failed[:20]:
        print("  failed", *f)
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
