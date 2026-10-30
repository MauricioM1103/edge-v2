"""Polymarket client - public Gamma API, no auth required.

Docs:
  https://docs.polymarket.com/

Endpoints we use:
  GET https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=500

Each market returns:
  question         "Will the Lakers beat the Celtics on Oct 29?"
  outcomes         JSON string: '["Yes","No"]'
  outcomePrices    JSON string: '["0.56","0.44"]'  (yes-share price 0-1)
  endDate          ISO; use for date matching
  slug             URL-safe id

Sports markets are mixed in with politics / crypto / weather. We filter by
checking the question text for both team names. Matching tolerates
abbreviations and nicknames via the canonical team mapper.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import List, Optional
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .team_mapping import canonical

GAMMA = "https://gamma-api.polymarket.com"


def _fetch(path: str, params: Optional[dict] = None, timeout: int = 10):
    url = GAMMA + path
    if params:
        url += "?" + urlencode(params)
    try:
        req = Request(url, headers={
            "User-Agent": "edge-v2/1.0",
            "Accept": "application/json",
        })
        with urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except (URLError, ValueError, TimeoutError, ConnectionError, OSError):
        return None


def list_active_markets(limit: int = 500, offset: int = 0) -> List[dict]:
    """All currently-active markets. Polymarket paginates with offset.

    The Gamma API caps per-request at 100 markets. We paginate client-side
    up to the requested `limit` by walking offsets.
    """
    PAGE = 100
    out: List[dict] = []
    cur_offset = offset
    while len(out) < limit:
        data = _fetch("/markets", {
            "active": "true", "closed": "false",
            "limit": str(PAGE), "offset": str(cur_offset),
            "order": "endDate", "ascending": "true",
        })
        if not data:
            break
        batch = data if isinstance(data, list) else (data.get("data") or [])
        if not batch:
            break
        out.extend(batch)
        if len(batch) < PAGE:
            break
        cur_offset += PAGE
    return out[:limit]


def _parse_prices(market: dict) -> Optional[tuple]:
    """Return (yes_price, no_price) as floats in [0,1], or None."""
    raw = market.get("outcomePrices") or market.get("outcome_prices")
    if not raw:
        return None
    try:
        prices = raw if isinstance(raw, list) else json.loads(raw)
        if not prices or len(prices) < 2:
            return None
        return (float(prices[0]), float(prices[1]))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _parse_outcomes(market: dict) -> list:
    raw = market.get("outcomes") or market.get("outcome_strings")
    if not raw:
        return []
    try:
        outs = raw if isinstance(raw, list) else json.loads(raw)
        return [str(o) for o in outs]
    except (TypeError, ValueError, json.JSONDecodeError):
        return []


def _question_matches_teams(question: str, home: str, away: str) -> bool:
    if not question:
        return False
    q = question.lower()
    hc = canonical(home or "")
    ac = canonical(away or "")
    if not hc or not ac:
        return False
    ht = hc.split()[-1]
    at = ac.split()[-1]
    return bool(ht) and bool(at) and ht in q and at in q


def find_game_market(
    markets: List[dict],
    home_name: str,
    away_name: str,
    event_start_utc: Optional[datetime] = None,
    tolerance_hours: int = 36,
) -> Optional[dict]:
    """Return the Polymarket market for this game, or None."""
    if not markets:
        return None
    candidates = []
    for m in markets:
        q = m.get("question") or m.get("title") or ""
        if not _question_matches_teams(q, home_name, away_name):
            continue
        if event_start_utc:
            end_raw = m.get("endDate") or m.get("end_date_iso")
            if end_raw:
                try:
                    end_dt = datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
                    if abs((end_dt - event_start_utc).total_seconds()) > tolerance_hours * 3600:
                        continue
                except ValueError:
                    pass
        candidates.append(m)
    if not candidates:
        return None
    # If multiple candidates, prefer the one whose end date is closest to game.
    if event_start_utc:
        def _skew(m):
            end_raw = m.get("endDate") or ""
            try:
                return abs((datetime.fromisoformat(end_raw.replace("Z","+00:00")) - event_start_utc).total_seconds())
            except ValueError:
                return 1e9
        candidates.sort(key=_skew)
    return candidates[0]


def home_implied_prob(market: dict, home_name: str, away_name: str) -> Optional[float]:
    """Return P(home_name wins) from a Polymarket game market.

    Polymarket encodes most sports markets as "Will <home> beat <away>?" with
    outcomes Yes/No where Yes=home wins. But some are flipped - we check the
    question wording to pick the right side.
    """
    if not market:
        return None
    prices = _parse_prices(market)
    if not prices:
        return None
    yes_p, no_p = prices
    q = (market.get("question") or "").lower()
    hc = canonical(home_name or "")
    ac = canonical(away_name or "")
    ht = hc.split()[-1] if hc else ""
    at = ac.split()[-1] if ac else ""
    if not (ht and at):
        return yes_p  # best-effort
    # "Will <team A> beat <team B>?" -> team A appears before "beat".
    idx_beat = q.find(" beat ")
    if idx_beat > 0:
        prefix = q[:idx_beat]
        if ht in prefix:
            return yes_p
        if at in prefix:
            return 1.0 - yes_p
    # Fallback: team listed earlier in question wins Yes.
    hi = q.find(ht) if ht else -1
    ai = q.find(at) if at else -1
    if hi >= 0 and (ai < 0 or hi < ai):
        return yes_p
    if ai >= 0 and (hi < 0 or ai < hi):
        return 1.0 - yes_p
    return yes_p
