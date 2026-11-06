"""Discord outbound webhook for high-confidence picks.

Fire-and-forget: given a webhook URL (from `.streamlit/secrets.toml` or an
env var), post a compact embed describing the pick. Dedupe is handled by
the caller via a local JSON log so we never spam the same pick twice.

The webhook URL is sensitive — it's a signed endpoint that lets anyone
holding it post to a specific channel. Never log the URL itself, and never
echo it back in the UI.
"""
from __future__ import annotations

import json
import os
import time
from typing import Dict, Optional
from urllib.error import URLError
from urllib.request import Request, urlopen


def _webhook_url() -> Optional[str]:
    """Resolve the Discord webhook URL from env or Streamlit secrets."""
    env = os.environ.get("DISCORD_WEBHOOK_URL")
    if env:
        return env
    try:
        import streamlit as st  # type: ignore
        return st.secrets.get("discord_webhook_url")  # type: ignore
    except Exception:
        return None


def is_configured() -> bool:
    return bool(_webhook_url())


def _embed_for_pick(pick: Dict) -> Dict:
    """Shape a Discord embed from one agent pick row."""
    edge_pct = float(pick.get("edge") or 0) * 100.0
    fair_pct = float(pick.get("fair_prob") or 0) * 100.0
    matchup = pick.get("matchup") or "—"
    side = pick.get("side_label") or ""
    book = pick.get("book") or "?"
    price = pick.get("price")
    sport = (pick.get("_sport") or "").upper()
    stake_d = float(pick.get("stake_dollars") or 0)
    # Discord embed colors are int RGB — emerald to match the dashboard.
    color = 0x10E0A1 if edge_pct > 0 else 0x7A8CAA
    return {
        "title": f"Edge · {sport} · {matchup}",
        "description": f"**{side}** @ {book} {price:+d}" if isinstance(price, (int, float)) else f"**{side}** @ {book}",
        "color": color,
        "fields": [
            {"name": "Edge", "value": f"{edge_pct:+.2f}%", "inline": True},
            {"name": "Fair prob", "value": f"{fair_pct:.1f}%", "inline": True},
            {"name": "Stake", "value": f"${stake_d:,.2f}", "inline": True},
        ],
        "footer": {"text": "Edge v2.1 · Pinnacle no-vig fair prob"},
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def send_pick(pick: Dict, timeout: float = 4.0) -> bool:
    """Post one pick. Returns True on 200/204. Any error is swallowed so a
    broken webhook never breaks the dashboard."""
    url = _webhook_url()
    if not url:
        return False
    payload = {
        "username": "Edge",
        "embeds": [_embed_for_pick(pick)],
    }
    data = json.dumps(payload).encode("utf-8")
    req = Request(url, data=data, headers={
        "Content-Type": "application/json",
        "User-Agent": "edge-dashboard/2.1",
    }, method="POST")
    try:
        with urlopen(req, timeout=timeout) as resp:
            return resp.status in (200, 204)
    except (URLError, TimeoutError, OSError):
        return False


# ---------------------------------------------------------------------------
# Dedupe log — a tiny JSON file tracking which picks have been sent so reruns
# don't spam the channel. Keyed by (sport, event_id, side_label, point).
# ---------------------------------------------------------------------------

_DEDUPE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "storage",
    "discord_sent.json",
)


def _dedupe_key(pick: Dict) -> str:
    return "|".join([
        str((pick.get("_sport") or "")).lower(),
        str(pick.get("espn_event_id") or pick.get("event_id") or ""),
        str(pick.get("side_label") or "").lower(),
        str(pick.get("point") or ""),
    ])


def _load_dedupe() -> set:
    try:
        with open(_DEDUPE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            return set(data) if isinstance(data, list) else set()
    except (OSError, ValueError):
        return set()


def _save_dedupe(keys: set) -> None:
    try:
        os.makedirs(os.path.dirname(_DEDUPE_PATH), exist_ok=True)
        with open(_DEDUPE_PATH, "w", encoding="utf-8") as f:
            # Trim to the most recent 500 keys so the file never grows unbounded.
            json.dump(list(keys)[-500:], f)
    except OSError:
        pass


def send_new_picks(picks: list, min_edge: float = 0.02) -> int:
    """Post each tier-A pick exactly once. `min_edge` matches the mockup's
    A-tier threshold. Returns the number of new posts made (0 when the
    webhook isn't configured or every pick is already dedupe-logged)."""
    if not is_configured():
        return 0
    sent = _load_dedupe()
    posted = 0
    for p in picks or []:
        if float(p.get("edge") or 0) < min_edge:
            continue
        key = _dedupe_key(p)
        if key in sent:
            continue
        if send_pick(p):
            sent.add(key)
            posted += 1
    if posted:
        _save_dedupe(sent)
    return posted
