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
    # Default to "All sports" so the main board surfaces every active slate out of the box.
    "sport_group": "All sports",
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
    # 2.5% per-bet cap means max play = 2.5u (with 1u = 1%), and 10 top picks
    # at 2.5u each fit exactly inside the 25% daily cap. Keeps stakes human.
    "agent_max_bet_pct": 0.025,
    # Fair prob source order (configurable, default = Pinnacle sharp).
    "agent_fair_source": "pinnacle",
    # Odds API regions. "us,eu" = 2 credits/sport but unlocks offshore US books
    # (Bovada, LowVig, BetOnline) alongside Pinnacle - these tolerate winning
    # accounts and produce bigger edges than the sharp exchanges alone.
    "odds_regions": "us,eu",
    # Which Odds API markets to pull from the BATCH /odds endpoint. Only core
    # markets (h2h, totals, spreads) work there - alternate markets (1H, BTTS,
    # props) return 0 games. Each market adds 1 credit/region.
    "odds_markets": "h2h,totals,spreads",
    # Alternate markets to pull via the PER-EVENT /events/{id}/odds endpoint.
    # These are opt-in because they cost markets×regions credits PER EVENT
    # (vs per sport for the batch endpoint). Empty string disables.
    # Supported: totals_h1, spreads_h1, h2h_h1 (team sports), btts (soccer).
    # Only runs for sports in `odds_alt_sports` to prevent credit blowup.
    "odds_alt_markets": "",
    # Allowlist of sports the per-event alt-market fetcher may hit. Default
    # is empty = run on everything, but credit cost scales with event count,
    # so narrowing this to the sports you actually need (e.g. ["nba","nfl"])
    # cuts the burn rate dramatically. Only applied when odds_alt_markets is set.
    "odds_alt_sports": [],
    # Cache TTL for per-event alt-market fetches. 1H lines don't move much
    # after open, so 6h cache + manual "Clear cache" when needed is sane.
    "odds_alt_ttl": 6 * 3600,
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
    # How many picks the main agent table surfaces (slider: 5, 10, 15, 20, 25).
    "agent_picks_shown": 10,
    # When true, every funded edge the agent surfaces is written to the picks
    # ledger automatically (dedup-aware), so you get a complete record of what
    # the agent recommended — not just what you clicked "Log" on.
    "agent_auto_log": True,
    # Seconds between live-score refreshes on the Dashboard. 0 disables the
    # auto-rerun entirely (manual refresh only). Only active while at least
    # one game on the slate is in-progress; outside those windows the
    # dashboard is idle regardless of this value.
    "live_refresh_sec": 30,
}


def _path() -> str:
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(here, "settings.json")


def load() -> Dict[str, Any]:
    """Load persisted settings merged over defaults.

    Also performs silent migrations for stale saved values from earlier releases
    so users don't have to click 'Reset to defaults' to get the new behavior.
    """
    out = dict(_DEFAULTS)
    try:
        with open(_path(), "r", encoding="utf-8") as f:
            disk = json.load(f)
        if isinstance(disk, dict):
            # Migration 1: previous odds_markets defaults ("h2h" too narrow,
            # the "h2h,totals,h2h_h1,totals_h1,btts" string that triggers a 0-
            # games API response, and the plain "h2h,totals" that preceded
            # spreads) each get snapped forward to the current default so the
            # batch endpoint actually fetches the full featured-market set.
            _RETIRED_MARKETS = {
                "h2h",
                "h2h,totals",
                "h2h,totals,h2h_h1,totals_h1,btts",
            }
            if disk.get("odds_markets") in _RETIRED_MARKETS:
                disk["odds_markets"] = _DEFAULTS["odds_markets"]
            # Migration 2: old unit_pct_of_kalshi default was 0.10 (10%); then
            # 0.01 (1%). If the saved value is one of those defaults, snap to
            # the current default so stake units stay in the familiar 0.5u-5u range.
            if disk.get("unit_pct_of_kalshi") in (0.10, 0.1):
                disk["unit_pct_of_kalshi"] = _DEFAULTS["unit_pct_of_kalshi"]
            # Migration 3: retired 5% default produced oversized stakes. Snap
            # anyone still on 0.05 forward to the new 2.5% default so a single
            # pick can't dominate the daily cap.
            if disk.get("agent_max_bet_pct") == 0.05:
                disk["agent_max_bet_pct"] = _DEFAULTS["agent_max_bet_pct"]
            # Migration 4: unit_pct that got dialed below 0.5% makes stake
            # units read as 20u / 30u / 50u which is confusing. Pin to default.
            upct = disk.get("unit_pct_of_kalshi")
            try:
                if upct is not None and float(upct) < 0.005:
                    disk["unit_pct_of_kalshi"] = _DEFAULTS["unit_pct_of_kalshi"]
            except (TypeError, ValueError):
                pass
            # Migration 5: the stale "US Majors" default was persisted before
            # sport_group was user-tunable. Snap it forward to the new default
            # so returning users see every sport on the main board; a user who
            # intentionally selects a narrower group will re-save that choice.
            if disk.get("sport_group") == "US Majors":
                disk["sport_group"] = _DEFAULTS["sport_group"]
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
