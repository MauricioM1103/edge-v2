"""Kalshi client - public markets (no auth) + optional live cash balance.

Public endpoints (no auth required):
  GET /trade-api/v2/events?series_ticker=KXNFLGAME&status=open
  GET /trade-api/v2/markets?event_ticker=KXNFLGAME-25OCT28DALWSH
  GET /trade-api/v2/markets?limit=1000&status=open&tickers=...

Private endpoints (auth required):
  GET /trade-api/v2/portfolio/balance   -> cash in cents

Auth model (v2):
  Each request signs {timestamp}{method}{path} with RSA-PSS SHA-256,
  base64-encodes the result, and sends three headers:
    KALSHI-ACCESS-KEY        access key id
    KALSHI-ACCESS-SIGNATURE  base64 signature
    KALSHI-ACCESS-TIMESTAMP  millisecond timestamp

Keys resolved in this order:
  1. KALSHI_ACCESS_KEY_ID + KALSHI_PRIVATE_KEY_PATH env vars
  2. st.secrets["kalshi_access_key_id"] + kalshi_private_key_pem
  3. none -> only public endpoints usable

If auth or the API itself fails, every function returns None/[] and the UI
continues rendering with a 'balance not available' hint instead of crashing.
"""
from __future__ import annotations

import base64
import json
import os
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE = "https://api.elections.kalshi.com/trade-api/v2"


# ---------------------------------------------------------------------------
# key resolution
# ---------------------------------------------------------------------------

def _get_secret(name_env: str, name_secrets: str) -> Optional[str]:
    v = os.environ.get(name_env)
    if v:
        return v
    try:
        import streamlit as st
        if hasattr(st, "secrets"):
            try:
                v = st.secrets.get(name_secrets)
                if v:
                    return str(v)
            except Exception:
                pass
    except ImportError:
        pass
    return None


def get_access_key_id() -> Optional[str]:
    return _get_secret("KALSHI_ACCESS_KEY_ID", "kalshi_access_key_id")


def get_private_key_pem() -> Optional[str]:
    """Return PEM string (either loaded from a file path or inlined in secrets)."""
    inline = _get_secret("KALSHI_PRIVATE_KEY_PEM", "kalshi_private_key_pem")
    if inline:
        return inline
    path = _get_secret("KALSHI_PRIVATE_KEY_PATH", "kalshi_private_key_path")
    if path:
        try:
            with open(os.path.expanduser(path), "r", encoding="utf-8") as f:
                return f.read()
        except OSError:
            return None
    return None


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------

def _sign(private_key_pem: str, message: str) -> Optional[str]:
    """RSA-PSS SHA-256 signature of `message`, base64-encoded."""
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
    except ImportError:
        return None
    try:
        key = serialization.load_pem_private_key(
            private_key_pem.encode("utf-8"), password=None
        )
        sig = key.sign(
            message.encode("utf-8"),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(sig).decode("ascii")
    except Exception:
        return None


def _auth_headers(method: str, path: str) -> Optional[Dict[str, str]]:
    """Build the three KALSHI-ACCESS-* headers. Returns None if keys missing."""
    key_id = get_access_key_id()
    pem = get_private_key_pem()
    if not (key_id and pem):
        return None
    ts = str(int(time.time() * 1000))
    msg = ts + method.upper() + path
    sig = _sign(pem, msg)
    if not sig:
        return None
    return {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-SIGNATURE": sig,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "Accept": "application/json",
    }


# ---------------------------------------------------------------------------
# http
# ---------------------------------------------------------------------------

def _get(path: str, params: Optional[dict] = None, authed: bool = False, timeout: int = 10):
    url = BASE + path
    if params:
        url += "?" + urlencode(params)
    headers = {"Accept": "application/json", "User-Agent": "edge-v2/1.0"}
    if authed:
        auth = _auth_headers("GET", path if not params else path + "?" + urlencode(params))
        if auth is None:
            # Try unsigned path for auth (some servers want the path-only variant).
            auth = _auth_headers("GET", path)
            if auth is None:
                return None
        headers.update(auth)
    try:
        req = Request(url, headers=headers)
        with urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except (URLError, ValueError, TimeoutError, ConnectionError, OSError):
        return None


# ---------------------------------------------------------------------------
# public: markets / events
# ---------------------------------------------------------------------------

def list_markets(status: str = "open", limit: int = 1000, series_ticker: Optional[str] = None,
                 event_ticker: Optional[str] = None) -> List[dict]:
    """Return list of markets. No auth required."""
    params = {"status": status, "limit": str(limit)}
    if series_ticker:
        params["series_ticker"] = series_ticker
    if event_ticker:
        params["event_ticker"] = event_ticker
    data = _get("/markets", params)
    if not data:
        return []
    return data.get("markets", []) or []


def list_events(series_ticker: Optional[str] = None, status: str = "open",
                limit: int = 200) -> List[dict]:
    params = {"status": status, "limit": str(limit)}
    if series_ticker:
        params["series_ticker"] = series_ticker
    data = _get("/events", params)
    if not data:
        return []
    return data.get("events", []) or []


# Kalshi series tickers for sports (the ones with per-game "winner" markets).
# Covers the US-majors cleanly; soccer and the long-tail sports are rarely on
# Kalshi so we focus on NFL/NBA/MLB/NHL/NCAAF/NCAAB where the exchange is active.
SERIES_TICKERS: Dict[str, List[str]] = {
    "nfl": ["KXNFLGAME"],
    "nba": ["KXNBAGAME"],
    "mlb": ["KXMLBGAME"],
    "nhl": ["KXNHLGAME"],
    "ncaaf": ["KXNCAAFGAME"],
    "ncaab": ["KXNCAABGAME"],
    "wnba": ["KXWNBAGAME"],
}


def fetch_sport_markets(sport_key: str) -> List[dict]:
    """All open winner markets for `sport_key`, across its game series."""
    series = SERIES_TICKERS.get(sport_key, [])
    out = []
    for s in series:
        out.extend(list_markets(series_ticker=s))
    return out


def market_implied_yes_prob(market: dict) -> Optional[float]:
    """Mid of yes bid/ask (cents 0-99) as a probability 0-1. None if no quote."""
    if not market:
        return None
    yb = market.get("yes_bid")
    ya = market.get("yes_ask")
    lp = market.get("last_price")
    if yb is None and ya is None and lp is None:
        return None
    try:
        if yb is not None and ya is not None:
            mid = (float(yb) + float(ya)) / 2.0
        elif ya is not None:
            mid = float(ya)
        elif yb is not None:
            mid = float(yb)
        else:
            mid = float(lp)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, mid / 100.0))


# ---------------------------------------------------------------------------
# authed: balance
# ---------------------------------------------------------------------------

def get_balance_cents() -> Optional[int]:
    """Live cash balance in cents, or None if auth is not configured."""
    data = _get("/portfolio/balance", authed=True)
    if not data:
        return None
    # The API returns {"balance": <cents>}
    try:
        return int(data.get("balance"))
    except (TypeError, ValueError):
        return None


def get_balance_dollars() -> Optional[float]:
    cents = get_balance_cents()
    if cents is None:
        return None
    return cents / 100.0


def manual_balance_override() -> Optional[float]:
    """Resolve a manual balance override from env or secrets."""
    v = _get_secret("KALSHI_BALANCE_USD", "kalshi_balance_usd")
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def resolve_balance() -> dict:
    """Resolve the user's effective Kalshi cash with source info for the UI.

    Returns {"usd": <float or None>, "source": "live" | "manual" | None}.
    Live API attempt first, then env/secrets override.
    """
    live = get_balance_dollars()
    if live is not None:
        return {"usd": live, "source": "live"}
    manual = manual_balance_override()
    if manual is not None:
        return {"usd": manual, "source": "manual"}
    return {"usd": None, "source": None}
