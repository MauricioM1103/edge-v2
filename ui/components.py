"""Reusable UI components (pure HTML strings so they can be used from any page)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from calc.odds import (
    format_american, implied_prob_american, american_to_decimal, american_to_fractional
)
from calc.ev import ev_percent
from calc.clv import line_move_prob_delta, classify_move

EASTERN = ZoneInfo("America/New_York")


def html(s: str):
    """Shorthand: render HTML in Streamlit.

    Streamlit's markdown parser closes raw HTML blocks on blank or 4+ space-indented
    lines. We flatten the string to one line (strip inter-tag whitespace) so the
    parser treats the whole thing as one HTML block.
    """
    import streamlit as st
    import re
    # Collapse all runs of whitespace between > and < to a single space,
    # then collapse remaining multi-whitespace to one space.
    flat = re.sub(r">\s+<", "><", s)
    flat = re.sub(r"\s+", " ", flat).strip()
    st.markdown(flat, unsafe_allow_html=True)


def brand_bar(subtitle: str = ""):
    # %-d/%-I isn't portable to Windows - use zero-padded codes and strip leading zeros manually.
    try:
        dt = datetime.now(EASTERN)
        day = str(dt.day)
        hour12 = dt.hour % 12 or 12
        now_et = f"{dt.strftime('%a %b')} {day} · {hour12}:{dt.strftime('%M %p')} ET"
    except Exception:
        now_et = ""
    html(f"""
    <div class="brand-bar">
      <div class="brand">
        <span class="dot"></span>
        <span class="name">Edge</span>
        <span class="ver">v2.0</span>
      </div>
      <div class="brand-right">{subtitle or now_et}</div>
    </div>
    """)


def metric_card(label: str, value: str, foot: str = "", tone: str = ""):
    tone_cls = f" {tone}" if tone in ("good", "bad", "accent") else ""
    return f"""
    <div class="metric-card">
      <div class="label">{label}</div>
      <div class="value{tone_cls}">{value}</div>
      {f'<div class="foot">{foot}</div>' if foot else ''}
    </div>
    """


def metric_row(items):
    """items: list of dicts with keys label, value, foot?, tone?."""
    cards = "\n".join(metric_card(**i) for i in items)
    html(f'<div class="metric-grid">{cards}</div>')


def _format_price_with_imp(american) -> str:
    s = format_american(american)
    p = implied_prob_american(american)
    if p is None:
        return f'<span class="price">{s}</span>'
    return f'<span class="price">{s}</span><span class="imp">({p*100:.1f}%)</span>'


def _format_point(point) -> str:
    if point is None:
        return ""
    try:
        v = float(point)
    except (TypeError, ValueError):
        return ""
    sign = "+" if v > 0 else ""
    return f" {sign}{v}"


def game_card(event: dict, odds_meta: Optional[dict] = None, prob_sources: Optional[dict] = None):
    """Render a single game card with ESPN schedule info and odds markets.

    prob_sources (optional): per-side implied probabilities from different venues
    for the side-by-side comparison strip. Shape:
      {
        "kalshi": {"home": 0.58, "away": 0.42},
        "dk":     {"home": 0.55, "away": 0.47},
        "consensus": {"home": 0.56, "away": 0.46},
      }
    """
    sport = event.get("sport") or ""
    status_state = event.get("status_state")
    status_detail = event.get("status_detail") or ""
    is_live = status_state == "in"
    is_final = status_state == "post"

    start_dt = event.get("start_utc")
    if start_dt:
        try:
            start_et = start_dt.astimezone(EASTERN).strftime("%I:%M %p ET").lstrip("0")
        except Exception:
            start_et = status_detail or "TBD"
    else:
        start_et = status_detail or "TBD"

    away = event.get("away") or {}
    home = event.get("home") or {}

    def _side(side_key, data):
        score = data.get("score")
        score_html = ""
        if score is not None and (is_live or is_final):
            score_html = f'<span class="vsin" style="font-size:22px; margin-left:6px;">{score}</span>'
        return f"""
        <div class="gc-team {side_key}">
          <div class="name">{data.get('name') or '-'} {score_html}</div>
          <div class="record">{data.get('record') or ''}</div>
        </div>
        """

    status_chip = ""
    if is_live:
        status_chip = f'<span class="chip live">● {status_detail or "Live"}</span>'
    elif is_final:
        status_chip = f'<span class="chip final">Final</span>'

    sport_chip = f'<span class="chip">{sport.upper()}</span>'

    # ----- lines block (h2h / spreads / totals) -----
    def _market_block(label, home_price, away_price, home_pt=None, away_pt=None, extra=""):
        if home_price is None and away_price is None:
            return f"""
            <div class="gc-line">
              <div class="label">{label}</div>
              <div class="row"><span class="side">—</span></div>
            </div>
            """
        return f"""
        <div class="gc-line">
          <div class="label">{label}{extra}</div>
          <div class="row">
            <span class="side">{away.get('abbr') or 'AWAY'}{_format_point(away_pt)}</span>
            {_format_price_with_imp(away_price)}
          </div>
          <div class="row">
            <span class="side">{home.get('abbr') or 'HOME'}{_format_point(home_pt)}</span>
            {_format_price_with_imp(home_price)}
          </div>
        </div>
        """

    lines_html = ""
    if odds_meta:
        move_html = ""
        h2h = odds_meta.get("h2h") or {}
        spr = odds_meta.get("spreads") or {}
        tot = odds_meta.get("totals") or {}
        # line move indicator (home h2h move)
        if h2h and h2h.get("home_opener") is not None:
            delta = line_move_prob_delta(h2h.get("home_opener"), h2h.get("home_price"))
            label = classify_move(delta)
            if delta is not None and abs(delta) >= 0.005:
                move_html = f'<span class="chip {("good" if delta>0 else "warn")}">{label} {delta*100:+.1f}%</span>'

        lines_html = f"""
        <div class="gc-head">
          <div style="display:flex; gap:6px; align-items:center;">
            <span class="chip">Consensus · {h2h.get('book_count') or spr.get('book_count') or tot.get('book_count') or 0} books</span>
            {move_html}
          </div>
          <div class="chip">Best price shown live</div>
        </div>
        <div class="gc-lines">
          {_market_block("Moneyline", h2h.get("home_price"), h2h.get("away_price"))}
          {_market_block("Spread", spr.get("home_price"), spr.get("away_price"), spr.get("home_point"), spr.get("away_point"))}
          {_market_block("Total", tot.get("home_price"), tot.get("away_price"))}
        </div>
        """
    else:
        lines_html = """
        <div class="gc-lines">
          <div class="gc-line"><div class="label">Lines</div><div class="row"><span class="side">Add The Odds API key for live markets</span></div></div>
        </div>
        """

    tv = event.get("broadcast") or ""
    venue = event.get("venue") or ""
    foot_items = []
    if venue:
        foot_items.append(f'<span class="item">◇ {venue}</span>')
    if tv:
        foot_items.append(f'<span class="item">▶ {tv}</span>')
    foot_html = f'<div class="gc-foot">{"".join(foot_items)}</div>' if foot_items else ""

    # ----- side-by-side implied-probability strip (Kalshi vs DK vs consensus) -----
    prob_strip_html = ""
    if prob_sources:
        def _fmt(v):
            if v is None:
                return "—"
            return f"{v*100:.1f}%"
        rows = []
        # order: Pinnacle (sharpest book) → exchanges (no-vig) → DK (public book) → consensus
        for label, key, chip_cls in [
            ("Pinnacle",   "pinnacle",   "good"),
            ("Kalshi",     "kalshi",     "accent"),
            ("Polymarket", "polymarket", "accent"),
            ("Betfair",    "betfair",    "accent"),
            ("DraftKings", "dk",         "warn"),
            ("Consensus",  "consensus",  ""),
        ]:
            src = prob_sources.get(key) or {}
            ah = _fmt(src.get("away"))
            hh = _fmt(src.get("home"))
            if ah == "—" and hh == "—":
                continue
            rows.append(
                f'<tr>'
                f'<td style="color:var(--muted); font-size:11px;"><span class="chip {chip_cls}" style="padding:2px 6px;">{label}</span></td>'
                f'<td class="mono num" style="text-align:right;">{ah}</td>'
                f'<td class="mono num" style="text-align:right; color:var(--accent);">{hh}</td>'
                f'</tr>'
            )
        if rows:
            prob_strip_html = (
                '<div style="border-top: 1px dashed var(--rule); padding-top: 10px;">'
                '<div class="label mono" style="font-size:10px; letter-spacing:0.14em; text-transform:uppercase; color:var(--muted); margin-bottom:6px;">Live Implied Probability</div>'
                '<table style="width:100%; border-collapse:collapse;" class="prob-strip">'
                f'<thead><tr><th></th><th style="text-align:right; font-size:10px; letter-spacing:0.1em; text-transform:uppercase; color:var(--muted); font-weight:500;">{(away.get("abbr") or "AWAY")}</th><th style="text-align:right; font-size:10px; letter-spacing:0.1em; text-transform:uppercase; color:var(--muted); font-weight:500;">{(home.get("abbr") or "HOME")}</th></tr></thead>'
                f'<tbody>{"".join(rows)}</tbody>'
                '</table>'
                '</div>'
            )

    html(f"""
    <div class="game-card">
      <div class="gc-head">
        <span class="gc-time">{start_et}</span>
        <div style="display:flex; gap:6px;">
          {sport_chip}
          {status_chip}
        </div>
      </div>
      <div class="gc-matchup">
        {_side("away", away)}
        <div class="gc-at">at</div>
        {_side("home", home)}
      </div>
      {prob_strip_html}
      {lines_html}
      {foot_html}
    </div>
    """)


def ev_tone(ev):
    try:
        v = float(ev) if ev is not None else None
    except (TypeError, ValueError):
        return ""
    if v is None:
        return ""
    if v > 0:
        return "good"
    if v < 0:
        return "bad"
    return ""


def odds_table_row(american):
    """Four-column HTML row showing American/Decimal/Fractional + implied prob."""
    dec = american_to_decimal(american)
    frac = american_to_fractional(american)
    imp = implied_prob_american(american)
    dec_s = f"{dec:.4f}" if dec is not None else "—"
    imp_s = f"{imp*100:.2f}%" if imp is not None else "—"
    return (
        "<tr>"
        f'<td class="mono">{format_american(american)}</td>'
        f'<td class="mono num">{dec_s}</td>'
        f'<td class="mono">{frac or "—"}</td>'
        f'<td class="mono num">{imp_s}</td>'
        "</tr>"
    )
