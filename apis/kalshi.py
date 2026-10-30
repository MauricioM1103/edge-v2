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

HOST = "https://api.elections.kalshi.com"
API_PREFIX = "/trade-api/v2"
BASE = HOST + API_PREFIX


# ---------------------------------------------------------------------------
# key resolution
# ---------------------------------------------------------------------------

def _session_val(key: str) -> Optional[str]:
    """Read a value from Streamlit session_state if available."""
    try:
        import streamlit as st
        return st.session_state.get(key)
    except Exception:
        return None


def _normalize_pem(pem_text: str) -> str:
    """Clean up a pasted PEM: strip BOM, normalize line endings, trim whitespace.

    Streamlit text_area may introduce CRLF or trailing spaces; Kalshi's RSA
    loader is forgiving but line endings matter when the PEM is malformed.
    """
    if not pem_text:
        return ""
    s = pem_text.replace("\r\n", "\n").replace("\r", "\n").strip()
    # If someone pasted a one-line PEM (markers glued together), expand it.
    if "-----BEGIN" in s and "\n" not in s:
        s = (
            s.replace(" -----", "\n-----")
             .replace("----- ", "-----\n")
        )
    return s


def set_runtime_credentials(access_key_id: str, private_key_pem: str) -> None:
    """Store Kalshi credentials in the current session only (never written to disk).

    Called from the sidebar 'Connect Kalshi' expander. Overrides env/secrets.
    """
    try:
        import streamlit as st
        st.session_state["_kalshi_rt_key_id"] = (access_key_id or "").strip()
        st.session_state["_kalshi_rt_pem"] = _normalize_pem(private_key_pem or "")
    except Exception:
        pass


def test_credentials() -> dict:
    """Ping /portfolio/balance with current creds. Returns a human-readable result.

    Shape:
      {"ok": True,  "balance_usd": 12.34}
      {"ok": False, "error": "...", "hint": "..."}
    """
    status, payload = _get_verbose("/portfolio/balance", authed=True)
    if status == "ok":
        try:
            cents = int(payload.get("balance"))
            return {"ok": True, "balance_usd": cents / 100.0}
        except (TypeError, ValueError, KeyError):
            return {"ok": False, "error": "Unexpected response from Kalshi.",
                    "hint": "The /portfolio/balance endpoint returned JSON without a `balance` field."}
    if status == "no_keys":
        return {"ok": False, "error": "Both the Access Key ID and PEM are required.",
                "hint": "Paste both fields and click Connect again."}
    if status == "sign_failed":
        return {"ok": False, "error": "Could not sign the request.",
                "hint": "PEM format wrong. It should look like:\n"
                        "-----BEGIN RSA PRIVATE KEY-----\n"
                        "MIIE... (multi-line base64 body) ...\n"
                        "-----END RSA PRIVATE KEY-----\n"
                        f"Detail: {payload.get('detail')}"}
    if status == "http_401":
        return {"ok": False, "error": "Kalshi rejected the request (401 Unauthorized).",
                "hint": "Common causes: wrong Access Key ID, PEM doesn't match that key, "
                        "or the key has been revoked. Response: " + (payload.get("body") or "")[:200]}
    if status == "http_403":
        return {"ok": False, "error": "Kalshi returned 403 Forbidden.",
                "hint": "The key may not have the right permissions, or your account is restricted."}
    if status == "http_429":
        return {"ok": False, "error": "Rate-limited by Kalshi (429).",
                "hint": "Wait a few seconds and try again."}
    if status.startswith("http_"):
        return {"ok": False, "error": f"Kalshi returned HTTP {payload.get('status')}.",
                "hint": (payload.get("body") or "")[:200]}
    if status == "network":
        return {"ok": False, "error": "Could not reach Kalshi.",
                "hint": payload.get("detail") or "network/timeout"}
    return {"ok": False, "error": "Unknown error.", "hint": str(payload)}


def clear_runtime_credentials() -> None:
    try:
        import streamlit as st
        for k in ("_kalshi_rt_key_id", "_kalshi_rt_pem"):
            if k in st.session_state:
                del st.session_state[k]
    except Exception:
        pass


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
    # Session (sidebar-entered) credentials take precedence.
    sv = _session_val("_kalshi_rt_key_id")
    if sv:
        return sv
    return _get_secret("KALSHI_ACCESS_KEY_ID", "kalshi_access_key_id")


def get_private_key_pem() -> Optional[str]:
    """Return PEM string (session value, inline secret, or file path)."""
    sv = _session_val("_kalshi_rt_pem")
    if sv:
        return sv
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
    """Build the three KALSHI-ACCESS-* headers. Returns None if keys missing.

    The signed path MUST include the full `/trade-api/v2/...` prefix per
    Kalshi's auth spec. Query strings are NOT included.
    """
    key_id = get_access_key_id()
    pem = get_private_key_pem()
    if not (key_id and pem):
        return None
    ts = str(int(time.time() * 1000))
    # Ensure the API prefix is on the path exactly once.
    full_path = path if path.startswith(API_PREFIX) else (API_PREFIX + path)
    msg = ts + method.upper() + full_path
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
        # Sign only the path (never with query string, per Kalshi's spec).
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


def _get_verbose(path: str, authed: bool = True, timeout: int = 10):
    """Like _get but returns (ok, payload_or_error_dict) with HTTP status/body.

    Used by the Connect button so the UI can show WHY auth failed:
      ("ok", {"balance": 10050})
      ("http_401", {"status": 401, "body": "<html>...</html>"})
      ("sign_failed", {"detail": "cryptography not installed"})
      ("no_keys", {"detail": "missing access key or PEM"})
      ("network", {"detail": "connection timeout"})
    """
    from urllib.error import HTTPError
    url = BASE + path
    headers = {"Accept": "application/json", "User-Agent": "edge-v2/1.0"}
    if authed:
        if not (get_access_key_id() and get_private_key_pem()):
            return ("no_keys", {"detail": "Missing access key id or PEM."})
        try:
            from cryptography.hazmat.primitives import hashes  # noqa
        except ImportError:
            return ("sign_failed", {"detail": "cryptography library not installed."})
        auth = _auth_headers("GET", path)
        if auth is None:
            return ("sign_failed", {"detail": "Could not sign request — check PEM format."})
        headers.update(auth)
    try:
        req = Request(url, headers=headers)
        with urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
            return ("ok", data)
    except HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        return (f"http_{e.code}", {"status": e.code, "body": body})
    except (URLError, ValueError, TimeoutError, ConnectionError, OSError) as e:
        return ("network", {"detail": str(e)})


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
