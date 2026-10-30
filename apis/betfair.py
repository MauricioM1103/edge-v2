"""Betfair Exchange client - optional, requires a Betfair account + API key.

Betfair's Exchange API uses two-factor authentication:
  1. Login endpoint takes username + password + application key -> session token
  2. Subsequent calls use X-Authentication: <sessionToken> + X-Application: <appKey>

If the user doesn't configure Betfair credentials, every function returns
[] / None and the UI silently omits the Betfair row from probability strips.

Credential sources (env or Streamlit secrets):
  BETFAIR_APP_KEY       / betfair_app_key
  BETFAIR_SESSION_TOKEN / betfair_session_token  (if already logged in)
  BETFAIR_USERNAME + BETFAIR_PASSWORD + BETFAIR_APP_KEY (for auto-login)

Note: Betfair's login endpoint requires HTTPS and a client-side certificate
for non-interactive login. For this app we recommend using the "non-cert"
login endpoint with a pre-generated session token, or configuring the token
manually in secrets.toml.

Setup guide: https://docs.developer.betfair.com/
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import List, Optional
from urllib.error import URLError
from urllib.request import Request, urlopen

from .team_mapping import canonical

API_BASE = "https://api.betfair.com/exchange/betting/json-rpc/v1"
LOGIN_BASE = "https://identitysso.betfair.com/api/login"

SPORT_IDS = {
    "nfl": "6423",       # American Football
    "ncaaf": "6423",
    "nba": "7522",       # Basketball
    "ncaab": "7522",
    "wnba": "7522",
    "mlb": "7511",       # Baseball
    "nhl": "7524",       # Ice Hockey
    "soccer": "1",       # Soccer master id (used by all soccer leagues)
    "epl": "1", "la_liga": "1", "bundesliga": "1", "serie_a": "1",
    "ligue_1": "1", "ucl": "1", "uel": "1", "mls": "1", "nwsl": "1", "liga_mx": "1",
    "atp": "2", "wta": "2",   # Tennis
    "pga": "3",               # Golf
    "ufc": "26420387",        # MMA
    "f1": "7",                # Motor Sport
    "nascar": "7",
}


def _get_secret(env_name: str, secrets_name: str) -> Optional[str]:
    v = os.environ.get(env_name)
    if v:
        return v
    try:
        import streamlit as st
        if hasattr(st, "secrets"):
            try:
                v = st.secrets.get(secrets_name)
                if v:
                    return str(v)
            except Exception:
                pass
    except ImportError:
        pass
    return None


def app_key() -> Optional[str]:
    return _get_secret("BETFAIR_APP_KEY", "betfair_app_key")


def session_token() -> Optional[str]:
    return _get_secret("BETFAIR_SESSION_TOKEN", "betfair_session_token")


def is_configured() -> bool:
    return bool(app_key()) and bool(session_token())


def _rpc(method: str, params: dict) -> Optional[dict]:
    """JSON-RPC call to the Betfair Exchange API."""
    ak = app_key()
    st_token = session_token()
    if not (ak and st_token):
        return None
    body = json.dumps({
        "jsonrpc": "2.0", "method": method,
        "params": params, "id": 1,
    }).encode("utf-8")
    headers = {
        "X-Application": ak,
        "X-Authentication": st_token,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "edge-v2/1.0",
    }
    try:
        req = Request(API_BASE, data=body, headers=headers, method="POST")
        with urlopen(req, timeout=10) as resp:
            data = json.load(resp)
            if "error" in data:
                return None
            return data.get("result")
    except (URLError, ValueError, TimeoutError, ConnectionError, OSError):
        return None


def list_sport_events(sport_key: str) -> List[dict]:
    """Current events for a sport (today and near future). Needs auth."""
    sport_id = SPORT_IDS.get(sport_key)
    if not sport_id:
        return []
    result = _rpc("SportsAPING/v1.0/listEvents", {
        "filter": {
            "eventTypeIds": [sport_id],
            "marketStartTime": {
                "from": datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat(),
            },
        },
        "maxResults": 200,
    })
    if not result:
        return []
    out = []
    for item in result:
        ev = item.get("event") or {}
        out.append({
            "id": ev.get("id"),
            "name": ev.get("name"),
            "country": ev.get("countryCode"),
            "open_date": ev.get("openDate"),
        })
    return out


def list_match_odds_markets(event_id: str) -> List[dict]:
    """Match-odds markets for an event with best back/lay prices."""
    if not event_id:
        return []
    result = _rpc("SportsAPING/v1.0/listMarketCatalogue", {
        "filter": {"eventIds": [str(event_id)], "marketTypeCodes": ["MATCH_ODDS"]},
        "maxResults": 10,
        "marketProjection": ["RUNNER_DESCRIPTION"],
    })
    if not result:
        return []
    out = []
    for m in result:
        out.append({
            "market_id": m.get("marketId"),
            "name": m.get("marketName"),
            "runners": [
                {"id": r.get("selectionId"), "name": r.get("runnerName")}
                for r in (m.get("runners") or [])
            ],
        })
    return out


def list_market_book(market_ids: List[str]) -> List[dict]:
    """Live prices for a list of market ids."""
    if not market_ids:
        return []
    result = _rpc("SportsAPING/v1.0/listMarketBook", {
        "marketIds": [str(x) for x in market_ids],
        "priceProjection": {
            "priceData": ["EX_BEST_OFFERS"],
            "exBestOffersOverrides": {"bestPricesDepth": 1},
        },
    })
    return result or []


def home_implied_prob_from_event(sport_key: str, home_name: str, away_name: str) -> Optional[float]:
    """Return P(home_name wins) from Betfair's Match Odds market. None if offline/unauth."""
    if not is_configured():
        return None
    events = list_sport_events(sport_key)
    if not events:
        return None
    hc = canonical(home_name or "")
    ac = canonical(away_name or "")
    if not hc or not ac:
        return None
    ht = hc.split()[-1]
    at = ac.split()[-1]
    matched_event = None
    for ev in events:
        name = (ev.get("name") or "").lower()
        if ht in name and at in name:
            matched_event = ev
            break
    if not matched_event:
        return None
    markets = list_match_odds_markets(matched_event["id"])
    if not markets:
        return None
    market = markets[0]
    books = list_market_book([market["market_id"]])
    if not books:
        return None
    book = books[0]
    runners = book.get("runners") or []
    # Betfair prices are decimal odds; implied prob = 1/decimal.
    rid_to_name = {r["id"]: r["name"] for r in market["runners"]}
    for r in runners:
        name = (rid_to_name.get(r.get("selectionId")) or "").lower()
        if not name:
            continue
        if ht in name:
            back = (r.get("ex") or {}).get("availableToBack") or []
            if back:
                price = back[0].get("price")
                if price:
                    try:
                        return 1.0 / float(price)
                    except (TypeError, ValueError):
                        return None
    return None
