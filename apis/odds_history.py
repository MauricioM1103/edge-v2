"""Historical Odds API client.

Credit cost: 10× live /odds. us × h2h,spreads,totals snapshot = 30 credits.
Historical snapshots never change, so we cache them permanently on disk and
re-serve on every subsequent lookup. The user's 20k credit budget stays safe.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from . import odds as odds_api

ODDS_BASE = "https://api.the-odds-api.com/v4"


def _get(path: str, params: dict):
    """Hit a historical endpoint, cache response permanently on success.

    Returns ("ok", data) | ("no_key"|"http_401"|...) | ("network", msg).
    """
    cache_url = f"{ODDS_BASE}{path}?{urlencode(params)}"
    hit = odds_api._cache_read(cache_url, ttl=-1)
    if hit is not None:
        return ("ok", hit)

    key = odds_api.get_api_key()
    if not key:
        return ("no_key", None)
    params = dict(params)
    params["apiKey"] = key
    url = f"{ODDS_BASE}{path}?{urlencode(params)}"
    try:
        req = Request(url, headers={"User-Agent": "edge-v2/1.0"})
        with urlopen(req, timeout=15) as resp:
            odds_api._update_credits_from_headers(resp.headers)
            data = json.load(resp)
            try:
                odds_api._cache_write(cache_url, data)
            except Exception:
                pass
            return ("ok", data)
    except HTTPError as e:
        return (f"http_{e.code}", None)
    except (URLError, ValueError, TimeoutError, ConnectionError, OSError) as e:
        return ("network", str(e))


def estimated_credit_cost_per_snapshot(regions: str, markets: str) -> int:
    """One historical snapshot costs 10× the live /odds rate."""
    return 10 * odds_api.estimated_credit_cost(regions, markets)


def historical_odds(
    sport: str, iso_timestamp: str,
    regions: str = "us",
    markets: str = "h2h,spreads,totals",
) -> dict:
    """Historical snapshot for one sport at one UTC timestamp (ISO 8601 Z).

    Returns:
      {"status": "ok" | "no_key" | "http_401" | "http_402" | "network",
       "data": [...] | None,
       "snapshot_time": "..."}
    Status 401/402 almost always means the API key's plan doesn't include history.
    """
    if not sport:
        return {"status": "no_key", "data": None}
    status, data = _get(
        f"/historical/sports/{sport}/odds",
        {
            "regions": regions,
            "markets": markets,
            "oddsFormat": "american",
            "dateFormat": "iso",
            "date": iso_timestamp,
        },
    )
    if status != "ok":
        return {"status": status, "data": None, "snapshot_time": iso_timestamp}
    # Historical endpoint wraps results under {"data": [...], "timestamp": ...}
    if isinstance(data, dict) and "data" in data:
        return {
            "status": "ok",
            "data": data.get("data") or [],
            "snapshot_time": data.get("timestamp") or iso_timestamp,
        }
    if isinstance(data, list):
        return {"status": "ok", "data": data, "snapshot_time": iso_timestamp}
    return {"status": "ok", "data": [], "snapshot_time": iso_timestamp}


def grading_scores_from_espn(sport_key: str, date_yyyymmdd: str) -> dict:
    """Pull final scores for a given day from ESPN. Keyed by (home_canon, away_canon).

    Used by the backtest to grade picks against outcomes.
    """
    from .espn import get_scoreboard, _ev_scores  # local import to avoid cycles
    from .team_mapping import canonical
    events = get_scoreboard(sport_key, date_yyyymmdd)
    out = {}
    for ev in events or []:
        if not ev.get("is_final"):
            continue
        home_name = (ev.get("home") or {}).get("name")
        away_name = (ev.get("away") or {}).get("name")
        key = (canonical(home_name), canonical(away_name))
        out[key] = _ev_scores(ev)
    return out
