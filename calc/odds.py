"""Odds conversion and implied probability.

All functions tolerate None/0/"" by returning None. No raises on bad input.
"""
from __future__ import annotations

from fractions import Fraction
from typing import Optional


def _coerce(x) -> Optional[float]:
    if x is None or x == "":
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def american_to_decimal(american) -> Optional[float]:
    """American moneyline (e.g. -110, +150) -> decimal odds (e.g. 1.9091)."""
    a = _coerce(american)
    if a is None or a == 0:
        return None
    if a > 0:
        return 1.0 + a / 100.0
    return 1.0 + 100.0 / abs(a)


def decimal_to_american(decimal) -> Optional[int]:
    """Decimal odds -> American moneyline, rounded to nearest integer."""
    d = _coerce(decimal)
    if d is None or d <= 1.0:
        return None
    if d >= 2.0:
        return int(round((d - 1.0) * 100.0))
    return int(round(-100.0 / (d - 1.0)))


def american_to_fractional(american) -> Optional[str]:
    """American -> 'num/den' fractional odds, limited to reasonable denominators."""
    a = _coerce(american)
    if a is None or a == 0:
        return None
    if a > 0:
        frac = Fraction(a / 100.0).limit_denominator(100)
    else:
        frac = Fraction(100.0 / abs(a)).limit_denominator(100)
    return f"{frac.numerator}/{frac.denominator}"


def implied_prob_american(american) -> Optional[float]:
    """Vig-inclusive implied win probability from an American price."""
    a = _coerce(american)
    if a is None or a == 0:
        return None
    if a > 0:
        return 100.0 / (a + 100.0)
    return abs(a) / (abs(a) + 100.0)


def implied_prob_decimal(decimal) -> Optional[float]:
    """Vig-inclusive implied probability from decimal odds."""
    d = _coerce(decimal)
    if d is None or d <= 1.0:
        return None
    return 1.0 / d


def no_vig_two_way(american_a, american_b):
    """Remove the overround from a two-outcome market.

    Returns (p_a_fair, p_b_fair) with p_a + p_b = 1.0, or (None, None).
    """
    pa = implied_prob_american(american_a)
    pb = implied_prob_american(american_b)
    if pa is None or pb is None:
        return (None, None)
    total = pa + pb
    if total <= 0:
        return (None, None)
    return (pa / total, pb / total)


def profit_on_win(stake, american) -> Optional[float]:
    """Dollars won on a winning bet of `stake` at American `american`."""
    s = _coerce(stake)
    a = _coerce(american)
    if s is None or a is None or s <= 0 or a == 0:
        return None
    if a > 0:
        return s * a / 100.0
    return s * 100.0 / abs(a)


def format_american(a) -> str:
    """Display helper: -110, +150, or '-' for missing."""
    v = _coerce(a)
    if v is None:
        return "-"
    i = int(round(v))
    return f"+{i}" if i > 0 else f"{i}"
