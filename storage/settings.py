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
    # Unit = this fraction of live Kalshi cash. 0.10 = 10%.
    "unit_pct_of_kalshi": 0.10,
    # Fallback bankroll used when Kalshi cash is unavailable.
    "manual_kalshi_cash": 500.0,
    # Lines display
    "primary_book": "kalshi",
    "fallback_book": "DraftKings",
    "extra_sources": [],  # extra rows in the prob strip
    # Agent
    "agent_kelly": 0.25,
    "agent_min_edge": 0.02,
    "agent_min_books": 3,
    "agent_max_bet_pct": 0.05,
    # Fair prob source order (configurable, default = Pinnacle sharp).
    "agent_fair_source": "pinnacle",
    # Odds API regions. "us,eu" includes Pinnacle (sharpest book) at 2× credit cost.
    # Set to "us" to halve the credit cost (and lose Pinnacle fair).
    "odds_regions": "us,eu",
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
