"""
TSP rules: the hard constraints every other tsp_* module goes through.

  - An IFT sets whole-percent targets that add to 100 across the whole balance.
  - Per calendar month: the first `flexible_moves_per_month` (2) IFTs may go
    in any direction. After that an IFT may only move money INTO G: no
    non-G fund may end with a larger share than it had.
  - A request submitted before `cutoff_et` (12:00 ET) on a business day
    fills at that day's close, otherwise at the next business day's close.
    Backtests use fill_lag_days (default 1): a signal on day T's close is
    submitted the next morning and fills at T+1's close. 0 is refused: it
    would trade at the close the signal was computed from (look-ahead).
  - Long only, no leverage, no costs.

A move is counted in the month of its FILL (the date TSP processes it).
Nothing in this module talks to TSP. It only checks what a person would do.
"""

import json
import math
import os
from datetime import date, datetime, time as dtime, timedelta, timezone

CONFIG_PATH = os.environ.get(
    "TSP_CONFIG", os.path.join(os.path.dirname(os.path.abspath(__file__)), "tsp_config.json"))
TOL = 1e-9
# a G-only move is computed after day t's close but fills at a later close;
# funds it keeps are capped this many points under their current share so a
# day of drift cannot turn it into a move out of G
G_ONLY_DRIFT_MARGIN = 1.0


class RuleError(ValueError):
    pass


def load_config(path=None):
    with open(path or CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    lag = cfg["rules"]["fill_lag_days"]
    if not isinstance(lag, int) or lag < 1:
        raise RuleError(f"rules.fill_lag_days must be an int >= 1, got {lag!r}: 0 would fill at "
                        "the same close the signal was computed from (look-ahead)")
    validate_allocation(cfg["base_allocation"], cfg["funds"])
    return cfg


# ------------------------------------------------------------- allocations ---

def validate_allocation(alloc, funds=None):
    """Whole percents, >= 0, adding to exactly 100. Returns a clean dict
    without zero entries; raises RuleError otherwise."""
    if not alloc:
        raise RuleError("empty allocation")
    out = {}
    for f, p in alloc.items():
        if funds is not None and f not in funds:
            raise RuleError(f"unknown fund {f!r}; known: {funds}")
        if isinstance(p, bool) or not isinstance(p, int) and not (isinstance(p, float) and p.is_integer()):
            raise RuleError(f"{f}: {p!r} is not a whole percent")
        p = int(p)
        if p < 0:
            raise RuleError(f"{f}: {p}% is negative (long only)")
        if p:
            out[f] = p
    if sum(out.values()) != 100:
        raise RuleError(f"allocation adds to {sum(out.values())}%, not 100%: {alloc}")
    return out


def parse_allocation(text, funds=None):
    """'C=60,S=20,G=20' -> {'C': 60, 'S': 20, 'G': 20} (validated)."""
    alloc = {}
    for part in text.split(","):
        k, _, v = part.partition("=")
        if not v:
            raise RuleError(f"bad allocation item {part!r}: use FUND=PCT")
        alloc[k.strip()] = int(v)
    return validate_allocation(alloc, funds)


def fmt_alloc(alloc):
    return " ".join(f"{f} {p:g}%" for f, p in sorted(alloc.items(), key=lambda kv: -kv[1]) if p)


def is_g_only_move(current_weights, target):
    """True if going from current_weights (fractions or percents, as held at
    the fill close) to target (percents) moves money only INTO G: no non-G
    fund ends with a larger share than it has now."""
    cur = _as_pct(current_weights)
    for f, p in target.items():
        if f != "G" and p > cur.get(f, 0.0) + TOL:
            return False
    return True


def _as_pct(w):
    tot = sum(w.values())
    if tot <= 0:
        return {}
    scale = 100.0 / tot
    return {f: v * scale for f, v in w.items()}


def g_only_version(current_weights, target, held):
    """The closest move toward `target` that only moves money into G, or
    None if the target wants no non-G fund cut below what is `held` (the
    last allocation set). Funds the target cuts go to the target share;
    the others keep their held share; each is capped at its current
    (drifted) share rounded down, and the rest goes to G. Always allowed,
    even with the flexible budget spent, and it does not repeat: once it is
    held, the target cuts nothing further."""
    cur = _as_pct(current_weights)
    cuts = [f for f in held if f != "G" and target.get(f, 0) < held[f]]
    if not cuts:
        return None
    out = {}
    for f in set(held) | set(cur):
        if f == "G":
            continue
        want = target.get(f, 0) if f in cuts else held.get(f, 0)
        keep = min(want, int(math.floor(cur.get(f, 0.0) - G_ONLY_DRIFT_MARGIN + TOL)))
        if keep > 0:
            out[f] = keep
    out["G"] = 100 - sum(out.values())
    return validate_allocation(out)


# -------------------------------------------------------------- move budget ---

class MoveBudget:
    """Counts filled IFTs per calendar month of the fill date."""

    def __init__(self, flexible_per_month=2):
        self.flexible = flexible_per_month
        self.used = {}          # (year, month) -> flexible moves used
        self.g_only = {}        # (year, month) -> moves made under the G rule

    @staticmethod
    def key(d):
        return (d.year, d.month)

    def remaining(self, d):
        return max(0, self.flexible - self.used.get(self.key(d), 0))

    def check(self, d, current_weights, target):
        """('flexible' | 'g_only') if the move may fill on date d; raises
        RuleError if it may not."""
        if self.remaining(d) > 0:
            return "flexible"
        if is_g_only_move(current_weights, target):
            return "g_only"
        raise RuleError(f"{d}: both flexible IFTs of {d:%Y-%m} are used and this move is not "
                        f"into G only (target {fmt_alloc(target)})")

    def record(self, d, kind):
        bucket = self.used if kind == "flexible" else self.g_only
        bucket[self.key(d)] = bucket.get(self.key(d), 0) + 1


def verify_audit(rows, flexible_per_month=2):
    """Independent check of a filled-IFT log: at most `flexible_per_month`
    flexible moves per fill month, and every move beyond them G-only.
    rows: dicts with fill_date (date or ISO str), kind, weights_before (pct),
    target (pct), status. Returns a list of violations (empty = clean)."""
    seen, bad = {}, []
    for r in rows:
        if r.get("status") != "filled":
            continue
        d = _to_date(r["fill_date"])
        k = (d.year, d.month)
        seen[k] = seen.get(k, 0) + 1
        if seen[k] > flexible_per_month and not is_g_only_move(r["weights_before"], r["target"]):
            bad.append(f"{d}: move #{seen[k]} of {d:%Y-%m} is not G-only "
                       f"({fmt_alloc(r['target'])})")
        if r.get("kind") == "flexible" and seen[k] > flexible_per_month:
            bad.append(f"{d}: move #{seen[k]} of {d:%Y-%m} was booked as flexible")
    return bad


def _to_date(d):
    return d if isinstance(d, date) else date.fromisoformat(str(d)[:10])


# ------------------------------------------------------------ time and days ---

def _nth_weekday(y, m, weekday, n):
    """n-th (1-based) weekday of a month; n=-1 is the last."""
    if n > 0:
        d = date(y, m, 1)
        d += timedelta((weekday - d.weekday()) % 7)
        return d + timedelta(7 * (n - 1))
    d = date(y + (m == 12), m % 12 + 1, 1) - timedelta(1)
    return d - timedelta((d.weekday() - weekday) % 7)


def et_offset_hours(dt_utc):
    """US Eastern offset (-4 or -5) for an aware UTC datetime. Stdlib only:
    the container may have no tz database. Rules since 2007: DST from the
    2nd Sunday of March 02:00 to the 1st Sunday of November 02:00; before:
    1st Sunday of April to the last Sunday of October."""
    y = dt_utc.year
    if y >= 2007:
        start, end = _nth_weekday(y, 3, 6, 2), _nth_weekday(y, 11, 6, 1)
    else:
        start, end = _nth_weekday(y, 4, 6, 1), _nth_weekday(y, 10, 6, -1)
    start_utc = datetime.combine(start, dtime(7), timezone.utc)     # 02:00 EST
    end_utc = datetime.combine(end, dtime(6), timezone.utc)         # 02:00 EDT
    return -4 if start_utc <= dt_utc < end_utc else -5


def to_et(dt_utc):
    """Aware UTC datetime -> naive US Eastern wall-clock datetime."""
    return (dt_utc + timedelta(hours=et_offset_hours(dt_utc))).replace(tzinfo=None)


def et_to_utc(d, hhmm):
    """date + 'HH:MM' Eastern -> aware UTC datetime."""
    h, m = map(int, hhmm.split(":"))
    guess = datetime.combine(d, dtime(h, m), timezone.utc) + timedelta(hours=5)
    return guess - timedelta(hours=5 + et_offset_hours(guess))


def _easter(y):
    a, b, c = y % 19, y // 100, y % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l_ = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l_) // 451
    month = (h + l_ - 7 * m + 114) // 31
    return date(y, month, (h + l_ - 7 * m + 114) % 31 + 1)


def _observed(d):
    return d + timedelta(1) if d.weekday() == 6 else d - timedelta(1) if d.weekday() == 5 else d


def market_holidays(y):
    """NYSE full-day holidays (TSP posts no prices on them). Rule-based; the
    one-off closures (e.g. 2012-10-29/30 Sandy, national days of mourning)
    are not here. Backtests use the price dates themselves, so this only
    decides 'next business day' for the live deadline."""
    hs = {
        _nth_weekday(y, 1, 0, 3),        # MLK day
        _nth_weekday(y, 2, 0, 3),        # Presidents' day
        _easter(y) - timedelta(2),       # Good Friday
        _nth_weekday(y, 5, 0, -1),       # Memorial day
        _observed(date(y, 7, 4)),
        _nth_weekday(y, 9, 0, 1),        # Labor day
        _nth_weekday(y, 11, 3, 4),       # Thanksgiving
        _observed(date(y, 12, 25)),
    }
    ny = date(y, 1, 1)
    if ny.weekday() != 5:                # NYSE does not observe a Saturday New Year on Dec 31
        hs.add(_observed(ny))
    if y >= 2022:
        hs.add(_observed(date(y, 6, 19)))
    return hs


def is_business_day(d):
    return d.weekday() < 5 and d not in market_holidays(d.year)


def next_business_day(d):
    d += timedelta(1)
    while not is_business_day(d):
        d += timedelta(1)
    return d


def previous_business_day(d):
    d -= timedelta(1)
    while not is_business_day(d):
        d -= timedelta(1)
    return d


def fill_date_for_request(submitted_utc, cutoff_et="12:00"):
    """The close an IFT submitted at `submitted_utc` fills at (live rule)."""
    et = to_et(submitted_utc)
    h, m = map(int, cutoff_et.split(":"))
    if is_business_day(et.date()) and et.time() < dtime(h, m):
        return et.date()
    return next_business_day(et.date())


def fill_index(signal_index, n_days, lag):
    """Backtest rule on a business-day calendar: a signal on day index i
    fills at the close of day i + lag. None if that is past the data."""
    if lag < 1:
        raise RuleError("fill lag must be >= 1 business day (no same-close fills)")
    j = signal_index + lag
    return j if j < n_days else None


def deadline(now_utc, cutoff_et="12:00"):
    """(fill date, deadline as aware UTC) for a request made now."""
    fd = fill_date_for_request(now_utc, cutoff_et)
    return fd, et_to_utc(fd, cutoff_et)


def business_days_left_in_month(d, calendar=None):
    """Business days after d in d's month (d excluded). With `calendar` (a
    sorted list of dates) the count comes from it, else from the rules."""
    if calendar is not None:
        return sum(1 for x in calendar if x > d and (x.year, x.month) == (d.year, d.month))
    n, x = 0, next_business_day(d)
    while (x.year, x.month) == (d.year, d.month):
        n, x = n + 1, next_business_day(x)
    return n
