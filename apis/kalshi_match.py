"""Map ESPN games to Kalshi winner markets.

Kalshi represents a game as TWO yes/no markets, one per team:
  event_ticker:   KXNFLGAME-26NOV09BUFMIN
  title:          "Minnesota wins"
  yes_sub_title:  "Minnesota"
  yes_bid/ask:    cents 0-99 (None if no quotes)
  last_price:     cents 0-99

Matching strategy:
  1. The event_ticker suffix encodes DATE + both teams' abbreviations,
     e.g. "26NOV09BUFMIN" -> date 2026-11-09, teams BUF+MIN.
     Extract and match against the ESPN game's date and team abbreviations.
  2. Among the markets of that event, pick the one whose yes_sub_title
     canonically matches the home team -> p_yes = P(home wins).
     If we can only find the away team's market -> p_home = 1 - p_yes.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from .team_mapping import canonical
from . import kalshi

_TICKER_SUFFIX_RE = re.compile(r"^(\d{2}[A-Z]{3}\d{2})([A-Z]{2,6})$")


def _parse_event_ticker(event_ticker: str) -> Optional[dict]:
    """Return {'date': 'YYYY-MM-DD', 'teams_blob': 'BUFMIN'} or None."""
    if not event_ticker:
        return None
    # strip the leading series ticker, e.g. KXNFLGAME-
    parts = event_ticker.split("-", 1)
    if len(parts) != 2:
        return None
    suffix = parts[1]
    m = _TICKER_SUFFIX_RE.match(suffix)
    if not m:
        return None
    date_tok, teams_blob = m.group(1), m.group(2)
    try:
        dt = datetime.strptime(date_tok, "%y%b%d")
    except ValueError:
        return None
    return {"date": dt.date().isoformat(), "teams_blob": teams_blob}


def _teams_in_blob(teams_blob: str, home_abbr: str, away_abbr: str) -> bool:
    if not teams_blob or not home_abbr or not away_abbr:
        return False
    tb = teams_blob.upper()
    return (home_abbr.upper() in tb) and (away_abbr.upper() in tb)


def _group_markets_by_event(markets: List[dict]) -> Dict[str, List[dict]]:
    out: Dict[str, List[dict]] = {}
    for m in markets:
        et = m.get("event_ticker") or ""
        out.setdefault(et, []).append(m)
    return out


def find_event_markets(
    kalshi_markets: List[dict],
    event_start_utc: Optional[datetime],
    home_abbr: str,
    away_abbr: str,
    home_name: str = "",
    away_name: str = "",
) -> List[dict]:
    """Return the list of yes/no markets for this ESPN game, or []."""
    if not kalshi_markets:
        return []
    grouped = _group_markets_by_event(kalshi_markets)
    target_date = None
    if event_start_utc:
        try:
            target_date = event_start_utc.date().isoformat()
        except Exception:
            target_date = None

    for event_ticker, markets in grouped.items():
        parsed = _parse_event_ticker(event_ticker)
        if not parsed:
            continue
        if not _teams_in_blob(parsed["teams_blob"], home_abbr or "", away_abbr or ""):
            continue
        if target_date and parsed["date"] != target_date:
            # allow +/-1 day for tz drift
            try:
                dt_tgt = datetime.fromisoformat(target_date)
                dt_tkr = datetime.fromisoformat(parsed["date"])
                if abs((dt_tgt - dt_tkr).days) > 1:
                    continue
            except ValueError:
                continue
        return markets

    # fallback: match by canonical name tokens in yes_sub_title
    for event_ticker, markets in grouped.items():
        subs = [canonical(m.get("yes_sub_title") or "") for m in markets]
        hc = canonical(home_name).split()[-1] if home_name else ""
        ac = canonical(away_name).split()[-1] if away_name else ""
        if not hc or not ac:
            continue
        matched = any(hc in s for s in subs) and any(ac in s for s in subs)
        if matched:
            return markets
    return []


def home_implied_prob(markets: List[dict], home_name: str, home_abbr: str = "") -> Optional[float]:
    """From a game's market list, pull P(home wins) by picking the home-side market."""
    if not markets:
        return None
    hc = canonical(home_name or "")
    hc_last = hc.split()[-1] if hc else ""

    def _is_home_market(m) -> bool:
        yes_sub_canon = canonical(m.get("yes_sub_title") or "")
        if not yes_sub_canon:
            return False
        if yes_sub_canon == hc:
            return True
        yes_last = yes_sub_canon.split()[-1]
        if hc_last and (yes_last == hc_last or hc_last in yes_sub_canon or yes_sub_canon in hc):
            return True
        return False

    home_m = next((m for m in markets if _is_home_market(m)), None)
    if home_m:
        return kalshi.market_implied_yes_prob(home_m)

    # otherwise take the first market's prob and flip
    other = markets[0]
    p = kalshi.market_implied_yes_prob(other)
    if p is None:
        return None
    return 1.0 - p


# Backwards-compat shims for the older entry points pages.py uses.
def match_market_for_event(kalshi_markets, event_start_utc, home_name, away_name):
    """Legacy single-market finder - returns the home-side market or any match."""
    markets = find_event_markets(kalshi_markets, event_start_utc, "", "", home_name, away_name)
    if not markets:
        return None
    hc = canonical(home_name or "")
    for m in markets:
        if canonical(m.get("yes_sub_title") or "") == hc:
            return m
    return markets[0]


def home_implied_prob_from_market(market, home_name):
    """Legacy single-market probability extraction."""
    if not market:
        return None
    p_yes = kalshi.market_implied_yes_prob(market)
    if p_yes is None:
        return None
    yes_sub_canon = canonical(market.get("yes_sub_title") or "")
    hc = canonical(home_name or "")
    if not yes_sub_canon or not hc:
        return p_yes
    if yes_sub_canon == hc or yes_sub_canon.split()[-1] == hc.split()[-1]:
        return p_yes
    return 1.0 - p_yes
