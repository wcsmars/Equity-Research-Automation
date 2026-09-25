"""Shared numeric/series helpers used across providers and models.

Pure functions, no I/O. Keep these dependency-light (stdlib + math only) so every
module can import them without pulling in heavy packages.
"""

from __future__ import annotations

import math
from typing import Optional, Sequence


def safe_div(num: Optional[float], den: Optional[float]) -> Optional[float]:
    """Division that returns None on zero/None/NaN inputs instead of raising."""
    if num is None or den is None:
        return None
    try:
        if den == 0 or _isnan(den) or _isnan(num):
            return None
        return num / den
    except (TypeError, ZeroDivisionError):
        return None


def _isnan(x: object) -> bool:
    return isinstance(x, float) and math.isnan(x)


def is_num(x: object) -> bool:
    """True if x is a finite real number."""
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def clean(values: Sequence[Optional[float]]) -> list[float]:
    """Drop None/NaN/inf from a sequence, returning a list of finite floats."""
    return [float(v) for v in values if is_num(v)]


def cagr(first: Optional[float], last: Optional[float], periods: int) -> Optional[float]:
    """Compound annual growth rate between first and last over `periods` years.

    Returns None if inputs are non-positive (sign change makes CAGR meaningless).
    """
    if not is_num(first) or not is_num(last) or periods <= 0:
        return None
    if first <= 0 or last <= 0:
        return None
    return (last / first) ** (1.0 / periods) - 1.0


def series_cagr(values: Sequence[Optional[float]],
                years: Optional[Sequence[Optional[float]]] = None) -> Optional[float]:
    """CAGR between the first and last *positive* entries of an annual series.

    Providers zero-fill missing years, so zero/None/NaN entries are skipped as
    endpoints, but the period count is the real distance between the endpoints
    (fiscal-year difference when ``years`` aligns with ``values``, else the index
    distance) so a gap in the middle is not compressed into fewer years.
    """
    values = list(values or [])
    pts = [(i, v) for i, v in enumerate(values) if is_num(v) and v > 0]
    if len(pts) < 2:
        return None
    (i0, v0), (i1, v1) = pts[0], pts[-1]
    periods = i1 - i0
    years = list(years or [])
    if len(years) == len(values):
        y0, y1 = years[i0], years[i1]
        if is_num(y0) and is_num(y1) and y0 > 0 and y1 > y0:
            periods = int(round(y1 - y0))
    return cagr(v0, v1, periods)


def incremental_ratio(changes: Sequence[Optional[float]],
                      levels: Sequence[Optional[float]]) -> Optional[float]:
    """Pooled ``sum(changes_i) / sum(levels_i - levels_{i-1})`` over usable years.

    Used for dNWC per unit of revenue change. Pooling (a change-weighted mean)
    keeps one near-flat year from dominating, as a mean of per-year ratios would.
    A year counts only when its change is finite and both levels are positive
    (zero-filled years are gaps, not data). Returns None if nothing is usable or
    the pooled level change is zero.
    """
    changes = list(changes or [])
    levels = list(levels or [])
    num = den = 0.0
    used = False
    for i in range(1, min(len(changes), len(levels))):
        cur, prev, chg = levels[i], levels[i - 1], changes[i]
        if is_num(chg) and is_num(cur) and is_num(prev) and cur > 0 and prev > 0:
            num += chg
            den += cur - prev
            used = True
    return safe_div(num, den) if used else None


def mean(values: Sequence[Optional[float]]) -> Optional[float]:
    vals = clean(values)
    return sum(vals) / len(vals) if vals else None


def median(values: Sequence[Optional[float]]) -> Optional[float]:
    vals = sorted(clean(values))
    n = len(vals)
    if n == 0:
        return None
    mid = n // 2
    return vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2.0


def percentile(values: Sequence[Optional[float]], q: float) -> Optional[float]:
    """Linear-interpolation percentile, q in [0, 1]."""
    vals = sorted(clean(values))
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0]
    idx = q * (len(vals) - 1)
    lo = int(math.floor(idx))
    hi = int(math.ceil(idx))
    if lo == hi:
        return vals[lo]
    frac = idx - lo
    return vals[lo] * (1 - frac) + vals[hi] * frac


def summary_stats(values: Sequence[Optional[float]]) -> dict:
    """{'median','mean','min','max','p25','p75','n'} for a sequence of multiples."""
    vals = clean(values)
    return {
        "n": len(vals),
        "mean": mean(vals),
        "median": median(vals),
        "min": min(vals) if vals else None,
        "max": max(vals) if vals else None,
        "p25": percentile(vals, 0.25),
        "p75": percentile(vals, 0.75),
    }


def net_debt_parts(bs: object) -> tuple[float, list[str]]:
    """(total_debt - cash, notes) from a balance-sheet snapshot, component-wise.

    A missing/non-finite component is treated as 0 on its own (with a note), so
    a missing cash figure no longer discards a known debt balance, and a None
    field cannot raise the way ``BalanceSheetSnapshot.net_debt`` would.
    """
    if bs is None:
        return 0.0, ["balance sheet unavailable; net debt assumed 0"]
    notes: list[str] = []
    debt = getattr(bs, "total_debt", None)
    cash = getattr(bs, "cash_and_investments", None)
    if not is_num(debt):
        debt = 0.0
        notes.append("total debt unavailable; assumed 0 in net debt")
    if not is_num(cash):
        cash = 0.0
        notes.append("cash & investments unavailable; assumed 0 in net debt")
    return float(debt) - float(cash), notes


def trim_outliers(values: Sequence[float], factor: float) -> list[float]:
    """Keep values within [median/factor, median*factor]. factor>1, e.g. 3.0.

    Only trims positive multiples; non-positive values are dropped (a negative P/E
    is meaningless for applying to the target). With fewer than three values there
    is no basis for calling one an outlier (and the arithmetic median of two
    always sits nearer the larger), so they are returned untrimmed.
    """
    pos = [v for v in clean(values) if v > 0]
    if len(pos) < 3:
        return pos
    med = median(pos)
    if med is None or med <= 0:
        return pos
    lo, hi = med / factor, med * factor
    return [v for v in pos if lo <= v <= hi]


def fade_path(start: float, end: float, n: int) -> list[float]:
    """Linearly interpolate from `start` to `end` over n steps (inclusive of end).

    Used to fade growth/margins from a near-term level toward a terminal level.
    Returns n values; the last equals `end`.
    """
    if n <= 0:
        return []
    if n == 1:
        return [end]
    return [start + (end - start) * i / (n - 1) for i in range(n)]
