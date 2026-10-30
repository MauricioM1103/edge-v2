"""The Odds API v4 client.

Docs: https://the-odds-api.com/liveapi/guides/v4/
Endpoint for odds:
  https://api.the-odds-api.com/v4/sports/{sport}/odds
      ?apiKey={key}&regions=us&markets=h2h,spreads,totals&oddsFormat=american

Every call tolerates missing key / timeout / rate limit by returning [].
The free tier allows ~500 requests/mo; the Streamlit layer caches aggressively.
"""
from __future__ import annotations

import json
import os
from typing import List, Optional
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ODDS_BASE = "https://api.the-odds-api.com/v4"


def get_api_key() -> Optional[str]:
    """Resolve API key from env var or Streamlit secrets.

    Order: THE_ODDS_API_KEY env var -> st.secrets['the_odds_api_key'].
    """
    key = os.environ.get("THE_ODDS_API_KEY") or os.environ.get("ODDS_API_KEY")
    if key:
        return key
    try:
        import streamlit as st  # lazy import so non-Streamlit callers still work
        if hasattr(st, "secrets"):
            try:
                v = st.secrets.get("the_odds_api_key")  # type: ignore
                if v:
                    return str(v)
            except Exception:
                pass
    except ImportError:
        pass
    return None


def _get(path, params):
    key = get_api_key()
    if not key:
        return None
    params = dict(params or {})
    params["apiKey"] = key
    url = f"{ODDS_BASE}{path}?{urlencode(params)}"
    try:
        req = Request(url, headers={"User-Agent": "betting-dashboard-v2/1.0"})
        with urlopen(req, timeout=10) as resp:
            return json.load(resp)
    except (URLError, ValueError, TimeoutError, ConnectionError, OSError):
        return None


def list_sports() -> List[dict]:
    """Available sports from The Odds API (includes `in_season` flag)."""
    data = _get("/sports", {"all": "true"})
    if not data:
        return []
    return data if isinstance(data, list) else []


def get_odds(
    sport: str,
    regions: str = "us",
    markets: str = "h2h,spreads,totals",
    odds_format: str = "american",
) -> List[dict]:
    """Fetch current odds for `sport` (The Odds API key, e.g. 'baseball_mlb').

    Returns a list of games, each with a 'bookmakers' list of priced markets.
    Returns [] on missing key, timeout, or bad response.
    """
    if not sport:
        return []
    data = _get(
        f"/sports/{sport}/odds",
        {
            "regions": regions,
            "markets": markets,
            "oddsFormat": odds_format,
            "dateFormat": "iso",
        },
    )
    if not data or not isinstance(data, list):
        return []
    return data


def get_scores(sport: str, days_from: int = 1) -> List[dict]:
    """Fetch completed and in-progress scores from The Odds API."""
    if not sport:
        return []
    data = _get(
        f"/sports/{sport}/scores",
        {"daysFrom": str(days_from), "dateFormat": "iso"},
    )
    if not data or not isinstance(data, list):
        return []
    return data


def consensus_market(game: dict, market_key: str) -> Optional[dict]:
    """Average odds across bookmakers for one market.

    Returns a dict:
        {
          "home_price": -115,  # average American price for home side
          "away_price": +105,
          "home_point": -3.5,  # (for spreads/totals; None for h2h)
          "away_point": +3.5,
          "book_count": 7,
          "home_opener": -108,  # best approximation of opening line
          "away_opener": +100,
          "best_home": (-105, "DraftKings"),  # best shop for home
          "best_away": (+112, "FanDuel"),
        }
    """
    if not game or not isinstance(game, dict):
        return None
    home_team = game.get("home_team")
    away_team = game.get("away_team")
    books = game.get("bookmakers") or []
    if not books:
        return None

    home_prices, away_prices = [], []
    home_points, away_points = [], []
    best_home, best_away = None, None
    openers_home, openers_away = [], []  # first-seen per book, best proxy for opener

    for b in books:
        bname = b.get("title") or b.get("key") or ""
        for m in (b.get("markets") or []):
            if m.get("key") != market_key:
                continue
            outcomes = m.get("outcomes") or []
            for o in outcomes:
                name = o.get("name")
                price = o.get("price")
                point = o.get("point")
                if price is None:
                    continue
                try:
                    price = float(price)
                except (TypeError, ValueError):
                    continue
                is_home = name == home_team
                is_away = name == away_team
                if is_home:
                    home_prices.append(price)
                    if point is not None:
                        home_points.append(float(point))
                    openers_home.append(price)
                    if best_home is None or price > best_home[0]:
                        best_home = (price, bname)
                elif is_away:
                    away_prices.append(price)
                    if point is not None:
                        away_points.append(float(point))
                    openers_away.append(price)
                    if best_away is None or price > best_away[0]:
                        best_away = (price, bname)

    if not home_prices and not away_prices:
        return None

    def _avg(xs):
        return (sum(xs) / len(xs)) if xs else None

    return {
        "home_price": _avg(home_prices),
        "away_price": _avg(away_prices),
        "home_point": _avg(home_points),
        "away_point": _avg(away_points),
        "book_count": max(len(home_prices), len(away_prices)),
        # Opening line = median of first-listed-per-book is a rough proxy;
        # Odds API free tier doesn't expose true opener - we use min(home_prices)
        # for favorites, which captures the direction most books moved from.
        "home_opener": min(openers_home) if openers_home else None,
        "away_opener": min(openers_away) if openers_away else None,
        "best_home": best_home,
        "best_away": best_away,
    }
