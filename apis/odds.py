"""The Odds API v4 client.

Docs: https://the-odds-api.com/liveapi/guides/v4/

Credit cost model:
  * Each /odds request costs `regions × markets` credits.
    us × "h2h,spreads,totals" = 3 credits per sport per fetch.
  * Historical /odds requests cost 10× the live rate (so 30 credits per snapshot).
  * The server returns `x-requests-remaining` and `x-requests-last` headers;
    we read those and persist them so the UI can show the running budget.
"""
from __future__ import annotations

import json
import os
import time
from typing import List, Optional
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ODDS_BASE = "https://api.the-odds-api.com/v4"

# Where we keep the latest credit-usage snapshot across restarts.
_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CREDIT_FILE = os.path.join(_HERE, ".odds_credits.json")
# On-disk response cache; keyed by URL (minus the API key).
_CACHE_DIR = os.path.join(_HERE, ".odds_cache")


def _load_credits() -> dict:
    try:
        with open(_CREDIT_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def _save_credits(d: dict) -> None:
    try:
        with open(_CREDIT_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2)
    except OSError:
        pass


def get_credit_status() -> dict:
    """Return the latest credit snapshot. Fields:
         remaining  - int | None  (x-requests-remaining)
         last_used  - int | None  (x-requests-last)
         updated_at - ISO string
    """
    return _load_credits()


def _update_credits_from_headers(headers) -> None:
    """Pull x-requests-* from the HTTP response and persist them."""
    try:
        remaining = headers.get("x-requests-remaining") or headers.get("X-Requests-Remaining")
        last = headers.get("x-requests-last") or headers.get("X-Requests-Last")
        used = headers.get("x-requests-used") or headers.get("X-Requests-Used")
        snapshot = _load_credits()
        if remaining is not None:
            try:
                snapshot["remaining"] = int(remaining)
            except (TypeError, ValueError):
                pass
        if last is not None:
            try:
                snapshot["last_used"] = int(last)
            except (TypeError, ValueError):
                pass
        if used is not None:
            try:
                snapshot["used_total"] = int(used)
            except (TypeError, ValueError):
                pass
        snapshot["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        _save_credits(snapshot)
    except Exception:
        pass


# ---------- on-disk cache ----------

def _cache_key(url_without_key: str) -> str:
    import hashlib
    h = hashlib.sha256(url_without_key.encode("utf-8")).hexdigest()[:32]
    return os.path.join(_CACHE_DIR, h + ".json")


def _cache_read(url_key: str, ttl: int) -> Optional[dict]:
    path = _cache_key(url_key)
    if not os.path.exists(path):
        return None
    try:
        if ttl > 0 and (time.time() - os.path.getmtime(path)) > ttl:
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def _cache_write(url_key: str, data) -> None:
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        with open(_cache_key(url_key), "w", encoding="utf-8") as f:
            json.dump(data, f)
    except (OSError, TypeError):
        pass


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


def _get(path, params, cache_ttl: int = 0, historical: bool = False):
    """GET from The Odds API with on-disk caching + credit header capture.

    cache_ttl:
      * 0        -> never return cached (always refetch unless historical)
      * N > 0    -> return cached response if younger than N seconds
      * -1       -> permanent cache (historical snapshots — the past doesn't change)
    """
    key = get_api_key()
    if not key:
        return None
    params = dict(params or {})
    # Cache key is the URL WITHOUT the api key, so swapping keys doesn't bust it.
    cache_url = f"{ODDS_BASE}{path}?{urlencode(params)}"
    if historical:
        hit = _cache_read(cache_url, ttl=-1)  # historical never expires
        if hit is not None:
            return hit
    elif cache_ttl > 0:
        hit = _cache_read(cache_url, ttl=cache_ttl)
        if hit is not None:
            return hit

    params["apiKey"] = key
    url = f"{ODDS_BASE}{path}?{urlencode(params)}"
    try:
        req = Request(url, headers={"User-Agent": "betting-dashboard-v2/1.0"})
        with urlopen(req, timeout=10) as resp:
            _update_credits_from_headers(resp.headers)
            data = json.load(resp)
            # Persist on success so a later restart / cache-clear still has it.
            try:
                _cache_write(cache_url, data)
            except Exception:
                pass
            return data
    except (URLError, ValueError, TimeoutError, ConnectionError, OSError):
        # On error, fall back to a stale cache entry if any exists.
        stale = _cache_read(cache_url, ttl=-1)
        return stale


def list_sports() -> List[dict]:
    """Available sports from The Odds API (includes `in_season` flag)."""
    data = _get("/sports", {"all": "true"}, cache_ttl=24 * 3600)  # sports change ~weekly
    if not data:
        return []
    return data if isinstance(data, list) else []


def get_odds(
    sport: str,
    regions: str = "us,eu",
    markets: str = "h2h,spreads,totals",
    odds_format: str = "american",
    cache_ttl: int = 300,
) -> List[dict]:
    """Fetch current odds for `sport` (The Odds API key, e.g. 'baseball_mlb').

    Returns a list of games, each with a 'bookmakers' list of priced markets.
    Returns [] on missing key, timeout, or bad response.

    cache_ttl: cache on disk for this many seconds (default 5 min). Set to
    larger values to conserve credits; set 0 to always refetch.
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
        cache_ttl=cache_ttl,
    )
    if not data or not isinstance(data, list):
        return []
    return data


def estimated_credit_cost(regions: str, markets: str) -> int:
    """Credit cost for ONE /odds request. us × h2h,spreads,totals = 3."""
    r = len([x for x in (regions or "").split(",") if x.strip()])
    m = len([x for x in (markets or "").split(",") if x.strip()])
    return max(1, r * m)


def list_events(sport: str, cache_ttl: int = 1800) -> List[dict]:
    """List upcoming events for a sport. Free (0 credits) and used to drive
    per-event alternate-market fetches without burning credits upfront.
    Returns [] on missing key / error."""
    if not sport:
        return []
    data = _get(
        f"/sports/{sport}/events",
        {"dateFormat": "iso"},
        cache_ttl=cache_ttl,
    )
    if not data or not isinstance(data, list):
        return []
    return data


def _event_odds_params(regions: str, markets: str, odds_format: str) -> dict:
    return {
        "regions": regions,
        "markets": markets,
        "oddsFormat": odds_format,
        "dateFormat": "iso",
    }


def get_event_odds(
    sport: str,
    event_id: str,
    regions: str = "us,eu",
    markets: str = "totals_h1",
    odds_format: str = "american",
    cache_ttl: int = 6 * 3600,
) -> Optional[dict]:
    """Fetch alternate markets (1H lines, BTTS, player props) for ONE event.

    These markets are NOT served by the batch /odds endpoint - requesting them
    there returns 0 games. Only the per-event endpoint carries them.

    Cost: markets × regions credits per call. 1H totals × us,eu = 2 credits per
    event. Cache aggressively (6h default) because 1H lines barely move after
    the market opens. Returns None on error."""
    if not sport or not event_id:
        return None
    data = _get(
        f"/sports/{sport}/events/{event_id}/odds",
        _event_odds_params(regions, markets, odds_format),
        cache_ttl=cache_ttl,
    )
    if not data or not isinstance(data, dict):
        return None
    return data


def event_odds_is_cached(
    sport: str,
    event_id: str,
    regions: str = "us,eu",
    markets: str = "totals_h1",
    odds_format: str = "american",
    cache_ttl: int = 6 * 3600,
) -> bool:
    """True when a get_event_odds call with these args would be served from
    the disk cache (so it costs 0 credits). Lets credit preflights count only
    the events that will actually hit the API."""
    if not sport or not event_id:
        return False
    path = f"/sports/{sport}/events/{event_id}/odds"
    cache_url = f"{ODDS_BASE}{path}?{urlencode(_event_odds_params(regions, markets, odds_format))}"
    return _cache_read(cache_url, ttl=cache_ttl) is not None


# ---------- player-prop daily spend ledger ----------
# Props are the only fetch whose cost scales with event count, so they get a
# hard per-day ceiling. Spend is measured from the API's own x-requests-used
# counter (delta across a fetch batch), not estimated, so cache hits cost 0.

_PROPS_SPEND_FILE = os.path.join(_CACHE_DIR, "props_spend.json")


def _central_day() -> str:
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/Chicago")).strftime("%Y-%m-%d")
    except Exception:
        return time.strftime("%Y-%m-%d")


def _load_props_spend() -> dict:
    try:
        with open(_PROPS_SPEND_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def props_spend_today() -> int:
    return int(_load_props_spend().get(_central_day(), 0) or 0)


def record_props_spend(credits: int) -> int:
    """Add `credits` to today's props spend; returns the new daily total.
    Only today's key is kept so the file never grows."""
    if credits <= 0:
        return props_spend_today()
    day = _central_day()
    total = int(_load_props_spend().get(day, 0) or 0) + int(credits)
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        with open(_PROPS_SPEND_FILE, "w", encoding="utf-8") as f:
            json.dump({day: total}, f)
    except (OSError, TypeError):
        pass
    return total


def merge_alt_markets_into_games(odds_games: List[dict], alt_data_by_eid: dict) -> List[dict]:
    """Extend each game's bookmakers with alt-market data from per-event fetch.

    `odds_games`: list of games from `get_odds` (batch endpoint).
    `alt_data_by_eid`: {event_id: event_data_from_get_event_odds, ...}

    For each matching event, merges every alt bookmaker's markets into the
    main-fetch bookmakers list. Existing markets are not overwritten; new
    ones are appended. Returns the mutated list (also in-place)."""
    for og in odds_games:
        eid = og.get("id")
        if not eid or eid not in alt_data_by_eid:
            continue
        alt_g = alt_data_by_eid[eid] or {}
        existing_books = {b.get("key"): b for b in (og.get("bookmakers") or [])}
        for alt_bk in (alt_g.get("bookmakers") or []):
            alt_key = alt_bk.get("key")
            alt_markets = alt_bk.get("markets") or []
            if not alt_markets:
                continue
            if alt_key in existing_books:
                existing = existing_books[alt_key]
                have_keys = {m.get("key") for m in (existing.get("markets") or [])}
                for m in alt_markets:
                    if m.get("key") and m.get("key") not in have_keys:
                        existing.setdefault("markets", []).append(m)
            else:
                og.setdefault("bookmakers", []).append(alt_bk)
    return odds_games


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


def _american_to_prob(price: float) -> Optional[float]:
    try:
        p = float(price)
    except (TypeError, ValueError):
        return None
    if p >= 100:
        return 100.0 / (p + 100.0)
    if p <= -100:
        return -p / (-p + 100.0)
    return None


def _prob_to_american(q: float) -> Optional[float]:
    if q is None or q <= 0 or q >= 1:
        return None
    if q >= 0.5:
        return -round(q / (1 - q) * 100)
    return round((1 - q) / q * 100)


# "Opening line" store: the Odds API plan has no line history, so the honest
# opener is the first price we saw today for each (game, market). Lives next to
# the response cache; on Streamlit Cloud it resets with the container, which
# still gives a correct "since boot" move signal.
_OPENERS_FILE = os.path.join(_CACHE_DIR, "openers.json")
_openers_mem: Optional[dict] = None


def _openers() -> dict:
    global _openers_mem
    if _openers_mem is None:
        try:
            with open(_OPENERS_FILE, "r", encoding="utf-8") as f:
                _openers_mem = json.load(f) or {}
        except (OSError, json.JSONDecodeError, ValueError):
            _openers_mem = {}
        today = time.strftime("%Y-%m-%d")
        _openers_mem = {k: v for k, v in _openers_mem.items() if k.startswith(today)}
    return _openers_mem


def _record_opener(game_id: str, market_key: str, snap: dict) -> dict:
    store = _openers()
    key = f"{time.strftime('%Y-%m-%d')}|{game_id}|{market_key}"
    if key not in store:
        store[key] = snap
        try:
            os.makedirs(_CACHE_DIR, exist_ok=True)
            with open(_OPENERS_FILE, "w", encoding="utf-8") as f:
                json.dump(store, f)
        except (OSError, TypeError):
            pass
    return store[key]


def consensus_market(game: dict, market_key: str) -> Optional[dict]:
    """Price summary for one market of one game.

    Pinnacle is the app's fair reference, so its quote is used when present;
    otherwise prices are averaged in implied-probability space (averaging
    American odds arithmetically produces nonsense like -2.5). Points use the
    same preference. Openers come from the first-seen-today store.

    Returns:
        {
          "home_price": -115, "away_price": +105,          # American, |p| >= 100
          "home_point": -3.5, "away_point": +3.5,          # None for h2h
          "book_count": 7,
          "home_opener": -108, "away_opener": +100,        # first seen today
          "home_point_opener": -3.0, "away_point_opener": +3.0,
          "best_home": (-105, "DraftKings"), "best_away": (+112, "FanDuel"),
          "source": "pinnacle" | "consensus",
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
    pin = {}

    for b in books:
        bname = b.get("title") or b.get("key") or ""
        is_pin = (b.get("key") or "").lower() == "pinnacle"
        for m in (b.get("markets") or []):
            if m.get("key") != market_key:
                continue
            for o in (m.get("outcomes") or []):
                name = o.get("name")
                try:
                    price = float(o.get("price"))
                except (TypeError, ValueError):
                    continue
                if abs(price) < 100:
                    continue
                point = o.get("point")
                try:
                    point = float(point) if point is not None else None
                except (TypeError, ValueError):
                    point = None
                # Totals outcomes are "Over"/"Under", not team names: Over rides
                # the home slot, Under the away slot (tiles print home_point as
                # the O/U line).
                if market_key == "totals":
                    nm = (name or "").lower()
                    name = home_team if nm == "over" else (away_team if nm == "under" else name)
                if name == home_team:
                    side = "home"
                    home_prices.append(price)
                    if point is not None:
                        home_points.append(point)
                    if best_home is None or price > best_home[0]:
                        best_home = (price, bname)
                elif name == away_team:
                    side = "away"
                    away_prices.append(price)
                    if point is not None:
                        away_points.append(point)
                    if best_away is None or price > best_away[0]:
                        best_away = (price, bname)
                else:
                    continue
                if is_pin:
                    pin[f"{side}_price"] = price
                    pin[f"{side}_point"] = point

    if not home_prices and not away_prices:
        return None

    def _avg_price(xs):
        probs = [q for q in (_american_to_prob(x) for x in xs) if q is not None]
        return _prob_to_american(sum(probs) / len(probs)) if probs else None

    def _avg_point(xs):
        return (sum(xs) / len(xs)) if xs else None

    out = {
        "home_price": pin.get("home_price") if pin.get("home_price") is not None else _avg_price(home_prices),
        "away_price": pin.get("away_price") if pin.get("away_price") is not None else _avg_price(away_prices),
        "home_point": pin.get("home_point") if pin.get("home_point") is not None else _avg_point(home_points),
        "away_point": pin.get("away_point") if pin.get("away_point") is not None else _avg_point(away_points),
        "book_count": max(len(home_prices), len(away_prices)),
        "best_home": best_home,
        "best_away": best_away,
        "source": "pinnacle" if pin else "consensus",
    }
    opener = _record_opener(str(game.get("id") or f"{away_team}@{home_team}"), market_key, {
        "home_price": out["home_price"], "away_price": out["away_price"],
        "home_point": out["home_point"], "away_point": out["away_point"],
    })
    out["home_opener"] = opener.get("home_price")
    out["away_opener"] = opener.get("away_price")
    out["home_point_opener"] = opener.get("home_point")
    out["away_point_opener"] = opener.get("away_point")
    return out
