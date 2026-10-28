"""
StrategyForge features — shadow feature vectors for trades.

Given a list of trades (from engine.backtest), computes ~30 features at each
trade's SIGNAL bar (the bar whose close generated the signal, one bar before
the entry fill). Two feature families:

  OHLCV-derived  — computed from the candles table, available for ALL trades
  Context-joined — joined from the context table (funding, OI, DVOL, order
                   book, sentiment); NULL for trades that predate the
                   collector (it started 2026-09-01), populated for
                   everything after.

Features do not affect trading decisions unless a cfg opts in through
cfg["filters"] (P3.1): a list of {"feature": name, "op": ">=" | "<=",
"value": x}, or for the categorical regime tag (P5.4) {"feature": "regime",
"op": "==" | "!=", "value": "trend_up" | "trend_down" | "chop"}. engine.backtest then builds a FeatureComputer lazily and lets a
signal through only if every filter holds AT THE SIGNAL BAR. A missing value
(None: not enough history, or a context feed that did not exist yet) blocks
the signal: a filter that cannot be evaluated is not satisfied. Which
features deserve to become filters is decided by feature_evals.py (P3.2).
"""

import json
import math
import operator
import sqlite3
from datetime import datetime, timezone

import engine

CONTEXT_MAX_AGE_S = 2 * 3600     # a context poll must be within 2h of signal

# Market regime at signal time (P3.4), from DAILY bars: the last completed
# UTC day before the signal. Not the trade's own timeframe: V4.1 only enters
# when 1h ADX is above its threshold and the slow EMA slopes its way, so a 1h
# tag is never 'chop' and mostly repeats the trade's direction (FINDINGS F7).
REGIME_TF = 86400
REGIME_EMA = 50               # slow EMA, daily closes
REGIME_SLOPE_DAYS = 5         # slope = EMA[d] - EMA[d-5]
REGIME_ADX = 20.0             # daily ADX(14) below this = chop
REGIMES = ("trend_up", "trend_down", "chop")
FILTER_OPS = {">=": operator.ge, "<=": operator.le}
# categorical features a filter may test for (in)equality: name -> allowed values
CATEGORICAL_OPS = {"==": operator.eq, "!=": operator.ne}
CATEGORICAL = {"regime": REGIMES}


def _pctile(window, value):
    if not window:
        return None
    below = sum(1 for w in window if w <= value)
    return 100.0 * below / len(window)


def _std(vals):
    if len(vals) < 2:
        return None
    m = sum(vals) / len(vals)
    return math.sqrt(sum((x - m) ** 2 for x in vals) / (len(vals) - 1))


def _autocorr1(rets):
    if len(rets) < 3:
        return None
    a = rets[:-1]
    b = rets[1:]
    ma = sum(a) / len(a)
    mb = sum(b) / len(b)
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    da = math.sqrt(sum((x - ma) ** 2 for x in a))
    db = math.sqrt(sum((y - mb) ** 2 for y in b))
    if da == 0 or db == 0:
        return None
    return num / (da * db)


class FeatureComputer:
    def __init__(self, conn, pair, tf_seconds=3600, start_ts=None, end_ts=None):
        """start_ts/end_ts bound the candles loaded (default: all). A filter
        passes the backtest's own bounds, so indicator warmup matches it."""
        self.conn = conn
        self.pair = pair
        self.tf = tf_seconds
        self._cache = {}
        self._bounds = (start_ts, end_ts)
        self._regime = None
        ts, o, h, l, c, v = engine.load_candles(
            conn, pair, end_ts if end_ts is not None else 2 ** 62, tf_seconds, start_ts)
        self.ts, self.o, self.h, self.l, self.c, self.v = ts, o, h, l, c, v
        self.index = {t: i for i, t in enumerate(ts)}
        self.atr = engine.atr(h, l, c, 14)
        self.ema_f = engine.ema(c, 9)
        self.ema_m = engine.ema(c, 21)
        self.ema_s = engine.ema(c, 50)
        self.vol_sma = engine.sma(v, 20)
        # bollinger bandwidth (20, 2)
        self.bb_bw = [None] * len(c)
        basis = engine.sma(c, 20)
        for i in range(19, len(c)):
            window = c[i - 19:i + 1]
            sd = _std(window)
            if basis[i] and sd is not None:
                self.bb_bw[i] = 4.0 * sd / basis[i] * 100.0
        # per-day bar counts for lookbacks
        self.bars_per_day = 86400 // tf_seconds

    # ---------------- OHLCV features at signal index i ----------------

    def ohlcv_features(self, i):
        f = {}
        ts, o, h, l, c, v = self.ts, self.o, self.h, self.l, self.c, self.v
        bpd = self.bars_per_day
        atr_v = self.atr[i]
        close = c[i]

        atr_pct = (atr_v / close * 100.0) if (atr_v and close) else None
        f["atr_pct"] = atr_pct
        lb90 = i - 90 * bpd
        if atr_pct is not None and lb90 >= 0:
            window = [self.atr[k] / c[k] * 100.0
                      for k in range(lb90, i) if self.atr[k] and c[k]]
            f["vol_regime_pctile"] = _pctile(window, atr_pct)
        else:
            f["vol_regime_pctile"] = None

        # trend age: bars since EMA stack (either direction) formed
        age = None
        if self.ema_f[i] and self.ema_m[i] and self.ema_s[i]:
            bull = self.ema_f[i] > self.ema_m[i] > self.ema_s[i]
            bear = self.ema_f[i] < self.ema_m[i] < self.ema_s[i]
            if bull or bear:
                age = 0
                k = i
                while k > 0:
                    kb = self.ema_f[k] and self.ema_m[k] and self.ema_s[k] and (
                        (bull and self.ema_f[k] > self.ema_m[k] > self.ema_s[k]) or
                        (bear and self.ema_f[k] < self.ema_m[k] < self.ema_s[k]))
                    if not kb:
                        break
                    age += 1
                    k -= 1
        f["trend_age_bars"] = age

        lb30 = max(0, i - 30 * bpd)
        hi30 = max(h[lb30:i + 1])
        lo30 = min(l[lb30:i + 1])
        f["dist_30d_high_pct"] = (hi30 - close) / close * 100.0
        f["dist_30d_low_pct"] = (close - lo30) / close * 100.0

        rng = h[i] - l[i]
        body = abs(c[i] - o[i])
        f["wick_ratio"] = ((rng - body) / rng) if rng > 0 else None

        rel_vol = v[i] / self.vol_sma[i] if self.vol_sma[i] else None
        f["rel_vol"] = rel_vol
        if rel_vol is not None and lb90 >= 0:
            window = [v[k] / self.vol_sma[k] for k in range(lb90, i) if self.vol_sma[k]]
            f["rel_vol_pctile"] = _pctile(window, rel_vol)
        else:
            f["rel_vol_pctile"] = None

        f["bb_bw"] = self.bb_bw[i]
        if self.bb_bw[i] is not None and lb90 >= 0:
            window = [x for x in self.bb_bw[lb90:i] if x is not None]
            f["bb_bw_pctile"] = _pctile(window, self.bb_bw[i])
        else:
            f["bb_bw_pctile"] = None

        streak = 0
        k = i
        while k > 0 and c[k] > c[k - 1]:
            streak += 1
            k -= 1
        if streak == 0:
            k = i
            while k > 0 and c[k] < c[k - 1]:
                streak -= 1
                k -= 1
        f["consec_closes"] = streak

        dt = datetime.fromtimestamp(ts[i], tz=timezone.utc)
        f["dow"] = dt.weekday()
        f["hour"] = dt.hour
        f["is_weekend"] = 1 if dt.weekday() >= 5 else 0

        if i >= bpd:
            f["ret_24h_pct"] = (close - c[i - bpd]) / c[i - bpd] * 100.0
        else:
            f["ret_24h_pct"] = None

        # realized vol, annualized %, from last 7d of bar returns
        lb7 = i - 7 * bpd
        if lb7 >= 1:
            rets = [(c[k] - c[k - 1]) / c[k - 1] for k in range(lb7, i + 1)]
            sd = _std(rets)
            bars_per_year = 365 * bpd
            f["rv7_ann_pct"] = sd * math.sqrt(bars_per_year) * 100.0 if sd is not None else None
            f["autocorr_7d"] = _autocorr1(rets)
        else:
            f["rv7_ann_pct"] = None
            f["autocorr_7d"] = None

        # vol-of-vol: atr_pct now vs 7d ago
        j = i - 7 * bpd
        if atr_pct is not None and j >= 0 and self.atr[j] and c[j]:
            prev = self.atr[j] / c[j] * 100.0
            f["vol_of_vol"] = (atr_pct - prev) / prev * 100.0 if prev else None
        else:
            f["vol_of_vol"] = None

        # HTF stretch: distance from 4h(or 1d) 50-EMA — approximate with slow EMA here
        f["stretch_slow_ema_pct"] = ((close - self.ema_s[i]) / self.ema_s[i] * 100.0
                                     if self.ema_s[i] else None)

        # ETH/BTC relative strength 7d (only meaningful joined per-pair)
        f["ethbtc_rs_7d"] = self._ethbtc_rs(ts[i])
        return f

    def _ethbtc_rs(self, at_ts):
        def close_at(pair, t):
            r = self.conn.execute(
                "SELECT close FROM candles WHERE pair=? AND ts<=? ORDER BY ts DESC LIMIT 1",
                (pair, t)).fetchone()
            return r[0] if r else None
        e_now = close_at("ETH-USD", at_ts)
        b_now = close_at("BTC-USD", at_ts)
        e_then = close_at("ETH-USD", at_ts - 7 * 86400)
        b_then = close_at("BTC-USD", at_ts - 7 * 86400)
        if None in (e_now, b_now, e_then, b_then) or b_now == 0 or b_then == 0:
            return None
        r_now = e_now / b_now
        r_then = e_then / b_then
        return (r_now - r_then) / r_then * 100.0 if r_then else None

    # ---------------- context features at signal time ----------------

    def _ctx_at(self, metric, pair, at_ts, max_age=CONTEXT_MAX_AGE_S):
        r = self.conn.execute(
            "SELECT value, ts FROM context WHERE metric=? AND pair=? AND ts<=?"
            " ORDER BY ts DESC LIMIT 1", (metric, pair, at_ts)).fetchone()
        if not r or at_ts - r[1] > max_age:
            return None
        return r[0]

    def _ctx_series(self, metric, pair, at_ts, lookback_s):
        rows = self.conn.execute(
            "SELECT value FROM context WHERE metric=? AND pair=? AND ts BETWEEN ? AND ?",
            (metric, pair, at_ts - lookback_s, at_ts)).fetchall()
        return [r[0] for r in rows]

    def context_features(self, at_ts):
        f = {}
        p = self.pair
        fr = self._ctx_at("funding_rate", p, at_ts)
        f["funding_rate"] = fr
        if fr is not None:
            hist = self._ctx_series("funding_rate", p, at_ts, 30 * 86400)
            sd = _std(hist)
            if sd and len(hist) >= 20:
                f["funding_z30"] = (fr - sum(hist) / len(hist)) / sd
            else:
                f["funding_z30"] = None
        else:
            f["funding_z30"] = None

        oi = self._ctx_at("open_interest", p, at_ts)
        f["open_interest"] = oi
        oi_prev = self._ctx_at("open_interest", p, at_ts - 86400, max_age=4 * 3600)
        f["oi_delta_24h_pct"] = ((oi - oi_prev) / oi_prev * 100.0
                                 if (oi is not None and oi_prev) else None)

        dvol = self._ctx_at("dvol", p, at_ts)
        f["dvol"] = dvol
        f["ob_imbalance_1pct"] = self._ctx_at("ob_imbalance_1pct", p, at_ts)
        f["spot_spread_bps"] = self._ctx_at("spot_spread_bps", p, at_ts)
        f["perp_spread_bps"] = self._ctx_at("perp_spread_bps", p, at_ts)
        f["fear_greed"] = self._ctx_at("fear_greed", "GLOBAL", at_ts, max_age=26 * 3600)
        f["btc_dominance"] = self._ctx_at("btc_dominance", "GLOBAL", at_ts, max_age=4 * 3600)
        return f

    def features_for_trade(self, trade):
        sig_ts = trade["signal_ts"]
        i = self.index.get(sig_ts)
        f = self.ohlcv_features(i) if i is not None else {}
        ctx = self.context_features(sig_ts)
        f.update(ctx)
        # IV - RV spread once both halves exist
        if f.get("dvol") is not None and f.get("rv7_ann_pct") is not None:
            f["iv_rv_spread"] = f["dvol"] - f["rv7_ann_pct"]
        else:
            f["iv_rv_spread"] = None
        return f


    def at(self, signal_ts, names=None):
        """Features at one signal bar, cached (a filter asks per bar). With
        names given and all of them OHLCV-derived, the context table is not
        queried at all."""
        key = (signal_ts, "ohlcv")
        if key not in self._cache:
            i = self.index.get(signal_ts)
            self._cache[key] = self.ohlcv_features(i) if i is not None else {}
        f = self._cache[key]
        if names is not None and all(n in f for n in names):
            return f
        if signal_ts not in self._cache:
            self._cache[signal_ts] = self.features_for_trade({"signal_ts": signal_ts})
        return self._cache[signal_ts]


    def regime(self, signal_ts):
        """Daily regime tag at the signal (RegimeTagger, built on first use
        with this computer's bounds: the same history adversary A9 tags with)."""
        if self._regime is None:
            self._regime = RegimeTagger(self.conn, self.pair, *self._bounds)
        return self._regime.at(signal_ts)


class RegimeTagger:
    """trend_up / trend_down / chop for a signal, or None while the daily
    EMA / ADX are still warming up (or the previous day has no candles)."""

    def __init__(self, conn, pair, start_ts=None, end_ts=None):
        ts, o, h, l, c, v = engine.load_candles(
            conn, pair, end_ts if end_ts is not None else 2 ** 62, REGIME_TF, start_ts)
        self.ema = engine.ema(c, REGIME_EMA)
        self.adx = engine.dmi(h, l, c, 14, 14)
        self.index = {t: i for i, t in enumerate(ts)}

    def at(self, signal_ts):
        i = self.index.get(signal_ts // REGIME_TF * REGIME_TF - REGIME_TF)
        if (i is None or i < REGIME_SLOPE_DAYS or self.adx[i] is None
                or self.ema[i - REGIME_SLOPE_DAYS] is None):
            return None
        if self.adx[i] < REGIME_ADX:
            return "chop"
        return "trend_up" if self.ema[i] > self.ema[i - REGIME_SLOPE_DAYS] else "trend_down"


def validate_filters(filters):
    for flt in filters:
        if set(flt) != {"feature", "op", "value"}:
            raise ValueError(f"filter needs exactly feature/op/value: {flt!r}")
        if flt["feature"] in CATEGORICAL:
            if flt["op"] not in CATEGORICAL_OPS:
                raise ValueError(f"{flt['feature']} filter op must be one of "
                                 f"{sorted(CATEGORICAL_OPS)}: {flt!r}")
            if flt["value"] not in CATEGORICAL[flt["feature"]]:
                raise ValueError(f"{flt['feature']} must be one of "
                                 f"{CATEGORICAL[flt['feature']]}: {flt!r}")
        elif flt["op"] not in FILTER_OPS or isinstance(flt["value"], str):
            raise ValueError(f"filter op must be one of {sorted(FILTER_OPS)} with a number: {flt!r}")


def describe(flt):
    v = flt["value"]
    return f"{flt['feature']} {flt['op']} {v if isinstance(v, str) else format(v, 'g')}"


def check_filters(fc, filters, signal_ts):
    """True if every filter holds at signal_ts. None blocks (a regime still in
    its daily warmup too); a name that is not a feature raises (a typo must
    not silently block every trade)."""
    numeric = [flt for flt in filters if flt["feature"] not in CATEGORICAL]
    f = fc.at(signal_ts, [flt["feature"] for flt in numeric]) if numeric else {}
    for flt in filters:
        if flt["feature"] == "regime":
            v, op = fc.regime(signal_ts), CATEGORICAL_OPS[flt["op"]]
        elif flt["feature"] not in f:
            raise KeyError(f"unknown feature {flt['feature']!r} in cfg['filters']")
        else:
            v, op = f[flt["feature"]], FILTER_OPS[flt["op"]]
        if v is None or not op(v, flt["value"]):
            return False
    return True


def ensure_tables(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS trade_features (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        candidate_id INTEGER,
        pair TEXT, tf_seconds INTEGER,
        signal_ts INTEGER, entry_ts INTEGER, direction TEXT,
        pnl REAL, reason TEXT,
        features_json TEXT
    )""")
    have = {r[1] for r in conn.execute("PRAGMA table_info(trade_features)")}
    if "regime" not in have:
        conn.execute("ALTER TABLE trade_features ADD COLUMN regime TEXT")
    conn.commit()


def annotate_trades(conn, candidate_id, pair, tf_seconds, trades):
    """Compute and store the feature vector for every trade. Returns count."""
    ensure_tables(conn)
    fc = FeatureComputer(conn, pair, tf_seconds)
    rt = RegimeTagger(conn, pair)
    # compute everything before the first INSERT: the write lock is then held
    # for the inserts only, not the feature work (the collector writes too)
    rows = [(candidate_id, pair, tf_seconds, t["signal_ts"], t["entry_ts"], t["dir"], t["pnl"],
             t["reason"], json.dumps(fc.features_for_trade(t)), rt.at(t["signal_ts"]))
            for t in trades]
    conn.executemany(
        "INSERT INTO trade_features (candidate_id, pair, tf_seconds, signal_ts,"
        " entry_ts, direction, pnl, reason, features_json, regime)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    conn.commit()
    return len(rows)


def backfill_regime(conn, progress=True):
    """Tag trade_features rows that have no regime yet. Returns rows updated.
    Rows whose signal is still in the daily warmup stay NULL."""
    ensure_tables(conn)
    n = 0
    for (pair,) in conn.execute("SELECT DISTINCT pair FROM trade_features"
                                " WHERE regime IS NULL").fetchall():
        rt = RegimeTagger(conn, pair)
        rows = conn.execute("SELECT id, signal_ts FROM trade_features"
                            " WHERE pair=? AND regime IS NULL", (pair,)).fetchall()
        upd = [(rt.at(sig), rid) for rid, sig in rows]
        upd = [u for u in upd if u[0] is not None]
        conn.executemany("UPDATE trade_features SET regime=? WHERE id=?", upd)
        conn.commit()
        n += len(upd)
        if progress:
            print(f"  {pair}: tagged {len(upd)} of {len(rows)} rows", flush=True)
    return n


def main():
    import argparse
    import os
    ap = argparse.ArgumentParser(description="trade_features maintenance")
    ap.add_argument("--backfill-regime", action="store_true",
                    help="tag every trade_features row that has no regime (P3.4)")
    args = ap.parse_args()
    if not args.backfill_regime:
        ap.error("nothing to do; pass --backfill-regime")
    conn = sqlite3.connect(os.environ.get("FORGE_DB", "/data/forge.db"))
    n = backfill_regime(conn)
    print(f"tagged {n} rows")
    for reg, cnt, net in conn.execute(
            "SELECT COALESCE(regime, '(warmup)'), COUNT(*), SUM(pnl) FROM trade_features"
            " GROUP BY 1 ORDER BY 1"):
        print(f"  {reg:10s} {cnt:6d} trades  net {net:+.0f}")
    print("Read: regime is the daily market state at signal time (50-EMA 5-day slope,")
    print(f"ADX(14) >= {REGIME_ADX:g} = trending). Net is summed over overlapping candidates,")
    print("so it says where the annotated strategies made money, not how much.")
    conn.close()


if __name__ == "__main__":
    main()
