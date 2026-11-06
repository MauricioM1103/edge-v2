"""Tab renderers: Dashboard, Pro Tools, Picks Ledger."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

import streamlit as st

from apis import espn, odds as odds_api, kalshi, polymarket, betfair, discord as discord_api
from apis.team_mapping import same_team
from apis.book_filter import book_market
from apis.kalshi_match import find_event_markets, home_implied_prob
from calc.odds import (
    american_to_decimal, american_to_fractional, decimal_to_american,
    implied_prob_american, format_american, profit_on_win, no_vig_two_way,
)
from calc.ev import expected_value, ev_percent, edge as calc_edge
from calc.kelly import kelly_fraction, kelly_stake
from calc.units import recommend_units, units_from_kelly, format_units
from calc.clv import line_move_prob_delta, classify_move, clv_percent
from calc import agent as agent_calc
from storage import picks as picks_store, settings as settings_store
from ui.components import (
    html, brand_bar, metric_row, game_card, ev_tone, odds_table_row,
)
from apis import odds_history
from backtest import engine as backtest_engine


# ============================================================================
# cached data loaders - respect API rate limits
# ============================================================================

@st.cache_data(ttl=20, show_spinner=False)
def load_espn_scoreboard(sport_key: str, date_str: Optional[str] = None):
    """ESPN scoreboard - 20s TTL so the live-refresh loop actually sees new
    scores on each tick. ESPN is free, so cache pressure is a non-issue."""
    try:
        return espn.get_scoreboard(sport_key, date_str)
    except Exception:
        return []


def _central_today_str() -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("America/Chicago")).strftime("%Y%m%d")


@st.cache_data(ttl=3600, show_spinner=False)
def load_odds(sport_key: str, regions: str = "eu", markets: str = "h2h", cache_ttl: int = 3600):
    """Fetch live odds for one sport. Default: Pinnacle only (eu), moneyline only (h2h)
    = 1 credit per sport. Set regions/markets wider in Settings for more coverage."""
    odds_sport = espn.ODDS_API_SPORT_KEYS.get(sport_key)
    if not odds_sport:
        return []
    try:
        return odds_api.get_odds(odds_sport, regions=regions, markets=markets, cache_ttl=cache_ttl)
    except Exception:
        return []


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def load_alt_markets(sport_key: str, event_ids_tuple: tuple, regions: str, alt_markets: str, cache_ttl: int = 6 * 3600):
    """Fetch alt markets (1H totals / BTTS / spreads_h1) for a batch of events
    via the per-event endpoint. Returns {event_id: event_data_dict}.

    Streamlit's cache_data takes the tuple of event IDs as part of the key so
    the same slate hits the cache; the underlying on-disk cache in apis.odds
    also persists across container restarts. Credit cost is markets × regions
    PER EVENT, which is why this is opt-in via the odds_alt_markets setting."""
    odds_sport = espn.ODDS_API_SPORT_KEYS.get(sport_key)
    if not odds_sport or not event_ids_tuple or not (alt_markets or "").strip():
        return {}
    out = {}
    for eid in event_ids_tuple:
        try:
            d = odds_api.get_event_odds(
                odds_sport, eid,
                regions=regions, markets=alt_markets, cache_ttl=cache_ttl,
            )
        except Exception:
            d = None
        if d:
            out[eid] = d
    return out


@st.cache_data(ttl=60, show_spinner=False)
def load_kalshi_markets(sport_key: str):
    try:
        return kalshi.fetch_sport_markets(sport_key)
    except Exception:
        return []


@st.cache_data(ttl=45, show_spinner=False)
def load_kalshi_balance():
    try:
        return kalshi.resolve_balance()
    except Exception:
        return {"usd": None, "source": None}


@st.cache_data(ttl=120, show_spinner=False)
def load_polymarket_markets():
    try:
        return polymarket.list_active_markets(limit=500)
    except Exception:
        return []


@st.cache_data(ttl=120, show_spinner=False)
def load_betfair_configured() -> bool:
    try:
        return betfair.is_configured()
    except Exception:
        return False


def _prob_sources_for(game_meta, kalshi_prob_home, polymarket_prob_home=None, betfair_prob_home=None):
    """Build the prob-strip dict: Pinnacle + DK + Kalshi + Polymarket + Betfair + consensus."""
    sources = {}
    raw = (game_meta or {}).get("raw")
    # Pinnacle (sharpest book, from Odds API if present)
    if raw:
        pin = book_market(raw, "Pinnacle", "h2h") or book_market(raw, "pinnacle", "h2h")
        if pin:
            pin_h = implied_prob_american(pin.get("home_price"))
            pin_a = implied_prob_american(pin.get("away_price"))
            if pin_h is not None or pin_a is not None:
                sources["pinnacle"] = {"home": pin_h, "away": pin_a}
        dk = book_market(raw, "DraftKings", "h2h") or book_market(raw, "draftkings", "h2h")
        if dk:
            dk_h = implied_prob_american(dk.get("home_price"))
            dk_a = implied_prob_american(dk.get("away_price"))
            if dk_h is not None or dk_a is not None:
                sources["dk"] = {"home": dk_h, "away": dk_a}
    # Kalshi (prediction market, no vig)
    if kalshi_prob_home is not None:
        sources["kalshi"] = {"home": kalshi_prob_home, "away": 1.0 - kalshi_prob_home}
    # Polymarket (prediction market, no vig)
    if polymarket_prob_home is not None:
        sources["polymarket"] = {"home": polymarket_prob_home, "away": 1.0 - polymarket_prob_home}
    # Betfair Exchange (peer-to-peer, no vig)
    if betfair_prob_home is not None:
        sources["betfair"] = {"home": betfair_prob_home, "away": 1.0 - betfair_prob_home}
    # Consensus from all sportsbooks (vig-included)
    h2h = (game_meta or {}).get("h2h") or {}
    ch_imp = implied_prob_american(h2h.get("home_price"))
    ca_imp = implied_prob_american(h2h.get("away_price"))
    if ch_imp is not None or ca_imp is not None:
        sources["consensus"] = {"home": ch_imp, "away": ca_imp}
    return sources or None


def _merge_events_with_odds(
    events, odds_games,
    kalshi_markets=None, polymarket_markets=None, sport_key=None,
):
    """Attach odds + Kalshi + Polymarket + Betfair implied probs to each ESPN event.

    Returns list of (event, odds_meta, exchange_probs) where exchange_probs
    is a dict with keys 'kalshi' / 'polymarket' / 'betfair' (each a float P(home) or None).
    """
    betfair_available = load_betfair_configured()
    out = []
    for ev in events:
        home_name = (ev.get("home") or {}).get("name")
        away_name = (ev.get("away") or {}).get("name")
        matched = None
        for og in odds_games:
            if same_team(og.get("home_team"), home_name) and same_team(og.get("away_team"), away_name):
                matched = og
                break
        odds_meta = None
        if matched:
            odds_meta = {
                "h2h": odds_api.consensus_market(matched, "h2h"),
                "spreads": odds_api.consensus_market(matched, "spreads"),
                "totals": odds_api.consensus_market(matched, "totals"),
                "raw": matched,
            }
        exch_probs = {"kalshi": None, "polymarket": None, "betfair": None}
        # Kalshi
        if kalshi_markets:
            home_abbr = (ev.get("home") or {}).get("abbr") or ""
            away_abbr = (ev.get("away") or {}).get("abbr") or ""
            event_markets = find_event_markets(
                kalshi_markets, ev.get("start_utc"),
                home_abbr, away_abbr, home_name, away_name,
            )
            if event_markets:
                exch_probs["kalshi"] = home_implied_prob(event_markets, home_name, home_abbr)
        # Polymarket
        if polymarket_markets:
            pm = polymarket.find_game_market(
                polymarket_markets, home_name, away_name, ev.get("start_utc"),
            )
            if pm:
                exch_probs["polymarket"] = polymarket.home_implied_prob(pm, home_name, away_name)
        # Betfair - only if auth configured; otherwise skip the round-trip
        if betfair_available and sport_key:
            try:
                exch_probs["betfair"] = betfair.home_implied_prob_from_event(
                    sport_key, home_name, away_name,
                )
            except Exception:
                exch_probs["betfair"] = None
        out.append((ev, odds_meta, exch_probs))
    return out


# ============================================================================
# Breaking News hero — bento-grid of ESPN news/injuries scoped to today's sports.
# ============================================================================

@st.cache_data(ttl=90, show_spinner=False)
def load_news_for_sports(sports_tuple: tuple, per_sport: int = 6):
    """Cached ESPN news fetch. 90s TTL so classifier changes propagate fast
    and severity badges stay fresh across short live-refresh ticks."""
    try:
        return espn.get_news_for_sports(list(sports_tuple), per_sport=per_sport)
    except Exception:
        return []


def _relative_time(iso_ts: str) -> str:
    if not iso_ts:
        return ""
    try:
        if iso_ts.endswith("Z"):
            iso_ts = iso_ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso_ts)
        delta = datetime.now(timezone.utc) - dt.astimezone(timezone.utc)
        s = int(delta.total_seconds())
        if s < 60:
            return "just now"
        if s < 3600:
            return f"{s // 60} min ago"
        if s < 86400:
            return f"{s // 3600} hr ago"
        return f"{s // 86400} d ago"
    except (ValueError, AttributeError):
        return ""


_SEV_ICON = {"out": "✕ Out", "gtd": "◐ Game-Time", "weather": "⚑ Weather", "line": "↑ Sharp Move", "news": "◆ News"}


def _sev_label_for(sev: str, size: str) -> str:
    """Spec §8: severity pill wording. The featured tile uses a longer form
    for high-impact outages so the hero reads like a dedicated callout."""
    sev = (sev or "news").lower()
    if sev == "out" and size == "featured":
        return "✕ Ruled Out · High Impact"
    return _SEV_ICON.get(sev, "◆ News")


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _tile_bg_colors(teams: list) -> tuple:
    """Pick (c1, c2) from first team's color, falling back to card bg."""
    primary = ""
    if teams:
        primary = (teams[0].get("color") or "").strip().lstrip("#")
    if primary and len(primary) in (3, 6):
        return (f"#{primary}", f"#{primary}22")
    return ("#131F35", "#0a1220")


# Team color lookup for tile gradients + board chips. Hex strings without leading
# "#". Covers NBA/NFL/MLB/NHL majors plus NCAAF/NCAAB blue-bloods and the top
# soccer clubs the news feed is likely to surface. Missing teams fall through to
# LEAGUE_COLOR_FALLBACK below.
TEAM_COLORS: dict = {
    # NBA
    "BOS": "007A33", "LAL": "552583", "GSW": "1D428A", "MIA": "98002E",
    "CHI": "CE1141", "NYK": "F58426", "PHI": "006BB6", "DAL": "00538C",
    "DEN": "0E2240", "PHX": "1D1160", "MIL": "00471B", "LAC": "C8102E",
    "CLE": "6F263D", "ATL": "E03A3E", "TOR": "CE1141", "MEM": "5D76A9",
    "SAS": "C4CED4", "POR": "E03A3E", "SAC": "5A2D81", "MIN": "0C2340",
    "OKC": "007AC1", "NOP": "0C2340", "UTA": "002B5C", "HOU": "CE1141",
    "IND": "FDBB30", "DET": "1D42BA", "BKN": "000000", "ORL": "0077C0",
    "WAS": "002B5C", "CHA": "1D1160",
    # NFL
    "KC": "E31837", "SF": "AA0000", "BUF": "00338D", "DAL_NFL": "003594",
    "PHI_NFL": "004C54", "GB": "203731", "NE": "002244", "PIT": "FFB612",
    "BAL": "241773", "MIA_NFL": "008E97", "CIN": "FB4F14", "CLE_NFL": "311D00",
    "DET_NFL": "0076B6", "LAR": "003594", "LAC_NFL": "0080C6", "LV": "000000",
    "SEA": "002244", "DEN_NFL": "FB4F14", "MIN_NFL": "4F2683", "TB": "D50A0A",
    "NO": "D3BC8D", "ATL_NFL": "A71930", "ARI_NFL": "97233F", "JAX": "006778",
    "TEN": "0C2340", "IND_NFL": "002C5F", "WAS_NFL": "5A1414", "HOU_NFL": "03202F",
    "NYG": "0B2265", "NYJ": "125740", "CAR_NFL": "0085CA", "CHI_NFL": "0B162A",
    # MLB
    "LAD": "005A9C", "NYY": "132448", "HOU_MLB": "EB6E1F", "ATL_MLB": "CE1141",
    "PHI_MLB": "E81828", "BOS_MLB": "BD3039", "MIL_MLB": "12284B", "CHC": "0E3386",
    "TOR_MLB": "134A8E", "SD": "2F241D", "SF_MLB": "FD5A1E", "BAL_MLB": "DF4601",
    "TEX_MLB": "003278", "MIN_MLB": "002B5C", "ARI_MLB": "A71930", "NYM": "002D72",
    "SEA_MLB": "0C2C56", "STL": "C41E3A", "CLE_MLB": "E31937", "DET_MLB": "0C2340",
    "CIN_MLB": "C6011F", "PIT_MLB": "FDB827", "KC_MLB": "004687", "CWS": "27251F",
    "TB_MLB": "092C5C", "COL": "33006F", "OAK": "003831", "WAS_MLB": "AB0003",
    "MIA_MLB": "00A3E0", "LAA": "BA0021",
    # NHL
    "BOS_NHL": "FFB81C", "TOR_NHL": "00205B", "COL_NHL": "6F263D", "EDM": "041E42",
    "FLA": "041E42", "NYR": "0038A8", "DAL_NHL": "006847", "CAR_NHL": "CC0000",
    "VGK": "B4975A", "TB_NHL": "002868", "PIT_NHL": "FCB514", "WAS_NHL": "C8102E",
    "NJD": "CE1126", "NYI": "00539B", "PHI_NHL": "F74902", "WPG": "041E42",
    "VAN": "00205B", "LAK": "111111", "SJS": "006D75", "MIN_NHL": "154734",
    "STL_NHL": "002F87", "NSH": "FFB81C", "CBJ": "002654", "SEA_NHL": "001628",
    "DET_NHL": "CE1126", "OTT": "C52032", "ARI_NHL": "8C2633", "MTL": "AF1E2D",
    "CHI_NHL": "CF0A2C", "BUF_NHL": "003087", "ANA": "F47A38", "CGY": "D2001C",
    # NCAA (major brands)
    "BAMA": "9E1B32", "UGA": "BA0C2F", "OSU": "BB0000", "MICH": "00274C",
    "ND": "0C2340", "TEX_NCAA": "BF5700", "USC": "990000", "LSU": "461D7C",
    "MISS": "CE1126", "AUB": "0C2340", "PENN": "041E42", "OREG": "154733",
    "WASH": "4B2E83", "FSU": "782F40", "CLEM": "F56600", "OU": "841617",
    "OKST": "FF7300", "KSU": "512888", "KU": "0051BA", "WVU": "002855",
    # Soccer
    "BAR": "A50044", "RMA": "FEBE10", "MCI": "6CABDD", "MUN": "DA291C",
    "LIV": "C8102E", "ARS": "EF0107", "CHE": "034694", "TOT": "132257",
    "BAY": "DC052D", "DOR": "FDE100", "PSG": "004170", "JUV": "000000",
    "INT": "0068B4", "MIL_SER": "FB090B", "ATM": "CB3524", "BVB": "FDE100",
}

LEAGUE_COLOR_FALLBACK: dict = {
    "nba": "1D428A", "wnba": "002B5C",
    "nfl": "013369", "ncaaf": "0B2265",
    "mlb": "002D72",
    "nhl": "000000",
    "ncaab": "0B2265", "ncaaw": "0B2265",
    "mls": "0B3D91", "epl": "3D195B", "la_liga": "EE8707",
    "bundesliga": "D20515", "serie_a": "008FD7", "ligue_1": "DAE025",
    "ucl": "0E1E8C", "uel": "F7901F", "nwsl": "C8102E", "liga_mx": "006847",
    "ufc": "D20A0A", "f1": "E10600", "nascar": "F6E400",
    "pga": "0D4C3D", "atp": "052E6E", "wta": "7C2855",
}

# Light-text teams whose primary color is bright enough that the .team-chip's
# default white glyph vanishes; the .team-chip.light class flips to navy ink.
_LIGHT_TEAM_CHIPS = {
    "IND", "GSW", "PIT", "PIT_MLB", "NSH", "BOS_NHL", "TOR_NHL",
    "KSU", "DOR", "BVB", "MISS",  # Ole Miss / Dortmund yellow
}


def _hex_to_rgb(h: str) -> tuple:
    h = (h or "").lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6:
        return (20, 30, 50)
    try:
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    except ValueError:
        return (20, 30, 50)


def _darken(hex_color: str, mix_black: float = 0.55) -> str:
    """Mix `hex_color` toward black by `mix_black` fraction. 0 = unchanged, 1 = black."""
    r, g, b = _hex_to_rgb(hex_color)
    mix_black = max(0.0, min(1.0, mix_black))
    r = int(r * (1 - mix_black))
    g = int(g * (1 - mix_black))
    b = int(b * (1 - mix_black))
    return f"#{r:02X}{g:02X}{b:02X}"


def _resolve_team_color(abbr: str, league: str = "") -> str:
    """Return a #RRGGBB for a team, falling back to the league's house color."""
    key = (abbr or "").upper().strip()
    if key and key in TEAM_COLORS:
        return "#" + TEAM_COLORS[key]
    lk = (league or "").lower()
    return "#" + LEAGUE_COLOR_FALLBACK.get(lk, "131F35")


def _team_color_pair(teams: list, league_key: str = "") -> tuple:
    """Return (c1, c2, accent) hex strings for a tile. c2 = team color, c1 = darkened."""
    accent = ""
    if teams:
        for t in teams:
            abbr = (t.get("abbr") or "").upper()
            col = TEAM_COLORS.get(abbr)
            if col:
                accent = "#" + col
                break
            raw = (t.get("color") or "").strip().lstrip("#")
            if raw and len(raw) in (3, 6):
                accent = ("#" + raw) if len(raw) == 6 else ("#" + "".join(c * 2 for c in raw))
                break
    if not accent:
        accent = "#" + LEAGUE_COLOR_FALLBACK.get((league_key or "").lower(), "131F35")
    c1 = _darken(accent, 0.55)
    c2 = accent
    return (c1, c2, accent)


_ESPN_LEAGUE_LOGO = "https://a.espncdn.com/i/teamlogos/leagues/500/{slug}.png"
# Sport_key → ESPN's league-logo slug. Falls back to the sport_key itself for
# soccer regions (ESPN keeps those in /soccer/ assets).
_LEAGUE_LOGO_SLUG = {
    "nfl": "nfl", "nba": "nba", "mlb": "mlb", "nhl": "nhl",
    "ncaaf": "ncaa", "ncaab": "ncaa", "ncaaw": "ncaa", "wnba": "wnba",
    "mls": "mls", "epl": "eng.1", "la_liga": "esp.1",
    "bundesliga": "ger.1", "serie_a": "ita.1", "ligue_1": "fra.1",
    "ucl": "uefa.champions", "uel": "uefa.europa", "nwsl": "nwsl",
    "liga_mx": "mex.1", "ufc": "ufc", "pga": "pga", "atp": "atp", "wta": "wta",
}


def _trunc_words(s: str, limit: int) -> str:
    """Truncate at word boundary with a trailing ellipsis so the clamp has room."""
    s = (s or "").strip()
    if len(s) <= limit:
        return s
    cut = s[:limit].rsplit(" ", 1)[0]
    return (cut or s[:limit]).rstrip(",.;:") + "…"


# ----------------------------------------------------------------------------
# Spec §1-4a: tile validation helpers. Every tile that reaches the UI must
# pass this validator or get dropped and replaced from the ranked queue.
# ----------------------------------------------------------------------------

def _resolve_tile_image(item: dict) -> tuple:
    """Return ('portrait'|'duo'|'solo'|'league', data_dict) or (None, None)
    when nothing on the item can supply a renderable image.

    The fallback chain mirrors the mockup: athlete headshot (best), two-team
    logo composite (next), one-team logo, league logo (last resort). Each
    rung requires a non-empty URL — we don't HEAD-check (too slow), we trust
    ESPN's CDN. Image onerror handlers on the rendered <img> swap in the
    ESPN league fallback so a cold CDN miss still paints something."""
    athlete = item.get("athlete") or {}
    headshot = (athlete.get("headshot") or "").strip()
    if headshot:
        return ("portrait", {"src": headshot})
    teams = item.get("teams") or []
    if len(teams) >= 2:
        l1 = (teams[0].get("logo") or "").strip()
        l2 = (teams[1].get("logo") or "").strip()
        if l1 and l2:
            return ("duo", {
                "l1": l1, "l2": l2,
                "a1": (teams[0].get("abbr") or "").upper(),
                "a2": (teams[1].get("abbr") or "").upper(),
            })
    if len(teams) == 1:
        l1 = (teams[0].get("logo") or "").strip()
        if l1:
            return ("solo", {"l1": l1, "a1": (teams[0].get("abbr") or "").upper()})
    slug = _LEAGUE_LOGO_SLUG.get((item.get("sport_key") or "").lower())
    if slug:
        return ("league", {
            "src": _ESPN_LEAGUE_LOGO.format(slug=slug),
            "alt": item.get("sport_key") or "",
        })
    return (None, None)


def _match_item_to_slate(item: dict, merged: list) -> tuple:
    """Return (ev, odds_meta) for the slate game this news story is about,
    or (None, None) when there's no match. Finished games (ESPN status_state
    == 'post', or an 'is_final' flag) count as no-match so the tile rotates
    out per spec §3.

    Sport-gated: an MLB news item ('TEX' = Rangers) must NOT match an NCAAF
    game ('TEX' = Longhorns). We require the matched slate game's sport to
    equal the news item's sport_key before accepting the match."""
    item_abbrs = {(t.get("abbr") or "").upper() for t in (item.get("teams") or [])}
    item_abbrs.discard("")
    if not item_abbrs:
        return (None, None)
    item_sport = (item.get("sport_key") or "").lower()
    for ev, odds_meta, _ex in (merged or []):
        # Sport gate — prevents cross-sport abbr collisions (TEX = Rangers
        # vs Longhorns, NYG = Giants NFL vs SF Giants MLB, etc).
        ev_sport = (ev.get("sport") or ev.get("sport_key") or "").lower()
        if item_sport and ev_sport and item_sport != ev_sport:
            continue
        away_abbr = ((ev.get("away") or {}).get("abbr") or "").upper()
        home_abbr = ((ev.get("home") or {}).get("abbr") or "").upper()
        if not (away_abbr in item_abbrs or home_abbr in item_abbrs):
            continue
        # Spec §3: finished games bump the tile.
        status = (ev.get("status_state") or "").lower()
        if status == "post" or ev.get("is_final"):
            continue
        return (ev, odds_meta or {})
    return (None, None)


def _ct_time_label(ev: dict) -> str:
    """Render ev['start_utc'] / 'commence_time' as '7:40 PM CT' (Central).
    Returns '' when nothing resolves — the validator then drops the tile."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    raw = ev.get("start_utc") or ev.get("commence_time") or ""
    if not raw:
        return ""
    try:
        if isinstance(raw, datetime):
            dt = raw
        else:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        ct = dt.astimezone(ZoneInfo("America/Chicago"))
        return ct.strftime("%-I:%M %p CT") if hasattr(ct, "strftime") else ""
    except Exception:
        try:
            return dt.astimezone(ZoneInfo("America/Chicago")).strftime("%#I:%M %p CT")
        except Exception:
            return ""


def _build_featured_matrix(item: dict, merged: list) -> str:
    """Spec §4: 4-cell matrix with REAL open→current numbers. Returns the HTML
    when all 4 cells can be filled with real data, else '' so the caller drops
    the tile per spec (no more em-dash fallback)."""
    ev, meta = _match_item_to_slate(item, merged)
    if not meta:
        return ""
    spr = meta.get("spreads") or {}
    tot = meta.get("totals") or {}
    h2h = meta.get("h2h") or {}
    away_abbr = ((ev.get("away") or {}).get("abbr") or "AWAY").upper()
    home_abbr = ((ev.get("home") or {}).get("abbr") or "HOME").upper()

    def _pair_point(cur_v, open_v=None):
        """Format spread/total points with sign and 0.5 snap."""
        if cur_v is None:
            return None
        cur_s = _fmt_point(cur_v)
        if not cur_s:
            return None
        if open_v is None:
            return cur_s
        open_s = _fmt_point(open_v)
        if not open_s or open_s == cur_s:
            return cur_s
        return f"{open_s} → {cur_s}"

    def _pair_total(cur_v, open_v=None):
        """Totals never carry signs — "45.5" not "+45.5"."""
        if not isinstance(cur_v, (int, float)):
            return None
        snapped = round(float(cur_v) * 2) / 2
        cur_s = f"{snapped:g}"
        if open_v is None:
            return cur_s
        if not isinstance(open_v, (int, float)):
            return cur_s
        open_snapped = round(float(open_v) * 2) / 2
        if open_snapped == snapped:
            return cur_s
        return f"{open_snapped:g} → {cur_s}"

    def _pair_price(cur_v, open_v=None):
        """American moneyline as whole-number integer with sign."""
        cur_s = _fmt_price(cur_v)
        if not cur_s:
            return None
        if open_v is None:
            return cur_s
        open_s = _fmt_price(open_v)
        if not open_s or open_s == cur_s:
            return cur_s
        return f"{open_s} → {cur_s}"

    # Spread/Total cells show the POINT (handicap/line), not the juice price.
    # ML cells show the American moneyline price on each side.
    cells = [
        ("Spread", _pair_point(spr.get("home_point")), "accent"),
        ("Total",  _pair_total(tot.get("home_point")), ""),
        (f"{away_abbr} ML", _pair_price(h2h.get("away_price"), h2h.get("away_opener")), ""),
        (f"{home_abbr} ML", _pair_price(h2h.get("home_price"), h2h.get("home_opener")), ""),
    ]
    # Spec §4: every cell must have real data; else drop the tile.
    if any(v is None for _k, v, _c in cells):
        return ""
    # ML classes: price got better for bettor (more plus) = good, worse = bad.
    def _ml_cls(opener, current):
        try:
            if opener is None or current is None:
                return ""
            return "good" if float(current) > float(opener) else ("bad" if float(current) < float(opener) else "")
        except (TypeError, ValueError):
            return ""
    away_cls = _ml_cls(h2h.get("away_opener"), h2h.get("away_price"))
    home_cls = _ml_cls(h2h.get("home_opener"), h2h.get("home_price"))
    cells[2] = (cells[2][0], cells[2][1], away_cls)
    cells[3] = (cells[3][0], cells[3][1], home_cls)
    inner = "".join(
        f'<div class="cell"><div class="k">{_esc(k)}</div>'
        f'<div class="v {cls}">{_esc(v)}</div></div>'
        for k, v, cls in cells
    )
    return f'<div class="matrix">{inner}</div>'


def _fmt_point(v) -> str:
    """Format a spread/total point as a 1-decimal number, e.g. -2.5, 224.5.
    Averages from consensus_market can be ugly floats like -1.38462; rounding
    to a half-point is the right level of precision for how bettors read a line."""
    if not isinstance(v, (int, float)):
        return ""
    # Snap to the nearest 0.5 (standard betting line granularity) then strip
    # trailing zeros for whole-number lines like "45" vs "45.0".
    snapped = round(float(v) * 2) / 2
    return f"{snapped:+g}" if snapped != 0 else "0"


def _fmt_price(v) -> str:
    """American price → whole number with explicit sign, e.g. +130, -140.
    Anything inside (-100, 100) is not a valid American price; refuse it so a
    bad upstream average can never reach a tile."""
    if not isinstance(v, (int, float)) or abs(float(v)) < 100:
        return ""
    return f"{int(round(float(v))):+d}"


def _market_tail_for(ev: dict, meta: dict) -> str:
    """Compact market-lines tail for mid/wide tile foot (spec §4). Format:
    '· HOME -2.5 · O 44.5 · ML -140' — pulls whatever the odds cache has.
    Returns '' when nothing numeric is available (validator then drops the
    tile per spec: no em-dashes in rendered tiles)."""
    if not meta:
        return ""
    home_abbr = ((ev.get("home") or {}).get("abbr") or "HOME").upper()
    spr = meta.get("spreads") or {}
    tot = meta.get("totals") or {}
    h2h = meta.get("h2h") or {}
    parts = []
    # Spread: show the HANDICAP (point), not the juice price.
    sp_pt_s = _fmt_point(spr.get("home_point"))
    if sp_pt_s:
        parts.append(f"· {home_abbr} {sp_pt_s}")
    # Total: show the O/U line (point), always positive.
    tot_pt = tot.get("home_point")
    if isinstance(tot_pt, (int, float)):
        _rt = round(float(tot_pt) * 2) / 2
        parts.append(f"· O {_rt:g}")
    # Moneyline: whole-number American price on the home side.
    ml_s = _fmt_price(h2h.get("home_price"))
    if ml_s:
        parts.append(f"· ML {ml_s}")
    return " ".join(parts)


def _validate_tile(item: dict, merged: list, size: str) -> dict:
    """Spec §4a: single validation pass. Returns a dict with pre-built pieces
    the renderer can drop in, OR {} when the tile can't be rendered cleanly.
    Rejection reason is attached to the empty dict as ['_drop_reason'] for
    the server-side drop log."""
    # Team tags on the item are only used to find the slate game.
    if not (item.get("teams") or []):
        return {"_drop_reason": "no_teams"}
    if not (item.get("sport_key") or "").strip():
        return {"_drop_reason": "no_league"}
    # Matched slate game — the source of truth for teams, logos, start time,
    # status and market lines. Rendering from the item's own tags is what
    # produced "DUKE at DUKE" chips next to another game's lines.
    matched_ev, matched_meta = _match_item_to_slate(item, merged)
    if matched_ev is None:
        return {"_drop_reason": "no_slate_match_or_finished"}
    away = matched_ev.get("away") or {}
    home = matched_ev.get("home") or {}
    away_abbr = (away.get("abbr") or "").upper()
    home_abbr = (home.get("abbr") or "").upper()
    if not away_abbr or not home_abbr or away_abbr == home_abbr:
        return {"_drop_reason": "bad_game_teams"}
    game_teams = [
        {"abbr": away_abbr, "name": away.get("name") or "", "logo": (away.get("logo") or "").strip()},
        {"abbr": home_abbr, "name": home.get("name") or "", "logo": (home.get("logo") or "").strip()},
    ]
    start_ct = _ct_time_label(matched_ev)
    if not start_ct:
        return {"_drop_reason": "no_start_time"}
    # Image: athlete headshot first, else the matched game's two ESPN logos
    # (scoreboard logo URLs are real for every league, college included).
    headshot = ((item.get("athlete") or {}).get("headshot") or "").strip()
    if headshot:
        image_kind, image_data = "portrait", {"src": headshot}
    elif game_teams[0]["logo"] and game_teams[1]["logo"]:
        image_kind, image_data = "duo", {
            "l1": game_teams[0]["logo"], "l2": game_teams[1]["logo"],
            "a1": away_abbr, "a2": home_abbr,
        }
    else:
        image_kind, image_data = _resolve_tile_image(item)
        if image_kind is None:
            return {"_drop_reason": "no_image"}
    # Featured needs the full 4-cell matrix; mid/wide need market-tail numbers.
    matrix_html = ""
    market_tail = ""
    if size == "featured":
        matrix_html = _build_featured_matrix(item, merged)
        if not matrix_html:
            return {"_drop_reason": "no_matrix_data"}
    else:
        market_tail = _market_tail_for(matched_ev, matched_meta)
        if not market_tail:
            return {"_drop_reason": "no_market_tail"}
    return {
        "image_kind": image_kind,
        "image_data": image_data,
        "matched_ev": matched_ev,
        "matched_meta": matched_meta,
        "teams": game_teams,
        "sport_key": (matched_ev.get("sport") or matched_ev.get("sport_key") or item.get("sport_key") or ""),
        "start_ct": start_ct,
        "matrix_html": matrix_html,
        "market_tail": market_tail,
    }


def _render_news_tile(item: dict, size: str = "mid", validated: dict = None) -> str:
    """Return the HTML for one bento tile. `size` ∈ {featured, mid, wide}.
    `validated` is the dict returned by _validate_tile() and MUST be passed —
    callers get it from _validate_tile and skip the render on empty dicts.
    No tile ever reaches this function missing an image, teams, league,
    start-time, or (for featured) the full matrix data."""
    v = validated or {}
    sev = (item.get("severity") or "news").lower()
    rel = _relative_time(item.get("published", ""))
    headline = _esc(item.get("headline", ""))
    raw_snippet = item.get("snippet", "") or ""
    snip_limit = 260 if size == "featured" else 140
    snippet = _esc(_trunc_words(raw_snippet, snip_limit))
    # Teams and league come from the matched slate game (validator), never
    # from the story's own tags, so chips, colors and lines describe one game.
    teams = v.get("teams") or item.get("teams") or []
    league_key = (v.get("sport_key") or item.get("sport_key") or "").lower()
    # Weather / line-move tiles get the mockup's exact palette. Team-colored
    # tiles derive c1/c2 from the primary team.
    if sev == "weather":
        c1, c2, team_color = ("#001a3a", "#00265f", "#5AB7FF")
    elif sev == "line":
        c1, c2, team_color = ("#041E42", "#021022", "#10E0A1")
    else:
        c1, c2, team_color = _team_color_pair(teams, league_key)

    # Image — pre-resolved by _validate_tile. Fallback league-logo substitute
    # fires via onerror if the CDN hiccups on a cold cache.
    image_kind = v.get("image_kind")
    image_data = v.get("image_data") or {}
    league_slug = _LEAGUE_LOGO_SLUG.get(league_key, "")
    fallback_logo = _ESPN_LEAGUE_LOGO.format(slug=league_slug) if league_slug else ""
    _onerr = f"this.onerror=null;this.src='{fallback_logo}';" if fallback_logo else ""
    portrait_html = ""
    logos_html = ""
    if image_kind == "portrait":
        # Portraits set background-image, so onerror can't swap. Pre-trusted
        # ESPN-headshot templates are stable; the duo-fallback path handles the
        # rare case where the CDN serves a 404.
        portrait_html = f'<div class="portrait" style="background-image:url(\'{_esc(image_data["src"])}\');"></div>'
    elif image_kind == "duo":
        logos_html = (
            f'<div class="team-logos">'
            f'<img src="{_esc(image_data["l1"])}" alt="{_esc(image_data["a1"])}" onerror="{_onerr}">'
            f'<span class="vs">vs</span>'
            f'<img src="{_esc(image_data["l2"])}" alt="{_esc(image_data["a2"])}" onerror="{_onerr}">'
            f'</div>'
        )
    elif image_kind == "solo":
        logos_html = (
            f'<div class="team-logos">'
            f'<img src="{_esc(image_data["l1"])}" alt="{_esc(image_data["a1"])}" onerror="{_onerr}">'
            f'</div>'
        )
    elif image_kind == "league":
        logos_html = (
            f'<div class="team-logos league">'
            f'<img src="{_esc(image_data["src"])}" alt="{_esc(image_data["alt"])}">'
            f'</div>'
        )

    # Build team chip HTML honoring per-team colors + light-text flip. Two chips
    # with an italic "at"/"vs" joiner, trailing meta line with league + CT time.
    def _chip_for(t: dict) -> str:
        abbr = (t.get("abbr") or "").upper()
        if not abbr:
            return ""
        col = _resolve_team_color(abbr, league_key)
        extra = " light" if abbr in _LIGHT_TEAM_CHIPS else ""
        return f'<span class="team-chip{extra}" style="--team-color:{col}">{_esc(abbr)}</span>'
    _chip_a = _chip_for(teams[0]) if len(teams) >= 1 else ""
    _chip_b = _chip_for(teams[1]) if len(teams) >= 2 else ""
    _at_or_vs = "at" if len(teams) >= 2 else ""
    # League label: "LA LIGA" / "NBA" / "NCAAB", never raw enum keys like LA_LIGA.
    _league_display = (espn.DISPLAY_NAMES.get(league_key) or league_key.upper().replace("_", " ")).strip()
    # Validator guarantees a real CT start time from the matched slate game
    # (spec §4a). Market-tail (spec §4) carries live spread/total numbers on
    # mid/wide tiles so the foot line is never information-free.
    _start_ct = (v.get("start_ct") or "").strip()
    _source = (item.get("source") or "ESPN").strip()
    _market_tail = (v.get("market_tail") or "").strip() if size != "featured" else ""
    _meta_bits = [f"· {_league_display}" if _league_display else ""]
    _meta_bits.append(f"· {_esc(_start_ct)}")
    _meta_bits.append(f"· {_esc(_source)}")
    if _market_tail:
        _meta_bits.append(_esc(_market_tail))
    meta_tail = f'<span style="color:#9fb0ce">{" ".join(b for b in _meta_bits if b)}</span>'
    # team-line used both in featured text-col and mid/wide foot. Composed once
    # so both placements stay in sync.
    if _chip_a and _chip_b:
        team_line_html = f'<div class="team-line">{_chip_a}<span>{_at_or_vs}</span>{_chip_b}{meta_tail}</div>'
    elif _chip_a:
        team_line_html = f'<div class="team-line">{_chip_a}{meta_tail}</div>'
    else:
        team_line_html = f'<div class="team-line">{meta_tail}</div>'

    # Footer content per spec §6.
    #  - Featured: source + author + optional EDGE line pulled from board picks.
    #  - Mid/Wide: team chips + league/time/source (reuses team_line).
    #  - Line-move tile: no author line, show chips in foot anyway.
    author = (item.get("author") or "").strip()
    if size == "featured":
        edge_line = item.get("edge_foot_html") or ""  # caller may stuff this in
        source_span = f'<span>▸ {_esc(_source)}{" · " + _esc(author) if author else ""}</span>'
        foot_html = source_span + (f'<span class="sep">·</span>{edge_line}' if edge_line else "")
    else:
        # team-line already carries league + time + source, so just embed it in .foot.
        foot_html = team_line_html

    # Build body. For featured tiles the team-line lives in .text-col above the
    # 4-cell matrix; for mid/wide tiles the team-line lives in .foot only.
    text_col_inner = f'<div class="h3">{headline}</div>'
    if snippet:
        text_col_inner += f'<div class="snippet">{snippet}</div>'
    if size == "featured":
        text_col_inner += team_line_html
        _mx = v.get("matrix_html") or ""
        if _mx:
            text_col_inner += _mx

    return (
        f'<div class="news-tile {size}" style="--c1:{c1}; --c2:{c2};">'
        f'{portrait_html}{logos_html}'
        f'<div class="scrim-left"></div>'
        f'<div class="scrim-bottom"></div>'
        f'<div class="accent-bar" style="--team-color:{team_color};"></div>'
        f'<div class="body">'
        f'<div class="head">'
        f'<span class="sev {sev}">{_sev_label_for(sev, size)}</span>'
        f'<span class="ts">{_esc(rel) or "—"}</span>'
        f'</div>'
        f'<div class="text-col">{text_col_inner}</div>'
        f'<div class="foot">{foot_html}</div>'
        f'</div>'
        f'</div>'
    )


def _render_line_moves_ticker(merged: list, top_picks: list = None) -> str:
    """Spec §9: ticker of REAL line moves pulled from the slate's spreads/totals.
    Each item is "AWAY–HOME open → current" with dn/up/pm classes based on
    the move's direction. Falls back to the 8 biggest-edge games tagged 'pm' if
    nothing on the slate moved yet.
    Returns "" (no ticker) when there's literally nothing to show."""
    moves: list = []
    for ev, odds_meta, _exch in (merged or []):
        om = odds_meta or {}
        away = (ev.get("away") or {}).get("abbr") or ""
        home = (ev.get("home") or {}).get("abbr") or ""
        if not (away and home):
            continue
        matchup = f"{away.upper()}–{home.upper()}"
        # Prefer spreads move (home side), fall back to totals (over side), else moneyline.
        spr = om.get("spreads") or {}
        tot = om.get("totals") or {}
        h2h = om.get("h2h") or {}
        open_v = cur_v = None
        tail = ""
        # Spread and total moves are measured in POINTS only; a price opener
        # must never be paired with a point (that printed "-175 → -1.94").
        if spr.get("home_point_opener") is not None and spr.get("home_point") is not None:
            try:
                open_v = float(spr.get("home_point_opener"))
                cur_v = float(spr.get("home_point"))
                tail = ""
            except (TypeError, ValueError):
                open_v = cur_v = None
        if open_v is None and tot.get("home_point_opener") is not None and tot.get("home_point") is not None:
            try:
                open_v = float(tot.get("home_point_opener"))
                cur_v = float(tot.get("home_point"))
                tail = " O/U"
            except (TypeError, ValueError):
                open_v = cur_v = None
        if open_v is None and h2h.get("home_opener") is not None and h2h.get("home_price") is not None:
            try:
                open_v = float(h2h.get("home_opener"))
                cur_v = float(h2h.get("home_price"))
                tail = ""
            except (TypeError, ValueError):
                open_v = cur_v = None
        if open_v is None or cur_v is None:
            continue
        delta = cur_v - open_v
        cls = "pm" if abs(delta) < 1e-6 else ("up" if delta > 0 else "dn")
        moves.append({
            "matchup": matchup + tail,
            "open": open_v,
            "cur": cur_v,
            "delta_abs": abs(delta),
            "cls": cls,
        })

    def _fmt(v: float) -> str:
        return f"{v:g}" if isinstance(v, float) and v == int(v) else f"{v:+g}" if v and v > 0 else f"{v:g}"

    # Keep only real moves, sorted by magnitude desc.
    moved = sorted([m for m in moves if m["cls"] != "pm"], key=lambda m: -m["delta_abs"])
    items_src = moved[:18]
    if not items_src:
        # Spec fallback: 8 biggest-edge games with pm class.
        picks = top_picks or []
        for e in picks[:8]:
            mp = (e.get("matchup") or "").replace(" at ", "–").replace(" vs ", "–")
            if "–" in mp:
                a, b = mp.split("–", 1)
                mp = f"{a.strip().split()[-1][:4]}–{b.strip().split()[-1][:4]}".upper()
            else:
                mp = mp[:10].upper()
            items_src.append({"matchup": mp, "open": None, "cur": None, "cls": "pm", "delta_abs": 0})
    if not items_src:
        # Cold load: no moves yet and no picks from a previous scan. Show the
        # current spread/total of up to 8 slate games as unchanged (pm) items
        # so the strip still carries real numbers instead of disappearing.
        for m in moves[:8]:
            items_src.append({**m, "open": m["cur"], "cls": "pm"})

    if not items_src:
        return ""
    items_html = []
    for m in items_src:
        if m["open"] is not None and m["cur"] is not None:
            _signed = abs(m["cur"]) < 15 and not m["matchup"].endswith("O/U")
            open_s = f"{m['open']:+g}" if _signed else f"{m['open']:g}"
            cur_s = f"{m['cur']:+g}" if _signed else f"{m['cur']:g}"
            # Unchanged lines read as one number, not "10 → 10".
            move_str = cur_s if m["cls"] == "pm" else f"{open_s} → {cur_s}"
        else:
            move_str = "on slate"
        items_html.append(
            f'<span class="item">'
            f'<span class="matchup">{_esc(m["matchup"])}</span> '
            f'<span class="{m["cls"]}">{_esc(move_str)}</span>'
            f'</span>'
        )
    # Exactly 2 copies for the CSS translateX(-50%) loop.
    run = "".join(items_html + items_html)
    return (
        '<div class="news-tile ticker">'
        '<span class="ticker-label"><span class="bolt">⚡</span> Live Line Moves</span>'
        f'<div class="ticker-track"><div class="ticker-run">{run}</div></div>'
        '</div>'
    )


def _teams_in_slate(merged: list) -> set:
    """Extract every team name/abbr involved in today's slate so we can filter
    the ESPN news feed to items that touch at least one of them."""
    out = set()
    for ev, _odds, _exch in (merged or []):
        for side in ("home", "away"):
            info = ev.get(side) or {}
            for key in ("name", "abbr", "shortDisplayName", "displayName"):
                v = info.get(key)
                if v:
                    out.add(str(v).strip().lower())
    return out


def _news_is_relevant(item: dict, slate_terms: set) -> bool:
    """True when the news headline/snippet mentions any team on tonight's slate,
    OR the news item itself carries a team.abbr that's on the slate."""
    # 1. Team metadata on the item
    for t in item.get("teams") or []:
        for key in ("abbr", "name"):
            v = (t.get(key) or "").strip().lower()
            if v and v in slate_terms:
                return True
    # 2. Term match in the headline (cheap and effective — team names are the
    # highest-signal tokens in sports headlines).
    text = f"{item.get('headline') or ''} {item.get('snippet') or ''}".lower()
    for term in slate_terms:
        # Require at least 3 chars to avoid matching generic 2-letter abbrs
        # in words like "at" or "vs".
        if len(term) >= 3 and term in text:
            return True
    return False


def _slate_relevance_score(item: dict, slate_terms: set, slate_sports: set) -> int:
    """Spec §2: higher = more relevant to today's slate.
      3 = team metadata on the item names a team on the slate
      2 = headline/snippet mentions a slate team name (≥3 chars)
      1 = the item's sport_key is one of the slate sports
      0 = otherwise (sorted to the back; usually dropped by relevance filter)"""
    # Direct team metadata match — strongest signal. A story tagged with both
    # teams of a slate game (a true preview) outranks a single-team mention.
    hits = 0
    for t in item.get("teams") or []:
        if any((t.get(k) or "").strip().lower() in slate_terms for k in ("abbr", "name") if t.get(k)):
            hits += 1
    if hits >= 2:
        return 4
    if hits == 1:
        return 3
    text = f"{item.get('headline') or ''} {item.get('snippet') or ''}".lower()
    for term in slate_terms:
        if len(term) >= 3 and term in text:
            return 2
    if (item.get("sport_key") or "").lower() in slate_sports:
        return 1
    return 0


def _render_breaking_news(sports_list: list, merged: list):
    """The Breaking News hero: 1 featured + up to 5 smaller tiles + ticker.

    Spec §1-4a pipeline:
      1. Fetch ESPN news for the current sports list.
      2. Score every item by slate relevance (team > league > neither).
      3. Build a ranked queue sorted by (relevance desc, severity, recency).
      4. Walk the queue; validate each candidate with _validate_tile. Dropped
         items (image missing / finished game / matrix data missing / etc.)
         get replaced from the queue so the hero always paints with 6
         complete tiles OR runs the queue dry with a visible empty-state.
    """
    import sys  # for drop-reason logging
    raw_news = load_news_for_sports(tuple(sports_list), per_sport=10) or []
    slate_terms = _teams_in_slate(merged)
    slate_sports = {(sp or "").lower() for sp in (sports_list or [])}
    # Dedupe by headline first — ESPN sometimes returns the same story across
    # multiple league endpoints (shared soccer feeds, cross-posted wires).
    _seen_titles = set()
    deduped = []
    for n in raw_news:
        key = (n.get("headline") or "").strip().lower()
        if key and key not in _seen_titles:
            _seen_titles.add(key)
            deduped.append(n)
    # Spec §2: relevance score drives ordering, severity breaks ties, recency
    # last. priority inversely ranks so lower = earlier.
    priority = {"out": 0, "gtd": 1, "weather": 2, "line": 3, "news": 4}
    ranked = sorted(
        deduped,
        key=lambda x: (
            -_slate_relevance_score(x, slate_terms, slate_sports),
            priority.get((x.get("severity") or "news"), 5),
            -(len(x.get("published") or "")),
        ),
    )
    # Spec §1+3+4a: validator + replace loop. For each slot, walk the ranked
    # queue skipping items already used. An item can fail for featured (needs
    # matrix) but pass for mid/wide (needs market-tail only), so we re-scan
    # the queue per slot instead of using a one-shot iterator.
    slot_sizes = ["featured", "mid", "mid", "wide", "wide", "wide"]
    accepted: list = []
    used_ids: set = set()
    drop_log: list = []
    for size in slot_sizes:
        picked = None
        picked_bundle = None
        for idx, item in enumerate(ranked):
            if idx in used_ids:
                continue
            v = _validate_tile(item, merged, size)
            if v and not v.get("_drop_reason"):
                picked = (idx, item)
                picked_bundle = v
                break
            if v and v.get("_drop_reason"):
                drop_log.append((size, v.get("_drop_reason"), (item.get("headline") or "")[:60]))
        if picked is None:
            # Featured can fail (needs matrix) while mid/wide pass (market-tail
            # only), so don't stop — continue scanning other slots.
            continue
        used_ids.add(picked[0])
        accepted.append((size, picked[1], picked_bundle))
    # Server-side drop log (stderr; Streamlit Cloud captures it).
    if drop_log:
        try:
            print(f"[breaking-news] dropped {len(drop_log)} candidate(s): "
                  + "; ".join(f"[{s}]{r}:'{h}'" for s, r, h in drop_log[:8]),
                  file=sys.stderr)
        except Exception:
            pass
    # Filter chips — All / Injuries / Weather / Line Moves + up to 2 league chips
    # sourced from the current board filter so they reflect what's on screen.
    league_chips = "".join(
        f'<span class="chip-filter">{(_esc(espn.DISPLAY_NAMES.get(sp, sp.upper()))).split(" ")[0]}</span>'
        for sp in (sports_list or [])[:2]
    )
    tile_cap = len(accepted)
    html(
        '<div class="news-head">'
        '<div class="news-title">'
        "<div class=\"h1\">Breaking · Today's Slate</div>"
        f'<span class="count-pill"><span class="cdot"></span>{tile_cap} live updates</span>'
        '</div>'
        '<div class="news-filters">'
        '<span class="chip-filter active">All</span>'
        '<span class="chip-filter">Injuries</span>'
        '<span class="chip-filter">Weather</span>'
        '<span class="chip-filter">Line Moves</span>'
        f'{league_chips}'
        '</div>'
        '</div>'
    )
    if not accepted:
        html(
            '<div class="news-empty">No breaking news from ESPN matches tonight\'s slate right now. '
            'The feed refreshes every 5 minutes, and tiles rotate out once a matched game finishes.</div>'
        )
        return
    tiles = [_render_news_tile(item, size=size, validated=v) for size, item, v in accepted]
    # Ticker pulls the previous render's top_picks from session_state so each
    # rerun shows real agent signals (first page load = matchup fallback until
    # the agent runs, every subsequent load = real edge arrows).
    _prev_top = st.session_state.get("_board_top_picks") or []
    tiles.append(_render_line_moves_ticker(merged, top_picks=_prev_top))
    html(f'<section class="bento">{"".join(tiles)}</section>')


def _render_board_filters_widget(all_available_sports: list, saved_board_sports: list,
                                   saved_board_markets: list, all_markets_opts: list) -> tuple:
    """Render the Sports + Markets multiselects IN THE SIDEBAR (spec §12).
    Returns the current (sports, markets) selections for the caller to persist.

    Default-all is intentional for both multiselects so a fresh visit shows the
    widest slate without hunting for toggles."""
    with st.sidebar:
        st.markdown("#### Board filters")
        board_sports = st.multiselect(
            "Sports",
            options=all_available_sports,
            default=[s for s in saved_board_sports if s in all_available_sports] or list(all_available_sports),
            format_func=lambda k: espn.DISPLAY_NAMES.get(k, k.upper()),
            key="board_sports_filter",
            placeholder="All sports",
            help="Which leagues the agent scans AND which sports feed the news hero. "
                 "Untick any to narrow the board and trim your Odds-API credit burn.",
        )
        board_markets = st.multiselect(
            "Markets",
            options=all_markets_opts,
            default=[m for m in saved_board_markets if m in all_markets_opts] or list(all_markets_opts),
            format_func=lambda m: {"h2h": "Moneyline", "spreads": "Spread", "totals": "Total"}.get(m, m),
            key="board_markets_filter",
            placeholder="All markets",
            help="Which bet markets the agent considers.",
        )
    return (list(board_sports), list(board_markets))


def _render_snapshot_strip(bankroll_display: str, bankroll_source: str, unit_pct: float,
                            slate_count: int, qual_count: int, exposure_usd: float, exposure_pct: float):
    # Spec §14: adds a "Snapshot" section kicker above the strip. The strip
    # itself loses its top margin because the kicker carries the spacing.
    html(
        '<div class="section-title">Snapshot</div>'
        '<div class="snap-strip">'
        f'<div class="cell"><span class="k">Bankroll</span><span class="v accent">{_esc(bankroll_display)}</span></div>'
        f'<div class="cell"><span class="k">Slate</span><span class="v">{slate_count} games</span></div>'
        f'<div class="cell"><span class="k">Qualifying picks</span><span class="v good">{qual_count}</span></div>'
        f'<div class="cell"><span class="k">Daily exposure</span><span class="v">${exposure_usd:,.0f} / {exposure_pct:.1f}%</span></div>'
        '</div>'
    )


# ============================================================================
# TAB 1 - Dashboard / Live Daily Slate
# ============================================================================

def render_dashboard(settings: dict):
    brand_bar("Live Daily Slate")

    # Live-score auto-refresh. We rerun the whole dashboard on an interval
    # so ESPN score fetches (20s TTL) get a chance to pick up new goals /
    # final whistles / live clocks without the user hitting refresh.
    # Odds API responses stay cached for the full live_ttl (1h default), so
    # this costs 0 Odds-API credits — only ESPN takes the hit, and ESPN is free.
    try:
        from streamlit_autorefresh import st_autorefresh  # type: ignore
    except ImportError:
        st_autorefresh = None  # dep not installed yet; fallback is manual refresh
    _saved_cfg_tmp = settings_store.load()
    _refresh_sec = int(_saved_cfg_tmp.get("live_refresh_sec", 30) or 0)
    _refresh_running = False

    # ===== Board filters: resolve early so the news hero + board use the same
    # selection. The actual Sports + Markets widgets render just above the
    # agent board (below, after the news hero + date picker) via
    # _render_board_filters_widget() — that keeps the top of the page clean. =====
    saved_cfg_board = settings_store.load()
    all_available_sports = list(dict.fromkeys(
        s for _g, group_list in espn.SPORT_GROUPS
        for s in group_list
    ))
    # Spec: default filter = ALL sports and ALL markets on a fresh visit so
    # users see the widest slate before narrowing. Saved selections still win.
    saved_board_sports = saved_cfg_board.get("board_sports") or list(all_available_sports)
    saved_board_markets = saved_cfg_board.get("board_markets") or ["h2h", "spreads", "totals"]

    # Prefer a live widget value (set earlier this session) over the saved one
    # so the news hero sees the CURRENT selection on rerun.
    board_sports = st.session_state.get("board_sports_filter") or [
        s for s in saved_board_sports if s in all_available_sports
    ] or list(all_available_sports)
    all_markets_opts = ["h2h", "spreads", "totals"]
    board_markets = st.session_state.get("board_markets_filter") or [
        m for m in saved_board_markets if m in all_markets_opts
    ] or all_markets_opts

    # Share with sidebar + Analysis tab via session_state.
    st.session_state["board_sports"] = list(board_sports)
    st.session_state["board_markets"] = list(board_markets)

    # Resolve downstream vars from the filter choices.
    sport_setting = list(board_sports)
    sports_list = sport_setting
    multi_sport_mode = len(sports_list) > 1
    unit_pct = settings.get("unit_pct") or 0.01
    bankroll_units = settings.get("bankroll_units") or 100.0
    kalshi_cash = settings.get("kalshi_cash") or 0.0

    # Spec §12: date picker + filters moved into the sidebar so nothing competes
    # with the Breaking News hero + ticker for the top-of-page real estate.
    from datetime import date, timedelta
    from zoneinfo import ZoneInfo
    central_today = datetime.now(ZoneInfo("America/Chicago")).date()
    with st.sidebar:
        st.markdown("#### Board date")
        dc1, dc2, dc3 = st.columns([1, 2, 1])
        with dc1:
            if st.button("←", key="sb_prev_date", use_container_width=True):
                cur = st.session_state.get("dash_date") or central_today
                st.session_state["dash_date"] = cur - timedelta(days=1)
                st.rerun()
        with dc2:
            picked_date = st.date_input(
                "Date", value=st.session_state.get("dash_date") or central_today,
                min_value=central_today - timedelta(days=180),
                max_value=central_today + timedelta(days=30),
                key="dash_date", label_visibility="collapsed",
            )
        with dc3:
            if st.button("→", key="sb_next_date", use_container_width=True):
                cur = st.session_state.get("dash_date") or central_today
                st.session_state["dash_date"] = cur + timedelta(days=1)
                st.rerun()
        if picked_date != central_today:
            if st.button("Today", key="go_today_sb", use_container_width=True):
                st.session_state["dash_date"] = central_today
                st.rerun()

    is_today = picked_date == central_today
    date_str_api = picked_date.strftime("%Y%m%d")
    date_str_pretty = picked_date.strftime("%A, %b %d, %Y")

    # fetch (one or many sports) — credit-conscious defaults.
    saved_cfg = settings_store.load()
    regions = saved_cfg.get("odds_regions") or "eu"
    markets_cfg = saved_cfg.get("odds_markets") or "h2h"
    live_ttl = int(saved_cfg.get("odds_live_ttl") or 3600)
    # Alt markets opt-in: comma-separated like "totals_h1,spreads_h1". When
    # non-empty, per-event endpoint runs for each sport in odds_alt_sports
    # (or every sport if that list is empty — be careful on credits).
    alt_markets_cfg = (saved_cfg.get("odds_alt_markets") or "").strip()
    alt_sports_allow = {str(x).strip().lower() for x in (saved_cfg.get("odds_alt_sports") or []) if str(x).strip()}
    alt_ttl = int(saved_cfg.get("odds_alt_ttl") or 6 * 3600)

    # Per-sport fetches, aggregated. CREDIT GUARD: only hit the Odds API
    # for sports that actually have ESPN games on the selected date.
    all_events_tagged: List[tuple] = []     # list of (sport_key, event)
    all_odds_games_tagged: List[tuple] = [] # list of (sport_key, odds_game)
    all_merged = []                          # list of (ev, odds_meta, exch_probs)
    for sp in sports_list:
        events_sp = load_espn_scoreboard(sp, date_str=date_str_api)
        for ev in events_sp or []:
            all_events_tagged.append((sp, ev))
        # Skip Odds API fetch entirely when ESPN shows no games on this date for this sport.
        has_games = bool(events_sp)
        odds_games_sp = load_odds(sp, regions=regions, markets=markets_cfg, cache_ttl=live_ttl) if (is_today and has_games) else []
        # Per-event alt-market enrichment: fetch 1H / BTTS lines for THIS sport's
        # events and merge them into the bookmakers list so the agent scanner
        # finds those edges without changing its interface. Gated by:
        #  - alt_markets_cfg non-empty (user opted in via Settings)
        #  - sport in allowlist (or allowlist empty = all applicable sports)
        #  - at least one game returned from the batch endpoint (else nothing
        #    to enrich, and no sense wasting credits)
        if alt_markets_cfg and odds_games_sp and is_today and (not alt_sports_allow or sp in alt_sports_allow):
            eids = tuple(og.get("id") for og in odds_games_sp if og.get("id"))
            if eids:
                try:
                    alt_data = load_alt_markets(sp, eids, regions, alt_markets_cfg, cache_ttl=alt_ttl)
                except Exception:
                    alt_data = {}
                if alt_data:
                    odds_api.merge_alt_markets_into_games(odds_games_sp, alt_data)
        for og in odds_games_sp or []:
            og["_sport"] = sp
            all_odds_games_tagged.append((sp, og))
        kalshi_markets_sp = load_kalshi_markets(sp) if (is_today and has_games) else []
        poly_sp = load_polymarket_markets() if sp == sports_list[0] else []  # Polymarket fetched once
        merged_sp = _merge_events_with_odds(
            events_sp, odds_games_sp,
            kalshi_markets=kalshi_markets_sp,
            polymarket_markets=poly_sp,
            sport_key=sp,
        )
        for ev, om, ex in merged_sp:
            ev["sport"] = ev.get("sport") or sp
            all_merged.append((ev, om, ex))

    events = [ev for _sp, ev in all_events_tagged]
    odds_games = [og for _sp, og in all_odds_games_tagged]
    merged = all_merged

    # summary metrics
    picks = picks_store.list_picks()
    s = picks_store.summarize(picks)
    live_count = sum(1 for ev, _, _ in merged if ev.get("is_live"))
    final_count = sum(1 for ev, _, _ in merged if ev.get("is_final"))
    upcoming = sum(1 for ev, _, _ in merged if ev.get("status_state") == "pre")
    api_key_ok = odds_api.get_api_key() is not None

    # Kick on the live auto-refresh now that we know how many games are
    # in-progress. We only poll while at least one game is live — once the
    # slate is all pre-game or all final there's nothing moving to refresh.
    if (
        st_autorefresh is not None
        and _refresh_sec > 0
        and live_count > 0
        and is_today
    ):
        st_autorefresh(
            interval=_refresh_sec * 1000,
            key="dash_live_refresh",
            # None = run forever while mounted.
            limit=None,
        )
        _refresh_running = True

    net_tone = "good" if (s["net_units"] or 0) > 0 else ("bad" if (s["net_units"] or 0) < 0 else "")
    roi_display = f"{(s['roi'] or 0)*100:+.1f}%" if s["roi"] is not None else "—"
    kalshi_source = settings.get("kalshi_source")
    kalshi_display = f"${kalshi_cash:,.2f}" if kalshi_cash else "—"

    # Sparkline: tiny bar of per-sport game counts.
    if multi_sport_mode:
        per_sport_counts = {}
        for ev, _om, _ex in merged:
            sp = ev.get("sport") or "?"
            per_sport_counts[sp] = per_sport_counts.get(sp, 0) + 1
        top3 = sorted(per_sport_counts.items(), key=lambda kv: -kv[1])[:3]
        spark = "  ".join(f"{k.upper()}·{v}" for k, v in top3)
        slate_foot = f"{live_count} live · {final_count} final · {spark}"
    else:
        slate_foot = f"{live_count} live · {final_count} final"
    slate_label = f"{len(sports_list)} sports" if multi_sport_mode else sport_setting.upper()
    # Hero: Breaking News & Injuries bento (scoped to today's sports), fed by
    # ESPN's news feed. Replaces the two metric cards (bankroll + slate) that
    # used to live here; those move to a compact strip at the bottom.
    _render_breaking_news(sports_list, merged)

    # First-timer welcome: if no API key and no picks yet, surface a 3-step
    # onboarding so new users know what to do before they stare at an empty board.
    has_any_picks = bool(picks)
    kalshi_connected = kalshi_source == "live"
    if not api_key_ok or (not kalshi_connected and not has_any_picks):
        step_odds = "✓" if api_key_ok else "1"
        step_kalshi = "✓" if kalshi_connected else "2"
        step_ready = "✓" if (api_key_ok and kalshi_connected) else "3"
        s_tone = lambda done: ("good" if done else "")
        html(
            '<div style="background:var(--card); border:1px solid var(--rule); '
            'border-radius:12px; padding:18px 20px; margin:8px 0 18px; '
            'box-shadow:var(--card-shadow);">'
            '<div style="display:flex; align-items:center; gap:10px; margin-bottom:12px;">'
            '<span class="chip accent" style="padding:3px 10px;">GET STARTED</span>'
            '<span style="font-family:Fraunces,serif; font-style:italic; font-size:17px;">'
            'Three steps and the agent is working for you.</span>'
            '</div>'
            '<div style="display:grid; grid-template-columns:repeat(auto-fit, minmax(220px, 1fr)); gap:12px;">'
            f'<div style="padding:12px 14px; background:var(--card-elev); border-radius:8px;">'
            f'<div class="mono {s_tone(api_key_ok)}" style="font-size:11px; letter-spacing:0.12em;'
            f' text-transform:uppercase; margin-bottom:4px;">Step {step_odds} · Odds API</div>'
            f'<div style="font-size:13px; color:var(--ink-dim);">'
            + ("Connected — live lines flowing." if api_key_ok else
               "Grab a free key at <a href='https://the-odds-api.com' target='_blank' style='color:var(--accent);'>the-odds-api.com</a>, add it in Settings → Odds API usage.")
            + '</div></div>'
            f'<div style="padding:12px 14px; background:var(--card-elev); border-radius:8px;">'
            f'<div class="mono {s_tone(kalshi_connected)}" style="font-size:11px; letter-spacing:0.12em;'
            f' text-transform:uppercase; margin-bottom:4px;">Step {step_kalshi} · Kalshi (optional)</div>'
            f'<div style="font-size:13px; color:var(--ink-dim);">'
            + ("Live cash + the agent book is set." if kalshi_connected else
               "Optional — connect your Kalshi API keys in the sidebar to size bets off real cash and get the agent's Kalshi-vs-Pinnacle picks.")
            + '</div></div>'
            f'<div style="padding:12px 14px; background:var(--card-elev); border-radius:8px;">'
            f'<div class="mono {s_tone(api_key_ok and kalshi_connected)}" style="font-size:11px; letter-spacing:0.12em;'
            f' text-transform:uppercase; margin-bottom:4px;">Step {step_ready} · Watch the board</div>'
            f'<div style="font-size:13px; color:var(--ink-dim);">'
            'The agent scans every active sport, finds Pinnacle-rated mispricings, and auto-logs every funded pick. '
            'Finished picks stay on the board as record — nothing to click.'
            '</div></div>'
            '</div></div>'
        )

    # Spec §12: date banner + live-refresh chip + credit meter moved to sidebar
    # so the ticker leads directly into the board header. A past / upcoming
    # notice still appears inline because that's genuinely action-relevant.
    if not is_today:
        if picked_date < central_today:
            html(f'<div class="hint warn" style="margin-bottom:6px;"><strong>{date_str_pretty}</strong> — past date. Showing ESPN finals only.</div>')
        else:
            html(f'<div class="hint" style="margin-bottom:6px;"><strong>{date_str_pretty}</strong> — upcoming. Live odds appear closer to game time.</div>')

    # Agent recommendations panel.
    # BUG FIX: previously we passed a flat dict across all sports to the scanner,
    # then looped sports_list and re-tagged each edge with whatever sport came first.
    # Teams like "Giants" (SF Giants MLB vs NY Giants NFL) could even collide.
    # Fix: split everything per-sport so each scan sees only that sport's data.
    kalshi_markets_by_sport: Dict[str, dict] = {}
    espn_events_by_sport: Dict[str, dict] = {}
    pinnacle_games_by_sport: Dict[str, list] = {}
    for sp in sports_list:
        if not is_today:
            continue
        espn_events_sp = load_espn_scoreboard(sp, date_str=date_str_api) or []
        espn_by_game = {}
        for ev in espn_events_sp:
            home_name = (ev.get("home") or {}).get("name")
            away_name = (ev.get("away") or {}).get("name")
            if home_name and away_name:
                espn_by_game[(home_name, away_name)] = ev
        espn_events_by_sport[sp] = espn_by_game

        all_markets_sp = load_kalshi_markets(sp)
        kalshi_by_game = {}
        for ev in espn_events_sp:
            home_name = (ev.get("home") or {}).get("name")
            away_name = (ev.get("away") or {}).get("name")
            home_abbr = (ev.get("home") or {}).get("abbr") or ""
            away_abbr = (ev.get("away") or {}).get("abbr") or ""
            if not all_markets_sp:
                continue
            event_markets = find_event_markets(
                all_markets_sp, ev.get("start_utc"),
                home_abbr, away_abbr, home_name, away_name,
            )
            if event_markets:
                kalshi_by_game[(home_name, away_name)] = event_markets
        kalshi_markets_by_sport[sp] = kalshi_by_game

        # Partition the already-fetched pinnacle games by their sport tag,
        # AND filter to games commencing today in Central time (the Odds API
        # returns all upcoming games, we want today's slate only).
        from zoneinfo import ZoneInfo as _ZI
        _CT = _ZI("America/Chicago")
        _today_c = picked_date
        def _is_today(og):
            commence = og.get("commence_time")
            if not commence:
                return True  # keep when the API omits a time
            try:
                dt = datetime.fromisoformat(commence.replace("Z", "+00:00"))
                return dt.astimezone(_CT).date() == _today_c
            except (ValueError, AttributeError):
                return True
        pinnacle_games_by_sport[sp] = [
            og for og in odds_games
            if og.get("_sport") == sp and _is_today(og)
        ]

    # Sports + Markets filter row — now rendered IMMEDIATELY above the agent
    # board (not at the top of the page), so the Breaking News hero gets the
    # top-of-page real estate and the filter sits next to the thing it controls.
    _new_sports, _new_markets = _render_board_filters_widget(
        all_available_sports, saved_board_sports, saved_board_markets, all_markets_opts,
    )
    # Persist widget changes (next rerun picks them up at the top of this fn).
    if _new_sports and _new_sports != list(saved_board_sports):
        settings_store.save({"board_sports": _new_sports})
    if _new_markets and _new_markets != list(saved_board_markets):
        settings_store.save({"board_markets": _new_markets})

    top_picks = _render_agent_panel(
        odds_games, kalshi_cash, settings,
        kalshi_markets_by_sport=kalshi_markets_by_sport,
        espn_events_by_sport=espn_events_by_sport,
        pinnacle_games_by_sport=pinnacle_games_by_sport,
        sports_list=sports_list,
    ) or []

    # Stash board context for the Analysis / Explore tabs. The main Board stays
    # clean — users opt into the deep dive explicitly by switching tabs. We
    # keep the whole slate (merged) and the top picks so Analysis can rebuild
    # the quant boards and the matchups grid without re-fetching data.
    st.session_state["_board_top_picks"] = top_picks
    st.session_state["_board_merged"] = merged
    st.session_state["_board_sports_list"] = sports_list
    st.session_state["_board_sport_setting"] = sport_setting
    st.session_state["_board_multi_sport_mode"] = multi_sport_mode

    # ----- Send-to-Discord button -----
    # Posts the exact board snapshot (same top-N rows the user is looking at)
    # to the configured webhook. Lives below the table so the table above
    # reflects what's about to be sent.
    if top_picks:
        sc1, sc2 = st.columns([1, 4])
        with sc1:
            if st.button("📤 Send to Discord", key="send_discord_btn",
                         use_container_width=True, type="primary",
                         help="Posts the current board snapshot with a timestamp."):
                hook = discord_api.get_webhook_url()
                if not hook:
                    st.warning("Add a Discord webhook URL in Settings → Discord integration.")
                else:
                    unit_pct = settings.get("unit_pct") or 0.01
                    unit_dollars = (settings.get("kalshi_cash") or 0) * unit_pct
                    sp_label = (
                        f"{len(sports_list)} sports"
                        if multi_sport_mode
                        else (sport_setting.upper() if isinstance(sport_setting, str)
                              else ",".join(sports_list).upper())
                    )
                    result = discord_api.send_board_snapshot(
                        top_picks,
                        unit_dollars=unit_dollars,
                        sports_label=sp_label,
                    )
                    if result.get("ok"):
                        actionable_n = sum(1 for p in top_picks if float(p.get("edge") or 0) > 0)
                        st.success(f"PNG snapshot posted ({actionable_n} actionable picks).")
                    else:
                        st.error(f"Discord post failed: {result.get('error') or 'unknown'}")
        with sc2:
            hook_set = bool(discord_api.get_webhook_url())
            chip = ("hook configured" if hook_set else
                    "no webhook set — add one in Settings → Discord integration")
            html(
                f'<div style="color:var(--muted); font-size:12px; padding-top:9px;">'
                f'Includes a Central-US timestamp, pick count, +EV marker on actionable rows. '
                f'<span class="mono" style="font-size:10px; opacity:0.8;">({chip})</span></div>'
            )

    if not merged:
        html('<div class="hint">No games on the slate for this sport right now. Try another sport in the sidebar, or check back later.</div>')
        return
    # Hint about the Analysis tab so users know where the deep dives moved.
    if top_picks:
        html(
            '<div class="hint" style="margin-top:14px;">'
            f'<strong>{len(top_picks)}</strong> funded pick(s) on the board. '
            'Switch to the <strong>Analysis</strong> tab for per-pick quant breakdowns, '
            'the probability ladder, Kelly math, and the matchups grid.'
            '</div>'
        )

    # Bottom snapshot strip: Bankroll + Slate + Qualifying picks + Daily exposure.
    # Demoted from the top of the page (replaced by the Breaking News hero);
    # still visible at a glance without competing with the news/injury feed.
    # Agent panel stores dollar stake under `stake_dollars` (our bug earlier
    # was looking for `stake`), so this now reflects actual exposure.
    try:
        total_exposure = sum(
            (float(p.get("stake_dollars") or p.get("stake") or 0.0))
            for p in (top_picks or []) if isinstance(p, dict)
        )
    except Exception:
        total_exposure = 0.0
    exposure_pct = (total_exposure / kalshi_cash * 100.0) if kalshi_cash else 0.0
    _render_snapshot_strip(
        bankroll_display=kalshi_display,
        bankroll_source=kalshi_source or "not set",
        unit_pct=unit_pct,
        slate_count=len(merged),
        qual_count=len(top_picks or []),
        exposure_usd=total_exposure,
        exposure_pct=exposure_pct,
    )


def render_analysis(settings: dict):
    """Deep-dive tab for the board: per-pick quant breakdown + quick EV + matchups grid.

    Reads board context the Dashboard already stashed in session_state so this
    tab doesn't re-fetch APIs. If the user hasn't opened the Dashboard yet we
    show a short prompt rather than running the full scan here.
    """
    brand_bar("Analysis · deep dive on today's picks")
    top_picks = st.session_state.get("_board_top_picks") or []
    merged = st.session_state.get("_board_merged") or []
    sports_list = st.session_state.get("_board_sports_list") or []
    sport_setting = st.session_state.get("_board_sport_setting") or "nfl"
    multi_sport_mode = bool(st.session_state.get("_board_multi_sport_mode"))
    if not (top_picks or merged):
        html(
            '<div class="hint">Open the <strong>Board</strong> tab first — once the '
            'agent scans today\'s slate, this tab fills with per-pick quant breakdowns '
            'and the matchups grid.</div>'
        )
        return

    kalshi_cash = settings.get("kalshi_cash") or 0.0
    unit_pct = settings.get("unit_pct") or 0.01
    bankroll_units = settings.get("bankroll_units") or 100.0

    if top_picks:
        _render_analysis_boards(top_picks, settings, kalshi_cash)
    if merged:
        html('<h3 style="margin-top:24px;">Quick-glance EV scanner</h3>')
        _quick_label = ",".join(sports_list) if multi_sport_mode else sport_setting
        _render_quick_calculator(merged, unit_pct, bankroll_units, _quick_label)
        _render_matchups_grid(merged, sports_list, settings, unit_pct, bankroll_units, multi_sport_mode)


def _render_matchups_grid(merged, sports_list, settings, unit_pct, bankroll_units, multi_sport_mode):
    """Game card grid - per-sport sections in all-sports mode, flat grid otherwise."""
    st.markdown('### Today\'s matchups')
    primary_book = settings.get("primary_book") or "kalshi"
    extra_sources = set(settings.get("extra_sources") or [])
    visible_sources = {"kalshi"} | extra_sources

    def _render_one_card(ev, odds_meta, exch_probs):
        prob_sources = _prob_sources_for(
            odds_meta,
            kalshi_prob_home=exch_probs.get("kalshi"),
            polymarket_prob_home=exch_probs.get("polymarket"),
            betfair_prob_home=exch_probs.get("betfair"),
        )
        prob_sources_filtered = None
        if prob_sources:
            key_map = {
                "kalshi": "kalshi", "pinnacle": "pinnacle",
                "draftkings": "dk", "polymarket": "polymarket",
                "betfair": "betfair", "consensus": "consensus",
            }
            prob_sources_filtered = {
                key_map[k]: prob_sources.get(key_map[k])
                for k in visible_sources
                if prob_sources.get(key_map.get(k, k))
            } or None
        game_card(
            ev, odds_meta,
            prob_sources=prob_sources_filtered,
            primary_book=primary_book,
            kalshi_prob_home=exch_probs.get("kalshi"),
        )
        _add_pick_control(ev, odds_meta, unit_pct, bankroll_units)

    if multi_sport_mode:
        by_sport_games = {}
        for item in merged:
            sp = (item[0].get("sport") or "other")
            by_sport_games.setdefault(sp, []).append(item)
        for sp in sorted(by_sport_games.keys(), key=lambda k: -len(by_sport_games[k])):
            items = by_sport_games[sp]
            st.markdown(f"#### {espn.DISPLAY_NAMES.get(sp, sp.upper())}  ·  {len(items)} games")
            cols = st.columns(2)
            for i, (ev, odds_meta, exch_probs) in enumerate(items):
                with cols[i % 2]:
                    _render_one_card(ev, odds_meta, exch_probs)
        return

    cols = st.columns(2)
    for i, (ev, odds_meta, exch_probs) in enumerate(merged):
        with cols[i % 2]:
            _render_one_card(ev, odds_meta, exch_probs)


def _render_agent_panel(odds_games, kalshi_cash, settings,
                        kalshi_markets_by_sport=None, sports_list=None,
                        espn_events_by_sport=None, pinnacle_games_by_sport=None):
    """Agent edge-scanner — Kalshi (free) as the book, Pinnacle no-vig as fair."""
    saved_cfg = settings_store.load()
    # "The Board · Full Slate" header gets rendered AFTER the scan completes
    # so the count pill can show real numbers instead of a placeholder. The
    # header render is deferred to the _render_board_header call below.
    _total_games = sum(len(v) for v in (pinnacle_games_by_sport or {}).values())

    def _render_board_header(total_games: int, actionable: int, watch: int):
        """Spec §6: count pill shows real actionable + watch numbers directly."""
        html(
            '<div class="board-hero-head">'
            '<div class="board-title">'
            '<div class="h1">The Board · Full Slate</div>'
            f'<span class="board-count">{total_games} games · '
            f'<b>{actionable}</b> actionable · {watch} watch</span>'
            '<span class="refresh-pill"><span class="dot"></span>Refreshing</span>'
            '</div>'
            '<select class="sort-select" disabled title="Agent sorts by edge % with actionable on top">'
            '<option>Rank by: Edge % descending (recommended)</option>'
            '<option>Rank by: Stake descending</option>'
            '<option>Rank by: Tier then edge</option>'
            '<option>Rank by: Kickoff time</option>'
            '<option>Rank by: Pinnacle fair prob</option>'
            '</select>'
            '</div>'
        )
    kelly_mult = settings.get("agent_kelly") or 0.25
    # Historic agent_min_edge is the AUTO-LOG threshold (picks need to be
    # +EV before they hit the ledger). For the BOARD we show every candidate
    # ranked by fair prob, so the scanner threshold is pinned negative to
    # let -EV + flat picks through for display. Kelly naturally returns 0
    # stake on non-positive-edge picks, so stake sizing stays honest.
    min_edge_log = settings.get("agent_min_edge") or 0.01
    min_edge_scan = -1.0
    max_bet = settings.get("agent_max_bet_pct") or 0.025
    daily_cap_pct = saved_cfg.get("daily_exposure_cap_pct", 0.25)

    if not kalshi_cash or kalshi_cash <= 0:
        html(
            '<div class="hint warn">Agent is <strong>idle</strong>: configure Kalshi cash so '
            'recommendations can be sized in dollars.</div>'
        )
        return []
    if not odds_games:
        html('<div class="hint">Agent is waiting on Pinnacle odds (used as the fair-prob reference).</div>')
        return []
    kalshi_markets_by_sport = kalshi_markets_by_sport or {}
    espn_events_by_sport = espn_events_by_sport or {}
    pinnacle_games_by_sport = pinnacle_games_by_sport or {}
    # Total matched Kalshi markets across all sports.
    total_matched = sum(len(v) for v in kalshi_markets_by_sport.values())
    if total_matched == 0:
        html(
            '<div class="hint">No matching Kalshi markets found for today\'s games. The agent only '
            'picks spots where it can bet on Kalshi — nothing to show yet.</div>'
        )
        return []
    # Credit tip: us,eu is the preferred combo (gives both Pinnacle fair + offshore shop books).
    # Only warn when the user has gone wider than us,eu, which burns credits with no payoff.
    cur_regions = saved_cfg.get("odds_regions") or "us,eu"
    if cur_regions not in ("us,eu", "eu"):
        html(
            '<div class="hint warn">You are on <code>'
            f'{cur_regions}</code> regions — spending extra credits. '
            '<code>us,eu</code> (2 credits/sport) gives both Pinnacle fair AND the offshore shop books. '
            'Switch in Settings.</div>'
        )

    # Scan ALL sports (we grouped Kalshi markets by game, Pinnacle by sport).
    edges = []
    scan_fn = getattr(agent_calc, "scan_kalshi_vs_pinnacle", None)
    if not callable(scan_fn):
        html('<div class="hint err">Agent scanner missing — try reloading.</div>')
        return []
    shop_books = saved_cfg.get("shop_books") or ["Bovada", "LowVig.ag", "BetOnline.ag", "BetUS", "MyBookie.ag"]
    markets_list = [m.strip() for m in (saved_cfg.get("odds_markets") or "h2h").split(",") if m.strip()]
    # When the per-event fetcher ran, those alt markets are now in each game's
    # bookmakers list. Append them to markets_list so the scanner iterates them
    # alongside the batch-endpoint markets.
    alt_markets_cfg = (saved_cfg.get("odds_alt_markets") or "").strip()
    if alt_markets_cfg:
        for m in alt_markets_cfg.split(","):
            m = m.strip()
            if m and m not in markets_list:
                markets_list.append(m)
    scan_other_fn = getattr(agent_calc, "scan_offshore_markets_vs_pinnacle", None)
    for sp in (sports_list or []):
        sp_kalshi = kalshi_markets_by_sport.get(sp, {})
        sp_pin = pinnacle_games_by_sport.get(sp, [])
        sp_espn = espn_events_by_sport.get(sp, {})
        if not sp_pin:
            continue  # no Pinnacle data for this sport today - skip entirely
        want_h2h = "h2h" in markets_list

        # h2h via Kalshi+shop vs Pinnacle - only when Kalshi has markets for this sport.
        if want_h2h and sp_kalshi:
            try:
                sport_edges = scan_fn(
                    kalshi_markets_by_game=sp_kalshi,
                    pinnacle_games=sp_pin,
                    bankroll_cash=kalshi_cash,
                    sport_key=sp,
                    kelly_mult=kelly_mult,
                    min_edge_pct=min_edge_scan,
                    max_bet_pct=max_bet,
                    shop_books=shop_books,
                    espn_events_by_game=sp_espn,
                )
            except Exception:
                sport_edges = []
            edges.extend(sport_edges)

        # Non-h2h markets ALWAYS go through the offshore scanner.
        # When this sport has NO Kalshi (soccer / tennis / F1 / UFC / etc.),
        # ALSO add h2h to the offshore list so we don't silently skip the sport.
        other_markets = [m for m in markets_list if m != "h2h"]
        if want_h2h and not sp_kalshi:
            other_markets = ["h2h"] + other_markets
        if other_markets and callable(scan_other_fn):
            try:
                other_edges = scan_other_fn(
                    pinnacle_games=sp_pin,
                    bankroll_cash=kalshi_cash,
                    sport_key=sp,
                    markets=other_markets,
                    shop_books=shop_books,
                    kelly_mult=kelly_mult,
                    min_edge_pct=min_edge_scan,
                    max_bet_pct=max_bet,
                    espn_events_by_game=sp_espn,
                )
            except Exception:
                other_edges = []
            edges.extend(other_edges)
    # Deduplicate across sports — key by (event, market, side).
    seen = set()
    deduped = []
    for e in edges:
        key = (e.get("event_id"), e.get("market"), e.get("side_label"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(e)

    # Collapse opposing sides of the same two-way market down to one row.
    # Scanners can emit both home and away when both show positive edge
    # (bookmakers price asymmetrically), but keeping both on the board is
    # noise — in a two-way market the fair probs sum to 1, so one side is
    # always the "favored by Pinnacle" bet. Keep that side.
    #
    # Group key normalization matters here:
    #  - h2h: no point, so (event, market) alone groups the two sides.
    #  - spreads: home_pt and away_pt are inverses (e.g. -3.5 / +3.5), so
    #    we key on abs(point) so "Lakers -3.5" and "Celtics +3.5" collapse.
    #  - totals: Over and Under share the SAME point, so point goes in
    #    directly; different lines (Over 45.5 vs Over 48.5) stay distinct.
    #  - alt markets (h2h_h1, spreads_h1, totals_h1, btts): same rules per family.
    def _group_key(e):
        mk = (e.get("market") or "h2h").lower()
        pt = e.get("point")
        if mk in ("spreads", "spreads_h1") and pt is not None:
            try:
                pt_norm = abs(float(pt))
            except (TypeError, ValueError):
                pt_norm = pt
        else:
            pt_norm = pt
        return (e.get("event_id"), mk, pt_norm)
    grouped: Dict[tuple, dict] = {}
    for e in deduped:
        gk = _group_key(e)
        prev = grouped.get(gk)
        if prev is None:
            grouped[gk] = e
            continue
        # Keep the side with the higher Pinnacle fair prob (the no-vig
        # favorite). Ties fall back to the larger edge.
        prev_fp = float(prev.get("fair_prob") or 0)
        cur_fp = float(e.get("fair_prob") or 0)
        if cur_fp > prev_fp + 1e-9:
            grouped[gk] = e
        elif abs(cur_fp - prev_fp) <= 1e-9 and float(e.get("edge") or 0) > float(prev.get("edge") or 0):
            grouped[gk] = e
    # Rank the board by Pinnacle fair prob (highest first) — the "most
    # confident this side wins" ordering. Edge magnitude is the tiebreaker.
    # Rank: positive-edge picks first (actionable), then watch-list picks
    # (fair-prob only, no price edge). Within each group, highest Pinnacle
    # fair prob first, bigger edge as tiebreaker.
    def _pos_edge_tier(e):
        return 0 if float(e.get("edge") or 0) > 0 else 1
    edges = sorted(
        grouped.values(),
        key=lambda e: (_pos_edge_tier(e), -float(e.get("fair_prob") or 0), -float(e.get("edge") or 0)),
    )

    # Apply total-exposure cap.
    _cap_fn = getattr(agent_calc, "apply_daily_cap", None)
    if callable(_cap_fn):
        try:
            edges = _cap_fn(edges, kalshi_cash, daily_cap_pct)
        except Exception:
            pass
    summary = agent_calc.summarize_edges(edges)
    unit_pct = settings.get("unit_pct") or 0.01
    unit_dollars = kalshi_cash * unit_pct
    # Convert stakes to units, rounded to nearest 0.5u for display.
    def _to_units_raw(dollars):
        return (dollars / unit_dollars) if unit_dollars > 0 else 0.0
    def _to_units(dollars):
        return round(_to_units_raw(dollars) * 2) / 2
    # Snapshot strip (rendered by render_dashboard after this returns) shows
    # total exposure in dollars; per-pick unit math stays inside _row.
    # Hide picks whose Kelly recommendation rounds to 0u (too small to be actionable).
    funded_pre = [e for e in edges if (e.get("stake_dollars") or 0) > 0]
    for e in funded_pre:
        if _to_units(e.get("stake_dollars") or 0) <= 0:
            e["stake_dollars"] = 0.0
            e["rounded_to_zero"] = True
    exposure_pct = (summary['total_stake'] / kalshi_cash) * 100 if kalshi_cash else 0
    # Spec §12: the 5-card metric row that used to sit here (Picks shown / Best /
    # Total stake / Exposure / Avg edge) is removed. Those numbers are demoted
    # to the bottom .snap-strip; the mockup leads ticker → board with no
    # metric interruption in between.

    if not edges:
        html('<div class="hint">No edges meet the threshold right now. Lower the edge requirement in the Settings tab to see thinner opportunities.</div>')
        return []
    # The Board's Sports + Markets filter row (above the agent panel) now
    # drives slate selection — no in-panel filter row here.
    board_sports_sel = {s.lower() for s in (st.session_state.get("board_sports") or [])}
    board_markets_sel = {m.lower() for m in (st.session_state.get("board_markets") or [])}
    def _keep(e):
        sp = (e.get("_sport") or "").lower()
        if board_sports_sel and sp and sp not in board_sports_sel:
            return False
        mk = (e.get("market") or "h2h").lower()
        if board_markets_sel and mk not in board_markets_sel:
            return False
        # Hide heavy Pinnacle favorites that don't carry a positive edge.
        # A 76% fair prob with no edge usually translates to a -400ish
        # moneyline — not worth the risk/reward even if it hits most of
        # the time, because the vig costs eat the long-run return. Rule:
        # drop when fair_prob > 75% AND edge <= 0.
        fair = float(e.get("fair_prob") or 0)
        edge_val = float(e.get("edge") or 0)
        if fair > 0.75 and edge_val <= 0:
            return False
        return True
    filtered = [e for e in edges if _keep(e)]
    if not filtered:
        html('<div class="hint">Every edge was filtered out. Clear a filter above to see picks again.</div>')
        return []
    # Previously we hid picks that didn't qualify for a Kelly stake. User
    # explicitly asked for every candidate to show on the board ranked by
    # Pinnacle fair prob — so "funded" is now just "all picks that passed
    # the Sports/Markets filter". Picks without a positive edge (stake = 0)
    # still render; they're the "Pinnacle favorites at a bad price" watch-list.
    funded = list(filtered)
    dropped = []  # kept for compatibility with the "capped out" expander below

    # Build a status/score cell for an edge based on ESPN state.
    from datetime import datetime
    from zoneinfo import ZoneInfo
    CT = ZoneInfo("America/Chicago")

    def _status_cell(e):
        state = e.get("status_state")
        hs = e.get("home_score"); as_ = e.get("away_score")
        if state == "post":
            score = f"{as_}–{hs}" if hs is not None and as_ is not None else "final"
            return f'<span class="chip" style="padding:2px 7px; font-size:9px;">FINAL</span><br><span class="mono" style="font-size:11px;">{score}</span>'
        if state == "in":
            score = f"{as_}–{hs}" if hs is not None and as_ is not None else ""
            detail = (e.get("status_detail") or "LIVE")[:18]
            return (
                f'<span class="chip live" style="padding:2px 7px; font-size:9px;">● {detail}</span>'
                + (f'<br><span class="mono" style="font-size:11px;">{score}</span>' if score else '')
            )
        # pre-game: show start time in CT (also use Pinnacle's commence_time as a
        # fallback when the ESPN state didn't attach — happens for sports whose
        # ESPN feed we skip, or when the team-name match fails).
        start = e.get("start_utc") or e.get("commence_time")
        if start:
            try:
                if isinstance(start, str):
                    start = datetime.fromisoformat(start.replace("Z", "+00:00"))
                t = start.astimezone(CT).strftime("%I:%M %p").lstrip("0") + " CT"
                return f'<span class="mono" style="font-size:11px; color:var(--muted);">{t}</span>'
            except Exception:
                pass
        return '<span style="color:var(--muted);">—</span>'

    def _grade_cell(e):
        g = e.get("grade")
        if g == "win":    return '<span class="chip good" style="padding:2px 7px; font-size:9px;">✓ WIN</span>'
        if g == "loss":   return '<span class="chip bad"  style="padding:2px 7px; font-size:9px;">✗ LOSS</span>'
        if g == "push":   return '<span class="chip warn" style="padding:2px 7px; font-size:9px;">= PUSH</span>'
        if g == "winning":return '<span class="chip good" style="padding:2px 7px; font-size:9px;">▲ ahead</span>'
        if g == "losing": return '<span class="chip bad"  style="padding:2px 7px; font-size:9px;">▼ behind</span>'
        if g == "tied":   return '<span class="chip" style="padding:2px 7px; font-size:9px;">= tied</span>'
        return '<span style="color:var(--muted); font-size:11px;">—</span>'

    def _esc_row(s: str) -> str:
        return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def _tier_for_edge(edge_val: float) -> str:
        """Match the mockup: A ≥ 2%, B ≥ 1%, C > 0, else Watch."""
        if edge_val >= 0.020:
            return "a"
        if edge_val >= 0.010:
            return "b"
        if edge_val > 0:
            return "c"
        return "w"

    def _methodology_tag(edge_val: float, is_settled: bool) -> str:
        if is_settled:
            return '<span class="method-tag watch">Settled</span>'
        if edge_val > 0.020:
            return '<span class="method-tag line">Sharp Move</span>'
        if edge_val > 0:
            return '<span class="method-tag model">Model Edge</span>'
        return '<span class="method-tag watch">Market</span>'

    def _abbr(name: str) -> str:
        """Compress a team name into a 3-4 letter abbr: uses last capitalized
        word, truncated. Good enough for display — downstream _resolve_team_color
        will fall back to the league color on misses."""
        if not name:
            return ""
        parts = [p for p in name.strip().split() if p]
        if not parts:
            return ""
        last = parts[-1]
        return last[:4].upper() if len(last) <= 4 else last[:3].upper()

    def _matchup_chips(e: dict) -> str:
        """Spec §13: two team chips carrying team colors, with an italic Fraunces
        'at'/'vs' between. Falls back to a single chip with the raw matchup for
        edges that don't parse cleanly (international soccer, odd punctuation)."""
        sport_key = (e.get("_sport") or "").lower()
        # Prefer explicit away/home abbrs the scanner carries; fall back to
        # splitting the matchup string.
        away_abbr = (e.get("away_abbr") or "").upper()
        home_abbr = (e.get("home_abbr") or "").upper()
        m = e.get("matchup", "") or ""
        # The scanner writes "Away @ Home"; ESPN-derived strings use "at"/"vs".
        joiner = next((j for j in (" @ ", " at ", " vs ") if j in m), None)
        if joiner and not (away_abbr and home_abbr):
            away, home = m.split(joiner, 1)
            away_abbr = away_abbr or _abbr(away.strip())
            home_abbr = home_abbr or _abbr(home.strip())
        if away_abbr and home_abbr:
            def _chip(abbr: str) -> str:
                col = _resolve_team_color(abbr, sport_key)
                extra = " light" if abbr in _LIGHT_TEAM_CHIPS else ""
                return f'<span class="team-chip{extra}" style="--team-color:{col}">{_esc_row(abbr)}</span>'
            return (
                f'{_chip(away_abbr)}'
                f'<span class="at">{"vs" if joiner == " vs " else "at"}</span>'
                f'{_chip(home_abbr)}'
            )
        return f'<span class="team-chip">{_esc_row(m[:28])}</span>'

    def _start_time_ct(e: dict) -> str:
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo as _Z
        state = e.get("status_state")
        if state == "post":
            hs = e.get("home_score"); as_ = e.get("away_score")
            score = f"{as_}–{hs}" if hs is not None and as_ is not None else ""
            return f"FINAL · {score}" if score else "FINAL"
        if state == "in":
            detail = (e.get("status_detail") or "LIVE")[:18]
            hs = e.get("home_score"); as_ = e.get("away_score")
            score = f" · {as_}–{hs}" if hs is not None and as_ is not None else ""
            return f"● {detail}{score}"
        start = e.get("start_utc") or e.get("commence_time")
        if start:
            try:
                if isinstance(start, str):
                    start = _dt.fromisoformat(start.replace("Z", "+00:00"))
                return (e.get("_sport") or "").upper() + " · " + start.astimezone(_Z("America/Chicago")).strftime("%I:%M %p").lstrip("0") + " CT"
            except Exception:
                pass
        return (e.get("_sport") or "").upper() or "—"

    # Book-column mode: "kalshi" shows the Kalshi price everywhere (with a
    # shop fallback for markets Kalshi doesn't list), "best" shows whichever
    # shop book had the longest price. Chosen in the sidebar.
    board_book_mode = (settings.get("board_book_mode") or "kalshi")

    def _kalshi_cents_to_american(cents) -> str:
        """Convert a Kalshi YES price (0–100 cents) to American odds notation."""
        try:
            p = max(0.01, min(0.99, float(cents) / 100.0))
        except (TypeError, ValueError):
            return "—"
        if p >= 0.5:
            return f"-{round(100 * p / (1 - p))}"
        return f"+{round(100 * (1 - p) / p)}"

    def _book_cell_for(e: dict) -> tuple:
        """Return (book_cell_html, price_display, price_american_or_none)
        honoring the sidebar book-mode choice. Falls back to shop book when
        Kalshi has no market for this edge."""
        kcents = e.get("kalshi_price_cents")
        if board_book_mode == "kalshi" and kcents is not None:
            price_disp = _kalshi_cents_to_american(kcents)
            cell = f'<span style="color:var(--accent); font-weight:600;">Kalshi</span> <span class="n">{price_disp}</span> <span style="color:var(--muted); font-size:10.5px;">({int(kcents)}¢)</span>'
            return (cell, price_disp)
        # best-book fallback (always used in "best" mode, and in "kalshi" mode
        # whenever Kalshi doesn't have the market)
        best_book = _esc_row(e.get("book", "?"))
        best_price = format_american(e.get("price"))
        return (f'{best_book} <span class="n">{best_price}</span>', best_price)

    def _row(e, highlight: bool = False, rank: int = 0):
        edge_val = float(e.get("edge") or 0)
        edge_pct = edge_val * 100.0
        fair_pct = float(e.get("fair_prob") or 0) * 100.0
        # Spec §13: picks read "PIT -1.5" / "BOS ML", not full team names.
        raw_side = e.get("side_label", "") or ""
        for full, ab in ((e.get("home_team"), e.get("home_abbr")), (e.get("away_team"), e.get("away_abbr"))):
            if full and ab and raw_side.startswith(full):
                raw_side = ab + raw_side[len(full):]
                break
        side_label = _esc_row(raw_side)
        book_cell_html, price_disp = _book_cell_for(e)
        stake_u = _to_units(e.get("stake_dollars") or 0)
        stake_d = float(e.get("stake_dollars") or 0)
        is_settled = (e.get("status_state") == "post") or (e.get("grade") in ("win", "loss", "push"))
        tier = _tier_for_edge(edge_val) if not is_settled else "w"
        market_label = e.get("market_label") or agent_calc.MARKET_LABELS.get(e.get("market", "h2h"), "Moneyline")
        edge_cls = "good" if edge_val > 0 else ("bad" if edge_val < -0.001 else "muted")
        fair_cls = "accent" if edge_val > 0 else "muted"
        stake_classes = "u" if stake_u > 0 else "u watch"
        stake_detail = f"${stake_d:,.2f}" if stake_u > 0 else "watch"
        stake_value = f"{stake_u:.1f}u" if stake_u > 0 else "—"
        return (
            '<tr>'
            f'<td class="rank-cell">{rank or ""}</td>'
            f'<td class="tier-cell"><span class="tier-badge {tier}">{tier.upper()}</span></td>'
            f'<td class="matchup-cell">'
            f'  <div class="chip-row">{_matchup_chips(e)}</div>'
            f'  <span class="time">{_esc_row(_start_time_ct(e))}'
            f'{(" " + _grade_cell(e)) if e.get("grade") in ("winning", "losing", "tied", "win", "loss", "push") else ""}</span>'
            f'</td>'
            f'<td><div class="side-cell"><span class="mkt">{_esc_row(market_label)}</span>'
            f'<span class="pick">{side_label}</span></div></td>'
            f'<td class="book-cell">{book_cell_html}</td>'
            f'<td class="num-cell">{price_disp}</td>'
            f'<td class="num-cell {fair_cls}">{fair_pct:.1f}%</td>'
            f'<td class="num-cell {edge_cls}">{edge_pct:+.1f}%</td>'
            f'<td><div class="stake-cell"><span class="{stake_classes}">{stake_value}</span>'
            f'<span class="d">{stake_detail}</span></div></td>'
            f'<td>{_methodology_tag(edge_val, is_settled)}</td>'
            '</tr>'
        )

    # Spec §13: header reads "Best Book" regardless of mode. The agent already
    # picks the best price across Kalshi + offshore shop books, and the cell
    # labels the source inline — the header stays stable.
    _book_col_label = "Best Book"
    header = (
        '<thead><tr>'
        '<th>#</th>'
        '<th>Tier</th>'
        '<th>Matchup · Start</th>'
        '<th>Side · Market</th>'
        f'<th>{_book_col_label}</th>'
        '<th class="num">Price</th>'
        '<th class="num">Fair %</th>'
        '<th class="num">Edge</th>'
        '<th class="num">Stake</th>'
        '<th>Methodology</th>'
        '</tr></thead>'
    )

    # Reorder: fresh picks (pre-game, live) on top sorted by edge desc; final
    # picks sink to the bottom but stay visible as a record of what the agent
    # ran today. User explicitly asked for this: finished picks keep their
    # slot on the board — they just get moved below the still-actionable rows.
    def _finished(e):
        return (e.get("status_state") == "post") or (e.get("grade") in ("win", "loss", "push"))
    fresh = [e for e in funded if not _finished(e)]
    settled_rows = [e for e in funded if _finished(e)]
    # Keep each half sorted by Pinnacle fair prob (highest first), with edge
    # magnitude as the tiebreaker so same-confidence picks still favor the
    # juicier price.
    # Same ordering as the master sort: positive edge first, then by fair
    # prob descending, then edge magnitude. Keeps the "actionable rows on
    # top, watch-list below" split visible within the fresh and settled halves.
    def _rank_key(e):
        tier = 0 if float(e.get("edge") or 0) > 0 else 1
        return (tier, -float(e.get("fair_prob") or 0), -float(e.get("edge") or 0))
    fresh.sort(key=_rank_key)
    settled_rows.sort(key=_rank_key)
    funded = fresh + settled_rows

    # Spec §6: no top-N cap. The full slate is now one table with two internal
    # dividers (Actionable + Full slate). The agent_picks_shown setting is kept
    # only to drive the analysis-card count below, which is still per-pick heavy.
    top = list(funded)
    overflow_funded: list = []  # kept for compatibility with downstream references
    # Attach stable anchor ids so the analysis boards can scroll-nav back and forth.
    for i, e in enumerate(top):
        e["_anchor"] = f"pick-{i+1}"

    # Spec §7: auto-log every actionable (edge > 0, stake > 0) pick to the ledger
    # as soon as the agent scan completes. auto_log_agent_edges internally skips
    # rows with stake_dollars <= 0, so handing it `top` logs only the positive-
    # edge rows that the daily cap funded. Dedup is keyed on
    # (sport, event_id, market, selection) inside storage.picks.upsert_agent_pick,
    # so a Streamlit rerun never double-logs. The "✓ N picks logged" caption
    # below replaces the one-click expander per spec.
    inserted = 0
    if saved_cfg.get("agent_auto_log", True):
        try:
            inserted = picks_store.auto_log_agent_edges(top, settings)
        except Exception:
            inserted = 0
    # Full-ledger sweep (hits historical dates via the pick's created_at
    # hint). Cheap when nothing is open; `auto_settle` only inserts if
    # the game is final.
    try:
        settled_count = picks_store.auto_settle()
        if settled_count:
            st.toast(f"Graded {settled_count} finished pick(s) from the record.", icon="✅")
    except Exception:
        pass

    html('<div id="agent-table-top" style="position:relative; top:-40px;"></div>')
    if not top:
        # Board scans with no edge floor, so an empty top list means either no
        # games matched the Sports/Markets filter or Pinnacle didn't price any
        # games we could join.
        suggestion = (
            "No games match the current Sports / Markets filter, or Pinnacle "
            "didn't have two-way prices on anything we scanned today. Widen the "
            "filter above the board or check back closer to game time."
        )
        html(f'<div class="hint">{suggestion}</div>')
        return []
    # Court Side body: a "◆ Positive edge" divider above actionable picks,
    # a "◇ Watch" divider above non-positive ones, and a "— finished picks —"
    # divider above any settled rows. Rank numbers reset the moment we hit the
    # watch group to make the actionable list stand on its own.
    def _is_pos(e):
        return float(e.get("edge") or 0) > 0 and not ((e.get("status_state") == "post") or (e.get("grade") in ("win", "loss", "push")))
    def _is_watch(e):
        return (not _is_pos(e)) and not ((e.get("status_state") == "post") or (e.get("grade") in ("win", "loss", "push")))
    def _is_settled(e):
        return (e.get("status_state") == "post") or (e.get("grade") in ("win", "loss", "push"))

    def _render_rows(items, watch_total: int = 0):
        # Spec §6: sort each group by edge % descending, actionable on top.
        def _edge_key(e):
            return -float(e.get("edge") or 0)
        pos_items = sorted([e for e in items if _is_pos(e)], key=_edge_key)
        watch_items = sorted([e for e in items if _is_watch(e)], key=_edge_key)
        settled_items = sorted([e for e in items if _is_settled(e)], key=_edge_key)
        parts = []
        if pos_items:
            parts.append(
                f'<tr class="row-divider divider-good"><td colspan="10">'
                f'◆ Actionable picks · {len(pos_items)}'
                f'</td></tr>'
            )
            for i, e in enumerate(pos_items, start=1):
                parts.append(_row(e, rank=i))
        if watch_items:
            parts.append(
                f'<tr class="row-divider divider-neutral"><td colspan="10">'
                f'◇ Full slate · {len(watch_items)}'
                f'</td></tr>'
            )
            for i, e in enumerate(watch_items, start=len(pos_items) + 1):
                parts.append(_row(e, rank=i))
        if settled_items:
            parts.append(
                '<tr class="row-divider divider-neutral"><td colspan="10">'
                f'— Finished picks · {len(settled_items)} on the board as record —'
                '</td></tr>'
            )
            for i, e in enumerate(settled_items, start=len(pos_items) + len(watch_items) + 1):
                parts.append(_row(e, rank=i))
        return "".join(parts)

    # Spec §6: board header now shows real actionable vs watch counts (no
    # placeholder). Count runs over the full `funded` list since there's no
    # top-N cap anymore.
    _actionable_total = sum(1 for e in funded if _is_pos(e))
    _watch_total = sum(1 for e in funded if _is_watch(e))
    _render_board_header(_total_games, _actionable_total, _watch_total)
    html(
        '<div class="board-wrap"><div class="beam"><i></i></div>'
        '<table class="board-table">'
        + header
        + f'<tbody>{_render_rows(top, watch_total=_watch_total)}</tbody>'
        + '</table>'
        '</div>'
    )
    # Spec §7: the one-click "Log pick" expander is gone. The agent auto-logs
    # every actionable pick on every scan via picks_store.auto_log_agent_edges
    # above, and the caption below shows how many new rows this scan added.
    # Dedup lives in storage.picks.upsert_agent_pick keyed on
    # (sport, event_id, market, selection) so reruns never double-log.
    _actionable_count = sum(1 for e in top if _is_pos(e))
    _caption_line = (
        f"<span class='check'>✓</span> {_actionable_count} actionable pick"
        f"{'s' if _actionable_count != 1 else ''} auto-logged to the ledger"
        + (f" · {inserted} new this scan" if inserted else " · no new rows this scan")
    )
    html(f'<div class="auto-log-caption">{_caption_line}</div>')

    if dropped:
        # Dropped picks hit the daily cap - show them separately with $0 stake so
        # the user can see opportunities their cap didn't fund.
        with st.expander(f"⚠ {len(dropped)} edges found but capped out (no stake)", expanded=False):
            by_sport: Dict[str, list] = {}
            for e in dropped:
                key = (e.get("_sport") or "other").upper()
                by_sport.setdefault(key, []).append(e)
            for sport_key in sorted(by_sport.keys(), key=lambda k: -max(ev["edge"] for ev in by_sport[k])):
                sport_edges = by_sport[sport_key]
                st.markdown(f"**{sport_key}** · {len(sport_edges)} edges")
                html(
                    '<table class="ledger" style="margin-top:4px;">'
                    + header
                    + f'<tbody>{"".join(_row(e, highlight=True) for e in sport_edges[:50])}</tbody>'
                    + '</table>'
                )
    html(
        '<div class="hint" style="margin-top:10px;">'
        f'Agent compares <strong>Kalshi</strong> + offshore books '
        f'(<em>Bovada, LowVig.ag, BetOnline.ag, BetUS, MyBookie.ag</em>) against <strong>Pinnacle no-vig</strong>. '
        f'For each outcome, the <strong>best price</strong> wins the row — bet on whichever book is shown. '
        f'Offshore books tolerate winning accounts; retail (DK/FD/MGM) are deliberately excluded. '
        f'Stakes sized at <strong>{kelly_mult*100:.0f}%-Kelly</strong>, 1u = {unit_pct*100:.1f}% of your Kalshi cash, '
        f'per-bet cap <strong>{max_bet/unit_pct:.1f}u</strong>, daily total cap <strong>{daily_cap_pct/unit_pct:.1f}u</strong>.'
        '</div>'
    )

    # Jump-nav to the per-pick analysis boards.
    if top:
        nav_links = " ".join(
            f'<a href="#{e["_anchor"]}" style="padding:3px 10px; background:var(--card-elev); '
            f'border:1px solid var(--rule); border-radius:999px; text-decoration:none; '
            f'color:var(--ink); font-family:JetBrains Mono,monospace; font-size:11px;">'
            f'#{i+1}</a>'
            for i, e in enumerate(top)
        )
        html(
            '<div style="margin-top:14px; padding:10px 14px; background:var(--card); '
            'border:1px solid var(--rule); border-radius:10px; display:flex; align-items:center; '
            'gap:10px; flex-wrap:wrap;">'
            '<span style="font-family:JetBrains Mono,monospace; font-size:10px; letter-spacing:0.14em; '
            'text-transform:uppercase; color:var(--muted);">Jump to analysis →</span>'
            f'{nav_links}'
            '</div>'
        )
    return top


def _render_analysis_boards(top_picks, settings, kalshi_cash):
    """One detailed quantitative-analysis card per top pick, with scroll-nav back to the main table."""
    if not top_picks:
        return
    saved_cfg = settings_store.load()
    unit_pct = settings.get("unit_pct") or 0.01
    unit_dollars = kalshi_cash * unit_pct
    kelly_mult = settings.get("agent_kelly") or 0.25
    max_bet = settings.get("agent_max_bet_pct") or 0.025
    daily_cap_pct = saved_cfg.get("daily_exposure_cap_pct", 0.25)

    def _units_rounded(dollars):
        if unit_dollars <= 0:
            return 0.0
        return round((dollars / unit_dollars) * 2) / 2

    html(
        '<h3 id="analysis-top" style="margin-top:34px; padding-top:10px; '
        'border-top:1px solid var(--rule);">Quantitative analysis</h3>'
        '<p style="color:var(--muted); font-size:13px; margin-top:-6px;">'
        'Each funded pick, decomposed: the fair-price source, the Kelly math, '
        'alternate venues, and current ESPN state.</p>'
    )

    for i, e in enumerate(top_picks):
        _render_single_pick_analysis(e, i + 1, len(top_picks), unit_dollars,
                                     _units_rounded, kelly_mult, max_bet, daily_cap_pct,
                                     unit_pct)


def _render_single_pick_analysis(e, idx, total, unit_dollars, _units_fn, kelly_mult, max_bet, daily_cap_pct, unit_pct=0.01):
    """Render one pick's detailed analysis card.

    V1 edgeboard surfaced a "Quant breakdown" that split the picture into three
    columns — probability ladder, EV math, Kelly math — and layered an LLM-
    generated rationale on top. We don't have an LLM wired here, so the
    rationale is synthesized from the numbers the agent already computed: edge
    magnitude vs sharp lines, kelly size vs. the per-bet cap, variance from
    one unit of stake, and ESPN state. The reader gets the same shape of
    answer without a round-trip to a model.
    """
    from calc.odds import american_to_decimal, implied_prob_american
    import math

    anchor = e.get("_anchor") or f"pick-{idx}"
    matchup = e.get("matchup") or "?"
    sport = (e.get("_sport") or "").upper()
    market_label = e.get("market_label") or agent_calc.MARKET_LABELS.get(e.get("market", "h2h"), "Moneyline")
    side_label = e.get("side_label") or ""
    best_book = e.get("book") or "?"
    best_price = format_american(e.get("price"))
    best_imp = e.get("book_implied") or 0
    fair_prob = e.get("fair_prob") or 0
    edge_pct = (e.get("edge") or 0) * 100
    stake_u = _units_fn(e.get("stake_dollars") or 0)
    stake_dollars = e.get("stake_dollars") or 0
    kelly_frac = e.get("kelly_frac") or 0
    status_detail = e.get("status_detail") or ""
    status_state = e.get("status_state") or ""
    hs = e.get("home_score"); as_ = e.get("away_score")
    kalshi_url = e.get("kalshi_url")
    grade = e.get("grade")

    # EV calculation
    dec = american_to_decimal(e.get("price"))
    profit_on_win = (dec - 1) * stake_dollars if dec and stake_dollars else 0
    ev_dollars = (fair_prob * profit_on_win) - ((1 - fair_prob) * stake_dollars)
    ev_pct = (ev_dollars / stake_dollars * 100) if stake_dollars > 0 else 0

    # Alternate books from candidates list (if present)
    candidates = e.get("candidates") or []
    if candidates:
        candidates_sorted = sorted(candidates, key=lambda c: c["implied"])
        best_imp_val = candidates_sorted[0]["implied"]
    else:
        candidates_sorted = []
        best_imp_val = best_imp

    # Prev/next anchors
    prev_a = f"pick-{idx-1}" if idx > 1 else "agent-table-top"
    next_a = f"pick-{idx+1}" if idx < total else "agent-table-top"
    prev_label = f"← Pick #{idx-1}" if idx > 1 else "↑ Main board"
    next_label = f"Pick #{idx+1} →" if idx < total else "↑ Main board"

    # Live/final status chip
    status_chip = ""
    if status_state == "post":
        score = f"{as_}–{hs}" if hs is not None and as_ is not None else "final"
        status_chip = f'<span class="chip" style="padding:3px 10px;">FINAL · {score}</span>'
    elif status_state == "in":
        score = f"{as_}–{hs}" if hs is not None and as_ is not None else ""
        status_chip = f'<span class="chip live" style="padding:3px 10px;">● LIVE · {status_detail} · {score}</span>'
    else:
        start_dt = e.get("start_utc") or e.get("commence_time")
        if start_dt:
            from datetime import datetime
            from zoneinfo import ZoneInfo
            try:
                if isinstance(start_dt, str):
                    start_dt = datetime.fromisoformat(start_dt.replace("Z", "+00:00"))
                t = start_dt.astimezone(ZoneInfo("America/Chicago")).strftime("%a %I:%M %p").lstrip("0") + " CT"
                status_chip = f'<span class="chip" style="padding:3px 10px;">{t}</span>'
            except Exception:
                pass

    # ----- Probability ladder (every book's implied prob + fair) -----
    ladder_rows = ""
    # Fair row first (reference).
    ladder_rows += (
        f'<tr style="background: color-mix(in oklab, var(--accent) 8%, transparent);">'
        f'<td><strong class="accent">Pinnacle fair (no-vig)</strong></td>'
        f'<td class="num mono"><strong>{fair_prob*100:.2f}%</strong></td>'
        f'<td class="num mono accent">—</td>'
        f'<td class="mono" style="color:var(--muted); font-size:11px;">reference</td>'
        f'</tr>'
    )
    for c in candidates_sorted:
        is_best = c["implied"] == best_imp_val
        edge_pts = (fair_prob - c["implied"]) * 100
        tone = "good" if edge_pts > 0 else ("bad" if edge_pts < 0 else "")
        label = (
            f'<span class="chip accent" style="padding:1px 7px; font-size:9px;">BEST</span>'
            if is_best else '<span style="color:var(--muted); font-size:11px;">—</span>'
        )
        ladder_rows += (
            f'<tr>'
            f'<td class="mono">{c["book"]}</td>'
            f'<td class="num mono">{c["implied"]*100:.2f}%</td>'
            f'<td class="num mono {tone}">{edge_pts:+.2f} pts</td>'
            f'<td>{label}</td>'
            f'</tr>'
        )
    ladder_table = (
        '<div class="mono" style="font-size:10px; letter-spacing:0.14em; text-transform:uppercase; '
        'color:var(--muted); margin-bottom:6px;">Probability ladder</div>'
        '<table class="ledger" style="margin:0;">'
        '<thead><tr><th>Source</th><th class="num">Implied</th>'
        '<th class="num">Edge vs fair</th><th></th></tr></thead>'
        f'<tbody>{ladder_rows}</tbody></table>'
        if ladder_rows else ''
    )

    # ----- EV decomposition -----
    # Pulls apart the win/loss branches so the user can see the shape of the bet.
    stake_1u = unit_dollars  # one unit in dollars
    profit_1u = ((dec or 1) - 1) * stake_1u if dec else 0
    ev_1u = (fair_prob * profit_1u) - ((1 - fair_prob) * stake_1u)
    breakeven_pct = (1.0 / (dec or 1)) * 100 if dec else 0
    ev_table = (
        '<div class="mono" style="font-size:10px; letter-spacing:0.14em; text-transform:uppercase; '
        'color:var(--muted); margin-bottom:6px;">EV math (per 1u)</div>'
        '<table class="ledger" style="margin:0;">'
        '<thead><tr><th>Component</th><th class="num">Value</th><th class="mono">Formula</th></tr></thead>'
        '<tbody>'
        f'<tr><td>Risk</td><td class="num mono">${stake_1u:,.2f}</td>'
        f'<td class="mono" style="color:var(--muted); font-size:11px;">1u = {unit_pct*100:.1f}% of roll</td></tr>'
        f'<tr><td>Decimal odds</td><td class="num mono">{dec:.4f}</td>'
        f'<td class="mono" style="color:var(--muted); font-size:11px;">{best_price} American</td></tr>'
        f'<tr><td>Profit if win</td><td class="num mono good">${profit_1u:,.2f}</td>'
        f'<td class="mono" style="color:var(--muted); font-size:11px;">(dec − 1) × risk</td></tr>'
        f'<tr><td>Loss if miss</td><td class="num mono bad">−${stake_1u:,.2f}</td>'
        f'<td class="mono" style="color:var(--muted); font-size:11px;">risk</td></tr>'
        f'<tr><td>Break-even %</td><td class="num mono">{breakeven_pct:.2f}%</td>'
        f'<td class="mono" style="color:var(--muted); font-size:11px;">1 / decimal odds</td></tr>'
        f'<tr style="background:color-mix(in oklab, var(--good) 10%, transparent);">'
        f'<td><strong>Expected value / 1u</strong></td>'
        f'<td class="num mono"><strong class="{"good" if ev_1u > 0 else "bad"}">${ev_1u:+,.2f}</strong></td>'
        f'<td class="mono" style="color:var(--muted); font-size:11px;">p·win − (1−p)·risk</td></tr>'
        '</tbody></table>'
    )

    # ----- Kelly math -----
    full_kelly = (kelly_frac / kelly_mult) if kelly_mult > 0 else 0.0
    cap_cash = stake_dollars  # already min-capped
    kelly_table = (
        '<div class="mono" style="font-size:10px; letter-spacing:0.14em; text-transform:uppercase; '
        'color:var(--muted); margin-bottom:6px;">Kelly math</div>'
        '<table class="ledger" style="margin:0;">'
        '<thead><tr><th>Component</th><th class="num">Value</th><th class="mono">Note</th></tr></thead>'
        '<tbody>'
        f'<tr><td>Full Kelly fraction</td><td class="num mono">{full_kelly*100:.2f}%</td>'
        f'<td class="mono" style="color:var(--muted); font-size:11px;">p·b − q / b</td></tr>'
        f'<tr><td>Kelly multiplier</td><td class="num mono">×{kelly_mult:.2f}</td>'
        f'<td class="mono" style="color:var(--muted); font-size:11px;">your conservatism dial</td></tr>'
        f'<tr><td>Scaled Kelly</td><td class="num mono accent">{kelly_frac*100:.2f}%</td>'
        f'<td class="mono" style="color:var(--muted); font-size:11px;">of bankroll</td></tr>'
        f'<tr><td>Per-bet cap</td><td class="num mono">{max_bet/unit_pct:.1f}u</td>'
        f'<td class="mono" style="color:var(--muted); font-size:11px;">hard ceiling</td></tr>'
        f'<tr style="background:color-mix(in oklab, var(--accent) 10%, transparent);">'
        f'<td><strong>Final stake</strong></td>'
        f'<td class="num mono"><strong>{stake_u:.2f}u (${cap_cash:,.2f})</strong></td>'
        f'<td class="mono" style="color:var(--muted); font-size:11px;">kelly ∩ cap</td></tr>'
        '</tbody></table>'
    )

    # ----- Data-driven rationale (V1 style, no LLM required) -----
    # Mix objective signals into human-readable paragraphs. Each paragraph has
    # a stable focus (market, pricing, sizing, risk, state) so the reader can
    # skim it like a research note.
    def _edge_tier(pts):
        if pts >= 5: return ("outlier", "This is in outlier territory for sharp-vs-sharp spread — most Pinnacle-rated edges live below 2 pts.")
        if pts >= 3: return ("large", "This is a large edge by sharp-market standards; such gaps rarely survive the final hour.")
        if pts >= 1.5: return ("solid", "A solid edge for Pinnacle-anchored pricing — the typical publishable band.")
        if pts >= 0.5: return ("thin", "Thin but legitimate — Pinnacle-vs-Kalshi often lives right around here.")
        return ("marginal", "A marginal edge; worth a flyer only if the risk cost is small.")
    tier_name, tier_note = _edge_tier(edge_pct)

    kelly_pct_of_cap = (stake_u / (max_bet / unit_pct)) if (max_bet / unit_pct) > 0 else 0
    # Compare the best book's implied to the median of the ladder for a
    # quick "is this one bookmaker, or is the whole market mispriced" read.
    if len(candidates_sorted) >= 2:
        med = sorted([c["implied"] for c in candidates_sorted])[len(candidates_sorted)//2]
        median_gap = (fair_prob - med) * 100
    else:
        median_gap = edge_pct

    # State paragraph.
    if status_state == "post":
        if grade == "win":
            state_sent = f"The game is final and the pick <strong class='good'>won</strong>."
        elif grade == "loss":
            state_sent = f"The game is final and the pick <strong class='bad'>lost</strong>."
        elif grade == "push":
            state_sent = f"The game is final and the pick <strong>pushed</strong>."
        else:
            state_sent = "The game is final; manual grading required for this market."
    elif status_state == "in":
        if grade == "winning":
            state_sent = f"The game is live and the pick is currently <strong class='good'>ahead</strong>."
        elif grade == "losing":
            state_sent = f"The game is live and the pick is currently <strong class='bad'>behind</strong>."
        elif grade == "tied":
            state_sent = f"The game is tied in-play; the pick's outcome is still open."
        else:
            state_sent = f"The game is live ({status_detail or 'in progress'})."
    else:
        state_sent = "Game has not kicked off yet; prices and the edge are still mobile."

    rationale_html = (
        '<div style="margin-top:14px; display:grid; grid-template-columns:1fr; gap:10px;">'
        '<div style="padding:14px 16px; background:var(--card-elev); border-radius:8px; '
        'font-size:13px; color:var(--ink-dim); line-height:1.65;">'
        f'<div style="margin-bottom:8px; font-family:JetBrains Mono,monospace; font-size:10px; '
        f'letter-spacing:0.14em; text-transform:uppercase; color:var(--muted);">'
        f'Agent rationale · {tier_name} edge</div>'
        # Market paragraph
        f'<p style="margin:0 0 8px 0;"><strong>Market read.</strong> Pinnacle\'s no-vig says '
        f'<strong>{fair_prob*100:.2f}%</strong> true for <em>{side_label}</em>. {best_book} is '
        f'offering <strong>{best_price}</strong> ({best_imp*100:.2f}% implied), a '
        f'<strong>{edge_pct:+.2f}-pt</strong> gap. {tier_note}</p>'
        # Pricing/ladder paragraph
        f'<p style="margin:0 0 8px 0;"><strong>Pricing.</strong> Across '
        f'{len(candidates_sorted) or 1} source(s) scanned, the median implied price for this side '
        f'leaves a <strong>{median_gap:+.2f}-pt</strong> gap to Pinnacle. '
        + ('A tight ladder means the mispricing is concentrated at this book — more likely to steam.'
           if abs(median_gap - edge_pct) < 1.0 else
           'The ladder is wide — this book is the clear outlier; expect it to pull toward the sharp line.')
        + '</p>'
        # Sizing paragraph
        f'<p style="margin:0 0 8px 0;"><strong>Sizing.</strong> Full Kelly points to '
        f'<strong>{full_kelly*100:.2f}%</strong> of bankroll; after a '
        f'<strong>{kelly_mult:.2f}×</strong> conservatism multiplier, that lands at '
        f'<strong>{stake_u:.2f}u (${cap_cash:,.2f})</strong>, which is '
        f'<strong>{kelly_pct_of_cap*100:.0f}%</strong> of the per-bet cap of '
        f'<strong>{max_bet/unit_pct:.1f}u</strong>. '
        + ('Right at the cap — a sign the scanner wants more, and the cap is doing its job.'
           if kelly_pct_of_cap >= 0.95 else
           'Comfortably inside the per-bet ceiling; no cap pressure here.')
        + '</p>'
        # Risk paragraph
        f'<p style="margin:0 0 8px 0;"><strong>Risk.</strong> One decision; outcome resolves at ~'
        f'<strong>{breakeven_pct:.2f}%</strong> break-even. Expected per unit: '
        f'<strong class="{"good" if ev_1u > 0 else "bad"}">${ev_1u:+,.2f}</strong>, '
        f'and the maximum drawdown on this ticket alone is <strong>{stake_u:.2f}u</strong>. '
        f'Variance σ of a single Bernoulli bet at this price ≈ '
        f'<strong>{math.sqrt(fair_prob*(1-fair_prob))*((dec or 1)-1)*stake_u:.2f}u</strong>.'
        '</p>'
        # State paragraph
        f'<p style="margin:0;"><strong>Game state.</strong> {state_sent}</p>'
        '</div>'
        '</div>'
    )

    ladder_ev_kelly_grid = (
        '<div style="display:grid; grid-template-columns:1fr 1fr 1fr; gap:14px; margin-top:16px;">'
        f'<div>{ladder_table}</div>'
        f'<div>{ev_table}</div>'
        f'<div>{kelly_table}</div>'
        '</div>'
    )

    # Open-bet CTA
    cta_html = ""
    if kalshi_url and best_book.lower() == "kalshi":
        cta_html = (
            f'<a href="{kalshi_url}" target="_blank" rel="noopener" '
            'style="display:inline-block; padding:8px 16px; background:var(--accent); color:var(--accent-ink); '
            'border-radius:8px; text-decoration:none; font-weight:600; font-size:13px;">'
            f'Open on Kalshi ↗</a>'
        )

    # Main board
    html(
        f'<div id="{anchor}" style="position:relative; top:-20px;"></div>'
        f'<div style="background:var(--card); border:1px solid var(--rule); border-radius:12px; '
        f'padding:20px 24px; margin-top:16px;">'
        # Header row
        f'<div style="display:flex; justify-content:space-between; align-items:flex-start; '
        f'flex-wrap:wrap; gap:12px; padding-bottom:12px; border-bottom:1px solid var(--rule); margin-bottom:14px;">'
        f'<div>'
        f'<div style="display:flex; align-items:center; gap:8px; margin-bottom:4px;">'
        f'<span class="chip accent" style="padding:3px 10px;">#{idx}</span>'
        f'<span class="chip" style="padding:3px 10px;">{sport}</span>'
        f'<span class="chip" style="padding:3px 10px;">{market_label}</span>'
        f'{status_chip}'
        f'</div>'
        f'<div style="font-family:Fraunces,serif; font-style:italic; font-size:22px; margin-top:4px;">{matchup}</div>'
        f'<div style="font-size:15px; color:var(--ink); margin-top:4px;"><strong>{side_label}</strong></div>'
        f'</div>'
        f'<div style="text-align:right;">'
        f'<div class="mono" style="font-size:10px; color:var(--muted); letter-spacing:0.12em; text-transform:uppercase;">Edge</div>'
        f'<div class="mono ev-plus" style="font-size:28px; font-weight:500;">{edge_pct:+.2f}pts</div>'
        f'{cta_html}'
        f'</div>'
        f'</div>'
        # Headline metric blocks
        f'<div style="display:grid; grid-template-columns:repeat(auto-fit, minmax(140px, 1fr)); gap:10px;">'
        f'<div class="metric-card"><div class="label">Price</div><div class="value">{best_price}</div><div class="foot">{best_book}</div></div>'
        f'<div class="metric-card"><div class="label">Market IP</div><div class="value">{best_imp*100:.2f}%</div><div class="foot">at that price</div></div>'
        f'<div class="metric-card"><div class="label">Pinnacle fair</div><div class="value accent">{fair_prob*100:.2f}%</div><div class="foot">no-vig consensus</div></div>'
        f'<div class="metric-card"><div class="label">Kelly</div><div class="value">{kelly_frac*100:.2f}%</div><div class="foot">{kelly_mult*100:.0f}% × full Kelly</div></div>'
        f'<div class="metric-card"><div class="label">Stake</div><div class="value accent">{stake_u:.1f}u</div><div class="foot">of {max_bet/unit_pct:.1f}u max</div></div>'
        f'<div class="metric-card"><div class="label">EV / 100u</div><div class="value {"good" if ev_pct > 0 else ""}">{ev_pct:+.2f}%</div><div class="foot">expected per 100u risked</div></div>'
        f'</div>'
        # Three-column quant decomposition (V1-style)
        f'{ladder_ev_kelly_grid}'
        # Multi-paragraph rationale
        f'{rationale_html}'
        # Nav footer
        f'<div style="margin-top:16px; padding-top:12px; border-top:1px solid var(--rule); '
        f'display:flex; justify-content:space-between; align-items:center;">'
        f'<a href="#{prev_a}" style="color:var(--muted); text-decoration:none; font-size:12px;">{prev_label}</a>'
        f'<a href="#agent-table-top" style="color:var(--accent); text-decoration:none; font-size:12px; font-weight:500;">↑ Main board</a>'
        f'<a href="#{next_a}" style="color:var(--muted); text-decoration:none; font-size:12px;">{next_label}</a>'
        f'</div>'
        f'</div>'
    )


def _render_quick_calculator(merged, unit_pct, bankroll_units, sport):
    """Compact bar: pick a game, see live EV table for both sides."""
    if not merged:
        return
    labels = [
        f"{ev['away']['short'] or ev['away']['name']} @ {ev['home']['short'] or ev['home']['name']}"
        for ev, _odds_meta, _exch in merged
    ]
    html('<h3>Quick-glance EV scanner</h3>')
    pick_idx = st.selectbox(
        "Pick a game", options=list(range(len(labels))),
        format_func=lambda i: labels[i], key="quick_pick", label_visibility="collapsed",
    )
    ev, odds_meta, _exch_probs = merged[pick_idx]
    projected_home = st.slider(
        "Your projected home win probability",
        min_value=0.0, max_value=1.0, value=0.55, step=0.01,
        key="quick_proj",
    )
    projected_away = 1.0 - projected_home

    h2h = (odds_meta or {}).get("h2h") or {}
    hp = h2h.get("home_price")
    ap = h2h.get("away_price")

    cols = st.columns(2)
    for col, side, name, prob, price in [
        (cols[0], "Away", ev["away"]["name"], projected_away, ap),
        (cols[1], "Home", ev["home"]["name"], projected_home, hp),
    ]:
        with col:
            imp = implied_prob_american(price)
            evp = ev_percent(prob, price)
            tone = "good" if (evp or 0) > 0 else ("bad" if (evp or 0) < 0 else "")
            ku = units_from_kelly(kelly_fraction(prob, price, multiplier=0.5), unit_pct)
            imp_s = f"{imp*100:.1f}%" if imp is not None else "—"
            evp_s = f"{evp*100:+.2f}%" if evp is not None else "—"
            price_s = format_american(price)
            html(f"""
            <div class="calc-wrap">
              <div style="display:flex; justify-content:space-between; align-items:baseline;">
                <div><span class="tag model">{side}</span> &nbsp;<strong>{name or '-'}</strong></div>
                <div class="mono">{price_s}</div>
              </div>
              <div class="calc-grid" style="margin-top:10px;">
                <div><div class="label mono" style="font-size:10px; color:var(--muted); letter-spacing:0.12em;">IMPLIED</div><div class="mono" style="font-size:16px;">{imp_s}</div></div>
                <div><div class="label mono" style="font-size:10px; color:var(--muted); letter-spacing:0.12em;">YOUR PROB</div><div class="mono" style="font-size:16px;">{prob*100:.1f}%</div></div>
                <div><div class="label mono" style="font-size:10px; color:var(--muted); letter-spacing:0.12em;">EV / $100</div><div class="mono {tone}" style="font-size:16px;">{evp_s}</div></div>
                <div><div class="label mono" style="font-size:10px; color:var(--muted); letter-spacing:0.12em;">HALF-KELLY</div><div class="mono" style="font-size:16px;">{format_units(ku)}</div></div>
              </div>
            </div>
            """)


def _add_pick_control(event, odds_meta, unit_pct, bankroll_units):
    """Collapsible 'Log pick' control under each game card."""
    key_prefix = f"pick_{event.get('id','?')}"
    with st.expander("＋ Log pick for this game", expanded=False):
        h2h = (odds_meta or {}).get("h2h") or {}
        spr = (odds_meta or {}).get("spreads") or {}
        tot = (odds_meta or {}).get("totals") or {}

        market = st.selectbox(
            "Market", options=["h2h", "spreads", "totals"],
            format_func=lambda m: {"h2h": "Moneyline", "spreads": "Spread", "totals": "Total"}[m],
            key=f"{key_prefix}_market",
        )
        home = event.get("home") or {}
        away = event.get("away") or {}

        if market == "h2h":
            side = st.radio(
                "Side", options=[away.get("name"), home.get("name")],
                key=f"{key_prefix}_side", horizontal=True,
            )
            default_price = h2h.get("away_price") if side == away.get("name") else h2h.get("home_price")
            point = None
        elif market == "spreads":
            side = st.radio(
                "Side", options=[away.get("name"), home.get("name")],
                key=f"{key_prefix}_side", horizontal=True,
            )
            default_point = spr.get("away_point") if side == away.get("name") else spr.get("home_point")
            point = st.number_input("Point", value=float(default_point or 0.0), step=0.5, key=f"{key_prefix}_pt")
            default_price = spr.get("away_price") if side == away.get("name") else spr.get("home_price")
        else:
            side = st.radio("Side", options=["Over", "Under"], key=f"{key_prefix}_side", horizontal=True)
            default_point = tot.get("home_point") or tot.get("away_point") or 0.0
            point = st.number_input("Total", value=float(default_point or 0.0), step=0.5, key=f"{key_prefix}_pt")
            default_price = tot.get("home_price") if side == "Over" else tot.get("away_price")

        price = st.number_input(
            "American price", value=float(default_price or -110),
            step=5.0, format="%.0f", key=f"{key_prefix}_price",
        )
        projected = st.slider(
            "Your projected win probability", 0.0, 1.0,
            value=float(implied_prob_american(price) or 0.5), step=0.01,
            key=f"{key_prefix}_proj",
        )
        methodology = st.selectbox(
            "Methodology", options=["Model Edge", "Line Move", "Situational", "Fade the Public"],
            key=f"{key_prefix}_meth",
        )
        # Breakdown field is hidden until the user explicitly asks for it.
        show_breakdown_key = f"{key_prefix}_show_notes"
        if st.session_state.get(show_breakdown_key):
            notes = st.text_area(
                "Quantitative breakdown",
                placeholder="Why this pick? EV, line move, situational angle, model vs. market...",
                key=f"{key_prefix}_notes", height=100,
            )
            if st.button("Hide breakdown", key=f"{key_prefix}_hide_notes"):
                st.session_state[show_breakdown_key] = False
                st.rerun()
        else:
            notes = st.session_state.get(f"{key_prefix}_notes", "")
            if st.button("＋ Add quantitative breakdown", key=f"{key_prefix}_add_notes"):
                st.session_state[show_breakdown_key] = True
                st.rerun()

        evp = ev_percent(projected, price)
        units_rec = recommend_units(evp)
        imp = implied_prob_american(price)

        c1, c2, c3 = st.columns(3)
        c1.metric("EV / $100", f"{(evp or 0)*100:+.2f}%" if evp is not None else "—")
        c2.metric("Implied", f"{(imp or 0)*100:.1f}%" if imp is not None else "—")
        c3.metric("Rec. units", format_units(units_rec))

        risk_u = st.number_input(
            "Risk units", min_value=0.0, max_value=5.0, step=0.5, value=float(units_rec),
            key=f"{key_prefix}_risk",
        )
        win_u = 0.0
        if risk_u > 0:
            stake_dollars = risk_u * unit_pct * 100.0  # per-$100-bankroll units
            prof = profit_on_win(stake_dollars, price)
            if prof is not None and stake_dollars > 0:
                win_u = risk_u * (prof / stake_dollars)

        if st.button("Save pick", key=f"{key_prefix}_save", disabled=(risk_u <= 0)):
            pick = {
                "sport": event.get("sport"),
                "event_id": event.get("id"),
                "event_name": event.get("name"),
                "market": market,
                "selection": side,
                "point": point,
                "price_american": price,
                "projected_prob": projected,
                "implied_prob": imp,
                "risk_units": risk_u,
                "win_units": win_u,
                "methodology": methodology,
                "notes": notes,
            }
            saved = picks_store.add_pick(pick)
            st.success(f"Pick saved: {side} {('@ ' + format_american(price)) if market=='h2h' else ''}")
            st.cache_data.clear()


# ============================================================================
# TAB 2 - Pro Handicapping Tools
# ============================================================================

def render_pro_tools(settings: dict):
    brand_bar("Pro Handicapping Tools")

    unit_pct = settings.get("unit_pct") or 0.01
    bankroll_units = settings.get("bankroll_units") or 100.0

    st.markdown("### Odds translator")
    st.caption("Enter a price in any format — the other formats and implied probability update instantly.")
    c1, c2, c3 = st.columns(3)
    with c1:
        am = st.number_input("American", value=-110.0, step=5.0, format="%.0f", key="pt_am")
    with c2:
        dec_val = american_to_decimal(am) or 1.0
        dec_in = st.number_input("Decimal", value=float(dec_val), step=0.01, format="%.4f", key="pt_dec")
    with c3:
        frac = american_to_fractional(am) or "—"
        st.text_input("Fractional", value=frac, disabled=True, key="pt_frac")

    # if user edited decimal, convert back
    if abs(dec_in - (dec_val or 0)) > 1e-4:
        am = decimal_to_american(dec_in) or am

    imp = implied_prob_american(am)
    html(f"""
    <table class="ledger" style="margin-top:14px;">
      <thead><tr><th>Format</th><th class="num">Value</th><th>Note</th></tr></thead>
      <tbody>
        <tr><td>American</td><td class="num mono">{format_american(am)}</td><td class="mono" style="color:var(--muted);">standard US price</td></tr>
        <tr><td>Decimal</td><td class="num mono">{american_to_decimal(am):.4f}</td><td class="mono" style="color:var(--muted);">payout multiplier incl. stake</td></tr>
        <tr><td>Fractional</td><td class="num mono">{american_to_fractional(am) or '—'}</td><td class="mono" style="color:var(--muted);">UK-style</td></tr>
        <tr><td><strong>Implied probability</strong></td><td class="num mono"><strong>{(imp*100):.2f}%</strong></td><td class="mono" style="color:var(--muted);">vig-inclusive</td></tr>
      </tbody>
    </table>
    """)

    st.markdown("### True Value calculator")
    st.caption("Compare your projected win probability to the market's implied price. Soft-green when EV > 0.")
    c1, c2, c3 = st.columns([1, 1, 1])
    tv_price = c1.number_input("Market price (American)", value=-110.0, step=5.0, format="%.0f", key="tv_price")
    tv_proj = c2.slider("Your projected win %", 0.0, 1.0, 0.55, 0.01, key="tv_proj")
    tv_stake = c3.number_input("Stake ($)", value=100.0, min_value=1.0, step=10.0, key="tv_stake")

    ev_dollars = expected_value(tv_proj, tv_stake, tv_price)
    evp = ev_percent(tv_proj, tv_price)
    tv_imp = implied_prob_american(tv_price)
    edge_val = calc_edge(tv_proj, tv_price)
    prof = profit_on_win(tv_stake, tv_price)

    tone = "good" if (evp or 0) > 0 else ("bad" if (evp or 0) < 0 else "")
    bg_tint = "color-mix(in oklab, var(--good) 10%, var(--card))" if tone == "good" else (
        "color-mix(in oklab, var(--bad) 10%, var(--card))" if tone == "bad" else "var(--card)"
    )
    ev_display = f"{ev_dollars:+.2f}" if ev_dollars is not None else "—"
    evp_display = f"{evp*100:+.2f}%" if evp is not None else "—"
    edge_display = f"{edge_val*100:+.2f} pts" if edge_val is not None else "—"
    prof_display = f"{prof:+.2f}" if prof is not None else "—"
    imp_display = f"{tv_imp*100:.2f}%" if tv_imp is not None else "—"

    html(f"""
    <div class="calc-wrap" style="background:{bg_tint};">
      <div class="calc-grid">
        <div><div class="label mono" style="font-size:10px; color:var(--muted); letter-spacing:0.12em;">EV ($)</div><div class="mono {tone}" style="font-size:22px;">{ev_display}</div></div>
        <div><div class="label mono" style="font-size:10px; color:var(--muted); letter-spacing:0.12em;">EV (%)</div><div class="mono {tone}" style="font-size:22px;">{evp_display}</div></div>
        <div><div class="label mono" style="font-size:10px; color:var(--muted); letter-spacing:0.12em;">EDGE vs. MARKET</div><div class="mono" style="font-size:22px;">{edge_display}</div></div>
        <div><div class="label mono" style="font-size:10px; color:var(--muted); letter-spacing:0.12em;">PROFIT ON WIN</div><div class="mono" style="font-size:22px;">${prof_display}</div></div>
        <div><div class="label mono" style="font-size:10px; color:var(--muted); letter-spacing:0.12em;">IMPLIED</div><div class="mono" style="font-size:22px;">{imp_display}</div></div>
      </div>
    </div>
    """)

    st.markdown("### Kelly bet-sizer")
    st.caption("Full Kelly is aggressive. Most pros bet 1/4 to 1/2 Kelly to survive variance.")
    c1, c2, c3 = st.columns([1, 1, 1])
    k_price = c1.number_input("Market price (American)", value=-110.0, step=5.0, format="%.0f", key="k_price")
    k_proj = c2.slider("Your win probability", 0.0, 1.0, 0.55, 0.01, key="k_proj")
    k_mult = c3.selectbox(
        "Kelly fraction", options=[1.0, 0.5, 0.25, 0.125],
        format_func=lambda v: {1.0: "Full Kelly", 0.5: "Half Kelly", 0.25: "Quarter Kelly", 0.125: "Eighth Kelly"}[v],
        index=1, key="k_mult",
    )
    bk_units = st.number_input("Bankroll (units)", value=float(bankroll_units), min_value=1.0, step=1.0, key="k_bk")
    bk_dollars = bk_units * unit_pct * 100.0

    kf = kelly_fraction(k_proj, k_price, k_mult) or 0.0
    stake_dollars = bk_dollars * kf
    stake_units = units_from_kelly(kelly_fraction(k_proj, k_price, k_mult), unit_pct)

    html(f"""
    <div class="calc-wrap">
      <div class="calc-grid">
        <div><div class="label mono" style="font-size:10px; color:var(--muted); letter-spacing:0.12em;">KELLY FRACTION</div><div class="mono" style="font-size:22px;">{kf*100:.2f}%</div></div>
        <div><div class="label mono" style="font-size:10px; color:var(--muted); letter-spacing:0.12em;">UNITS TO RISK</div><div class="mono accent" style="font-size:22px;">{format_units(stake_units)}</div></div>
        <div><div class="label mono" style="font-size:10px; color:var(--muted); letter-spacing:0.12em;">RAW STAKE</div><div class="mono" style="font-size:22px;">${stake_dollars:,.2f}</div></div>
      </div>
    </div>
    """)

    st.markdown("### No-vig fair line")
    st.caption("Strip the house margin from a two-way market to see the market's \"true\" prices.")
    c1, c2 = st.columns(2)
    nv_home = c1.number_input("Home American", value=-150.0, step=5.0, format="%.0f", key="nv_home")
    nv_away = c2.number_input("Away American", value=130.0, step=5.0, format="%.0f", key="nv_away")
    ph_fair, pa_fair = no_vig_two_way(nv_home, nv_away)
    if ph_fair is None:
        st.info("Enter valid two-way prices.")
    else:
        fair_home_am = decimal_to_american(1.0 / ph_fair)
        fair_away_am = decimal_to_american(1.0 / pa_fair)
        html(f"""
        <table class="ledger" style="margin-top:10px;">
          <thead><tr><th>Side</th><th class="num">Market</th><th class="num">Fair</th><th class="num">Vig cost</th></tr></thead>
          <tbody>
            <tr><td>Home</td><td class="num mono">{format_american(nv_home)}</td><td class="num mono accent">{format_american(fair_home_am)}</td><td class="num mono">{(implied_prob_american(nv_home)-ph_fair)*100:+.2f} pts</td></tr>
            <tr><td>Away</td><td class="num mono">{format_american(nv_away)}</td><td class="num mono accent">{format_american(fair_away_am)}</td><td class="num mono">{(implied_prob_american(nv_away)-pa_fair)*100:+.2f} pts</td></tr>
          </tbody>
        </table>
        """)


# ============================================================================
# TAB 3 - Verified Picks Ledger
# ============================================================================

def render_picks(settings: dict):
    brand_bar("Verified Picks Ledger · by slate date")

    # Backfill the slate-date anchor on legacy rows so every historical pick
    # groups cleanly by date. Idempotent — rows that already have pick_date
    # are untouched, so this is safe to call on every ledger view.
    try:
        picks_store.backfill_pick_dates()
    except Exception:
        pass

    cols = st.columns([1.1, 1.2, 1, 1, 1, 1])
    with cols[0]:
        if st.button("↻ Auto-settle from ESPN"):
            n = picks_store.auto_settle()
            st.cache_data.clear()
            st.success(f"Settled {n} pick(s).")
    with cols[1]:
        # Legacy cleanup: picks logged before the two-way collapse landed can
        # still show both sides of the same market. One click fixes.
        if st.button("⟲ Collapse opposing sides"):
            removed = picks_store.collapse_duplicate_sides()
            st.cache_data.clear()
            if removed:
                st.success(f"Removed {removed} duplicate opposing-side pick(s).")
            else:
                st.info("No opposing-side duplicates found.")
    with cols[2]:
        filter_status = st.selectbox(
            "Filter", options=["All", "Open", "Won", "Lost", "Push"],
            key="ledger_filter", label_visibility="collapsed",
        )
    with cols[3]:
        filter_sport = st.selectbox(
            "Sport", options=["All"] + sorted({p.get("sport") or "" for p in picks_store.list_picks() if p.get("sport")}),
            key="ledger_sport_filter", label_visibility="collapsed",
        )
    with cols[4]:
        # Source filter — distinguishes picks the agent auto-logged from manual
        # entries so users can audit either stream in isolation.
        filter_source = st.selectbox(
            "Source", options=["All", "Agent (auto)", "Manual"],
            key="ledger_source_filter", label_visibility="collapsed",
        )
    with cols[5]:
        # Ported from V1 Bet Log — one-click CSV download. Streams everything
        # currently in the ledger, not just the filtered rows, so users can
        # grab a full history without having to toggle filters off first.
        all_picks = picks_store.list_picks()
        if all_picks:
            import csv, io
            buf = io.StringIO()
            fieldnames = [
                "id", "created_at", "sport", "event_name", "market", "selection", "point",
                "price_american", "implied_prob", "projected_prob",
                "risk_units", "win_units", "status", "clv_pct", "methodology", "notes",
            ]
            writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for p in all_picks:
                writer.writerow({k: p.get(k) for k in fieldnames})
            st.download_button(
                "⬇ CSV", buf.getvalue().encode("utf-8"),
                "picks.csv", "text/csv",
                use_container_width=True, key="ledger_csv_dl",
            )

    picks = picks_store.list_picks()
    s = picks_store.summarize(picks)

    net_tone = "good" if (s["net_units"] or 0) > 0 else ("bad" if (s["net_units"] or 0) < 0 else "")
    roi_display = f"{(s['roi'] or 0)*100:+.2f}%" if s["roi"] is not None else "—"
    win_pct = f"{(s['win_pct'] or 0)*100:.1f}%" if s["win_pct"] is not None else "—"

    metric_row([
        {"label": "Open", "value": str(s["open"])},
        {"label": "Record", "value": f"{s['wins']}-{s['losses']}-{s['pushes']}"},
        {"label": "Win %", "value": win_pct, "foot": f"{s['wins']+s['losses']} decided"},
        {"label": "Risked", "value": f"{s['risked_units']:.2f}u"},
        {"label": "Net", "value": f"{s['net_units']:+.2f}u", "tone": net_tone},
        {"label": "ROI", "value": roi_display, "tone": net_tone},
        {"label": "Avg CLV", "value": f"{(s['avg_clv_pct'] or 0)*100:+.2f}%" if s["avg_clv_pct"] is not None else "—"},
    ])

    # apply filters
    def _keep(p):
        status = p.get("status") or "open"
        if filter_status == "Open" and status != "open":
            return False
        if filter_status == "Won" and status != "win":
            return False
        if filter_status == "Lost" and status != "loss":
            return False
        if filter_status == "Push" and status != "push":
            return False
        if filter_sport != "All" and p.get("sport") != filter_sport:
            return False
        src = p.get("source") or "manual"
        if filter_source == "Agent (auto)" and src != "agent-auto":
            return False
        if filter_source == "Manual" and src == "agent-auto":
            return False
        return True
    rows = [p for p in picks if _keep(p)]

    if not rows:
        html('<div class="hint">No picks match the current filter. Log one from the Dashboard tab.</div>')
        return

    # ----- Daily records view (primary) -----
    # Users want to look back at a specific day's slate: how many picks the
    # agent made, which games, how they graded, net units for the day.
    # Group by `pick_date` (Central-US slate) descending.
    st.markdown("### Daily records")
    st.caption(
        "Each day's slate stays anchored to its Central-US date. Pick records "
        "are permanent — this list is the audit trail of what the agent "
        "recommended and how it finished, by day."
    )
    daily = picks_store.group_by_date(rows)
    if not daily:
        html('<div class="hint">No dated picks yet.</div>')
    else:
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo as _ZI
        today_c = datetime.now(_ZI("America/Chicago")).strftime("%Y-%m-%d")
        for i, day in enumerate(daily):
            d = day["date"]
            try:
                pretty = _dt.strptime(d, "%Y-%m-%d").strftime("%A, %b %d, %Y")
            except ValueError:
                pretty = d
            is_today_slate = (d == today_c)
            # Header line summarizing the day.
            net_tone_d = "good" if day["net_units"] > 0 else ("bad" if day["net_units"] < 0 else "")
            roi_d = f"{(day['roi'] or 0)*100:+.2f}%" if day["roi"] is not None else "—"
            record = f"{day['wins']}-{day['losses']}-{day['pushes']}"
            open_part = f" · {day['open']} open" if day["open"] else ""
            today_badge = (
                ' <span class="chip accent" style="padding:1px 7px; font-size:9.5px;">TODAY</span>'
                if is_today_slate else ""
            )
            header = (
                f"**{pretty}**{today_badge} · "
                f"**{day['count']} picks** · {record}{open_part} · "
                f"risk {day['risked_units']:.2f}u · "
                f"net <span style='color:var(--{'good' if day['net_units']>0 else ('bad' if day['net_units']<0 else 'muted')});'>"
                f"{day['net_units']:+.2f}u</span> · ROI {roi_d}"
            )
            # Today auto-expands; older days stay collapsed for scan-ability.
            with st.expander(f"{pretty} · {day['count']} picks · {record} · {day['net_units']:+.2f}u",
                             expanded=is_today_slate):
                # Full table of the day's picks.
                html(
                    '<table class="ledger" style="margin-top:4px;">'
                    '<thead><tr>'
                    '<th>Logged</th><th>Src</th><th>Sport</th><th>Match-up</th><th>Market</th>'
                    '<th>Selection</th><th class="num">Price</th><th class="num">Imp</th>'
                    '<th class="num">Proj</th><th class="num">Risk</th>'
                    '<th class="num">Net</th><th>Status</th><th class="num">CLV</th>'
                    '</tr></thead><tbody>'
                )
                for p in sorted(day["picks"], key=lambda x: x.get("created_at") or "", reverse=True):
                    status = p.get("status") or "open"
                    status_cls = {"open":"","win":"good","loss":"bad","push":"warn","void":""}.get(status,"")
                    status_lbl = status.upper()
                    clv = p.get("clv_pct")
                    clv_s = f"{clv*100:+.2f}%" if clv is not None else "—"
                    src = p.get("source") or "manual"
                    src_chip = (
                        '<span class="chip accent" style="padding:1px 7px; font-size:9.5px;">AGENT</span>'
                        if src == "agent-auto" else
                        '<span class="chip" style="padding:1px 7px; font-size:9.5px;">MANUAL</span>'
                    )
                    risk_u = float(p.get("risk_units") or 0)
                    win_u = float(p.get("win_units") or 0)
                    if status == "win":
                        net_u = win_u
                    elif status == "loss":
                        net_u = -risk_u
                    else:
                        net_u = 0.0
                    net_class = "good" if net_u > 0 else ("bad" if net_u < 0 else "")
                    net_disp = (
                        f'<span class="num mono {net_class}">{net_u:+.2f}u</span>'
                        if status in ("win","loss") else '<span style="color:var(--muted);">—</span>'
                    )
                    html(f"""
                    <tr>
                      <td class="mono" style="font-size:11px; color:var(--muted);">{(p.get('created_at') or '')[11:16]}</td>
                      <td>{src_chip}</td>
                      <td class="mono" style="color:var(--muted);">{(p.get('sport') or '').upper()}</td>
                      <td>{p.get('event_name') or '-'}</td>
                      <td class="mono" style="color:var(--muted);">{(p.get('market') or '').upper()}</td>
                      <td><strong>{p.get('selection') or '-'}</strong>{(' ' + (f"{p.get('point'):+g}" if p.get('point') is not None else '')) if p.get('point') is not None else ''}</td>
                      <td class="num mono">{format_american(p.get('price_american'))}</td>
                      <td class="num mono">{(p.get('implied_prob') or 0)*100:.1f}%</td>
                      <td class="num mono">{(p.get('projected_prob') or 0)*100:.1f}%</td>
                      <td class="num mono">{risk_u:.2f}u</td>
                      <td class="num mono">{net_disp}</td>
                      <td><span class="chip {status_cls}">{status_lbl}</span></td>
                      <td class="num mono">{clv_s}</td>
                    </tr>
                    """)
                html("</tbody></table>")

    # ----- Flat all-picks table (secondary, for raw lookup) -----
    with st.expander(f"🔎 Flat all-picks table ({len(rows)} rows)", expanded=False):
        html("""
    <table class="ledger">
      <thead>
        <tr>
          <th>Slate</th><th>Logged</th><th>Src</th><th>Sport</th><th>Match-up</th><th>Market</th>
          <th>Selection</th><th class="num">Price</th><th class="num">Imp</th>
          <th class="num">Proj</th><th>Method</th><th class="num">Risk</th>
          <th class="num">To Win</th><th>Status</th><th class="num">CLV</th>
        </tr>
      </thead>
      <tbody>
    """)
        for p in sorted(rows, key=lambda x: x.get("created_at") or "", reverse=True):
            status = p.get("status") or "open"
            status_cls = {
                "open": "", "win": "good", "loss": "bad", "push": "warn", "void": "",
            }.get(status, "")
            status_lbl = status.upper()
            clv = p.get("clv_pct")
            clv_s = f"{clv*100:+.2f}%" if clv is not None else "—"
            meth = p.get("methodology") or ""
            tag_cls = {
                "Model Edge": "model", "Line Move": "line",
                "Situational": "sit", "Fade the Public": "fade",
            }.get(meth, "model")
            src = p.get("source") or "manual"
            src_chip = (
                '<span class="chip accent" style="padding:1px 7px; font-size:9.5px;">AGENT</span>'
                if src == "agent-auto" else
                '<span class="chip" style="padding:1px 7px; font-size:9.5px;">MANUAL</span>'
            )
            html(f"""
        <tr>
          <td class="mono" style="font-size:11px; color:var(--muted);">{p.get('pick_date') or '—'}</td>
          <td class="mono" style="font-size:11px; color:var(--muted);">{(p.get('created_at') or '')[11:16]}</td>
          <td>{src_chip}</td>
          <td class="mono" style="color:var(--muted);">{(p.get('sport') or '').upper()}</td>
          <td>{p.get('event_name') or '-'}</td>
          <td class="mono" style="color:var(--muted);">{(p.get('market') or '').upper()}</td>
          <td><strong>{p.get('selection') or '-'}</strong>{(' ' + (f"{p.get('point'):+g}" if p.get('point') is not None else '')) if p.get('point') is not None else ''}</td>
          <td class="num mono">{format_american(p.get('price_american'))}</td>
          <td class="num mono">{(p.get('implied_prob') or 0)*100:.1f}%</td>
          <td class="num mono">{(p.get('projected_prob') or 0)*100:.1f}%</td>
          <td><span class="tag {tag_cls}">{meth}</span></td>
          <td class="num mono">{(p.get('risk_units') or 0):.2f}u</td>
          <td class="num mono">{(p.get('win_units') or 0):.2f}u</td>
          <td><span class="chip {status_cls}">{status_lbl}</span></td>
          <td class="num mono">{clv_s}</td>
        </tr>
        """)
        html("</tbody></table>")

    with st.expander("Pick notes & manual actions"):
        for p in sorted(rows, key=lambda x: x.get("created_at") or "", reverse=True):
            st.markdown(f"**{p.get('event_name') or '-'}** — {p.get('selection')} ({format_american(p.get('price_american'))})")
            if p.get("notes"):
                st.caption(p.get("notes"))
            c1, c2, c3, c4 = st.columns([2, 1, 1, 1])
            with c1:
                closing = st.number_input(
                    "Closing American price", value=float(p.get("closing_price_american") or p.get("price_american") or -110),
                    step=5.0, format="%.0f", key=f"close_{p.get('id')}",
                )
            if c2.button("Save CLV", key=f"savecl_{p.get('id')}"):
                picks_store.update_closing_price(p.get("id"), closing)
                st.cache_data.clear()
                st.success("CLV saved.")
            if c3.button("Mark void", key=f"void_{p.get('id')}"):
                p["status"] = "void"
                import json as _j, os as _o
                here = _o.path.dirname(_o.path.dirname(_o.path.abspath(__file__)))
                with open(_o.path.join(here, "picks.json"), "w", encoding="utf-8") as f:
                    _j.dump(picks_store.list_picks(), f, indent=2, default=str)
                # safer: just use delete+add
                picks_store.delete_pick(p.get("id"))
                picks_store.add_pick(p)
                st.cache_data.clear()
                st.success("Marked void.")
            if c4.button("Delete", key=f"del_{p.get('id')}"):
                picks_store.delete_pick(p.get("id"))
                st.cache_data.clear()
                st.success("Deleted.")
            st.divider()


# ============================================================================
# TAB - Results (high-confidence agent picks + whether they hit)
# ============================================================================
#
# User-driven view: which picks did the agent flag as higher-than-coinflip
# confidence (Pinnacle no-vig fair prob > 55%), and what was the hit rate?
# This is the "did the agent actually get it right" readout — intentionally
# separate from the Ledger so the signal isn't lost in the noise.

HIGH_CONF_THRESHOLD = 0.55

def render_results(settings: dict):
    brand_bar(f"Results · past 7 days · picks with > {int(HIGH_CONF_THRESHOLD*100)}% Pinnacle fair prob")

    # Backfill pick_date on legacy rows so the by-date grouping works.
    try:
        picks_store.backfill_pick_dates()
    except Exception:
        pass

    # Last 7 slate dates (Central), inclusive of today.
    from datetime import timedelta as _td
    from zoneinfo import ZoneInfo as _ZI
    today_c = datetime.now(_ZI("America/Chicago")).date()
    window_start = today_c - _td(days=6)  # 7 days including today
    window_dates = {(today_c - _td(days=n)).strftime("%Y-%m-%d") for n in range(7)}

    all_picks = picks_store.list_picks()
    high_conf = [
        p for p in all_picks
        if (p.get("source") or "manual") == "agent-auto"
        and float(p.get("projected_prob") or 0) > HIGH_CONF_THRESHOLD
        and (p.get("pick_date") or "")[:10] in window_dates
    ]
    if not high_conf:
        html(
            f'<div class="hint">No agent picks above the {int(HIGH_CONF_THRESHOLD*100)}% '
            'Pinnacle fair prob threshold in the past 7 days. The Results tab lights up once '
            'the agent surfaces high-confidence edges and they grade from ESPN. '
            f'Window: <strong>{window_start.strftime("%b %d")} – {today_c.strftime("%b %d, %Y")}</strong>.</div>'
        )
        return

    # Decide-only stats — opens don't count as results until they grade.
    decided = [p for p in high_conf if (p.get("status") or "open") in ("win", "loss")]
    wins = sum(1 for p in decided if p.get("status") == "win")
    losses = len(decided) - wins
    hit_rate = (wins / len(decided)) if decided else None
    pending = len(high_conf) - len(decided) - sum(1 for p in high_conf if p.get("status") == "push")
    # Expected win-rate if the Pinnacle fair prob is calibrated: average of
    # projected_probs across decided rows. Difference vs realized is the
    # calibration gap.
    exp = [float(p.get("projected_prob") or 0) for p in decided]
    exp_rate = (sum(exp) / len(exp)) if exp else None
    gap_pts = ((hit_rate - exp_rate) * 100) if (hit_rate is not None and exp_rate is not None) else None

    tone = "good" if (hit_rate or 0) >= (exp_rate or 0) else "bad"
    metric_row([
        {"label": f"Picks > {int(HIGH_CONF_THRESHOLD*100)}%", "value": str(len(high_conf)),
         "foot": f"{pending} open · {len(decided)} decided"},
        {"label": "Record", "value": f"{wins}-{losses}",
         "foot": f"n = {len(decided)} decided"},
        {"label": "Hit rate", "value": (f"{hit_rate*100:.1f}%" if hit_rate is not None else "—"),
         "tone": tone, "foot": "actual wins / decided"},
        {"label": "Expected", "value": (f"{exp_rate*100:.1f}%" if exp_rate is not None else "—"),
         "foot": "avg Pinn fair prob"},
        {"label": "Gap vs expected",
         "value": (f"{gap_pts:+.1f} pts" if gap_pts is not None else "—"),
         "tone": tone,
         "foot": "positive = agent beats fair"},
    ])

    # Per-day summary + drill-down (newest first).
    daily = picks_store.group_by_date(high_conf)
    st.markdown("### By slate date")
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo as _ZI
    today_c = datetime.now(_ZI("America/Chicago")).strftime("%Y-%m-%d")
    for day in daily:
        d = day["date"]
        try:
            pretty = _dt.strptime(d, "%Y-%m-%d").strftime("%A, %b %d, %Y")
        except ValueError:
            pretty = d
        items = day["picks"]
        d_wins = sum(1 for p in items if p.get("status") == "win")
        d_losses = sum(1 for p in items if p.get("status") == "loss")
        d_open = sum(1 for p in items if (p.get("status") or "open") == "open")
        d_hit = (d_wins / (d_wins + d_losses)) if (d_wins + d_losses) else None
        hit_disp = f" · hit {d_hit*100:.0f}%" if d_hit is not None else ""
        is_today = (d == today_c)
        header = (
            f"{pretty} · {len(items)} high-conf picks · "
            f"{d_wins}-{d_losses}{(' · ' + str(d_open) + ' open') if d_open else ''}"
            f"{hit_disp}"
        )
        with st.expander(header, expanded=is_today):
            # Table: event, side, fair prob, price, status with hit highlight.
            rows_html = ""
            for p in sorted(items, key=lambda x: -float(x.get("projected_prob") or 0)):
                status = p.get("status") or "open"
                fair = float(p.get("projected_prob") or 0)
                proj_tone = "good" if fair >= 0.65 else "accent"
                if status == "win":
                    result_chip = '<span class="chip good" style="padding:2px 9px;">✓ HIT</span>'
                    row_bg = ' style="background:color-mix(in oklab, var(--good) 8%, transparent);"'
                elif status == "loss":
                    result_chip = '<span class="chip bad" style="padding:2px 9px;">✗ MISS</span>'
                    row_bg = ' style="background:color-mix(in oklab, var(--bad) 8%, transparent);"'
                elif status == "push":
                    result_chip = '<span class="chip warn" style="padding:2px 9px;">= PUSH</span>'
                    row_bg = ""
                else:
                    result_chip = '<span class="chip" style="padding:2px 9px;">· OPEN</span>'
                    row_bg = ""
                mk = (p.get("market") or "h2h").upper()
                sport = (p.get("sport") or "").upper()
                side = p.get("selection") or "—"
                pt = p.get("point")
                if pt is not None and pt != 0 and " " not in str(side):
                    try:
                        side = f"{side} {float(pt):+g}"
                    except (TypeError, ValueError):
                        pass
                rows_html += (
                    f'<tr{row_bg}>'
                    f'<td class="mono" style="color:var(--muted); font-size:11px;">{sport}</td>'
                    f'<td>{p.get("event_name") or "—"}</td>'
                    f'<td class="mono" style="color:var(--muted); font-size:11px;">{mk}</td>'
                    f'<td><strong>{side}</strong></td>'
                    f'<td class="num mono {proj_tone}"><strong>{fair*100:.1f}%</strong></td>'
                    f'<td class="num mono">{format_american(p.get("price_american"))}</td>'
                    f'<td>{result_chip}</td>'
                    f'</tr>'
                )
            html(
                '<table class="ledger" style="margin-top:4px;">'
                '<thead><tr>'
                '<th>Sport</th><th>Event</th><th>Market</th><th>Pick</th>'
                '<th class="num">Pinn fair</th>'
                '<th class="num">Price</th>'
                '<th>Result</th>'
                '</tr></thead>'
                f'<tbody>{rows_html}</tbody></table>'
            )

    # One-line interpretive footer.
    if hit_rate is not None and exp_rate is not None:
        if gap_pts is not None and gap_pts >= 0:
            interp = (
                f"Agent is beating its own fair-prob estimate by "
                f"<strong class='good'>{gap_pts:+.1f} pts</strong> — "
                "picks above 55% confidence are converting at or above their expected rate."
            )
        else:
            interp = (
                f"Agent is running <strong class='bad'>{gap_pts:+.1f} pts</strong> below "
                "its fair-prob estimate — small samples bounce, but watch this gap as n grows."
            )
        html(f'<div class="hint" style="margin-top:14px;">{interp}</div>')


# ============================================================================
# TAB - Performance (equity curve + drawdown + withdrawal hints)
# ============================================================================
#
# Ported from V1 edgeboard's "Bankroll & Performance" page. Adapted to v2's
# JSON picks ledger (no SQLAlchemy models here) and unit-based accounting.

def render_performance(settings: dict):
    brand_bar("Performance · equity curve + drawdown + withdrawals")

    picks = picks_store.list_picks()
    settled = [p for p in picks if (p.get("status") or "open") in ("win", "loss", "push")]
    if not settled:
        html(
            '<div class="hint">No settled picks yet. Log a few from the Dashboard and come back once '
            'ESPN auto-settles them — the equity curve, high-water mark, and withdrawal suggestions '
            'need realized P&L to work from.</div>'
        )
        return

    # Build an in-memory equity series. Starts at the configured bankroll
    # (Kalshi cash) and compounds settled P&L in dollars so the curve matches
    # what the user actually has in their account.
    kalshi_cash = float(settings.get("kalshi_cash") or 0) or 100.0
    unit_pct = settings.get("unit_pct") or 0.01
    unit_dollars = kalshi_cash * unit_pct
    start_bank = kalshi_cash
    settled_sorted = sorted(
        settled, key=lambda p: p.get("created_at") or "",
    )
    equity = start_bank
    rows = [{"when": "start", "equity": start_bank, "pl": 0.0}]
    for p in settled_sorted:
        status = p.get("status")
        risk_u = float(p.get("risk_units") or 0)
        win_u = float(p.get("win_units") or 0)
        if status == "win":
            pl = win_u * unit_dollars
        elif status == "loss":
            pl = -risk_u * unit_dollars
        else:
            pl = 0.0
        equity += pl
        rows.append({"when": p.get("created_at") or "", "equity": equity, "pl": pl})

    hwm = max(r["equity"] for r in rows)
    cur = rows[-1]["equity"]
    dd = (cur - hwm) / hwm if hwm > 0 else 0.0
    net = cur - start_bank
    roi = net / start_bank if start_bank > 0 else 0.0
    tone = "good" if net > 0 else ("bad" if net < 0 else "")

    metric_row([
        {"label": "Starting", "value": f"${start_bank:,.2f}", "foot": "Kalshi cash at session start"},
        {"label": "Current", "value": f"${cur:,.2f}", "tone": tone, "foot": f"net {net:+,.2f}"},
        {"label": "ROI", "value": f"{roi*100:+.2f}%", "tone": tone},
        {"label": "High-water", "value": f"${hwm:,.2f}", "foot": "best balance to date"},
        {"label": "Drawdown", "value": f"{dd*100:.1f}%", "tone": "warn" if dd < -0.05 else "",
         "foot": "from HWM"},
        {"label": "Settled picks", "value": str(len(settled))},
    ])

    # Equity curve.
    try:
        import pandas as _pd
        df = _pd.DataFrame(rows)
        if len(df) > 1:
            st.markdown("### Equity curve")
            # Use the row index for x-axis so uneven timestamps don't warp the chart.
            st.line_chart(df["equity"], height=260)
    except ImportError:
        pass

    # Rolling CLV — kept simple (mean of the last 50 CLV% observations), which
    # is the quick-glance signal V1 surfaced; the deeper t-test lives on the
    # Calibration tab.
    clv_vals = [float(p.get("clv_pct")) for p in picks if p.get("clv_pct") is not None]
    if clv_vals:
        try:
            import pandas as _pd
            s = _pd.Series(clv_vals).rolling(min(50, len(clv_vals)), min_periods=1).mean()
            st.markdown("### Rolling CLV (last 50)")
            st.line_chart(s, height=200)
            avg_clv = sum(clv_vals) / len(clv_vals)
            beats = sum(1 for v in clv_vals if v > 0) / len(clv_vals)
            metric_row([
                {"label": "Mean CLV%", "value": f"{avg_clv*100:+.2f}%",
                 "tone": "good" if avg_clv > 0 else "bad"},
                {"label": "Beat-close rate", "value": f"{beats*100:.1f}%",
                 "foot": f"n={len(clv_vals)}",
                 "tone": "good" if beats > 0.5 else ""},
            ])
        except ImportError:
            pass

    # Withdrawal suggestions — V1's "sweep 50% of profit above the prior HWM"
    # rule. Only kicks in when the current balance is new all-time-high and the
    # dollar amount is meaningful.
    st.markdown("### Withdrawal suggestions")
    prior_hwm = max((r["equity"] for r in rows[:-1]), default=start_bank) if len(rows) > 1 else start_bank
    profit_above_hwm = max(0.0, cur - prior_hwm)
    monthly = profit_above_hwm * 0.5
    if monthly >= 1.0:
        html(
            '<div class="hint" style="background:color-mix(in oklab, var(--good) 10%, var(--card));">'
            f'<strong>Suggested monthly sweep:</strong> ${monthly:,.2f} '
            f'(50% of profit above the prior HWM of ${prior_hwm:,.2f}).</div>'
        )
    else:
        html(
            '<div class="hint">No sweep suggested — you are not above a prior high-water mark, '
            'so leaving every dollar in the bankroll is the right call.</div>'
        )
    cushion_ok = cur >= prior_hwm * 1.10
    if cushion_ok:
        html(
            '<div class="hint">Weekly sweep is also safe — balance is at least 10% above the prior '
            'HWM, so there is cushion for variance.</div>'
        )
    else:
        html(
            '<div class="hint" style="color:var(--muted);">Weekly sweep locked until the balance '
            'clears +10% above the prior HWM.</div>'
        )


# ============================================================================
# TAB - Calibration (reliability + Brier + CLV t-test)
# ============================================================================
#
# Ported from V1 edgeboard's "Calibration" page. Simplified for the JSON ledger.

def render_calibration(settings: dict):
    brand_bar("Calibration · reliability, Brier, CLV t-test")

    picks = picks_store.list_picks()
    settled = [
        p for p in picks
        if (p.get("status") or "open") in ("win", "loss")
        and p.get("projected_prob") is not None
    ]
    if not settled:
        html(
            '<div class="hint">Calibration needs settled wins/losses with a projected probability '
            'on each pick. Log picks from the Dashboard (projected prob is captured automatically for '
            'agent picks) and let ESPN auto-settle them.</div>'
        )
        return

    # Reliability — bucket by projected probability.
    try:
        import pandas as _pd
        data = []
        for p in settled:
            data.append({
                "proj": float(p.get("projected_prob") or 0),
                "won": 1 if (p.get("status") == "win") else 0,
                "edge": (float(p.get("projected_prob") or 0) - float(p.get("implied_prob") or 0)),
                "clv": float(p.get("clv_pct") or 0) if p.get("clv_pct") is not None else None,
            })
        df = _pd.DataFrame(data)
        st.markdown("### Reliability — projected probability vs. realized win rate")
        bins = [0.0, 0.3, 0.4, 0.5, 0.6, 0.7, 1.0]
        df["bucket"] = _pd.cut(df["proj"], bins=bins, include_lowest=True)
        rel = df.groupby("bucket", observed=True).agg(
            n=("won", "count"),
            realized=("won", "mean"),
            expected=("proj", "mean"),
        ).reset_index()
        rel["gap_pts"] = (rel["realized"] - rel["expected"]) * 100
        if rel.empty:
            st.caption("Not enough settled picks across the probability buckets yet.")
        else:
            # Render as a lightweight HTML table so the formatting matches the
            # rest of the dashboard rather than Streamlit's default dataframe.
            rows_html = ""
            for _, r in rel.iterrows():
                tone = "good" if r["gap_pts"] >= 0 else "bad"
                rows_html += (
                    f'<tr>'
                    f'<td class="mono">{r["bucket"]}</td>'
                    f'<td class="num mono">{int(r["n"])}</td>'
                    f'<td class="num mono">{r["expected"]*100:.1f}%</td>'
                    f'<td class="num mono">{r["realized"]*100:.1f}%</td>'
                    f'<td class="num mono {tone}">{r["gap_pts"]:+.1f}</td>'
                    f'</tr>'
                )
            html(
                '<table class="ledger" style="margin-top:6px;">'
                '<thead><tr><th>Projected</th><th class="num">n</th>'
                '<th class="num">Expected win %</th><th class="num">Realized win %</th>'
                '<th class="num">Δ (pts)</th></tr></thead>'
                f'<tbody>{rows_html}</tbody></table>'
            )
        # Brier — the standard calibration cost. 0 is perfect; a fair coin would score 0.25.
        brier = ((df["proj"] - df["won"]) ** 2).mean()
        st.markdown("### Scoring")
        tone = "good" if brier < 0.24 else ("warn" if brier < 0.26 else "bad")
        metric_row([
            {"label": "Brier score", "value": f"{brier:.4f}",
             "tone": tone, "foot": "lower is better · fair coin = 0.2500"},
            {"label": "Settled picks", "value": str(len(settled))},
            {"label": "Mean edge", "value": f"{df['edge'].mean()*100:+.2f} pts",
             "foot": "projected − implied"},
        ])
    except ImportError:
        html('<div class="hint err">pandas not available — install pandas to see calibration charts.</div>')
        return

    # CLV t-test — V1's go/no-go for taking the agent live. Need the picks to
    # carry a closing_price so clv_pct is populated; prompt the user otherwise.
    st.markdown("### CLV t-test — go/no-go")
    clv_vals = df["clv"].dropna().tolist()
    n = len(clv_vals)
    if n < 2:
        html(
            '<div class="hint">Need at least 2 picks with a recorded closing price to run a t-test. '
            'Add closing prices in the Picks Ledger (expand a pick → "Save CLV").</div>'
        )
    else:
        import math, statistics
        m = statistics.mean(clv_vals)
        sd = statistics.stdev(clv_vals) if n > 1 else 0.0
        t = (m / (sd / math.sqrt(n))) if sd > 0 else 0.0
        passes = (n >= 300) and (t > 1.65)
        tone = "good" if passes else "warn"
        metric_row([
            {"label": "n observations", "value": str(n),
             "foot": "need ≥ 300 for the gate"},
            {"label": "Mean CLV%", "value": f"{m*100:+.2f}%",
             "tone": "good" if m > 0 else "bad"},
            {"label": "t-stat", "value": f"{t:.2f}",
             "foot": "need > 1.65 (one-sided, p < 0.05)",
             "tone": tone},
        ])
        if passes:
            html(
                '<div class="hint" style="background:color-mix(in oklab, var(--good) 10%, var(--card));">'
                '<strong>CLV gate passes.</strong> The agent is beating closing-line value with '
                'statistical significance — safe to size up (keep stakes inside your Kelly / cap limits).</div>'
            )
        else:
            html(
                '<div class="hint warn"><strong>CLV gate fails.</strong> Keep sizing conservative '
                'until you have ≥ 300 observations and a one-sided t-stat above 1.65 — that is the '
                'standard sharp threshold for proving edge rather than variance.</div>'
            )


# ============================================================================
# TAB 4 - Settings
# ============================================================================

def render_settings(settings: dict):
    brand_bar("Settings · configuration & API usage")
    saved = settings_store.load()

    # ----- Credit meter -----
    st.markdown("### Odds API usage")
    credits = odds_api.get_credit_status()
    remaining = credits.get("remaining")
    used = credits.get("used_total")
    last = credits.get("last_used")

    budget = 20_000
    pct_used = None
    if remaining is not None:
        try:
            pct_used = max(0.0, min(1.0, 1.0 - (int(remaining) / budget)))
        except (TypeError, ValueError):
            pct_used = None

    cards = [
        {"label": "Remaining credits", "value": str(remaining) if remaining is not None else "—",
         "foot": "20k monthly budget", "tone": "good" if (remaining or 0) > 2000 else ("warn" if (remaining or 0) > 500 else "bad")},
        {"label": "Last request", "value": str(last) if last is not None else "—",
         "foot": "cost of last call"},
        {"label": "Total used", "value": str(used) if used is not None else "—",
         "foot": "this billing cycle"},
    ]
    if pct_used is not None:
        cards.append({"label": "% used", "value": f"{pct_used*100:.1f}%", "tone": "warn" if pct_used > 0.75 else ""})
    metric_row(cards)

    cur_regions = saved.get("odds_regions") or "eu"
    cur_markets = saved.get("odds_markets") or "h2h"
    cur_ttl = int(saved.get("odds_live_ttl") or 3600)
    cost_per_fetch = odds_api.estimated_credit_cost(cur_regions, cur_markets)
    st.caption(
        f"Current cost: **{cost_per_fetch} credits** per sport per fetch "
        f"(regions=`{cur_regions}` · markets=`{cur_markets}` · cache={cur_ttl//60} min). "
        f"On-disk cache at `.odds_cache/` — historical snapshots cached permanently."
    )
    if cost_per_fetch > 1:
        html(
            '<div class="hint warn" style="margin-top:6px;">'
            '<strong>Credit tip:</strong> set regions to <code>eu</code> and markets to <code>h2h</code> '
            'to make each sport cost <strong>1 credit</strong>. Pinnacle lives in <code>eu</code> '
            'and is all the agent needs for fair-prob. You bet on <strong>Kalshi</strong> (free), '
            'so extra US books are optional.'
            '</div>'
        )

    # ----- Live Kalshi balance -----
    st.markdown("### Kalshi")
    balance = load_kalshi_balance() or {"usd": None, "source": None}
    live_usd = balance.get("usd")
    live_src = balance.get("source")
    if live_src == "live":
        html(f'<div class="hint" style="background:color-mix(in oklab, var(--good) 10%, var(--card));">'
             f'<strong>Live balance:</strong> ${live_usd:,.2f} · source: Kalshi API</div>')
    elif live_src == "manual":
        html(f'<div class="hint warn"><strong>Manual balance:</strong> ${live_usd:,.2f} · '
             f'connect the API in the sidebar for live.</div>')
    else:
        html('<div class="hint warn">Not connected. Enter credentials in the sidebar.</div>')

    unit_dollars = settings.get("unit_dollars") or 0.0
    unit_pct = settings.get("unit_pct") or 0.10
    cash = settings.get("kalshi_cash") or 0.0
    metric_row([
        {"label": "Kalshi cash", "value": f"${cash:,.2f}", "foot": settings.get("kalshi_source") or "unknown"},
        {"label": "1 unit", "value": f"${unit_dollars:,.2f}", "foot": f"{unit_pct*100:.1f}% of cash", "tone": "accent"},
        {"label": "Bankroll (units)", "value": f"{settings.get('bankroll_units') or 0:.1f}u"},
    ])

    # ----- Agent reference -----
    st.markdown("### Agent configuration")
    primary = settings.get("primary_book") or "kalshi"
    fallback = saved.get("fallback_book") or "DraftKings"
    html(
        f'<div class="hint">'
        f'<strong>Fair-probability source (in order):</strong> '
        f'1) Pinnacle no-vig (the sharpest book — best proxy for "true" probability), '
        f'2) Kalshi YES-mid (exchange, no vig, moneyline only), '
        f'3) <strong>{fallback}</strong> no-vig, '
        f'4) multi-book consensus no-vig. '
        f'The agent then scans every bookmaker <em>and Kalshi</em> for prices that imply '
        f'a probability lower than that fair prob. '
        f'An edge of +2 pts means the fair prob is 2 percentage points above what that book is pricing.'
        f'</div>'
    )

    # Allow configuration here too (duplicates sidebar for permanence).
    c1, c2, c3 = st.columns(3)
    with c1:
        new_fallback = st.selectbox(
            "Fallback book (when Pinnacle & Kalshi both miss)",
            options=["DraftKings", "FanDuel", "BetMGM", "Caesars"],
            index=["DraftKings","FanDuel","BetMGM","Caesars"].index(fallback) if fallback in ["DraftKings","FanDuel","BetMGM","Caesars"] else 0,
            key="set_fallback_book",
        )
        if new_fallback != fallback:
            settings_store.save({"fallback_book": new_fallback})
            st.success(f"Fallback book saved: {new_fallback}")
    with c2:
        current_regions = saved.get("odds_regions") or "us,eu"
        region_options = ["us,eu", "eu", "us", "us,us2,eu", "us,eu,uk,au"]
        new_regions = st.selectbox(
            "Odds API regions",
            options=region_options,
            index=region_options.index(current_regions) if current_regions in region_options else 0,
            key="set_regions",
            help="us,eu (recommended) = Pinnacle fair + offshore shop books (Bovada/LowVig/BetOnline), "
                 "2 credits per sport. eu alone = Pinnacle only (1 credit), smaller edges because you "
                 "can only bet on Kalshi.",
        )
        if new_regions != current_regions:
            settings_store.save({"odds_regions": new_regions})
            st.cache_data.clear()
            n = len(new_regions.split(","))
            st.success(f"Regions: {new_regions} → {n * len(cur_markets.split(','))} credits per sport per fetch.")

    # Markets picker — split into two groups by API endpoint, because the two
    # cost VERY differently. Batch markets cost regions*markets credits per
    # SPORT. Alt markets cost regions*markets credits per EVENT - 10-100x more.
    st.markdown("### Markets to scan")
    st.caption(
        "**Batch endpoint** markets cost regions × markets credits per sport "
        "fetch. **Per-event** markets (1H lines, BTTS) cost that much per event "
        "— 10-100× more. Only enable per-event markets if your plan can afford it."
    )
    batch_markets = ["h2h", "totals", "spreads"]
    alt_markets_opts = ["totals_h1", "spreads_h1", "h2h_h1", "btts"]
    labels = {
        "h2h": "Moneyline (h2h) — required for Kalshi",
        "totals": "Full-game totals (O/U)",
        "spreads": "Point spreads",
        "h2h_h1": "1st-half moneyline",
        "totals_h1": "1st-half totals (O/U)",
        "spreads_h1": "1st-half spread",
        "btts": "Both teams to score (soccer only)",
    }
    cur_markets_list = [m.strip() for m in (saved.get("odds_markets") or "h2h").split(",") if m.strip()]
    # Batch picker — the standard /odds endpoint.
    new_markets = st.multiselect(
        "Batch markets (`/odds` endpoint)",
        options=batch_markets,
        default=[m for m in cur_markets_list if m in batch_markets],
        format_func=lambda m: labels.get(m, m),
        key="set_markets",
    )
    if "h2h" not in new_markets:
        new_markets = ["h2h"] + new_markets  # always keep h2h for Kalshi
    new_markets_str = ",".join(new_markets)
    n_regions = len(cur_regions.split(","))
    n_markets = len(new_markets)
    new_cost = n_regions * n_markets
    st.caption(
        f"Batch cost: **{new_cost} credits per sport per fetch** "
        f"({n_regions} regions × {n_markets} markets)."
    )
    if new_markets_str != (saved.get("odds_markets") or "h2h"):
        settings_store.save({"odds_markets": new_markets_str})
        st.cache_data.clear()
        st.success(f"Batch markets: {new_markets_str}")

    # Per-event picker — opt-in because of credit cost.
    cur_alt = (saved.get("odds_alt_markets") or "").strip()
    cur_alt_list = [m.strip() for m in cur_alt.split(",") if m.strip()] if cur_alt else []
    new_alt = st.multiselect(
        "Alt markets (`/events/{id}/odds` endpoint) — one call per event",
        options=alt_markets_opts,
        default=cur_alt_list,
        format_func=lambda m: labels.get(m, m),
        key="set_alt_markets",
        help="These markets are served ONLY by the per-event endpoint. Each "
             "selection adds 1 credit per region per event. Cache TTL is 6h.",
    )
    new_alt_str = ",".join(new_alt)
    # Allowlist of sports the per-event fetcher runs on - only shown when alt
    # markets are enabled, because it's a credit guard, not a feature.
    if new_alt:
        all_sport_keys = sorted(espn.ODDS_API_SPORT_KEYS.keys())
        display_names = espn.DISPLAY_NAMES if hasattr(espn, "DISPLAY_NAMES") else {}
        cur_alt_sports = list(saved.get("odds_alt_sports") or [])
        new_alt_sports = st.multiselect(
            "Alt-markets allowlist (sports)",
            options=all_sport_keys,
            default=cur_alt_sports,
            format_func=lambda s: display_names.get(s, s.upper()),
            key="set_alt_sports",
            help="ONLY these sports hit the per-event endpoint for alt markets. "
                 "Leaving this empty runs it on EVERY sport with games today "
                 "— credit blowup risk. Start with 2-3 sports.",
        )
        if new_alt_sports != cur_alt_sports:
            settings_store.save({"odds_alt_sports": new_alt_sports})
            st.cache_data.clear()
        _alt_sports_for_cost = new_alt_sports or ["(all applicable sports)"]
        st.caption(
            f"Alt cost: **{n_regions * len(new_alt)} credits per event per refresh**, "
            f"on {len(_alt_sports_for_cost)} sport(s). "
            f"A 20-event slate = {n_regions * len(new_alt) * 20} credits per refresh. "
            f"6h cache keeps it to ~4 refreshes/day."
        )
    if new_alt_str != cur_alt:
        settings_store.save({"odds_alt_markets": new_alt_str})
        st.cache_data.clear()
        if new_alt_str:
            st.success(f"Alt markets enabled: {new_alt_str}")
        else:
            st.info("Alt markets disabled.")
    with c3:
        if st.button("Clear Odds API disk cache"):
            import shutil, os
            here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            cache = os.path.join(here, ".odds_cache")
            try:
                shutil.rmtree(cache, ignore_errors=True)
                st.cache_data.clear()
                st.success("Disk cache cleared. Next refresh will re-fetch.")
            except Exception as e:
                st.error(str(e))

    # ----- Reset -----
    st.markdown("### Reset")
    rc1, rc2 = st.columns([1, 3])
    with rc1:
        if st.button("Reset to recommended defaults", use_container_width=True, type="primary"):
            import os
            try:
                path = os.path.join(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "settings.json",
                )
                if os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass
            # Clear sidebar session_state so sliders snap back to defaults.
            for k in list(st.session_state.keys()):
                if k.startswith("sb_") or k.startswith("set_") or k.startswith("agent_f_"):
                    del st.session_state[k]
            st.cache_data.clear()
            st.success("Settings reset. Reloading…")
            st.rerun()
    with rc2:
        st.caption(
            "This clears your saved settings.json and resets to the credit-efficient defaults: "
            "regions=eu, markets=h2h, cache=60min, min-edge=1%, unit=10% of Kalshi cash, "
            "daily cap=25%, theme=dark. "
            "Does **not** clear your API keys or Kalshi connection."
        )

    # ----- Discord integration -----
    st.markdown("### Discord integration")
    cur_hook = discord_api.get_webhook_url() or ""
    cur_site = discord_api.get_site_url() or ""
    masked = (cur_hook[:48] + "…") if cur_hook else ""
    dc1, dc2 = st.columns([3, 1])
    with dc1:
        hook_in = st.text_input(
            "Webhook URL",
            value=cur_hook,
            type="password",
            placeholder="https://discord.com/api/webhooks/...",
            help="Server Settings → Integrations → Webhooks → New Webhook → "
                 "Copy URL. Stored in .streamlit/secrets.toml (gitignored).",
            key="set_discord_hook",
        )
    with dc2:
        c_save, c_test = st.columns(2)
        if c_save.button("Save", use_container_width=True, key="set_discord_save"):
            if discord_api.save_webhook_url(hook_in or ""):
                st.cache_data.clear()
                st.success("Webhook " + ("saved." if hook_in else "cleared."))
                st.rerun()
            else:
                st.error("Could not write secrets.toml")
        if c_test.button("Test", use_container_width=True, key="set_discord_test"):
            if not (hook_in or cur_hook):
                st.warning("Enter a webhook URL first.")
            else:
                probe = [{
                    "_sport": "TEST", "matchup": "Edge agent · reachability probe",
                    "side_label": "Webhook ping (synthetic +EV row)",
                    "fair_prob": 0.56, "edge": 0.015, "price": -115, "stake_dollars": 10.0,
                }]
                r = discord_api.send_board_snapshot(
                    probe, unit_dollars=10.0,
                    webhook_url=(hook_in or cur_hook),
                    site_url=(st.session_state.get("_pending_site_url") or cur_site) or None,
                    sports_label="test",
                )
                if r.get("ok"):
                    st.success("Test post landed in Discord.")
                else:
                    st.error(f"Test failed: {r.get('error') or 'unknown'}")
    # Site URL (link included in Discord messages)
    site_in = st.text_input(
        "Dashboard URL (link included in Discord posts)",
        value=cur_site,
        placeholder="https://your-edge-v2.streamlit.app",
        help="The live URL of this dashboard (your Streamlit Cloud deployment). "
             "Added to every Discord snapshot so viewers can open the board.",
        key="set_site_url",
    )
    # Remember pending value for the Test button (before Save).
    st.session_state["_pending_site_url"] = site_in
    if site_in != cur_site:
        if st.button("Save dashboard URL", key="set_site_save"):
            if discord_api.save_site_url(site_in or ""):
                st.success("Dashboard URL " + ("saved." if site_in else "cleared."))
                st.rerun()
            else:
                st.error("Could not write secrets.toml")
    if cur_hook:
        st.caption(f"Current webhook (masked): `{masked}`")
    st.caption(
        "The webhook is write-only — Discord can't talk back. The Board's "
        "**📤 Send to Discord** button posts a **PNG snapshot of the actionable (+EV) "
        "picks** with a Central-US timestamp, counts of watch-list / extra +EV picks "
        "still on the board, and a link back to the live dashboard."
    )

    # ----- Live score auto-refresh -----
    st.markdown("### Live score auto-refresh")
    lr1, lr2 = st.columns([1, 3])
    with lr1:
        cur_lr = int(saved.get("live_refresh_sec", 30) or 0)
        lr_options = [0, 15, 30, 60, 120]
        lr_pick = st.select_slider(
            "Refresh interval",
            options=lr_options,
            value=cur_lr if cur_lr in lr_options else 30,
            format_func=lambda v: "Off" if v == 0 else f"{v}s",
            key="set_live_refresh",
        )
        if lr_pick != cur_lr:
            settings_store.save({"live_refresh_sec": int(lr_pick)})
            st.success(f"Live refresh: {'OFF' if lr_pick == 0 else str(lr_pick)+'s'}.")
    with lr2:
        st.caption(
            "Rerun the Dashboard automatically to pull fresh ESPN live scores "
            "while the tab is open. Only polls while at least one game on the "
            "slate is in-progress — outside those windows the tab is idle. "
            "ESPN is free; Odds API responses stay cached, so this costs 0 credits."
        )

    # ----- Agent auto-logging -----
    st.markdown("### Agent auto-log")
    al1, al2 = st.columns([1, 3])
    with al1:
        auto_log = st.toggle(
            "Record every funded edge",
            value=bool(saved.get("agent_auto_log", True)),
            key="set_auto_log",
            help="On: every funded pick the agent surfaces is written to the "
                 "Picks Ledger automatically (dedup-aware) and settled from "
                 "ESPN. Off: ledger stays manual — only picks you click Log "
                 "on are recorded.",
        )
        if auto_log != bool(saved.get("agent_auto_log", True)):
            settings_store.save({"agent_auto_log": bool(auto_log)})
            st.cache_data.clear()
            st.success(f"Auto-log is now {'ON' if auto_log else 'OFF'}.")
    with al2:
        st.caption(
            "The ledger (and Performance / Calibration tabs) only know what the agent did "
            "if its picks were saved. Auto-log keeps that record for you — dedup is on sport × "
            "ESPN event × market × side, so re-renders don't double-write. "
            "Settle from ESPN finals runs after each log."
        )

    # ----- Theme + exposure cap -----
    st.markdown("### Appearance & risk")
    tc1, tc2 = st.columns(2)
    with tc1:
        theme = st.selectbox(
            "Theme", options=["dark", "light"],
            index=0 if saved.get("theme", "dark") == "dark" else 1,
            format_func=lambda t: t.title(),
            key="set_theme",
        )
        if theme != saved.get("theme", "dark"):
            settings_store.save({"theme": theme})
            st.success(f"Theme set to {theme}. Reloading…")
            st.rerun()
    with tc2:
        cap = st.slider(
            "Daily total-exposure cap (% of Kalshi cash)",
            5, 100, int(saved.get("daily_exposure_cap_pct", 0.25) * 100), 5,
            key="set_cap",
            help="If the agent's total recommended stakes exceed this % of your "
                 "Kalshi cash, every stake is scaled down proportionally.",
        ) / 100.0
        if abs(cap - saved.get("daily_exposure_cap_pct", 0.25)) > 1e-6:
            settings_store.save({"daily_exposure_cap_pct": cap})
            st.success(f"Daily cap: {cap*100:.0f}% of Kalshi cash")

    # ----- Explanations -----
    st.markdown("### What the columns mean")
    html(
        '<div class="hint">'
        '<strong>Fair IP</strong> (fair implied probability) = the probability the agent considers '
        '"true" for a given side. It comes from <strong>Pinnacle no-vig</strong> first — Pinnacle is '
        'the sharpest book in the industry; stripping its small vig gives the tightest proxy for the '
        'market\'s true probability. If Pinnacle isn\'t in the feed for a game, it falls to '
        'Kalshi (exchange, moneyline only), then your fallback book, then consensus. '
        '<br><br><strong>Book IP</strong> = the implied probability a specific sportsbook — '
        'including Kalshi — is offering (vig-inclusive for sportsbooks; raw price for Kalshi). '
        '<br><strong>Edge (pts)</strong> = Fair IP − Book IP, in percentage points. '
        'A +4 pts edge means the sharp consensus considers the outcome 4 points more likely than '
        'this book is pricing it, so you expect long-run profit at that price.'
        '</div>'
    )

    # ----- Access from phone / away from home -----
    st.markdown("### Access from phone or anywhere")
    html(
        '<div class="hint">'
        '<strong>Yes — this works from your phone, anywhere with internet.</strong> '
        'The app is deployed on Streamlit Cloud, which gives you a public URL '
        '(for this repo it looks like <code>edge-v2-xxxx.streamlit.app</code>). '
        'Open that URL in Safari / Chrome on your phone and you get the same '
        'board, auto-refreshing live scores and all. Add it to your home screen '
        '(Share → Add to Home Screen) and it behaves like a native app.'
        '<br><br>'
        '<strong>What travels with you:</strong> every setting (sport group, '
        'Kelly multiplier, auto-log preference, Kalshi credentials) is server-side '
        'in the Streamlit container, so your phone sees the same state as your laptop. '
        'Picks you log on one device appear on the other immediately.'
        '<br><br>'
        '<strong>Credit guard on mobile:</strong> the auto-refresh only runs while '
        'you have at least one live game AND the tab is visible — closing the tab '
        'on your phone stops the polling, so you can\'t accidentally burn Odds-API '
        'credits in your pocket.'
        '</div>'
    )

    # ----- Saved settings dump -----
    with st.expander("Current saved settings (JSON on disk)"):
        st.json(saved)


# ============================================================================
# TAB 5 - Backtest
# ============================================================================

def render_backtest(settings: dict):
    brand_bar("Backtest · 30-day simulated performance")

    all_sports = ["nfl","nba","mlb","nhl","ncaaf","ncaab","wnba","mls"]
    c1, c2, c3, c4 = st.columns([2, 1, 1, 1])
    with c1:
        picked_sports = st.multiselect(
            "Sports to include",
            options=all_sports,
            default=["nfl", "nba", "mlb", "nhl"],
            format_func=lambda k: espn.DISPLAY_NAMES.get(k, k.upper()),
            key="bt_sports",
        )
    with c2:
        start_bankroll = st.number_input("Starting bankroll ($)", 10.0, 100000.0, 100.0, 10.0, key="bt_bank")
    with c3:
        days = st.number_input("Days back", 1, 90, 30, 1, key="bt_days")
    with c4:
        top_n = st.number_input("Top-N picks/day", 1, 30, 5, 1, key="bt_topn")

    # ----- credit preflight -----
    bt_regions = settings_store.load().get("odds_regions", "us,eu")
    cost_per_snapshot = odds_history.estimated_credit_cost_per_snapshot(bt_regions, "h2h,spreads,totals")
    est_requests = len(picked_sports) * int(days)
    est_credits = est_requests * cost_per_snapshot
    credit_status = odds_api.get_credit_status()
    remaining = credit_status.get("remaining")

    # Cached snapshots don't re-charge; estimate assumes everything is new.
    tone = "warn" if (remaining is not None and est_credits > remaining * 0.5) else ""
    if remaining is not None and est_credits > remaining:
        tone = "bad"

    metric_row([
        {"label": "Est. credits", "value": str(est_credits),
         "foot": f"{est_requests} snapshots × {cost_per_snapshot} ea", "tone": tone},
        {"label": "Remaining budget", "value": str(remaining) if remaining is not None else "—"},
        {"label": "Cached snapshots", "value": _count_cached_snapshots(),
         "foot": "cost 0 on reruns"},
    ])
    html(
        '<div class="hint warn">Historical odds cost <strong>10× live odds</strong> per snapshot '
        '(us × h2h,spreads,totals = <strong>30 credits</strong> each). '
        'We cache every snapshot to disk permanently, so re-running this backtest later costs 0 credits. '
        'Still, keep <strong>sports × days × 30</strong> comfortably under your remaining budget before running.</div>'
    )

    if st.button("▶ Run backtest", type="primary", disabled=not picked_sports):
        progress = st.progress(0, text="starting…")
        def _cb(frac, msg):
            progress.progress(min(1.0, max(0.0, frac)), text=msg)
        result = backtest_engine.run(
            sports=picked_sports,
            start_bankroll=float(start_bankroll),
            days=int(days),
            top_n_per_day=int(top_n),
            kelly_mult=settings.get("agent_kelly") or 0.25,
            min_edge_pct=settings.get("agent_min_edge") or 0.02,
            max_bet_pct=settings.get("agent_max_bet_pct") or 0.10,
            fallback_book=settings.get("fallback_book") or "DraftKings",
            progress_cb=_cb,
        )
        progress.empty()
        st.session_state["bt_last_result"] = result

    result = st.session_state.get("bt_last_result")
    if not result:
        return

    # Render summary
    for w in (result.warnings or []):
        html(f'<div class="hint err">{w}</div>')

    net = result.end_bankroll - result.start_bankroll
    wins = sum(1 for p in result.picks if p.outcome == "win")
    losses = sum(1 for p in result.picks if p.outcome == "loss")
    pushes = sum(1 for p in result.picks if p.outcome == "push")
    ungraded = sum(1 for p in result.picks if p.outcome in ("nograde", "pending"))
    roi = (net / result.start_bankroll) if result.start_bankroll else None
    tone = "good" if net > 0 else ("bad" if net < 0 else "")

    metric_row([
        {"label": "Starting", "value": f"${result.start_bankroll:,.2f}"},
        {"label": "Ending", "value": f"${result.end_bankroll:,.2f}", "tone": tone},
        {"label": "Net", "value": f"${net:+,.2f}", "tone": tone},
        {"label": "ROI", "value": f"{(roi or 0)*100:+.1f}%" if roi is not None else "—", "tone": tone},
        {"label": "Record", "value": f"{wins}-{losses}-{pushes}", "foot": f"{ungraded} ungraded"},
        {"label": "Picks placed", "value": str(len(result.picks))},
    ])

    # Daily bankroll chart
    if result.daily:
        try:
            import pandas as pd
            df = pd.DataFrame(result.daily).set_index("date")
            st.line_chart(df["bankroll_eod"], height=240)
        except ImportError:
            # Fallback: render dates/bankroll as a table.
            lines = "<br>".join(f"{d['date']}  ${d['bankroll_eod']:,.2f}" for d in result.daily)
            html(f'<div class="hint">{lines}</div>')

    # Picks table
    st.markdown("### Simulated picks")
    rows = []
    for p in sorted(result.picks, key=lambda x: x.date, reverse=True)[:200]:
        outcome_tone = {"win": "ev-plus", "loss": "ev-minus", "push": "", "nograde": "", "pending": ""}.get(p.outcome, "")
        rows.append(
            f'<tr>'
            f'<td class="mono" style="font-size:11px;">{p.date}</td>'
            f'<td class="mono" style="color:var(--muted);">{p.sport.upper()}</td>'
            f'<td>{p.matchup}</td>'
            f'<td class="mono" style="color:var(--muted);">{p.market.upper()}</td>'
            f'<td><strong>{p.side_label}</strong>{(" "+str(p.point)) if p.point is not None else ""}</td>'
            f'<td class="num mono">{format_american(p.price)}</td>'
            f'<td class="mono" style="color:var(--muted);">{p.book}</td>'
            f'<td class="num mono">{p.edge_pct:+.2f}</td>'
            f'<td class="num mono">${p.stake:,.2f}</td>'
            f'<td class="mono {outcome_tone}"><strong>{p.outcome.upper()}</strong></td>'
            f'<td class="num mono {outcome_tone}">${p.profit:+,.2f}</td>'
            f'</tr>'
        )
    html(
        '<table class="ledger"><thead><tr>'
        '<th>Date</th><th>Sport</th><th>Matchup</th><th>Mkt</th><th>Side</th>'
        '<th class="num">Price</th><th>Book</th>'
        '<th class="num">Edge pts</th><th class="num">Stake</th>'
        '<th>Result</th><th class="num">P&amp;L</th>'
        '</tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table>'
    )


def _count_cached_snapshots() -> str:
    import os
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cache_dir = os.path.join(here, ".odds_cache")
    try:
        return str(len([f for f in os.listdir(cache_dir) if f.endswith(".json")]))
    except (OSError, FileNotFoundError):
        return "0"
