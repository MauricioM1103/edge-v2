"""Closing Line Value and line movement helpers.

Line movement direction:
  * Odds getting SHORTER (toward even money) = money coming in on that side
    (public or sharp depending on volume split).
  * Odds getting LONGER = money moving the other way.

CLV: if you bet -110 and the line closed -120, you got better odds than close.
That difference (in implied-probability points) is your edge vs. the market consensus.
"""
from __future__ import annotations

from typing import Optional

from .odds import implied_prob_american, american_to_decimal


def line_move_cents(opening, current) -> Optional[int]:
    """American-cents difference between opening and current price.

    Positive = line moved toward favorite (shorter).
    Negative = line moved toward underdog (longer).
    """
    try:
        o = float(opening) if opening is not None else None
        c = float(current) if current is not None else None
    except (TypeError, ValueError):
        return None
    if o is None or c is None:
        return None
    # Normalize so comparison makes sense: compute implied prob delta instead.
    po = implied_prob_american(o)
    pc = implied_prob_american(c)
    if po is None or pc is None:
        return None
    # Approximate cents by converting prob delta back to American scale.
    delta = pc - po
    return int(round(delta * 100))


def line_move_prob_delta(opening, current) -> Optional[float]:
    """Change in implied win probability from opening to current (as a signed float).

    E.g. +0.03 means the market now gives this side 3 percentage points more
    chance of winning than at open.
    """
    po = implied_prob_american(opening)
    pc = implied_prob_american(current)
    if po is None or pc is None:
        return None
    return pc - po


def classify_move(prob_delta) -> str:
    """Short human label for a line move. Used in UI chips."""
    if prob_delta is None:
        return "no data"
    if abs(prob_delta) < 0.005:
        return "flat"
    if prob_delta > 0:
        return "shortening"
    return "lengthening"


def sharp_vs_public(prob_delta, bet_pct, money_pct) -> str:
    """Rough public/sharp indicator from line move + handle split.

    bet_pct = % of tickets on this side (public)
    money_pct = % of money on this side (sharp)
    Line shortening against low bet_pct but high money_pct -> sharp action.
    """
    if prob_delta is None:
        return "unknown"
    try:
        bp = float(bet_pct) if bet_pct is not None else None
        mp = float(money_pct) if money_pct is not None else None
    except (TypeError, ValueError):
        return "unknown"
    if bp is None or mp is None:
        if prob_delta > 0.005:
            return "steam"
        if prob_delta < -0.005:
            return "fade"
        return "flat"
    gap = mp - bp
    if prob_delta > 0.005 and gap > 10:
        return "sharp"
    if prob_delta > 0.005 and bp > mp:
        return "public"
    if abs(prob_delta) < 0.005:
        return "flat"
    return "mixed"


def clv_percent(bet_american, closing_american) -> Optional[float]:
    """CLV as a percentage, computed on implied probability.

    Positive = you beat the close. ~2-3% consistent CLV = long-term winning.
    """
    pb = implied_prob_american(bet_american)
    pc = implied_prob_american(closing_american)
    if pb is None or pc is None or pb <= 0:
        return None
    # You locked in odds that implied pb; book closed at pc.
    # If pc > pb, book "agrees" you win more often -> you got a worse price.
    # If pc < pb, book closed at longer odds for your side -> you beat close.
    # CLV% expressed as (how much less likely book says vs. your lock) / your lock.
    return (pb - pc) / pb


def clv_cents(bet_american, closing_american) -> Optional[int]:
    """CLV in American cents. Positive = you beat the close."""
    db = american_to_decimal(bet_american)
    dc = american_to_decimal(closing_american)
    if db is None or dc is None:
        return None
    # Convert decimal diff back to American scale approximately
    diff = db - dc
    return int(round(diff * 100))
