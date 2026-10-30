"""Persistent user settings, saved as JSON next to app.py.

Everything the sidebar / settings page tweaks lives here so the app remembers
across sessions. Secrets (API keys) are stored separately in
`.streamlit/secrets.toml` — that file is gitignored.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict

_DEFAULTS: Dict[str, Any] = {
    "sport_group": "US Majors",
    "sport": "nfl",
    # Unit = this fraction of live Kalshi cash. Default 1% follows pro convention:
    # 1u = 1% of bankroll, typical plays 0.5-3u, max confidence = 5u.
    "unit_pct_of_kalshi": 0.01,
    # Fallback bankroll used when Kalshi cash is unavailable.
    "manual_kalshi_cash": 500.0,
    # Lines display
    "primary_book": "kalshi",
    "fallback_book": "DraftKings",
    "extra_sources": [],  # extra rows in the prob strip
    # Agent
    "agent_kelly": 0.25,
    # 1% default - Kalshi vs Pinnacle are both near-fair markets, so edges are
    # smaller than the 2% you'd see scanning retail US books. Raise this to
    # trim down to only the biggest edges.
    "agent_min_edge": 0.01,
    "agent_min_books": 3,
    "agent_max_bet_pct": 0.05,
    # Fair prob source order (configurable, default = Pinnacle sharp).
    "agent_fair_source": "pinnacle",
    # Odds API regions. "us,eu" = 2 credits/sport but unlocks offshore US books
    # (Bovada, LowVig, BetOnline) alongside Pinnacle - these tolerate winning
    # accounts and produce bigger edges than the sharp exchanges alone.
    "odds_regions": "us,eu",
    # Which Odds API markets to pull. "h2h" is enough for moneyline edges on
    # Kalshi (which only lists winner markets). Each extra market multiplies
    # credit cost (us,eu × 3 markets = 6 credits/sport/fetch).
    # Supported: h2h, totals, spreads, h2h_h1, totals_h1, btts (soccer only).
    "odds_markets": "h2h",
    # Cache TTL for live odds in seconds. 3600 = 1h.
    "odds_live_ttl": 3600,
    # Books the agent will shop against Pinnacle fair, in addition to Kalshi.
    # Offshore books tolerate sharp action - DK/FanDuel/MGM/Caesars will ban
    # winning accounts within weeks so they are intentionally excluded.
    "shop_books": ["Bovada", "LowVig.ag", "BetOnline.ag", "BetUS", "MyBookie.ag"],
    # Daily total-exposure cap as a fraction of Kalshi cash. Prevents the agent
    # from recommending more stakes than the bankroll can cover across many picks.
    "daily_exposure_cap_pct": 0.25,
    # Theme: "dark" (default) or "light".
    "theme": "dark",
}


def _path() -> str:
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(here, "settings.json")


def load() -> Dict[str, Any]:
    """Load persisted settings merged over defaults."""
    out = dict(_DEFAULTS)
    try:
        with open(_path(), "r", encoding="utf-8") as f:
            disk = json.load(f)
        if isinstance(disk, dict):
            out.update(disk)
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    return out


def save(settings: Dict[str, Any]) -> None:
    """Persist settings. Never raises."""
    try:
        current = load()
        current.update(settings or {})
        with open(_path(), "w", encoding="utf-8") as f:
            json.dump(current, f, indent=2, default=str)
    except OSError:
        pass


def save_kalshi_keys(access_key_id: str, private_key_pem: str) -> bool:
    """Persist Kalshi credentials to .streamlit/secrets.toml (gitignored).

    This file is read by Streamlit's st.secrets at startup, which Kalshi's
    client already consults. The file never leaves the local machine.
    Returns True on write success.
    """
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    secrets_path = os.path.join(here, ".streamlit", "secrets.toml")
    try:
        os.makedirs(os.path.dirname(secrets_path), exist_ok=True)
        # Preserve any existing non-Kalshi entries (e.g. the_odds_api_key).
        existing = ""
        if os.path.exists(secrets_path):
            with open(secrets_path, "r", encoding="utf-8") as f:
                existing = f.read()

        # Strip prior Kalshi entries, keep everything else verbatim.
        kept_lines = []
        skip_block = False
        for line in existing.split("\n"):
            stripped = line.strip()
            if (stripped.startswith("kalshi_access_key_id") or
                stripped.startswith("kalshi_balance_usd") or
                stripped.startswith("kalshi_private_key_path")):
                continue
            if stripped.startswith("kalshi_private_key_pem"):
                skip_block = True
                continue
            if skip_block:
                if '"""' in line:
                    skip_block = False
                continue
            kept_lines.append(line)
        kept = "\n".join(kept_lines).rstrip() + "\n" if kept_lines else ""

        # Serialize PEM as a TOML triple-quoted string.
        pem_norm = (private_key_pem or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        block = (
            f'kalshi_access_key_id = "{(access_key_id or "").strip()}"\n'
            f'kalshi_private_key_pem = """{pem_norm}\n"""\n'
        )
        with open(secrets_path, "w", encoding="utf-8") as f:
            f.write(kept + ("\n" if kept else "") + block)
        return True
    except OSError:
        return False


def clear_kalshi_keys() -> None:
    """Remove Kalshi entries from the local secrets.toml."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    secrets_path = os.path.join(here, ".streamlit", "secrets.toml")
    if not os.path.exists(secrets_path):
        return
    try:
        with open(secrets_path, "r", encoding="utf-8") as f:
            text = f.read()
        out, skip_block = [], False
        for line in text.split("\n"):
            s = line.strip()
            if s.startswith(("kalshi_access_key_id", "kalshi_balance_usd", "kalshi_private_key_path")):
                continue
            if s.startswith("kalshi_private_key_pem"):
                skip_block = True
                continue
            if skip_block:
                if '"""' in line:
                    skip_block = False
                continue
            out.append(line)
        with open(secrets_path, "w", encoding="utf-8") as f:
            f.write("\n".join(out))
    except OSError:
        pass
