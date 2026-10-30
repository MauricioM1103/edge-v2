"""Kelly Criterion bet sizing.

f* = (b*p - q) / b   where b = decimal_odds - 1, p = win prob, q = 1 - p.
Returns the FRACTION of bankroll to risk; always clamped to [0, 1].
"""
from __future__ import annotations

from typing import Optional

from .odds import american_to_decimal


def kelly_fraction(p_win, american, multiplier: float = 1.0) -> Optional[float]:
    """Kelly fraction of bankroll at American `american`, win prob `p_win`.

    `multiplier` lets the caller pick quarter-Kelly (0.25), half-Kelly (0.5), etc.
    Returns 0.0 for -EV bets. Returns None if inputs invalid.
    """
    try:
        p = float(p_win) if p_win is not None else None
        m = float(multiplier) if multiplier is not None else 1.0
    except (TypeError, ValueError):
        return None
    if p is None or p < 0 or p > 1:
        return None
    d = american_to_decimal(american)
    if d is None or d <= 1.0:
        return None
    b = d - 1.0
    q = 1.0 - p
    f = (b * p - q) / b
    if f <= 0:
        return 0.0
    f = f * m
    return max(0.0, min(1.0, f))


def kelly_stake(bankroll, p_win, american, multiplier: float = 1.0) -> Optional[float]:
    """Dollar stake per the Kelly fraction scaled by `multiplier`."""
    try:
        bk = float(bankroll) if bankroll is not None else None
    except (TypeError, ValueError):
        return None
    if bk is None or bk <= 0:
        return None
    f = kelly_fraction(p_win, american, multiplier)
    if f is None:
        return None
    return bk * f
