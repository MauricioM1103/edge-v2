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


#: Words that say nothing about WHICH club a name refers to.
_GENERIC_NAME_TOKENS = {"fc", "sc", "cf", "ac", "afc", "cd", "club", "city", "united", "real", "st", "state", "the", "de"}


def _name_tokens(name: str) -> List[str]:
    """Canonical tokens with Kalshi's spelling habits normalised.

    Kalshi writes "Kansas St.", "Miami (FL)", "Saint Louis" and truncates
    long names to an initial ("Los Angeles R"). A parenthetical is dropped,
    a trailing "st" becomes "state" and a leading "saint" becomes "st".
    """
    raw = re.sub(r"\([^)]*\)", " ", str(name or ""))
    toks = canonical(raw).split()
    if len(toks) > 1 and toks[-1] == "st":
        toks[-1] = "state"
    if toks and toks[0] == "saint":
        toks[0] = "st"
    return toks


def _contains_run(longer: List[str], shorter: List[str]) -> bool:
    n = len(shorter)
    return n > 0 and any(longer[i:i + n] == shorter for i in range(len(longer) - n + 1))


def names_team(sub_title: str, team: str) -> bool:
    """Could this Kalshi market name refer to this team?

    Deliberately permissive: on its own it only says "plausible". The
    caller (event_side_map) accepts an event only when the names fit the
    two teams in exactly ONE way, which is what makes it safe.
    """
    a, b = _name_tokens(sub_title), _name_tokens(team)
    if not a or not b:
        return False
    if a == b:
        return True
    if all(t in _GENERIC_NAME_TOKENS for t in a) or all(t in _GENERIC_NAME_TOKENS for t in b):
        return False
    # "Green Bay" / "Green Bay Packers", "Salt Lake" / "Real Salt Lake",
    # "Brest" / "Stade Brest 29": one name's words sit inside the other's.
    if _contains_run(b, a) or _contains_run(a, b):
        return True
    # Truncated to an initial: "Los Angeles R" / "Los Angeles Rams".
    if len(a) >= 2 and len(a[-1]) <= 2 and len(b) >= len(a) and a[:-1] == b[:len(a) - 1] and b[len(a) - 1].startswith(a[-1]):
        return True
    # Person names across feeds: "F. Tiafoe" / "Frances Tiafoe".
    return same_contestant(sub_title, team)


def event_side_map(markets: List[dict], home: str, away: str, commence_time=None,
                   max_day_gap: int = 1) -> Optional[Dict[str, str]]:
    """Check that a Kalshi event really is THIS game and say which market is which.

    Returns {market_ticker: "home" | "away" | "draw"} or None when the event
    cannot be shown to be this game. Every price the Board shows from Kalshi
    must pass through here.

    An event is accepted only when all of these hold:
      * its date (from the ticker) is within `max_day_gap` days of the game.
        A team's next fixture has the same team in it and a different date;
      * it has exactly two non-tie markets, and
      * those two names fit the two teams in exactly one way. If "Sacramento"
        fits the home team but the other market says "Utah" and the away
        team is San Antonio, this is a different game and nothing is used.

    The old matching accepted an event when a fragment of each team name
    appeared anywhere in the market names, with no date check, so "ac" (from
    "Le Havre AC") matched "Strasbourg Alsace" and a different week's game
    supplied the price.
    """
    if not markets or not home or not away:
        return None
    event_ticker = (markets[0].get("event_ticker") or "")
    if any((m.get("event_ticker") or "") != event_ticker for m in markets):
        return None
    tdate = _ticker_date(event_ticker)
    gdate = None
    if commence_time:
        try:
            ct = commence_time if isinstance(commence_time, datetime) else datetime.fromisoformat(str(commence_time).replace("Z", "+00:00"))
            gdate = ct.date()
        except (ValueError, TypeError):
            gdate = None
    if gdate is not None:
        if tdate is None or abs((gdate - tdate).days) > max_day_gap:
            return None
    ties = [m for m in markets if is_tie_market(m)]
    sides = [m for m in markets if not is_tie_market(m)]
    if len(sides) != 2 or len(ties) > 1:
        return None
    a, b = sides
    na, nb = a.get("yes_sub_title") or "", b.get("yes_sub_title") or ""
    fit_ab = names_team(na, home) and names_team(nb, away)   # a=home, b=away
    fit_ba = names_team(na, away) and names_team(nb, home)   # a=away, b=home
    if fit_ab == fit_ba:
        # Neither arrangement fits, or both do. Exact names break a tie
        # ("Florida" and "Florida St." both fit "Florida State Seminoles"
        # loosely, but only one of them fits it exactly once normalised).
        ta, tb, th, tw = _name_tokens(na), _name_tokens(nb), _name_tokens(home), _name_tokens(away)
        def _lead(x, y):  # x is the leading words of y
            return len(x) <= len(y) and y[:len(x)] == x
        score_ab = (len(ta) if _lead(ta, th) else 0) + (len(tb) if _lead(tb, tw) else 0)
        score_ba = (len(ta) if _lead(ta, tw) else 0) + (len(tb) if _lead(tb, th) else 0)
        if not (fit_ab and fit_ba) or score_ab == score_ba:
            return None
        fit_ab, fit_ba = score_ab > score_ba, score_ba > score_ab
    out = {}
    out[a.get("ticker") or na] = "home" if fit_ab else "away"
    out[b.get("ticker") or nb] = "away" if fit_ab else "home"
    for t in ties:
        out[t.get("ticker") or "tie"] = "draw"
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
        # Abbreviations overlap ("SA" sits inside "SAC"), so a ticker match
        # alone can pick another game played the same day. When the team
        # names are known the market names have to fit them too.
        if home_name and away_name and event_side_map(markets, home_name, away_name, event_start_utc) is None:
            continue
        return markets

    # Fallback by name. Same rule as everywhere else: right date, and the two
    # market names fit the two teams in exactly one way.
    if home_name and away_name:
        for event_ticker, markets in grouped.items():
            if event_side_map(markets, home_name, away_name, event_start_utc) is not None:
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


# ---------------------------------------------------------------------------
# Name-based matching (tennis / combat sports / soccer)
# ---------------------------------------------------------------------------
# Team sports are matched through ESPN abbreviations in the event ticker.
# Players and fighters have no abbreviation table, and ESPN's scoreboard does
# not list tennis match-by-match, so these are matched straight against the
# sharp-book slate: a Kalshi event belongs to an Odds API game when each of
# its "<name> wins" markets names one of the two contestants.

_DATE_PREFIX_RE = re.compile(r"^(\d{2}[A-Z]{3}\d{2})")
_TIE_NAMES = {"tie", "draw"}


def is_tie_market(market: dict) -> bool:
    """True for the "Tie is the result" market of a three-way soccer event."""
    return canonical(market.get("yes_sub_title") or "") in _TIE_NAMES


def _ticker_date(event_ticker: str):
    parts = (event_ticker or "").split("-", 1)
    if len(parts) != 2:
        return None
    m = _DATE_PREFIX_RE.match(parts[1])
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%y%b%d").date()
    except ValueError:
        return None


def same_contestant(a: str, b: str) -> bool:
    """Loose person / club name equality across feeds.

    Handles "Frances Tiafoe" vs "F. Tiafoe" vs "Tiafoe", accents, and club
    short names ("Tottenham" vs "Tottenham Hotspur"). Requires the surname
    (last token) to agree, and first initials to agree when both are given,
    so two different players who share a surname on one card stay apart.
    """
    ca, cb = canonical(a), canonical(b)
    if not ca or not cb:
        return False
    if ca == cb:
        return True
    ta, tb = ca.split(), cb.split()
    # One name is a leading slice of the other: "tottenham" / "tottenham hotspur".
    n = min(len(ta), len(tb))
    if ta[:n] == tb[:n]:
        return True
    if ta[-1] != tb[-1]:
        return False
    if len(ta) > 1 and len(tb) > 1 and ta[0][0] != tb[0][0]:
        return False
    return True


def match_markets_to_games(kalshi_markets: List[dict], odds_games: List[dict],
                           max_day_gap: int = 1) -> Dict[tuple, List[dict]]:
    """Group Kalshi markets under the Odds API game they price.

    Returns {(home_team, away_team): [markets]} keyed by the Odds API names,
    which is the key shape scan_kalshi_vs_pinnacle looks games up by. The
    tie market of a three-way event rides along in the list. An event is
    only attached when BOTH contestants are named by its markets and the
    ticker date is within `max_day_gap` days of the game's start, so a
    rematch next week cannot borrow this week's prices.
    """
    out: Dict[tuple, List[dict]] = {}
    if not kalshi_markets or not odds_games:
        return out
    grouped = _group_markets_by_event(kalshi_markets)
    for g in odds_games:
        home = g.get("home_team") or ""
        away = g.get("away_team") or ""
        if not home or not away:
            continue
        game_date = None
        ct = g.get("commence_time")
        if ct:
            try:
                game_date = datetime.fromisoformat(str(ct).replace("Z", "+00:00")).date()
            except ValueError:
                game_date = None
        for event_ticker, markets in grouped.items():
            if event_side_map(markets, home, away, ct, max_day_gap=max_day_gap) is None:
                continue
            out[(home, away)] = markets
            break
    return out
