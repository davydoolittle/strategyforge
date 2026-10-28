"""
StrategyForge indicators — the bar-series maths shared by every strategy
family (app/strategies/) and by features.py.

Moved verbatim out of engine.py in P5.2; engine re-exports them, so
`engine.ema(...)` keeps working. Each takes plain lists and returns a list of
the same length with None until the indicator has warmed up. Seeding matches
the validated V4.1 engine (EMA seeds from an SMA of the first n values, RMA
likewise), not Pine's ta.ema.
"""

from datetime import datetime, timezone


def sma(vals, n):
    out = [None] * len(vals)
    s = 0.0
    for i, v in enumerate(vals):
        s += v
        if i >= n:
            s -= vals[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def rma(vals, n):
    out = [None] * len(vals)
    seed = None
    s = 0.0
    for i, v in enumerate(vals):
        if v is None:
            continue
        if seed is None:
            s += v
            if i >= n - 1:
                seed = s / n
                out[i] = seed
        else:
            seed = (seed * (n - 1) + v) / n
            out[i] = seed
    return out


def ema(vals, n):
    out = [None] * len(vals)
    alpha = 2.0 / (n + 1)
    prev = None
    s = 0.0
    for i, v in enumerate(vals):
        if prev is None:
            s += v
            if i >= n - 1:
                prev = s / n
                out[i] = prev
        else:
            prev = alpha * v + (1 - alpha) * prev
            out[i] = prev
    return out


def rsi(close, n):
    gains = [max(close[i] - close[i - 1], 0.0) for i in range(1, len(close))]
    losses = [max(close[i - 1] - close[i], 0.0) for i in range(1, len(close))]
    ag = rma(gains, n)
    al = rma(losses, n)
    out = [None] * len(close)
    for i in range(1, len(close)):
        a, b = ag[i - 1], al[i - 1]
        if a is None or b is None:
            continue
        out[i] = 100.0 if b == 0 else 100.0 - 100.0 / (1.0 + a / b)
    return out


def true_range(h, l, c):
    out = [h[0] - l[0]]
    for i in range(1, len(h)):
        out.append(max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])))
    return out


def atr(h, l, c, n):
    return rma(true_range(h, l, c), n)


def dmi(h, l, c, di_len, adx_len):
    plus_dm = [0.0]
    minus_dm = [0.0]
    for i in range(1, len(h)):
        up = h[i] - h[i - 1]
        dn = l[i - 1] - l[i]
        plus_dm.append(up if (up > dn and up > 0) else 0.0)
        minus_dm.append(dn if (dn > up and dn > 0) else 0.0)
    tr_s = rma(true_range(h, l, c), di_len)
    pdm_s = rma(plus_dm, di_len)
    mdm_s = rma(minus_dm, di_len)
    dx = [None] * len(h)
    for i in range(len(h)):
        if tr_s[i] and pdm_s[i] is not None and mdm_s[i] is not None and tr_s[i] > 0:
            dip = 100.0 * pdm_s[i] / tr_s[i]
            dim = 100.0 * mdm_s[i] / tr_s[i]
            denom = dip + dim
            dx[i] = 0.0 if denom == 0 else 100.0 * abs(dip - dim) / denom
    return rma(dx, adx_len)


def donchian_prev(h, l, n):
    hi = [None] * len(h)
    lo = [None] * len(h)
    for i in range(n, len(h)):
        hi[i] = max(h[i - n:i])
        lo[i] = min(l[i - n:i])
    return hi, lo


def vwap_daily(ts, h, l, c, v):
    out = [None] * len(c)
    day = None
    pv = 0.0
    vv = 0.0
    for i in range(len(c)):
        d = datetime.fromtimestamp(ts[i], tz=timezone.utc).date()
        if d != day:
            day, pv, vv = d, 0.0, 0.0
        typ = (h[i] + l[i] + c[i]) / 3.0
        pv += typ * v[i]
        vv += v[i]
        out[i] = pv / vv if vv > 0 else None
    return out


def htf_confirmed(ts, c, htf_seconds, ema_len):
    bucket_map = {}
    for i in range(len(ts)):
        bucket_map[ts[i] // htf_seconds] = c[i]
    keys = sorted(bucket_map)
    closes = [bucket_map[k] for k in keys]
    emas = ema(closes, ema_len)
    key_index = {k: i for i, k in enumerate(keys)}
    out_c = [None] * len(ts)
    out_e = [None] * len(ts)
    for i in range(len(ts)):
        b = ts[i] // htf_seconds - 1
        if b in key_index:
            j = key_index[b]
            out_c[i] = closes[j]
            out_e[i] = emas[j]
    return out_c, out_e
