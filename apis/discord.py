"""Discord webhook sender for the Board snapshot.

Renders the actionable (+EV) picks as a PNG screenshot and posts it to a
Discord channel via its incoming webhook. The PNG mimics the Board's own
dark-theme table: aligned columns, monospace numbers, ★ accents on edge.

Webhook URL loading order:
  1. explicit argument passed by the caller
  2. .streamlit/secrets.toml  →  discord_webhook_url = "..."
  3. env var DISCORD_WEBHOOK_URL
Site-URL loads similarly from `site_url` key / `EDGE_SITE_URL` env var.
Never log the webhook URL — it's the authorization token for the channel.
"""
from __future__ import annotations

import io
import json
import os
import ssl
import uuid
from datetime import datetime
from typing import Optional, List
from urllib.error import URLError, HTTPError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont

_CT = ZoneInfo("America/Chicago")

# ----- palette: mirrors the Board's dark theme (ui/style.py) -----
_BG = (10, 15, 28)          # --bg #0A0F1C
_CARD = (20, 28, 51)        # --card #141C33
_CARD_ELEV = (27, 36, 64)   # --card-elev #1B2440
_RULE = (35, 48, 85)        # --rule #233055
_RULE_STRONG = (51, 68, 113)  # --rule-strong #334471
_INK = (241, 245, 249)      # --ink
_INK_DIM = (203, 213, 225)  # --ink-dim
_MUTED = (127, 140, 168)    # --muted
_ACCENT = (56, 189, 248)    # --accent sky-500
_GOOD = (34, 197, 94)       # --good
_BAD = (239, 68, 68)        # --bad
_WARN = (245, 158, 11)
_LIVE = (244, 63, 94)       # --live


def _secret(name: str) -> Optional[str]:
    try:
        import streamlit as st
        if hasattr(st, "secrets") and name in st.secrets:
            v = st.secrets[name]
            return str(v) if v else None
    except Exception:
        pass
    return None


def get_webhook_url() -> Optional[str]:
    return (
        _secret("discord_webhook_url")
        or os.environ.get("DISCORD_WEBHOOK_URL")
    )


def get_site_url() -> Optional[str]:
    """URL of the deployed dashboard — used to link back from Discord."""
    return (
        _secret("site_url")
        or os.environ.get("EDGE_SITE_URL")
    )


def save_webhook_url(url: str) -> bool:
    return _save_secret("discord_webhook_url", url)


def save_site_url(url: str) -> bool:
    return _save_secret("site_url", url)


def _save_secret(key: str, value: str) -> bool:
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    secrets_path = os.path.join(here, ".streamlit", "secrets.toml")
    try:
        os.makedirs(os.path.dirname(secrets_path), exist_ok=True)
        existing = ""
        if os.path.exists(secrets_path):
            with open(secrets_path, "r", encoding="utf-8") as f:
                existing = f.read()
        kept = [ln for ln in existing.splitlines()
                if not ln.strip().startswith(f"{key} ")
                and not ln.strip().startswith(f"{key}=")]
        new_content = "\n".join(kept).rstrip()
        value = (value or "").strip()
        if value:
            if new_content:
                new_content += "\n"
            safe = value.replace('"', '\\"')
            new_content += f'{key} = "{safe}"\n'
        with open(secrets_path, "w", encoding="utf-8") as f:
            f.write(new_content + ("\n" if not new_content.endswith("\n") else ""))
        return True
    except OSError:
        return False


# ----- Font loading -----

_FONT_CACHE: dict = {}

def _font(size: int, bold: bool = False, mono: bool = False) -> ImageFont.ImageFont:
    """Load a bundled TrueType font, fall back to PIL's default bitmap font."""
    key = (size, bold, mono)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    # Pillow ships fonts under pil/fonts — DejaVuSans is usually present.
    candidates: List[str] = []
    if mono:
        candidates += ["DejaVuSansMono-Bold.ttf" if bold else "DejaVuSansMono.ttf"]
        candidates += ["consola.ttf" if bold else "consola.ttf"]  # Windows
    else:
        candidates += ["DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"]
        candidates += ["segoeuib.ttf" if bold else "segoeui.ttf"]  # Windows
    for name in candidates:
        try:
            f = ImageFont.truetype(name, size=size)
            _FONT_CACHE[key] = f
            return f
        except (OSError, IOError):
            continue
    _FONT_CACHE[key] = ImageFont.load_default()
    return _FONT_CACHE[key]


def _text_w(draw, text, font) -> int:
    try:
        bbox = draw.textbbox((0, 0), text, font=font)
        return int(bbox[2] - bbox[0])
    except AttributeError:
        return draw.textsize(text, font=font)[0]


def _ellipsize(draw, text, font, max_w) -> str:
    if _text_w(draw, text, font) <= max_w:
        return text
    while text and _text_w(draw, text + "…", font) > max_w:
        text = text[:-1]
    return text + "…"


# ----- PNG renderer -----

def _tier_from_edge(edge_val: float) -> str:
    """Mirror the site's tier chip: W (watch), S, A, A+ by edge magnitude."""
    if edge_val <= 0:
        return "W"
    if edge_val < 0.02:
        return "S"
    if edge_val < 0.05:
        return "A"
    return "A+"


def _tier_color(tier: str):
    return {
        "A+": _GOOD,
        "A":  _ACCENT,
        "S":  _WARN,
        "W":  _MUTED,
    }.get(tier, _MUTED)


def render_board_png(
    picks: List[dict],
    unit_dollars: float,
    sports_label: str,
    total_actionable: int,
    watch_count: int,
    truncated: int = 0,
) -> bytes:
    """Render an actionable-picks snapshot that mirrors the live Board.

    Columns, in the same order as the site table:
      #, Tier, Matchup · Start, Side · Market, Best Book, Price,
      Book Prob %, Sharp/Fair Prob %, Edge, Stake, Methodology
    Adds a "● LIVE PICK · IN GAME" chip when the pick was surfaced during
    the game (status_state == "in").
    """
    W = 1480
    row_h = 54  # taller rows so two-line matchup/side cells breathe
    header_h = 120
    table_header_h = 46
    footer_h = 74
    body_h = max(row_h, row_h * len(picks))
    H = header_h + table_header_h + body_h + footer_h

    img = Image.new("RGB", (W, H), _BG)
    draw = ImageDraw.Draw(img)

    # ----- Title -----
    now_ct = datetime.now(_CT)
    when_str = now_ct.strftime("%a, %b %d, %Y · %I:%M %p").replace(" 0", " ") + " CT"

    f_title = _font(28, bold=True)
    f_sub = _font(14)
    f_chip = _font(11, bold=True, mono=True)
    f_chip_sm = _font(10, bold=True, mono=True)

    dot_cx, dot_cy, dot_r = 34, 42, 8
    draw.ellipse([dot_cx - dot_r, dot_cy - dot_r, dot_cx + dot_r, dot_cy + dot_r], fill=_ACCENT)
    draw.text((56, 24), "Edge · Board snapshot", font=f_title, fill=_INK)

    draw.text((56, 64), when_str, font=f_sub, fill=_MUTED)
    subline_right = f"{sports_label}  ·  {total_actionable} actionable  ·  {watch_count} watch-list"
    rw = _text_w(draw, subline_right, f_sub)
    draw.text((W - rw - 28, 64), subline_right, font=f_sub, fill=_MUTED)
    draw.line([(28, 100), (W - 28, 100)], fill=_RULE, width=1)

    # ----- Columns (match the live board header verbatim) -----
    cols = [
        ("#",             52,  "num"),
        ("Tier",          70,  "chip"),
        ("Matchup · Start", 340, "left"),
        ("Side · Market", 230,  "left"),
        ("Best Book",     120,  "chip"),
        ("Price",          78,  "num"),
        ("Book Prob %",   100,  "num"),
        ("Sharp/Fair %",  120,  "num"),
        ("Edge",           78,  "num"),
        ("Stake",          90,  "num"),
        ("Methodology",   154,  "left"),
    ]
    th_y = header_h
    draw.rectangle([(0, th_y), (W, th_y + table_header_h)], fill=_CARD_ELEV)
    f_th = _font(11, bold=True, mono=True)
    x = 28
    for label, w, kind in cols:
        tw = _text_w(draw, label.upper(), f_th)
        if kind == "num":
            draw.text((x + w - tw - 12, th_y + 17), label.upper(), font=f_th, fill=_MUTED)
        else:
            draw.text((x + 10, th_y + 17), label.upper(), font=f_th, fill=_MUTED)
        x += w
    draw.line([(0, th_y + table_header_h), (W, th_y + table_header_h)], fill=_RULE_STRONG, width=1)

    # ----- Rows -----
    f_row = _font(13, mono=True)
    f_row_b = _font(13, bold=True, mono=True)
    f_match = _font(15, bold=True)
    f_side = _font(14, bold=True)
    f_sub_row = _font(11, mono=True)

    for i, p in enumerate(picks, start=1):
        y = header_h + table_header_h + (i - 1) * row_h
        if i % 2 == 0:
            draw.rectangle([(0, y), (W, y + row_h)], fill=_CARD)

        edge_val = float(p.get("edge") or 0)
        edge_pct = edge_val * 100.0
        fair_pct = float(p.get("fair_prob") or 0) * 100.0
        book_imp_pct = float(p.get("book_implied") or 0) * 100.0
        matchup = str(p.get("matchup") or "")
        side = str(p.get("side_label") or "")
        price = p.get("price")
        try:
            price_s = f"{int(price):+d}" if price is not None else "—"
        except (TypeError, ValueError):
            price_s = str(price)
        stake_u = p.get("_stake_units")
        stake_s = f"{float(stake_u):.1f}u" if stake_u is not None else "—"
        market_label = p.get("market_label") or {"h2h":"Moneyline","spreads":"Spread","totals":"Total"}.get(p.get("market") or "h2h", "Moneyline")
        best_book = str(p.get("book") or "—")
        start_time = p.get("_start_time_ct") or ""
        is_live_pick = (p.get("status_state") == "in")

        x = 28
        # Col 0: #
        rank_lbl = str(i)
        tw = _text_w(draw, rank_lbl, f_row_b)
        draw.text((x + cols[0][1] - tw - 12, y + 18), rank_lbl, font=f_row_b, fill=_INK_DIM)
        x += cols[0][1]

        # Col 1: Tier chip
        tier = _tier_from_edge(edge_val)
        tier_color = _tier_color(tier)
        chip_x0 = x + 10
        chip_y0 = y + 15
        chip_w = 46
        draw.rounded_rectangle(
            [chip_x0, chip_y0, chip_x0 + chip_w, chip_y0 + 22],
            radius=6,
            fill=(*tier_color, 48) if hasattr(tier_color, '__len__') and len(tier_color)==3 else _CARD_ELEV,
            outline=tier_color, width=1,
        )
        tw = _text_w(draw, tier, f_chip)
        draw.text((chip_x0 + (chip_w - tw) // 2, chip_y0 + 4), tier, font=f_chip, fill=tier_color)
        x += cols[1][1]

        # Col 2: Matchup · Start (two lines)
        mw = cols[2][1] - 18
        mu = _ellipsize(draw, matchup, f_match, mw)
        draw.text((x + 10, y + 7), mu, font=f_match, fill=_INK)
        time_line = start_time
        if is_live_pick:
            time_line = (time_line + "  " if time_line else "") + "● LIVE · IN GAME"
        if time_line:
            color = _LIVE if is_live_pick else _MUTED
            draw.text((x + 10, y + 31), _ellipsize(draw, time_line, f_sub_row, mw),
                      font=f_sub_row, fill=color)
        x += cols[2][1]

        # Col 3: Side · Market (two lines, like the live board's side-cell)
        sw = cols[3][1] - 18
        draw.text((x + 10, y + 7), _ellipsize(draw, market_label, f_sub_row, sw),
                  font=f_sub_row, fill=_MUTED)
        draw.text((x + 10, y + 24), _ellipsize(draw, side, f_side, sw),
                  font=f_side, fill=_ACCENT if edge_val > 0 else _INK)
        x += cols[3][1]

        # Col 4: Best Book chip
        is_kalshi = best_book.lower() == "kalshi"
        book_color = _ACCENT if is_kalshi else _WARN
        chip_w = min(cols[4][1] - 18, _text_w(draw, best_book, f_chip) + 18)
        chip_x0 = x + 10
        chip_y0 = y + 15
        draw.rounded_rectangle(
            [chip_x0, chip_y0, chip_x0 + chip_w, chip_y0 + 22],
            radius=6, fill=_CARD_ELEV, outline=book_color, width=1,
        )
        tw = _text_w(draw, best_book, f_chip)
        draw.text((chip_x0 + (chip_w - tw) // 2, chip_y0 + 4), best_book, font=f_chip, fill=book_color)
        x += cols[4][1]

        # Col 5: Price
        tw = _text_w(draw, price_s, f_row_b)
        draw.text((x + cols[5][1] - tw - 12, y + 18), price_s, font=f_row_b, fill=_INK)
        x += cols[5][1]

        # Col 6: Book Prob %
        s = f"{book_imp_pct:.1f}%"
        tw = _text_w(draw, s, f_row)
        draw.text((x + cols[6][1] - tw - 12, y + 18), s, font=f_row, fill=_MUTED)
        x += cols[6][1]

        # Col 7: Sharp/Fair Prob %
        s = f"{fair_pct:.1f}%"
        tw = _text_w(draw, s, f_row_b)
        fair_color = _ACCENT if edge_val > 0 else _MUTED
        draw.text((x + cols[7][1] - tw - 12, y + 18), s, font=f_row_b, fill=fair_color)
        x += cols[7][1]

        # Col 8: Edge
        edge_s = f"{edge_pct:+.2f}"
        edge_color = _GOOD if edge_val > 0 else (_BAD if edge_val < -0.001 else _MUTED)
        tw = _text_w(draw, edge_s, f_row_b)
        draw.text((x + cols[8][1] - tw - 12, y + 18), edge_s, font=f_row_b, fill=edge_color)
        x += cols[8][1]

        # Col 9: Stake
        stake_color = _ACCENT if (stake_u or 0) > 0 else _MUTED
        tw = _text_w(draw, stake_s, f_row_b)
        draw.text((x + cols[9][1] - tw - 12, y + 18), stake_s, font=f_row_b, fill=stake_color)
        x += cols[9][1]

        # Col 10: Methodology — "Model Edge · Live" / "Model Edge"
        meth = "Model Edge"
        meth_line2 = "In-play" if is_live_pick else "Pre-game"
        draw.text((x + 10, y + 11), meth, font=f_sub_row, fill=_INK_DIM)
        meth_color = _LIVE if is_live_pick else _MUTED
        draw.text((x + 10, y + 30), meth_line2, font=f_sub_row, fill=meth_color)

        # Row divider
        draw.line([(28, y + row_h - 1), (W - 28, y + row_h - 1)], fill=_RULE, width=1)

    # ----- Footer -----
    foot_y = header_h + table_header_h + body_h + 14
    f_foot = _font(13)
    f_foot_b = _font(13, bold=True)
    draw.line([(28, foot_y - 6), (W - 28, foot_y - 6)], fill=_RULE, width=1)
    foot_left = f"Showing {len(picks)} actionable (+EV) picks"
    if truncated:
        foot_left += f"  ·  +{truncated} more actionable hidden"
    draw.text((36, foot_y + 10), foot_left, font=f_foot_b, fill=_INK_DIM)
    foot_right = "Tier A+ = big edge · A = standard · S = thin · W = watch-list    ·    Edge agent"
    rw = _text_w(draw, foot_right, f_foot)
    draw.text((W - rw - 36, foot_y + 10), foot_right, font=f_foot, fill=_MUTED)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# ----- Multipart POST -----

def _multipart_post(url: str, payload: dict, png_bytes: bytes,
                    filename: str = "board.png", timeout: float = 15.0) -> dict:
    """POST payload_json + file to Discord's webhook multipart endpoint."""
    boundary = f"----edge{uuid.uuid4().hex}"
    body = io.BytesIO()
    def _w(b):
        body.write(b if isinstance(b, bytes) else b.encode("utf-8"))

    # payload_json part
    _w(f"--{boundary}\r\n")
    _w('Content-Disposition: form-data; name="payload_json"\r\n')
    _w("Content-Type: application/json\r\n\r\n")
    _w(json.dumps(payload))
    _w("\r\n")

    # file part
    _w(f"--{boundary}\r\n")
    _w(f'Content-Disposition: form-data; name="files[0]"; filename="{filename}"\r\n')
    _w("Content-Type: image/png\r\n\r\n")
    _w(png_bytes)
    _w("\r\n")
    _w(f"--{boundary}--\r\n")

    data = body.getvalue()
    req = Request(
        url,
        data=data,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": "edge-v2-dashboard",
        },
        method="POST",
    )
    try:
        with urlopen(req, timeout=timeout, context=ssl.create_default_context()) as resp:
            status = resp.getcode()
            return {"ok": 200 <= status < 300, "status": status, "error": None}
    except HTTPError as e:
        return {"ok": False, "status": e.code, "error": f"Discord returned HTTP {e.code}"}
    except URLError as e:
        return {"ok": False, "status": 0, "error": f"Network error: {e.reason}"}
    except (ValueError, TimeoutError, OSError) as e:
        return {"ok": False, "status": 0, "error": str(e)}


# ----- Public send -----

def send_board_snapshot(
    top_picks: list,
    unit_dollars: float = 0.0,
    webhook_url: Optional[str] = None,
    sports_label: str = "all sports",
    site_url: Optional[str] = None,
    max_rows: int = 15,
    timeout: float = 15.0,
) -> dict:
    """Post a PNG snapshot of the ACTIONABLE (+EV) picks to Discord.

    Message body carries:
      - a title line + timestamp,
      - how many watch-list + extra +EV picks are still on the dashboard,
      - a link back to the live dashboard for the full view.
    """
    url = webhook_url or get_webhook_url()
    if not url:
        return {"ok": False, "status": 0, "error": "No Discord webhook configured."}
    if not top_picks:
        return {"ok": False, "status": 0, "error": "Board is empty — nothing to send."}

    # Split by actionable vs watch-list.
    actionable = [p for p in top_picks if float(p.get("edge") or 0) > 0]
    watch_count = len(top_picks) - len(actionable)

    if not actionable:
        return {
            "ok": False, "status": 0,
            "error": f"No actionable (+EV) picks on the board right now. "
                     f"({watch_count} watch-list picks are ranked below.)",
        }

    # Precompute stake units for the renderer.
    for p in actionable:
        try:
            sd = float(p.get("stake_dollars") or 0)
        except (TypeError, ValueError):
            sd = 0.0
        p["_stake_units"] = (
            round((sd / unit_dollars) * 2) / 2 if unit_dollars > 0 else None
        )

    rendered = actionable[:max_rows]
    truncated = max(0, len(actionable) - max_rows)
    png = render_board_png(
        rendered,
        unit_dollars=unit_dollars,
        sports_label=sports_label,
        total_actionable=len(actionable),
        watch_count=watch_count,
        truncated=truncated,
    )

    # ----- Message body -----
    now_ct = datetime.now(_CT)
    unix_ts = int(now_ct.timestamp())
    site_link = site_url or get_site_url()
    site_line = (
        f"\n🔗 **Open the live board:** {site_link}"
        if site_link else
        "\n_Add your dashboard URL in Settings → Discord to include a link back._"
    )
    more_bits = []
    if truncated:
        more_bits.append(f"**+{truncated} more actionable (+EV)**")
    if watch_count:
        more_bits.append(f"**{watch_count} watch-list** (ranked by Pinn fair prob, no price edge)")
    more_line = (" · ".join(more_bits) + "\n") if more_bits else ""

    content = (
        f"🎯 **Edge · Board snapshot** — <t:{unix_ts}:F> (<t:{unix_ts}:R>)\n"
        f"📊 **{len(actionable)} actionable (+EV)** pick(s) on {sports_label}\n"
        f"{('📂 Also on board: ' + more_line) if more_line else ''}"
        f"{site_line}"
    )
    payload = {"username": "Edge · Board", "content": content}
    return _multipart_post(url, payload, png, filename="edge-board.png", timeout=timeout)
