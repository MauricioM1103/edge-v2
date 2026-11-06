"""Discord webhook sender for the Board snapshot.

Posts the top-N picks currently on the Board to a Discord channel via its
incoming webhook. The webhook URL is write-only: Discord can't send back,
so there's no two-way interaction — the user clicks a button here, the
snapshot lands in Discord, done.

Webhook URL loading order:
  1. explicit argument passed by the caller
  2. .streamlit/secrets.toml  →  discord_webhook_url = "..."
  3. env var DISCORD_WEBHOOK_URL

Never log the URL — it's the authorization token for that webhook.
"""
from __future__ import annotations

import json
import os
import ssl
from datetime import datetime
from typing import Optional
from urllib.error import URLError, HTTPError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

_CT = ZoneInfo("America/Chicago")


def _secret(name: str) -> Optional[str]:
    """Fetch a value from Streamlit secrets without crashing when secrets.toml is absent."""
    try:
        import streamlit as st
        if hasattr(st, "secrets") and name in st.secrets:
            v = st.secrets[name]
            return str(v) if v else None
    except Exception:
        pass
    return None


def get_webhook_url() -> Optional[str]:
    """Resolve the webhook URL from secrets / env, or None if not configured."""
    return (
        _secret("discord_webhook_url")
        or os.environ.get("DISCORD_WEBHOOK_URL")
    )


def save_webhook_url(url: str) -> bool:
    """Persist the webhook URL to .streamlit/secrets.toml (gitignored).

    Returns True on write success. Stripped of whitespace; blank string
    removes the saved value.
    """
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    secrets_path = os.path.join(here, ".streamlit", "secrets.toml")
    try:
        os.makedirs(os.path.dirname(secrets_path), exist_ok=True)
        existing = ""
        if os.path.exists(secrets_path):
            with open(secrets_path, "r", encoding="utf-8") as f:
                existing = f.read()
        # Drop any prior discord_webhook_url line, keep the rest.
        kept = [ln for ln in existing.splitlines()
                if not ln.strip().startswith("discord_webhook_url")]
        new_content = "\n".join(kept).rstrip()
        url = (url or "").strip()
        if url:
            if new_content:
                new_content += "\n"
            # Escape any double quotes in the url — unlikely but defensive.
            safe = url.replace('"', '\\"')
            new_content += f'discord_webhook_url = "{safe}"\n'
        with open(secrets_path, "w", encoding="utf-8") as f:
            f.write(new_content + ("\n" if not new_content.endswith("\n") else ""))
        return True
    except OSError:
        return False


def _format_pick_line(idx: int, p: dict) -> str:
    """One line of the board snapshot table. Tab-separated so Discord's
    code-block rendering aligns columns in the user's monospace font."""
    sport = (p.get("_sport") or "").upper()[:5]
    matchup = (p.get("matchup") or "")[:34]
    side = (p.get("side_label") or "")[:22]
    fair = float(p.get("fair_prob") or 0) * 100
    edge = float(p.get("edge") or 0) * 100
    price = p.get("price")
    try:
        price_s = f"{int(price):+d}" if price is not None else "—"
    except (TypeError, ValueError):
        price_s = str(price)
    stake_u = p.get("_stake_units")
    stake_s = f"{float(stake_u):>4.1f}u" if stake_u is not None else "   —"
    marker = "★" if edge > 0 else " "  # mark actionable (+EV) rows
    return (
        f"{marker} {idx:>2}. {sport:<5}  "
        f"{matchup:<34}  {side:<22}  "
        f"{fair:>5.1f}%  {edge:+6.2f}  {price_s:>5}  {stake_s}"
    )


def send_board_snapshot(
    top_picks: list,
    unit_dollars: float = 0.0,
    webhook_url: Optional[str] = None,
    sports_label: str = "all sports",
    timeout: float = 10.0,
) -> dict:
    """POST the board snapshot to Discord. Returns {ok, status, error}.

    The message uses a code block so column alignment survives Discord's
    markdown renderer. A row prefixed with ★ carries positive EV
    (actionable); plain-prefixed rows are watch-list (ranked by fair
    prob but no price edge).
    """
    url = webhook_url or get_webhook_url()
    if not url:
        return {"ok": False, "status": 0, "error": "No Discord webhook configured."}
    if not top_picks:
        return {"ok": False, "status": 0, "error": "Board is empty — nothing to send."}

    # Precompute stake units so _format_pick_line can print them.
    for p in top_picks:
        try:
            sd = float(p.get("stake_dollars") or 0)
        except (TypeError, ValueError):
            sd = 0.0
        if unit_dollars > 0:
            p["_stake_units"] = round((sd / unit_dollars) * 2) / 2
        else:
            p["_stake_units"] = None

    now_ct = datetime.now(_CT)
    when_str = now_ct.strftime("%a, %b %d, %Y · %I:%M %p").replace(" 0", " ") + " CT"
    # Also provide a precise ISO timestamp Discord can render as a relative age.
    unix_ts = int(now_ct.timestamp())

    pos_count = sum(1 for p in top_picks if float(p.get("edge") or 0) > 0)
    watch_count = len(top_picks) - pos_count

    header = (
        f"🎯 **Edge · Board snapshot** — <t:{unix_ts}:F> (<t:{unix_ts}:R>)\n"
        f"📅 {when_str} · {sports_label} · "
        f"**{pos_count} +EV**, {watch_count} watch-list\n"
    )
    # Column header for the code block
    table_header = (
        "  #  SPORT  MATCHUP                             PICK                    "
        " FAIR   EDGE  PRICE  STAKE"
    )
    lines = [table_header]
    for i, p in enumerate(top_picks, start=1):
        lines.append(_format_pick_line(i, p))
    legend = "\n★ = +EV (actionable)   blank = watch-list (ranked by Pinn fair prob)"
    body = header + "```\n" + "\n".join(lines) + legend + "\n```"

    # Discord's hard limit is 2000 chars for content. If we overflow, trim
    # the oldest watch-list rows first and note the truncation.
    if len(body) > 1950:
        kept = []
        trimmed_count = 0
        for p in top_picks:
            trial = kept + [p]
            trial_body = header + "```\n" + "\n".join(
                [table_header] + [_format_pick_line(i+1, pp) for i, pp in enumerate(trial)]
            ) + legend + "\n```"
            if len(trial_body) > 1900:
                trimmed_count = len(top_picks) - len(kept)
                break
            kept.append(p)
        body = header + "```\n" + "\n".join(
            [table_header] + [_format_pick_line(i+1, pp) for i, pp in enumerate(kept)]
        ) + legend + f"\n+ {trimmed_count} more hidden (Discord 2k char limit)\n```"

    payload = {"username": "Edge · Board", "content": body}
    req = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "edge-v2-dashboard"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=timeout, context=ssl.create_default_context()) as resp:
            status = resp.getcode()
            return {"ok": 200 <= status < 300, "status": status, "error": None}
    except HTTPError as e:
        # Never echo the URL back in an error — the token lives in the path.
        return {"ok": False, "status": e.code, "error": f"Discord returned HTTP {e.code}"}
    except URLError as e:
        return {"ok": False, "status": 0, "error": f"Network error: {e.reason}"}
    except (ValueError, TimeoutError, OSError) as e:
        return {"ok": False, "status": 0, "error": str(e)}
