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

Features never affect trading decisions. They exist to be analyzed later:
which conditions separate winning signals from losing ones, validated
in-sample -> out-of-sample before anything is ever promoted to a filter.
"""

import json
import math
import sqlite3
from datetime import datetime, timezone

import engine

CONTEXT_MAX_AGE_S = 2 * 3600     # a context poll must be within 2h of signal


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
    def __init__(self, conn, pair, tf_seconds=3600):
        self.conn = conn
        self.pair = pair
        self.tf = tf_seconds
        ts, o, h, l, c, v = engine.load_candles(conn, pair, 2 ** 62, tf_seconds)
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
        f = self.ohlcv_features(i) if i is not None and i > 200 * self.bars_per_day // 24 else {}
        if i is not None:
            f = self.ohlcv_features(i)
        ctx = self.context_features(sig_ts)
        f.update(ctx)
        # IV - RV spread once both halves exist
        if f.get("dvol") is not None and f.get("rv7_ann_pct") is not None:
            f["iv_rv_spread"] = f["dvol"] - f["rv7_ann_pct"]
        else:
            f["iv_rv_spread"] = None
        return f


def ensure_tables(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS trade_features (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        candidate_id INTEGER,
        pair TEXT, tf_seconds INTEGER,
        signal_ts INTEGER, entry_ts INTEGER, direction TEXT,
        pnl REAL, reason TEXT,
        features_json TEXT
    )""")
    conn.commit()


def annotate_trades(conn, candidate_id, pair, tf_seconds, trades):
    """Compute and store the feature vector for every trade. Returns count."""
    ensure_tables(conn)
    fc = FeatureComputer(conn, pair, tf_seconds)
    n = 0
    for t in trades:
        feats = fc.features_for_trade(t)
        conn.execute(
            "INSERT INTO trade_features (candidate_id, pair, tf_seconds, signal_ts,"
            " entry_ts, direction, pnl, reason, features_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (candidate_id, pair, tf_seconds, t["signal_ts"], t["entry_ts"],
             t["dir"], t["pnl"], t["reason"], json.dumps(feats)),
        )
        n += 1
    conn.commit()
    return n
