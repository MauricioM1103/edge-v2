"""Agent that builds one parlay from the day's tier-A edges.

A "capper" in sports betting is the person who handicaps the slate and
hands out a short list of picks. This module is the agent-flavoured
equivalent: given the current Board (every edge the scanner surfaced),
pick a small set of tier-A legs that are good enough to be worth
combining, enforce the "no two legs from the same event" correlation
guard, and return the parlay with a short natural-language rationale
for the UI to show next to it.

Design notes
------------
- Only tier-A edges feed in (edge ≥ 2% vs the Board's own cutoff).
  Combining weaker edges stacks vig faster than they stack value.
- No two legs from the same event. Legs on correlated outcomes (a team
  moneyline paired with the same team's spread cover, or a player's
  points-over paired with the team total over) blow up the independence
  assumption parlay math rests on.
- Target 3 legs by default; 2–5 is the practical range. One-leg
  "parlays" are just straight bets. Six+ leg parlays stack so much vig
  that even a +5 % edge per leg lands the parlay at ~−3 % EV.
- Prefer legs that kick off soon, so the parlay resolves within the
  user's watching window — a 4-leg parlay that spans 72 hours is
  useless as a daily capper pick.
- Prefer sharper fair sources (Pinnacle > book consensus > alt
  consensus) when scores are close — the more reliable the fair prob,
  the less model risk in the joint.

None of this makes parlays a +EV strategy long-run. The tool is a
button, not a betting program.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence

from . import parlay as parlay_math


#: Default tier-A edge cutoff, mirrors storage.picks.TIER_A_EDGE.
TIER_A_EDGE = 0.020
#: Hard default: never fewer than two legs (one leg is a single pick).
MIN_LEGS = 2
#: Hard default: cap at five legs — past that, vig stack sinks any edge.
MAX_LEGS = 5
#: Preferred target when the slate supports it.
TARGET_LEGS = 3
#: How far out a leg's kickoff can be, in hours, before it drops in rank.
SOON_BONUS_HOURS = 6


def _edge_of(e: dict) -> float:
    try:
        return float(e.get("edge") or 0)
    except (TypeError, ValueError):
        return 0.0


def _fair_of(e: dict) -> float:
    try:
        return float(e.get("fair_prob") or 0)
    except (TypeError, ValueError):
        return 0.0


def _source_tier(e: dict) -> int:
    """Ranks fair-source reliability: Pinnacle > consensus > alt consensus."""
    src = (e.get("fair_source") or "").lower()
    if "pinnacle" in src:
        return 0
    if "alt" in src:
        return 2
    if "consensus" in src:
        return 1
    return 3


def _hours_to_kickoff(e: dict, now: Optional[datetime] = None) -> float:
    now = now or datetime.now(timezone.utc)
    start = e.get("start_utc") or e.get("commence_time")
    if not start:
        return 24.0  # treat unknown as far-out so it ranks below known-soon legs
    try:
        if isinstance(start, str):
            start = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError, AttributeError):
        return 24.0
    hours = (start - now).total_seconds() / 3600.0
    # Already in-play or finished → treat as "far out" so a pre-game leg
    # is preferred over a leg whose game has already started.
    if hours <= 0:
        return 24.0
    return hours


def _is_settled(e: dict) -> bool:
    if e.get("status_state") == "post":
        return True
    return (e.get("grade") or "").lower() in ("win", "loss", "push")


def _is_pre_game(e: dict) -> bool:
    """True when the edge is on an unstarted game.

    In-play edges can still appear on the Board when a line moves during
    the game, but they're bad parlay fodder because the leg could settle
    in minutes, before the other legs' games even start.
    """
    state = e.get("status_state")
    if state == "pre":
        return True
    if state in ("in", "post"):
        return False
    # Unknown state (ESPN name-match miss or scanner ran without an
    # ESPN map) — fall back to commence_time: future kickoff is pregame,
    # a kickoff in the past is in-play or over and the leg is skipped.
    start = e.get("start_utc") or e.get("commence_time")
    if not start:
        return False
    try:
        if isinstance(start, str):
            start = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError, AttributeError):
        return False
    return start > datetime.now(timezone.utc)


def _rank_key(e: dict) -> tuple:
    """Higher edges and closer kickoffs come first; sharper fair breaks ties."""
    edge = _edge_of(e)
    hours = _hours_to_kickoff(e)
    soon = -1 if hours <= SOON_BONUS_HOURS else 0
    return (-edge, soon, _source_tier(e), -_fair_of(e))


def select_parlay_legs(
    edges: Sequence[dict],
    target_legs: int = TARGET_LEGS,
    min_legs: int = MIN_LEGS,
    max_legs: int = MAX_LEGS,
    min_leg_edge: float = TIER_A_EDGE,
    now: Optional[datetime] = None,
) -> List[dict]:
    """Pick up to `target_legs` tier-A edges for a parlay.

    Rules enforced:
      - Each leg's edge >= min_leg_edge.
      - Leg must be a pre-game unsettled row (no finished / in-play legs).
      - No two legs share an event_id.
      - Ordered best-edge first, with a bonus for games kicking off soon.

    Returns at most `max_legs` legs, in rank order. Caller should refuse
    to build a parlay if fewer than `min_legs` come back.
    """
    pool: List[dict] = []
    seen_events = set()
    for e in sorted((e for e in edges if e), key=_rank_key):
        if _edge_of(e) < min_leg_edge:
            continue
        if _is_settled(e):
            continue
        if not _is_pre_game(e):
            continue
        eid = e.get("event_id")
        if eid is None or eid in seen_events:
            continue
        pool.append(e)
        seen_events.add(eid)
        if len(pool) >= max_legs:
            break
    # Prefer the target count, but never return fewer than exist.
    want = min(max(target_legs, min_legs), max_legs)
    return pool[:want] if len(pool) >= min_legs else pool[: max(0, len(pool))]


def _leg_label(e: dict) -> str:
    """One-line human label for a parlay leg in the capper's note."""
    sport = (e.get("_sport") or "").upper()
    sel = e.get("side_label") or ""
    return f"{sport} · {sel}"


def _capper_rationale(legs: List[dict], parlay: dict) -> str:
    """Short capper-voice paragraph explaining why these legs.

    Intentionally concrete and short: name the legs, state the combined
    price, state the joint fair probability, acknowledge the vig
    compounding rather than dressing it up. The UI shows this verbatim.
    """
    if not legs:
        return (
            "No tier-A legs on today's slate clear the parlay bar — "
            "every edge either sits below 2 % or lives on a game that's "
            "already in play. Nothing for the capper to build on today."
        )
    n = len(legs)
    combined = parlay.get("combined_american")
    joint_fair_pct = (parlay.get("joint_fair_prob") or 0) * 100.0
    joint_imp_pct = (parlay.get("joint_implied_prob") or 0) * 100.0
    edge_pct = (parlay.get("edge") or 0) * 100.0
    sports = sorted({(e.get("_sport") or "").upper() for e in legs if e.get("_sport")})
    sport_mix = ", ".join(sports) if sports else "mixed"
    bullets = " · ".join(
        f"{_leg_label(e)} ({(_edge_of(e) * 100):+.1f}pts)" for e in legs
    )
    sign = "+" if (combined or 0) >= 0 else ""
    verdict = (
        "the joint fair probability clears the stack, so this one survives "
        "the vig-compounding math — small sample, so size accordingly"
        if edge_pct > 0 else
        "even with tier-A legs the vig stack puts the parlay in the red; "
        "the capper flags it as a losing bet in expectation"
    )
    return (
        f"Capper's {n}-leg parlay ({sport_mix}): {bullets}. Combined "
        f"{sign}{combined}, joint fair {joint_fair_pct:.1f} % vs market "
        f"{joint_imp_pct:.1f} % → edge {edge_pct:+.2f} pts. "
        f"{verdict}."
    )


def build_capper_parlay(
    edges: Sequence[dict],
    bankroll: float = 500.0,
    kelly_mult: float = 0.25,
    target_legs: int = TARGET_LEGS,
    min_legs: int = MIN_LEGS,
    max_legs: int = MAX_LEGS,
    min_leg_edge: float = TIER_A_EDGE,
    now: Optional[datetime] = None,
) -> dict:
    """End-to-end: pick legs, compute the parlay math, write the rationale.

    Returns a dict shaped for the UI and the ledger upsert, with these
    fields:
      legs: list of leg dicts (sport, event_id, market, selection,
            point, price_american, fair_prob, implied_prob, book,
            event_name, start_utc).
      combined_american: int (rounded combined moneyline)
      combined_decimal: float
      joint_fair_prob: float
      joint_implied_prob: float
      edge: float (joint_fair − joint_implied)
      kelly_fraction: float (quarter-Kelly by default)
      stake_dollars: float (kelly_fraction × bankroll)
      profit_on_win: float (dollar profit at that stake)
      ev_dollars: float (expected $ return at that stake)
      rationale: str (capper-voice paragraph)
      built_at: ISO timestamp
    """
    legs_edges = select_parlay_legs(
        edges,
        target_legs=target_legs,
        min_legs=min_legs,
        max_legs=max_legs,
        min_leg_edge=min_leg_edge,
        now=now,
    )
    out: Dict = {
        "legs": [],
        "combined_american": None,
        "combined_decimal": None,
        "joint_fair_prob": None,
        "joint_implied_prob": None,
        "edge": None,
        "kelly_fraction": 0.0,
        "stake_dollars": 0.0,
        "profit_on_win": None,
        "ev_dollars": None,
        "rationale": "",
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if len(legs_edges) < min_legs:
        out["rationale"] = _capper_rationale(legs_edges, out)
        return out

    legs: List[dict] = []
    for e in legs_edges:
        legs.append({
            "sport": (e.get("_sport") or "").lower(),
            "event_id": e.get("event_id"),
            "event_name": e.get("matchup") or "",
            "market": e.get("market") or "h2h",
            "selection": e.get("side_label") or "",
            "point": e.get("point"),
            "price_american": e.get("price"),
            "fair_prob": e.get("fair_prob"),
            "implied_prob": e.get("book_implied"),
            "fair_source": e.get("fair_source"),
            "book": e.get("book"),
            "start_utc": e.get("start_utc") or e.get("commence_time"),
            "is_prop": bool(e.get("is_prop")),
            "player": e.get("prop_player"),
            # Leg-level status mirrors the agent board at build time so
            # the parlay can show "still live / already pushed" while
            # the game is in play.
            "status": "open",
        })
    prices = [leg["price_american"] for leg in legs]
    fairs = [leg["fair_prob"] for leg in legs]
    out["legs"] = legs
    out["combined_decimal"] = parlay_math.combined_decimal(prices)
    out["combined_american"] = parlay_math.combined_american(prices)
    out["joint_fair_prob"] = parlay_math.joint_fair_prob(fairs)
    out["joint_implied_prob"] = parlay_math.joint_implied_prob(prices)
    if out["joint_fair_prob"] is not None and out["joint_implied_prob"] is not None:
        out["edge"] = out["joint_fair_prob"] - out["joint_implied_prob"]
    kf = parlay_math.parlay_kelly_fraction(fairs, prices, multiplier=kelly_mult)
    out["kelly_fraction"] = kf or 0.0
    try:
        stake = float(bankroll) * (kf or 0.0)
    except (TypeError, ValueError):
        stake = 0.0
    out["stake_dollars"] = round(stake, 2)
    out["profit_on_win"] = parlay_math.parlay_profit_on_win(stake, prices)
    out["ev_dollars"] = parlay_math.parlay_ev_dollars(stake, fairs, prices)
    out["rationale"] = _capper_rationale(legs_edges, out)
    return out
