"""Parlay combinatorics and expected-value math.

A parlay multiplies the decimal odds of each leg into one combined price,
and the joint probability of winning is the product of each leg's win
probability (under the assumption of independence). Because every leg
multiplies the vig, parlays nearly always carry more drag than the single
bets that make them up: a 3-leg parlay of three −110 bets stacks ~15 %
vig, so each leg has to overcome a bigger wall before the parlay goes
positive in expectation.

The tooling here is intentionally sober: it only produces +EV parlays
when the legs themselves clear a fair-prob threshold, and the Kelly
sizing is fractional by default (quarter-Kelly) because parlay variance
is higher than any single leg's.

Push and void handling follows standard sportsbook rules:
  - Any leg LOSS → parlay loses outright.
  - Any leg PUSH or VOID → that leg drops, parlay recomputes with the
    remaining legs. A one-leg leftover pays that leg's standalone odds.
    A zero-leg leftover is itself a push (stake refunded).
  - All legs WIN → parlay wins at the combined price.
"""
from __future__ import annotations

from typing import Iterable, List, Optional, Sequence, Tuple

from .odds import american_to_decimal, decimal_to_american, implied_prob_american


def _decimal_from_american(american) -> Optional[float]:
    return american_to_decimal(american)


def combined_decimal(american_prices: Sequence) -> Optional[float]:
    """Combined decimal odds across every leg; None if any leg is invalid."""
    if not american_prices:
        return None
    total = 1.0
    for p in american_prices:
        d = _decimal_from_american(p)
        if d is None or d <= 1.0:
            return None
        total *= d
    return total


def combined_american(american_prices: Sequence) -> Optional[int]:
    """Combined American moneyline across every leg (rounded)."""
    d = combined_decimal(american_prices)
    if d is None:
        return None
    return decimal_to_american(d)


def joint_fair_prob(fair_probs: Sequence[float]) -> Optional[float]:
    """Joint fair win probability assuming leg independence.

    Returns the product of all leg fair probs. None when any entry is
    missing or outside [0, 1]. Callers are responsible for keeping legs
    uncorrelated (no two legs on the same game); the capper enforces
    that at selection time.
    """
    if not fair_probs:
        return None
    out = 1.0
    for p in fair_probs:
        try:
            x = float(p)
        except (TypeError, ValueError):
            return None
        if not (0.0 <= x <= 1.0):
            return None
        out *= x
    return out


def joint_implied_prob(american_prices: Sequence) -> Optional[float]:
    """Vig-inclusive joint implied probability of every leg winning.

    This is the market's own "break-even" probability for the parlay —
    the number the book has to beat to make money. Comparing it to
    joint_fair_prob gives the parlay's edge.
    """
    if not american_prices:
        return None
    out = 1.0
    for p in american_prices:
        imp = implied_prob_american(p)
        if imp is None:
            return None
        out *= imp
    return out


def parlay_edge(fair_probs: Sequence[float], american_prices: Sequence) -> Optional[float]:
    """Parlay edge = joint fair prob − joint implied prob (in [0, 1] space).

    Positive means the book is underpricing this specific combination
    even after the vig multiplies across legs; negative means the vig
    stack has put the parlay in the red before it's even settled.
    """
    jf = joint_fair_prob(fair_probs)
    ji = joint_implied_prob(american_prices)
    if jf is None or ji is None:
        return None
    return jf - ji


def parlay_profit_on_win(stake: float, american_prices: Sequence) -> Optional[float]:
    """Dollar profit (not total return) if every leg hits, at this stake."""
    d = combined_decimal(american_prices)
    if d is None:
        return None
    try:
        s = float(stake)
    except (TypeError, ValueError):
        return None
    return s * (d - 1.0)


def parlay_ev_dollars(
    stake: float,
    fair_probs: Sequence[float],
    american_prices: Sequence,
) -> Optional[float]:
    """Expected dollar return at this stake.

    EV = joint_fair * profit_on_win − (1 − joint_fair) * stake. A positive
    number means the parlay is +EV at the stated fair probs; a negative
    number means the vig and the compound miss-probability already have it.
    """
    profit = parlay_profit_on_win(stake, american_prices)
    jf = joint_fair_prob(fair_probs)
    if profit is None or jf is None:
        return None
    try:
        s = float(stake)
    except (TypeError, ValueError):
        return None
    return jf * profit - (1.0 - jf) * s


def parlay_kelly_fraction(
    fair_probs: Sequence[float],
    american_prices: Sequence,
    multiplier: float = 0.25,
) -> Optional[float]:
    """Kelly fraction on the parlay's joint probability + combined decimal.

    Multiplier defaults to 0.25 (quarter-Kelly) because parlay variance
    is strictly higher than any single leg's — full-Kelly on a four-leg
    parlay would blow through a normal bankroll on the first streak of
    missed legs. Returns 0.0 for a −EV parlay, None for bad inputs.
    """
    jf = joint_fair_prob(fair_probs)
    d = combined_decimal(american_prices)
    try:
        m = float(multiplier) if multiplier is not None else 1.0
    except (TypeError, ValueError):
        return None
    if jf is None or d is None or d <= 1.0:
        return None
    b = d - 1.0
    q = 1.0 - jf
    f = (b * jf - q) / b
    if f <= 0:
        return 0.0
    f *= m
    return max(0.0, min(1.0, f))


# ---------------------------------------------------------------------------
# Settlement
# ---------------------------------------------------------------------------

#: Statuses treated as "leg hit" when settling a parlay.
WIN_STATUSES = {"win"}
#: Statuses that drop the leg and let the rest of the parlay stand.
PUSH_STATUSES = {"push", "void"}
#: Statuses that kill the parlay outright.
LOSS_STATUSES = {"loss"}
#: Any other value (including "open") means the parlay is still live.


def settle_parlay(leg_statuses: Iterable[str], leg_prices: Iterable) -> Tuple[str, Optional[int]]:
    """Resolve a parlay from the individual leg outcomes.

    Returns (status, effective_combined_american).
      status:
        "win"  — every surviving leg hit.
        "loss" — at least one leg lost.
        "push" — every leg pushed / voided (nothing survives, stake back).
        "open" — at least one leg is still unresolved and none has lost.
      effective_combined_american:
        Combined American price of the SURVIVING legs after dropping
        pushes / voids (what the stake was actually at risk against).
        None when no leg survives or inputs are invalid.
    """
    statuses = [(s or "open").lower() for s in leg_statuses]
    prices = list(leg_prices)
    if len(statuses) != len(prices) or not statuses:
        return "open", None
    # A loss anywhere kills the parlay regardless of still-open legs.
    if any(s in LOSS_STATUSES for s in statuses):
        # Combined price uses the surviving (non-push) legs so the
        # ledger still shows what the stake was risked against.
        surviving = [p for s, p in zip(statuses, prices) if s not in PUSH_STATUSES]
        return "loss", combined_american(surviving) if surviving else None
    # Any leg still open AND no losses → parlay still live.
    if any(s not in (WIN_STATUSES | PUSH_STATUSES | LOSS_STATUSES) for s in statuses):
        return "open", None
    # Every leg is settled; drop pushes and see what's left.
    winners = [p for s, p in zip(statuses, prices) if s in WIN_STATUSES]
    if not winners:
        # All legs pushed → parlay pushes (stake refunded).
        return "push", None
    return "win", combined_american(winners)
