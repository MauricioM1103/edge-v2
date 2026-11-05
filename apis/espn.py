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


def is_event_final(sport_key: str, event_id: str,
                   date: Optional[str] = None) -> Optional[dict]:
    """Minimal lookup for a single event's final status, used by picks auto-settle.

    When `date` is given (YYYYMMDD) we hit the dated scoreboard directly — the
    "today" scoreboard rolls off historical games, so picks logged yesterday
    could never settle without a date-aware fallback. If today has no hit
    and no date was passed, we quietly scan the last 7 days so recently-closed
    picks (overnight games, early-morning soccer) still grade.
    """
    if not event_id:
        return None
    want = str(event_id)
    # Preferred: dated scoreboard (deterministic, cheap once cached).
    if date:
        for ev in get_scoreboard(sport_key, date=date):
            if ev.get("id") == want:
                return {
                    "is_final": bool(ev.get("is_final")),
                    **_ev_scores(ev),
                }
        return None
    # Fallback: today's scoreboard first.
    for ev in get_scoreboard(sport_key):
        if ev.get("id") == want:
            return {
                "is_final": bool(ev.get("is_final")),
                **_ev_scores(ev),
            }
    # Last resort: sweep the last 7 days so overnight / yesterday games grade.
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    today = _dt.now(_tz.utc).date()
    for n in range(1, 8):
        d = (today - _td(days=n)).strftime("%Y%m%d")
        for ev in get_scoreboard(sport_key, date=d):
            if ev.get("id") == want:
                return {
                    "is_final": bool(ev.get("is_final")),
                    **_ev_scores(ev),
                }
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


# ============================================================================
# News + injuries — feed for the Breaking News hero on the Dashboard.
# ============================================================================

ESPN_NEWS = "https://site.api.espn.com/apis/site/v2/sports/{path}/news?limit=25"
ESPN_HEADSHOT = "https://a.espncdn.com/i/headshots/{league}/players/full/{pid}.png"
ESPN_TEAMLOGO = "https://a.espncdn.com/i/teamlogos/{league}/500/{abbr}.png"

# ESPN "news" `path` → headshot/logo `league` slug.
_LEAGUE_FROM_SPORT: Dict[str, str] = {
    "nfl": "nfl", "nba": "nba", "mlb": "mlb", "nhl": "nhl",
    "ncaaf": "college-football", "ncaab": "mens-college-basketball",
    "ncaaw": "womens-college-basketball",
    "wnba": "wnba", "mls": "soccer", "epl": "soccer", "la_liga": "soccer",
    "bundesliga": "soccer", "serie_a": "soccer", "ligue_1": "soccer",
    "ucl": "soccer", "uel": "soccer", "nwsl": "soccer", "liga_mx": "soccer",
    "ufc": "mma", "boxing": "boxing",
}


import re as _re

# Word-boundary patterns only: substring matching was flagging "Windy
# Amateur Championship" as a weather-impacting item and "Red Bulls" (NY Red
# Bulls MLS) as a line move. We use raw \b word boundaries and multi-word
# phrases are matched verbatim (not individually).
_SEV_PATTERNS = {
    "out": _re.compile(
        r"\b("
        r"ruled\s+out|out\s+(?:for\s+(?:the\s+)?(?:season|year|game|week|month)|indefinitely|tonight)|"
        r"out\s+\d+\s+(?:weeks|games|months)|"
        r"sidelined|placed\s+on\s+(?:ir|injured\s+reserve|il)|carted\s+off|"
        r"will\s+miss\s+(?:the\s+)?(?:game|start|season|year|match|rest|remainder|week)|"
        r"won'?t\s+play|"
        r"season-?ending|tears?\s+(?:his\s+)?acl|torn\s+(?:acl|mcl|achilles)|"
        r"suspended\s+\d+\s+(?:games|weeks|matches)"
        r")\b",
        _re.I,
    ),
    "gtd": _re.compile(
        r"\b("
        r"game-?time\s+decision|game\s+time\s+decision|questionable|doubtful|"
        r"day-to-day|day\s+to\s+day|probable|limited\s+practice"
        r")\b",
        _re.I,
    ),
    "weather": _re.compile(
        # Weather context only when combined with a venue/game indicator — avoids
        # matching "windy course" or "stormy debut".
        r"\b("
        r"(?:high\s+)?winds?\s+(?:expected|forecast|at)|"
        r"heavy\s+rain|downpour|thunderstorm(?:s)?|snow\s+game|"
        r"weather\s+(?:delay|forecast|concern|impact)|"
        r"temperatures?\s+(?:drop|in\s+the)|forecast\s+(?:calls|shows)"
        r")\b",
        _re.I,
    ),
    "line": _re.compile(
        r"\b("
        r"line\s+(?:move|movement|moves)|sharp\s+(?:money|action|side)|"
        r"steam\s+move|reverse\s+line\s+movement|rlm|"
        r"total\s+(?:drops?|climbs?|rises?)|spread\s+(?:moves?|shifts?)|"
        r"odds?\s+shift|closing\s+line"
        r")\b",
        _re.I,
    ),
}


def _classify_news(headline: str, description: str) -> str:
    """Severity bucket — out / gtd / weather / line / news.
    Checks the HEADLINE first (strongest signal), then the description.
    Word-boundary matching only — no substring hits that misclassify
    general stories as weather or injury."""
    hd = headline or ""
    desc = description or ""
    for sev in ("out", "gtd", "line", "weather"):
        pat = _SEV_PATTERNS[sev]
        if pat.search(hd):
            return sev
    # Description is a weaker signal — only check "out" / "gtd" there.
    for sev in ("out", "gtd"):
        if _SEV_PATTERNS[sev].search(desc):
            return sev
    return "news"


def _fetch_json(url: str, timeout: float = 4.0):
    # Caller (ui layer) handles st.cache_data; apis stays framework-free.
    try:
        req = Request(url, headers={"User-Agent": "edge-dashboard/2.1"})
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except (URLError, TimeoutError, ValueError, OSError):
        return None


def get_news(sport_key: str, limit: int = 12) -> List[Dict]:
    """Return a list of news/injury items for one sport. Each item:
        {
          id, headline, snippet, published, severity,
          sport_key, league,
          athlete: {id, name, headshot},     # may be None
          teams:   [{abbr, logo, color}],     # 0..2 entries
          url,
        }
    All network errors become an empty list. Caller should cache (st.cache_data).
    """
    path = SPORT_PATHS.get(sport_key)
    if not path:
        return []
    url = ESPN_NEWS.format(path=path)
    data = _fetch_json(url)
    if not data or not isinstance(data, dict):
        return []
    league = _LEAGUE_FROM_SPORT.get(sport_key, sport_key)
    articles = data.get("articles") or data.get("headlines") or []
    out: List[Dict] = []
    for a in articles[:limit]:
        if not isinstance(a, dict):
            continue
        headline = a.get("headline") or a.get("title") or ""
        description = a.get("description") or a.get("summary") or ""
        if not headline:
            continue
        # Extract athlete (first one, if any).
        athlete = None
        for key in ("categories", "athletes"):
            cats = a.get(key) or []
            for c in cats:
                if not isinstance(c, dict):
                    continue
                ath = c.get("athlete") if key == "categories" else c
                if isinstance(ath, dict) and ath.get("id"):
                    pid = ath.get("id")
                    athlete = {
                        "id": pid,
                        "name": ath.get("displayName") or ath.get("fullName") or "",
                        "headshot": (ath.get("headshot") or {}).get("href")
                        or ESPN_HEADSHOT.format(league=league, pid=pid),
                    }
                    break
            if athlete:
                break
        # Extract teams (up to 2).
        teams: List[Dict] = []
        for key in ("categories",):
            cats = a.get(key) or []
            for c in cats:
                if not isinstance(c, dict):
                    continue
                t = c.get("team")
                if isinstance(t, dict) and t.get("abbreviation"):
                    abbr = (t.get("abbreviation") or "").lower()
                    teams.append({
                        "abbr": t.get("abbreviation") or "",
                        "name": t.get("displayName") or "",
                        "color": t.get("color") or "",
                        "logo": t.get("logos", [{}])[0].get("href")
                        if t.get("logos") else ESPN_TEAMLOGO.format(league=league, abbr=abbr),
                    })
                if len(teams) >= 2:
                    break
        # Public link.
        link = ""
        for ln in a.get("links") or []:
            if isinstance(ln, dict):
                web = (ln.get("web") or {}).get("href") if isinstance(ln.get("web"), dict) else ln.get("href")
                if web:
                    link = web
                    break
        out.append({
            "id": a.get("id") or headline[:40],
            "headline": headline.strip(),
            "snippet": (description or "").strip(),
            "published": a.get("published") or a.get("lastModified") or "",
            "severity": _classify_news(headline, description),
            "sport_key": sport_key,
            "league": league,
            "athlete": athlete,
            "teams": teams[:2],
            "url": link,
        })
    return out


def get_news_for_sports(sport_keys: List[str], per_sport: int = 6) -> List[Dict]:
    """Aggregate news across multiple sports, newest first (by `published`)."""
    items: List[Dict] = []
    for sp in sport_keys:
        try:
            items.extend(get_news(sp, limit=per_sport))
        except Exception:
            continue
    items.sort(key=lambda x: x.get("published") or "", reverse=True)
    return items
