"""ESPN scoreboard client.

Uses the public (unofficial, undocumented) ESPN site API:
    https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard

Supported sports (sport_key -> ESPN path):
    nfl         -> football/nfl
    nba         -> basketball/nba
    mlb         -> baseball/mlb
    nhl         -> hockey/nhl
    ncaaf       -> football/college-football
    ncaab       -> basketball/mens-college-basketball
    wnba        -> basketball/wnba
    mls         -> soccer/usa.1

No API key required. Every call is wrapped in try/except so a timeout or
rate limit returns None/[] and the UI continues rendering.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional
from urllib.error import URLError
from urllib.request import Request, urlopen

ESPN_SCOREBOARD = (
    "https://site.api.espn.com/apis/site/v2/sports/{path}/scoreboard"
)
ESPN_SCOREBOARD_DATE = ESPN_SCOREBOARD + "?dates={date}"

SPORT_PATHS: Dict[str, str] = {
    # US majors
    "nfl": "football/nfl",
    "nba": "basketball/nba",
    "mlb": "baseball/mlb",
    "nhl": "hockey/nhl",
    "ncaaf": "football/college-football",
    "ncaab": "basketball/mens-college-basketball",
    "ncaaw": "basketball/womens-college-basketball",
    "wnba": "basketball/wnba",
    "mls": "soccer/usa.1",
    # International soccer
    "epl": "soccer/eng.1",
    "la_liga": "soccer/esp.1",
    "bundesliga": "soccer/ger.1",
    "serie_a": "soccer/ita.1",
    "ligue_1": "soccer/fra.1",
    "ucl": "soccer/uefa.champions",
    "uel": "soccer/uefa.europa",
    "nwsl": "soccer/usa.nwsl",
    "liga_mx": "soccer/mex.1",
    # Combat / individual sports
    "ufc": "mma/ufc",
    "boxing": "boxing",
    "pga": "golf/pga",
    "atp": "tennis/atp",
    "wta": "tennis/wta",
    # Motorsport
    "f1": "racing/f1",
    "nascar": "racing/nascar-premier",
    # Other
    "cfl": "football/cfl",
    "xfl": "football/xfl",
}

# How The Odds API names those same sports (so UI can present a unified picker).
ODDS_API_SPORT_KEYS: Dict[str, str] = {
    "nfl": "americanfootball_nfl",
    "nba": "basketball_nba",
    "mlb": "baseball_mlb",
    "nhl": "icehockey_nhl",
    "ncaaf": "americanfootball_ncaaf",
    "ncaab": "basketball_ncaab",
    "ncaaw": "basketball_ncaaw",
    "wnba": "basketball_wnba",
    "mls": "soccer_usa_mls",
    "epl": "soccer_epl",
    "la_liga": "soccer_spain_la_liga",
    "bundesliga": "soccer_germany_bundesliga",
    "serie_a": "soccer_italy_serie_a",
    "ligue_1": "soccer_france_ligue_one",
    "ucl": "soccer_uefa_champs_league",
    "uel": "soccer_uefa_europa_league",
    "nwsl": "soccer_usa_nwsl",
    "liga_mx": "soccer_mexico_ligamx",
    "ufc": "mma_mixed_martial_arts",
    "boxing": "boxing_boxing",
    "pga": "golf_pga_championship_winner",
    "atp": "tennis_atp_us_open",   # Odds API lists ATP by tournament
    "wta": "tennis_wta_us_open",
    "f1": "motorsport_f1_championship_winner",
    "nascar": "motorsport_nascar_championship_winner",
    "cfl": "americanfootball_cfl",
    "xfl": "americanfootball_xfl",
}

DISPLAY_NAMES: Dict[str, str] = {
    "nfl": "NFL",
    "nba": "NBA",
    "mlb": "MLB",
    "nhl": "NHL",
    "ncaaf": "NCAA Football",
    "ncaab": "NCAA Basketball (M)",
    "ncaaw": "NCAA Basketball (W)",
    "wnba": "WNBA",
    "mls": "MLS",
    "epl": "Premier League",
    "la_liga": "La Liga",
    "bundesliga": "Bundesliga",
    "serie_a": "Serie A",
    "ligue_1": "Ligue 1",
    "ucl": "Champions League",
    "uel": "Europa League",
    "nwsl": "NWSL",
    "liga_mx": "Liga MX",
    "ufc": "UFC / MMA",
    "boxing": "Boxing",
    "pga": "PGA Tour",
    "atp": "Tennis — ATP",
    "wta": "Tennis — WTA",
    "f1": "Formula 1",
    "nascar": "NASCAR",
    "cfl": "CFL",
    "xfl": "UFL",
}


# Logical groupings so the UI can offer a nicer picker.
# "All sports" scans every sport we support. The Dashboard pre-checks ESPN
# per sport and only hits Odds API when there are games on the selected date,
# so empty days don't burn credits.
SPORT_GROUPS = [
    ("All sports", [
        # US Majors
        "nfl", "nba", "mlb", "nhl", "wnba",
        # College
        "ncaaf", "ncaab", "ncaaw",
        # Soccer (Europe + Americas)
        "epl", "la_liga", "bundesliga", "serie_a", "ligue_1",
        "ucl", "uel", "mls", "nwsl", "liga_mx",
        # Tennis
        "atp", "wta",
        # Combat / Individual
        "ufc", "boxing", "pga",
        # Motorsport
        "f1", "nascar",
    ]),
    ("US Majors", ["nfl", "nba", "mlb", "nhl", "wnba"]),
    ("College", ["ncaaf", "ncaab", "ncaaw"]),
    ("Soccer", ["mls", "epl", "la_liga", "bundesliga", "serie_a", "ligue_1", "ucl", "uel", "nwsl", "liga_mx"]),
    ("Combat / Individual", ["ufc", "boxing", "pga", "atp", "wta"]),
    ("Motorsport", ["f1", "nascar"]),
    ("Other", ["cfl", "xfl"]),
]


def _fetch(url, timeout=10):
    try:
        req = Request(url, headers={"User-Agent": "betting-dashboard-v2/1.0"})
        with urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except (URLError, ValueError, TimeoutError, ConnectionError, OSError):
        return None


def _comp_team(competitor):
    team = competitor.get("team") or {}
    return {
        "name": team.get("displayName") or team.get("name") or "",
        "short": team.get("shortDisplayName") or team.get("abbreviation") or "",
        "abbr": team.get("abbreviation") or "",
        "logo": team.get("logo"),
        "record": _parse_record(competitor.get("records") or []),
        "score": _coerce_int(competitor.get("score")),
        "winner": bool(competitor.get("winner")) if competitor.get("winner") is not None else None,
        "home_away": competitor.get("homeAway"),
    }


def _parse_record(records):
    for r in records:
        if r.get("type") == "total" or r.get("name") == "overall":
            return r.get("summary") or ""
    if records:
        return records[0].get("summary") or ""
    return ""


def _coerce_int(x):
    if x is None or x == "":
        return None
    try:
        return int(x)
    except (TypeError, ValueError):
        try:
            return int(float(x))
        except (TypeError, ValueError):
            return None


def get_scoreboard(sport_key: str, date: Optional[str] = None) -> List[dict]:
    """Return list of events for `sport_key` (today, or `date` as YYYYMMDD)."""
    path = SPORT_PATHS.get(sport_key)
    if not path:
        return []
    if date:
        url = ESPN_SCOREBOARD_DATE.format(path=path, date=date)
    else:
        url = ESPN_SCOREBOARD.format(path=path)
    data = _fetch(url)
    if not data:
        return []
    events = []
    for ev in data.get("events", []) or []:
        comps = (ev.get("competitions") or [{}])[0]
        competitors = comps.get("competitors") or []
        if len(competitors) < 2:
            continue
        home = next((c for c in competitors if c.get("homeAway") == "home"), competitors[0])
        away = next((c for c in competitors if c.get("homeAway") == "away"), competitors[-1])

        status = (ev.get("status") or {}).get("type") or {}
        status_state = status.get("state")  # pre, in, post
        detail = status.get("detail") or status.get("shortDetail") or ""

        start = ev.get("date") or comps.get("date")
        start_dt = None
        if start:
            try:
                start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
            except ValueError:
                start_dt = None

        venue = (comps.get("venue") or {}).get("fullName") or ""
        broadcast = ""
        broadcasts = comps.get("broadcasts") or []
        if broadcasts:
            names = broadcasts[0].get("names") or []
            broadcast = " · ".join(names[:2])

        events.append({
            "id": str(ev.get("id") or ""),
            "sport": sport_key,
            "name": ev.get("name") or f"{away.get('team', {}).get('displayName','')} at {home.get('team', {}).get('displayName','')}",
            "short_name": ev.get("shortName") or "",
            "start_utc": start_dt,
            "start_raw": start,
            "status_state": status_state,
            "status_detail": detail,
            "is_final": status_state == "post",
            "is_live": status_state == "in",
            "home": _comp_team(home),
            "away": _comp_team(away),
            "venue": venue,
            "broadcast": broadcast,
        })
    return events


def is_event_final(sport_key: str, event_id: str) -> Optional[dict]:
    """Minimal lookup for a single event's final status, used by picks auto-settle.

    Returns dict with `is_final`, `home_score`, `away_score`, `home_won` on success;
    or None if the event cannot be found.
    """
    if not event_id:
        return None
    events = get_scoreboard(sport_key)
    for ev in events:
        if ev.get("id") == str(event_id):
            if not ev.get("is_final"):
                return {"is_final": False, **_ev_scores(ev)}
            return {"is_final": True, **_ev_scores(ev)}
    return None


def _ev_scores(ev):
    hs = ev.get("home", {}).get("score")
    ascore = ev.get("away", {}).get("score")
    home_won = None
    if hs is not None and ascore is not None:
        home_won = hs > ascore
    return {
        "home_score": hs,
        "away_score": ascore,
        "home_won": home_won,
        "home_name": ev.get("home", {}).get("name"),
        "away_name": ev.get("away", {}).get("name"),
    }


def all_sport_keys() -> List[str]:
    return list(SPORT_PATHS.keys())
