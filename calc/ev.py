"""Expected value calculator.

EV = p_win * profit_on_win - p_lose * stake
"""
from __future__ import annotations

from typing import Optional

from .odds import profit_on_win, implied_prob_american


def expected_value(p_win, stake, american) -> Optional[float]:
    """Dollar EV of a bet at `american` odds with projected win prob `p_win`.

    Returns None if inputs are missing or invalid.
    """
    try:
        p = float(p_win) if p_win is not None else None
        s = float(stake) if stake is not None else None
    except (TypeError, ValueError):
        return None
    if p is None or s is None:
        return None
    if p < 0 or p > 1 or s <= 0:
        return None
    prof = profit_on_win(s, american)
    if prof is None:
        return None
    return p * prof - (1.0 - p) * s


def ev_percent(p_win, american) -> Optional[float]:
    """EV as a percentage of stake (independent of stake size).

    Returns None on invalid input.
    """
    ev = expected_value(p_win, 100.0, american)
    if ev is None:
        return None
    return ev / 100.0


def edge(p_win, american) -> Optional[float]:
    """Difference between projected probability and vig-inclusive implied prob.

    Positive edge = your model thinks you win more often than the book is pricing.
    """
    try:
        p = float(p_win) if p_win is not None else None
    except (TypeError, ValueError):
        return None
    ip = implied_prob_american(american)
    if p is None or ip is None:
        return None
    return p - ip
