"""Unit sizing. Converts raw bankroll fractions into 0.5u - 3u recommendations.

Conventions used across the app:
  * 1 unit  = unit_pct of bankroll (default 1%)
  * No display of raw dollars - everything shown in units.
"""
from __future__ import annotations

from typing import Optional

UNIT_MIN = 0.5
UNIT_MAX = 3.0
UNIT_STEP = 0.5


def recommend_units(ev_pct) -> float:
    """Map EV% to a 0.5u - 3u recommendation.

    0 - 1%   edge -> 0.5u
    1 - 3%   edge -> 1u
    3 - 5%   edge -> 1.5u
    5 - 8%   edge -> 2u
    8 - 12%  edge -> 2.5u
    > 12%    edge -> 3u
    None / <=0 edge -> 0u
    """
    try:
        v = float(ev_pct) if ev_pct is not None else None
    except (TypeError, ValueError):
        return 0.0
    if v is None or v <= 0:
        return 0.0
    pct = v * 100.0 if abs(v) < 1 else v
    if pct < 1:
        return 0.5
    if pct < 3:
        return 1.0
    if pct < 5:
        return 1.5
    if pct < 8:
        return 2.0
    if pct < 12:
        return 2.5
    return 3.0


def units_from_kelly(kelly_fraction, unit_pct) -> float:
    """Convert a raw Kelly fraction into clamped unit size.

    unit_pct is the fraction of bankroll that = 1 unit (e.g. 0.01 for 1%).
    """
    try:
        kf = float(kelly_fraction) if kelly_fraction is not None else None
        up = float(unit_pct) if unit_pct is not None else None
    except (TypeError, ValueError):
        return 0.0
    if kf is None or up is None or up <= 0 or kf <= 0:
        return 0.0
    raw_units = kf / up
    # snap to 0.5u increments and clamp
    stepped = round(raw_units / UNIT_STEP) * UNIT_STEP
    return max(0.0, min(UNIT_MAX, stepped))


def format_units(u) -> str:
    """Display helper."""
    try:
        v = float(u) if u is not None else None
    except (TypeError, ValueError):
        return "-"
    if v is None or v <= 0:
        return "0u"
    return f"{v:.1f}u"
