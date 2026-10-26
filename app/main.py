"""
StrategyForge — Phase A-1: data foundation
Collects and archives everything the backtesting factory and shadow-feature
analysis will need:

  1) 1h OHLCV candles from Coinbase public API  -> candles table
     (backfills BACKFILL_DAYS on first run, then tops up hourly; if
     BACKFILL_DAYS is later raised, also walks back from the oldest stored
     bar to the new target, or to the pair's listing date if that is later)
  2) External context feeds (polled, ephemeral)  -> context table
     - Kraken Futures: funding rate, predicted funding, open interest,
       perp spread (bps) for BTC/ETH/SOL perps          [US-accessible]
     - Deribit: DVOL implied-volatility index, BTC + ETH [public]
     - Coinbase: order-book imbalance within 1% of mid   [public]
     - alternative.me: Fear & Greed index                [public]
     - CoinGecko: BTC dominance %                        [public]

All feeds are shadow data: nothing here trades or decides anything.
Every poll is timestamped so signals can later be joined to the market
context that existed at signal time.
"""

import json
import os
import sqlite3
import time
import urllib.request
from datetime import datetime, timedelta, timezone

DB_PATH = os.environ.get("FORGE_DB", "/data/forge.db")
# The ONE list of traded pairs: collector, optimizer default and per-pair
# seeds (optimizer.seed_for_pair uses each pair's index here). APPEND ONLY —
# reordering or removing a pair silently changes every later pair's seed.
# A PAIRS env var is ignored (a warning is logged at startup).
PAIRS = [
    "BTC-USD",
    "ETH-USD",
    "SOL-USD",
    "XRP-USD",   # P2.4.2, 2026-10-25: ~$78M/day 30d avg on Coinbase, >= 1,100 days hourly
]
# Verified price increment per pair (Coinbase GET /products/{pair}
# quote_increment, checked 2026-10-26). Slippage is slippage_ticks * tick, so a
# wrong tick is a wrong cost model: the 0.01 default made XRP (~$2.5) pay
# ~100x its real slippage (FINDINGS F1). A pair missing here is UNVERIFIED and
# adversary A7 will not use it as transfer evidence.
TICK_SIZE = {
    "BTC-USD": 0.01,
    "ETH-USD": 0.01,
    "SOL-USD": 0.01,
    "XRP-USD": 0.0001,
}
BACKFILL_DAYS = int(os.environ.get("BACKFILL_DAYS", "730"))
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")

CANDLE_INTERVAL_S = 3600       # top up candles hourly
CONTEXT_INTERVAL_S = 900       # poll context feeds every 15 minutes
SLOW_INTERVAL_S = 3600         # fear&greed / dominance hourly

KRAKEN_PERP_MAP = {
    "PF_XBTUSD": "BTC-USD",
    "PF_ETHUSD": "ETH-USD",
    "PF_SOLUSD": "SOL-USD",
}
DVOL_CURRENCIES = ["BTC", "ETH"]


# ----------------------------------------------------------------- helpers ---

def log(msg: str):
    print(f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}Z {msg}", flush=True)


def http_json(url: str, timeout: int = 20):
    req = urllib.request.Request(url, headers={"User-Agent": "strategyforge/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def ntfy(msg: str):
    if not NTFY_TOPIC:
        return
    try:
        req = urllib.request.Request(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=msg.encode(),
            headers={"Title": "StrategyForge"},
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass


def warn_if_pairs_env() -> bool:
    """PAIRS used to be overridable from the environment; it no longer is
    (one source of truth, stable seeds). Say so loudly if it is still set."""
    if not os.environ.get("PAIRS"):
        return False
    log(f"WARNING: PAIRS env var is set ({os.environ['PAIRS']!r}) and IGNORED. "
        f"The pair list lives only in main.py; remove the env var from the container.")
    return True


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS candles (
        pair TEXT NOT NULL,
        ts INTEGER NOT NULL,            -- unix seconds, bar OPEN time, UTC
        open REAL, high REAL, low REAL, close REAL, volume REAL,
        PRIMARY KEY (pair, ts)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS context (
        ts INTEGER NOT NULL,            -- unix seconds of the poll
        metric TEXT NOT NULL,
        pair TEXT NOT NULL,             -- 'GLOBAL' for market-wide metrics
        value REAL,
        PRIMARY KEY (ts, metric, pair)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS poll_errors (
        ts INTEGER, source TEXT, error TEXT
    )""")
    conn.commit()
    return conn


def record_error(conn, source: str, err: Exception):
    log(f"ERROR [{source}] {err}")
    conn.execute(
        "INSERT INTO poll_errors (ts, source, error) VALUES (?, ?, ?)",
        (int(time.time()), source, str(err)[:500]),
    )
    conn.commit()


def put_context(conn, ts: int, metric: str, pair: str, value):
    if value is None:
        return
    conn.execute(
        "INSERT OR REPLACE INTO context (ts, metric, pair, value) VALUES (?, ?, ?, ?)",
        (ts, metric, pair, float(value)),
    )


# ----------------------------------------------------------------- candles ---

def fetch_coinbase_candles(pair: str, start: datetime, end: datetime):
    """Coinbase Exchange public candles, granularity 3600, max 300 per call.
    Returns list of [time, low, high, open, close, volume]."""
    url = (
        f"https://api.exchange.coinbase.com/products/{pair}/candles"
        f"?granularity=3600&start={start.isoformat()}&end={end.isoformat()}"
    )
    return http_json(url)


def store_candles(conn, pair: str, rows) -> int:
    for r in rows:
        conn.execute(
            "INSERT OR REPLACE INTO candles (pair, ts, open, high, low, close, volume)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (pair, int(r[0]), r[3], r[2], r[1], r[4], r[5]),
        )
    conn.commit()
    return len(rows)


# pairs whose history ran out before the BACKFILL_DAYS target (listed later
# than that); remembered per process so the hourly top-up doesn't re-ask
_history_exhausted = set()


def backfill_backwards(conn, pair: str, oldest: datetime, target: datetime) -> int:
    """Walk back from the oldest stored bar to target in 300-bar chunks.
    Stops early when Coinbase returns nothing older than the chunk's end,
    i.e. the pair's listing date has been reached."""
    log(f"[{pair}] history starts {oldest:%Y-%m-%d %H:%M}, BACKFILL_DAYS={BACKFILL_DAYS} "
        f"wants {target:%Y-%m-%d %H:%M} - backfilling backwards")
    inserted = 0
    cursor = oldest
    while cursor > target:
        chunk_start = max(cursor - timedelta(hours=300), target)
        try:
            rows = fetch_coinbase_candles(pair, chunk_start, cursor)
        except Exception as e:
            record_error(conn, f"coinbase-candles-back-{pair}", e)
            break
        if not any(int(r[0]) < cursor.timestamp() for r in rows):
            _history_exhausted.add(pair)
            log(f"[{pair}] no candles before {cursor:%Y-%m-%d %H:%M} - "
                f"that is the start of Coinbase history for this pair")
            break
        inserted += store_candles(conn, pair, rows)
        cursor = chunk_start
        time.sleep(0.35)  # stay far under public rate limits
    if inserted:
        log(f"[{pair}] backward backfill upserted: {inserted}")
    return inserted


def update_candles(conn, pair: str):
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    target = now - timedelta(days=BACKFILL_DAYS)
    lo, hi = conn.execute(
        "SELECT MIN(ts), MAX(ts) FROM candles WHERE pair = ?", (pair,)).fetchone()

    inserted = 0
    if hi is None:
        cursor = target
        log(f"[{pair}] no candles yet - backfilling {BACKFILL_DAYS} days")
    else:
        cursor = datetime.fromtimestamp(hi, tz=timezone.utc)
        oldest = datetime.fromtimestamp(lo, tz=timezone.utc)
        if oldest > target and pair not in _history_exhausted:
            inserted += backfill_backwards(conn, pair, oldest, target)

    while cursor < now:
        chunk_end = min(cursor + timedelta(hours=300), now)
        try:
            rows = fetch_coinbase_candles(pair, cursor, chunk_end)
        except Exception as e:
            record_error(conn, f"coinbase-candles-{pair}", e)
            return inserted
        inserted += store_candles(conn, pair, rows)
        cursor = chunk_end
        time.sleep(0.35)  # stay far under public rate limits
    if inserted:
        log(f"[{pair}] candles upserted: {inserted}")
    return inserted


# ------------------------------------------------------------ context feeds ---

def poll_kraken_futures(conn, ts: int):
    """Funding rate, predicted funding, open interest, perp spread (bps)."""
    data = http_json("https://futures.kraken.com/derivatives/api/v3/tickers")
    for t in data.get("tickers", []):
        pair = KRAKEN_PERP_MAP.get(t.get("symbol"))
        if not pair:
            continue
        put_context(conn, ts, "funding_rate", pair, t.get("fundingRate"))
        put_context(conn, ts, "funding_rate_pred", pair, t.get("fundingRatePrediction"))
        put_context(conn, ts, "open_interest", pair, t.get("openInterest"))
        bid, ask = t.get("bid"), t.get("ask")
        if bid and ask and (bid + ask) > 0:
            mid = (bid + ask) / 2.0
            put_context(conn, ts, "perp_spread_bps", pair, (ask - bid) / mid * 10000.0)
        put_context(conn, ts, "perp_mark_price", pair, t.get("markPrice"))
    conn.commit()


def poll_deribit_dvol(conn, ts: int):
    """DVOL implied-volatility index (annualized %, e.g. 55.3)."""
    end_ms = ts * 1000
    start_ms = (ts - 7200) * 1000
    for cur in DVOL_CURRENCIES:
        url = (
            "https://www.deribit.com/api/v2/public/get_volatility_index_data"
            f"?currency={cur}&resolution=3600&start_timestamp={start_ms}&end_timestamp={end_ms}"
        )
        data = http_json(url)
        candles = (data.get("result") or {}).get("data") or []
        if candles:
            # each entry: [ts_ms, open, high, low, close]
            put_context(conn, ts, "dvol", f"{cur}-USD", candles[-1][4])
    conn.commit()


def poll_orderbook_imbalance(conn, ts: int):
    """Bid/ask depth ratio within 1% of mid, from Coinbase public book.
    Value > 0.5 means more resting bid volume than ask volume."""
    for pair in PAIRS:
        url = (
            "https://api.coinbase.com/api/v3/brokerage/market/product_book"
            f"?product_id={pair}&limit=250"
        )
        try:
            data = http_json(url)
        except Exception as e:
            record_error(conn, f"coinbase-book-{pair}", e)
            continue
        book = data.get("pricebook") or {}
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        if not bids or not asks:
            continue
        best_bid = float(bids[0]["price"])
        best_ask = float(asks[0]["price"])
        mid = (best_bid + best_ask) / 2.0
        lo, hi = mid * 0.99, mid * 1.01
        bid_vol = sum(float(b["size"]) for b in bids if float(b["price"]) >= lo)
        ask_vol = sum(float(a["size"]) for a in asks if float(a["price"]) <= hi)
        total = bid_vol + ask_vol
        if total > 0:
            put_context(conn, ts, "ob_imbalance_1pct", pair, bid_vol / total)
        put_context(conn, ts, "spot_spread_bps", pair, (best_ask - best_bid) / mid * 10000.0)
    conn.commit()


def poll_fear_greed(conn, ts: int):
    data = http_json("https://api.alternative.me/fng/?limit=1")
    entries = data.get("data") or []
    if entries:
        put_context(conn, ts, "fear_greed", "GLOBAL", entries[0].get("value"))
    conn.commit()


def poll_btc_dominance(conn, ts: int):
    data = http_json("https://api.coingecko.com/api/v3/global")
    pct = ((data.get("data") or {}).get("market_cap_percentage") or {}).get("btc")
    put_context(conn, ts, "btc_dominance", "GLOBAL", pct)
    conn.commit()


# -------------------------------------------------------------------- main ---

def main():
    log(f"StrategyForge A-1 starting. pairs={PAIRS} backfill_days={BACKFILL_DAYS}")
    warn_if_pairs_env()
    conn = db()

    last_candle = 0.0
    last_ctx = 0.0
    last_slow = 0.0
    announced = False

    while True:
        now = time.time()
        ts = int(now)

        if now - last_candle >= CANDLE_INTERVAL_S or last_candle == 0:
            for pair in PAIRS:
                update_candles(conn, pair)
            last_candle = now
            if not announced:
                n = conn.execute("SELECT COUNT(*) FROM candles").fetchone()[0]
                log(f"initial candle store: {n} rows")
                ntfy(f"StrategyForge online. Candle store: {n} rows across {len(PAIRS)} pairs.")
                announced = True

        if now - last_ctx >= CONTEXT_INTERVAL_S or last_ctx == 0:
            for name, fn in (
                ("kraken-futures", poll_kraken_futures),
                ("deribit-dvol", poll_deribit_dvol),
                ("coinbase-book", poll_orderbook_imbalance),
            ):
                try:
                    fn(conn, ts)
                except Exception as e:
                    record_error(conn, name, e)
            last_ctx = now
            log("context poll complete")

        if now - last_slow >= SLOW_INTERVAL_S or last_slow == 0:
            for name, fn in (
                ("fear-greed", poll_fear_greed),
                ("btc-dominance", poll_btc_dominance),
            ):
                try:
                    fn(conn, ts)
                except Exception as e:
                    record_error(conn, name, e)
            last_slow = now

        time.sleep(30)


if __name__ == "__main__":
    main()