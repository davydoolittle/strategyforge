"""
P9.4 stock tracker: the S&P 100 as display-only context (V2_PROMPT 8c).

Today's S&P 100 constituents, for screening today's names only. **No backtest
on individual stocks uses this list**: it holds only the companies in the index
now, so any history read from it carries survivorship bias. Backtests on
individual stocks need a registration on P12's point-in-time member list
(stocks.db), as Phase 15 did.

  members   Wikipedia's "S&P 100" constituents table (symbol, name, GICS sector),
            stored in sp100_members with the source and the date read. A name
            that leaves is kept with active = 0, never deleted.
  instruments  each member gets an `instruments` row: asset_class 'equity',
            session 'us_equity', no spread_bps, so engine.py refuses to
            backtest it until someone sets a cost on purpose.
  bars      daily adjusted bars from Alpaca (2016 on, splits and dividends),
            through equities.backfill, one vendor per symbol, batch commits.

  python tracker.py --members        # refresh the list and the instruments rows
  python tracker.py --bars           # refresh the members' daily bars
  python tracker.py --refresh        # both, then fundamentals and screens (the daily job)
"""

import argparse
import sqlite3
from datetime import datetime, timezone

import equities as E
import main
import stocks as S

SOURCE = "Wikipedia, S&P 100 (en.wikipedia.org/wiki/S%26P_100), constituents table"
PAGE = "S&P_100"
ASSET_CLASS = "equity"
COUNT_RANGE = (95, 110)        # 100 companies, a few with two share classes


def ensure_tables(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS sp100_members (
        symbol TEXT PRIMARY KEY, name TEXT, sector TEXT, source TEXT,
        first_seen TEXT, last_seen TEXT, active INTEGER NOT NULL DEFAULT 1)""")
    conn.commit()


def parse_members(page_html):
    """[(symbol, name, sector)] from the constituents table: the one whose
    header row is Symbol / Name / Sector."""
    import re
    for table in re.findall(r"<table[^>]*>(.*?)</table>", page_html, re.S):
        rows = [S._cells(r) for r in re.findall(r"<tr[^>]*>(.*?)</tr>", table, re.S)]
        if rows and rows[0][:3] == ["Symbol", "Name", "Sector"]:
            return [(r[0].upper(), r[1], r[2]) for r in rows[1:] if len(r) >= 3 and r[0]]
    raise ValueError("no Symbol / Name / Sector table on the S&P 100 page: layout changed?")


def store_members(conn, members, today):
    """Upsert today's list; names not on it become inactive. Returns (added, left)."""
    ensure_tables(conn)
    E.ensure_tables(conn)
    if not COUNT_RANGE[0] <= len(members) <= COUNT_RANGE[1]:
        raise SystemExit(f"S&P 100 list has {len(members)} names: page changed? nothing stored")
    have = {r[0]: r[1] for r in conn.execute("SELECT symbol, active FROM sp100_members")}
    now = {m[0] for m in members}
    added = sorted(now - {s for s, a in have.items() if a})
    left = sorted({s for s, a in have.items() if a} - now)
    for sym, name, sector in members:
        conn.execute("INSERT INTO sp100_members VALUES (?, ?, ?, ?, ?, ?, 1) ON CONFLICT(symbol) DO UPDATE SET"
                     " name=excluded.name, sector=excluded.sector, source=excluded.source,"
                     " last_seen=excluded.last_seen, active=1", (sym, name, sector, SOURCE, today, today))
        conn.execute("INSERT OR IGNORE INTO instruments (symbol, asset_class, tick_size, session)"
                     " VALUES (?, ?, ?, ?)", (sym, ASSET_CLASS, E.TICK_SIZE, E.SESSION))
    conn.executemany("UPDATE sp100_members SET active=0 WHERE symbol=?", [(s,) for s in left])
    conn.commit()
    return added, left


def active(conn):
    ensure_tables(conn)
    return [r[0] for r in conn.execute("SELECT symbol FROM sp100_members WHERE active=1 ORDER BY symbol")]


def refresh_members(conn, get=S.http_get, today=None):
    today = today or datetime.now(timezone.utc).date().isoformat()
    members = parse_members(S.wiki_html(PAGE, get))
    added, left = store_members(conn, members, today)
    print(f"S&P 100: {len(members)} names from {SOURCE}, read {today}; added {added or '-'}; left {left or '-'}")
    return members


def refresh_bars(conn, headers=None):
    """Alpaca adjusted daily bars for the active members (equities.backfill:
    restartable, only changed rows written, batch commits)."""
    syms = [s for s in active(conn) if s not in E.RESEARCH_SYMBOLS]
    done = E.backfill(conn, headers or E.load_credentials(), symbols=syms, fetch=E.fetch_bars,
                      exchange=E.fetch_exchange, source="alpaca")
    print(f"bars: {len(done)} of {len(syms)} members refreshed")
    return done


def main_cli(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--members", action="store_true")
    ap.add_argument("--bars", action="store_true")
    ap.add_argument("--refresh", action="store_true", help="members, bars, fundamentals and screens")
    args = ap.parse_args(argv)
    conn = sqlite3.connect(main.DB_PATH, timeout=main.DB_TIMEOUT_S)
    if args.members or args.refresh:
        refresh_members(conn)
    if args.bars or args.refresh:
        refresh_bars(conn)
    if args.refresh:
        try:
            import fundamentals
            fundamentals.refresh(conn)
        except ImportError:
            pass
        try:
            import screens
            screens.run(conn)
        except ImportError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
