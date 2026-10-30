"""Tab renderers: Dashboard, Pro Tools, Picks Ledger."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

import streamlit as st

from apis import espn, odds as odds_api, kalshi, polymarket, betfair
from apis.team_mapping import same_team
from apis.book_filter import book_market
from apis.kalshi_match import find_event_markets, home_implied_prob
from calc.odds import (
    american_to_decimal, american_to_fractional, decimal_to_american,
    implied_prob_american, format_american, profit_on_win, no_vig_two_way,
)
from calc.ev import expected_value, ev_percent, edge as calc_edge
from calc.kelly import kelly_fraction, kelly_stake
from calc.units import recommend_units, units_from_kelly, format_units
from calc.clv import line_move_prob_delta, classify_move, clv_percent
from calc import agent as agent_calc
from storage import picks as picks_store, settings as settings_store
from ui.components import (
    html, brand_bar, metric_row, game_card, ev_tone, odds_table_row,
)
from apis import odds_history
from backtest import engine as backtest_engine


# ============================================================================
# cached data loaders - respect API rate limits
# ============================================================================

@st.cache_data(ttl=120, show_spinner=False)
def load_espn_scoreboard(sport_key: str, date_str: Optional[str] = None):
    try:
        return espn.get_scoreboard(sport_key, date_str)
    except Exception:
        return []


@st.cache_data(ttl=180, show_spinner=False)
def load_odds(sport_key: str, regions: str = "us,eu"):
    odds_sport = espn.ODDS_API_SPORT_KEYS.get(sport_key)
    if not odds_sport:
        return []
    try:
        return odds_api.get_odds(odds_sport, regions=regions)
    except Exception:
        return []


@st.cache_data(ttl=60, show_spinner=False)
def load_kalshi_markets(sport_key: str):
    try:
        return kalshi.fetch_sport_markets(sport_key)
    except Exception:
        return []


@st.cache_data(ttl=45, show_spinner=False)
def load_kalshi_balance():
    try:
        return kalshi.resolve_balance()
    except Exception:
        return {"usd": None, "source": None}


@st.cache_data(ttl=120, show_spinner=False)
def load_polymarket_markets():
    try:
        return polymarket.list_active_markets(limit=500)
    except Exception:
        return []


@st.cache_data(ttl=120, show_spinner=False)
def load_betfair_configured() -> bool:
    try:
        return betfair.is_configured()
    except Exception:
        return False


def _prob_sources_for(game_meta, kalshi_prob_home, polymarket_prob_home=None, betfair_prob_home=None):
    """Build the prob-strip dict: Pinnacle + DK + Kalshi + Polymarket + Betfair + consensus."""
    sources = {}
    raw = (game_meta or {}).get("raw")
    # Pinnacle (sharpest book, from Odds API if present)
    if raw:
        pin = book_market(raw, "Pinnacle", "h2h") or book_market(raw, "pinnacle", "h2h")
        if pin:
            pin_h = implied_prob_american(pin.get("home_price"))
            pin_a = implied_prob_american(pin.get("away_price"))
            if pin_h is not None or pin_a is not None:
                sources["pinnacle"] = {"home": pin_h, "away": pin_a}
        dk = book_market(raw, "DraftKings", "h2h") or book_market(raw, "draftkings", "h2h")
        if dk:
            dk_h = implied_prob_american(dk.get("home_price"))
            dk_a = implied_prob_american(dk.get("away_price"))
            if dk_h is not None or dk_a is not None:
                sources["dk"] = {"home": dk_h, "away": dk_a}
    # Kalshi (prediction market, no vig)
    if kalshi_prob_home is not None:
        sources["kalshi"] = {"home": kalshi_prob_home, "away": 1.0 - kalshi_prob_home}
    # Polymarket (prediction market, no vig)
    if polymarket_prob_home is not None:
        sources["polymarket"] = {"home": polymarket_prob_home, "away": 1.0 - polymarket_prob_home}
    # Betfair Exchange (peer-to-peer, no vig)
    if betfair_prob_home is not None:
        sources["betfair"] = {"home": betfair_prob_home, "away": 1.0 - betfair_prob_home}
    # Consensus from all sportsbooks (vig-included)
    h2h = (game_meta or {}).get("h2h") or {}
    ch_imp = implied_prob_american(h2h.get("home_price"))
    ca_imp = implied_prob_american(h2h.get("away_price"))
    if ch_imp is not None or ca_imp is not None:
        sources["consensus"] = {"home": ch_imp, "away": ca_imp}
    return sources or None


def _merge_events_with_odds(
    events, odds_games,
    kalshi_markets=None, polymarket_markets=None, sport_key=None,
):
    """Attach odds + Kalshi + Polymarket + Betfair implied probs to each ESPN event.

    Returns list of (event, odds_meta, exchange_probs) where exchange_probs
    is a dict with keys 'kalshi' / 'polymarket' / 'betfair' (each a float P(home) or None).
    """
    betfair_available = load_betfair_configured()
    out = []
    for ev in events:
        home_name = (ev.get("home") or {}).get("name")
        away_name = (ev.get("away") or {}).get("name")
        matched = None
        for og in odds_games:
            if same_team(og.get("home_team"), home_name) and same_team(og.get("away_team"), away_name):
                matched = og
                break
        odds_meta = None
        if matched:
            odds_meta = {
                "h2h": odds_api.consensus_market(matched, "h2h"),
                "spreads": odds_api.consensus_market(matched, "spreads"),
                "totals": odds_api.consensus_market(matched, "totals"),
                "raw": matched,
            }
        exch_probs = {"kalshi": None, "polymarket": None, "betfair": None}
        # Kalshi
        if kalshi_markets:
            home_abbr = (ev.get("home") or {}).get("abbr") or ""
            away_abbr = (ev.get("away") or {}).get("abbr") or ""
            event_markets = find_event_markets(
                kalshi_markets, ev.get("start_utc"),
                home_abbr, away_abbr, home_name, away_name,
            )
            if event_markets:
                exch_probs["kalshi"] = home_implied_prob(event_markets, home_name, home_abbr)
        # Polymarket
        if polymarket_markets:
            pm = polymarket.find_game_market(
                polymarket_markets, home_name, away_name, ev.get("start_utc"),
            )
            if pm:
                exch_probs["polymarket"] = polymarket.home_implied_prob(pm, home_name, away_name)
        # Betfair - only if auth configured; otherwise skip the round-trip
        if betfair_available and sport_key:
            try:
                exch_probs["betfair"] = betfair.home_implied_prob_from_event(
                    sport_key, home_name, away_name,
                )
            except Exception:
                exch_probs["betfair"] = None
        out.append((ev, odds_meta, exch_probs))
    return out


# ============================================================================
# TAB 1 - Dashboard / Live Daily Slate
# ============================================================================

def render_dashboard(settings: dict):
    brand_bar("Live Daily Slate")

    sport = settings.get("sport") or "nfl"
    unit_pct = settings.get("unit_pct") or 0.01
    bankroll_units = settings.get("bankroll_units") or 100.0
    kalshi_cash = settings.get("kalshi_cash") or 0.0

    # fetch
    saved_cfg = settings_store.load()
    regions = saved_cfg.get("odds_regions") or "us,eu"
    events = load_espn_scoreboard(sport)
    odds_games = load_odds(sport, regions=regions)
    kalshi_markets = load_kalshi_markets(sport)
    polymarket_markets = load_polymarket_markets()
    merged = _merge_events_with_odds(
        events, odds_games,
        kalshi_markets=kalshi_markets,
        polymarket_markets=polymarket_markets,
        sport_key=sport,
    )

    # summary metrics
    picks = picks_store.list_picks()
    s = picks_store.summarize(picks)
    live_count = sum(1 for ev, _, _ in merged if ev.get("is_live"))
    final_count = sum(1 for ev, _, _ in merged if ev.get("is_final"))
    upcoming = sum(1 for ev, _, _ in merged if ev.get("status_state") == "pre")
    api_key_ok = odds_api.get_api_key() is not None

    net_tone = "good" if (s["net_units"] or 0) > 0 else ("bad" if (s["net_units"] or 0) < 0 else "")
    roi_display = f"{(s['roi'] or 0)*100:+.1f}%" if s["roi"] is not None else "—"
    kalshi_source = settings.get("kalshi_source")
    kalshi_display = f"${kalshi_cash:,.2f}" if kalshi_cash else "—"

    metric_row([
        {"label": "Kalshi cash", "value": kalshi_display, "foot": (kalshi_source or "not configured"), "tone": "accent" if kalshi_source == "live" else ""},
        {"label": "Bankroll", "value": f"{bankroll_units:.1f}u", "foot": f"1u = {unit_pct*100:.1f}% of roll"},
        {"label": "Open picks", "value": str(s["open"]), "foot": "awaiting result"},
        {"label": "Record", "value": f"{s['wins']}-{s['losses']}-{s['pushes']}", "foot": f"settled: {s['total_settled']}"},
        {"label": "Net units", "value": f"{s['net_units']:+.2f}u", "foot": roi_display + " ROI", "tone": net_tone},
        {"label": "Avg CLV", "value": f"{(s['avg_clv_pct'] or 0)*100:+.2f}%" if s["avg_clv_pct"] is not None else "—", "foot": "closing line value"},
        {"label": f"{sport.upper()} today", "value": f"{live_count + upcoming + final_count}", "foot": f"{live_count} live · {final_count} final"},
    ])

    if not api_key_ok:
        html('<div class="hint warn">The Odds API key not set — showing ESPN schedule only. Add <code>THE_ODDS_API_KEY</code> to your environment or <code>.streamlit/secrets.toml</code> to enable live lines, EV, and Kelly suggestions.</div>')

    # Agent recommendations panel - pass per-game Kalshi probs so agent uses them as fair.
    kalshi_probs_by_game = {}
    for og in odds_games or []:
        home_name = og.get("home_team")
        away_name = og.get("away_team")
        # Find the ESPN event that matches this odds game for the Kalshi lookup.
        for ev, _om, exch in merged:
            if same_team(ev.get("home", {}).get("name"), home_name) and \
               same_team(ev.get("away", {}).get("name"), away_name):
                if exch.get("kalshi") is not None:
                    kalshi_probs_by_game[og.get("id")] = exch["kalshi"]
                break
    _render_agent_panel(odds_games, kalshi_cash, settings, kalshi_probs_by_game=kalshi_probs_by_game)

    if not merged:
        html('<div class="hint">No games on the slate for this sport right now. Try another sport in the sidebar, or check back later.</div>')
        return

    # quick calc feed above the grid
    _render_quick_calculator(merged, unit_pct, bankroll_units, sport)

    # 3-column game grid - Streamlit columns to get real multi-column
    st.markdown('### Today\'s matchups')
    primary_book = settings.get("primary_book") or "kalshi"
    extra_sources = set(settings.get("extra_sources") or [])
    # Kalshi always shows in the strip; the user toggles the rest.
    visible_sources = {"kalshi"} | extra_sources
    cols = st.columns(2)
    for i, (ev, odds_meta, exch_probs) in enumerate(merged):
        with cols[i % 2]:
            all_sources = _prob_sources_for(
                odds_meta,
                kalshi_prob_home=exch_probs.get("kalshi"),
                polymarket_prob_home=exch_probs.get("polymarket"),
                betfair_prob_home=exch_probs.get("betfair"),
            )
            # Filter down to what the user asked to see.
            prob_sources = None
            if all_sources:
                # map UI key -> prob_sources key (dk == draftkings)
                key_map = {
                    "kalshi": "kalshi", "pinnacle": "pinnacle",
                    "draftkings": "dk", "polymarket": "polymarket",
                    "betfair": "betfair", "consensus": "consensus",
                }
                prob_sources = {
                    key_map[k]: all_sources.get(key_map[k])
                    for k in visible_sources
                    if all_sources.get(key_map.get(k, k))
                }
                if not prob_sources:
                    prob_sources = None
            game_card(
                ev, odds_meta,
                prob_sources=prob_sources,
                primary_book=primary_book,
                kalshi_prob_home=exch_probs.get("kalshi"),
            )
            _add_pick_control(ev, odds_meta, unit_pct, bankroll_units)


def _render_agent_panel(odds_games, kalshi_cash, settings, kalshi_probs_by_game=None):
    """Agent edge-scanner. Uses Kalshi as fair (DK fallback), sizes with Kalshi cash."""
    st.markdown("### Agent · Edge scanner")
    kelly_mult = settings.get("agent_kelly") or 0.25
    min_edge = settings.get("agent_min_edge") or 0.02
    min_books = settings.get("agent_min_books") or 1
    max_bet = settings.get("agent_max_bet_pct") or 0.05
    fallback_book = settings.get("fallback_book") or "DraftKings"

    if not kalshi_cash or kalshi_cash <= 0:
        html(
            '<div class="hint warn">Agent is <strong>idle</strong>: configure Kalshi cash in the sidebar '
            '(live via API or manual override) so recommendations can be sized in dollars.</div>'
        )
        return

    if not odds_games:
        html('<div class="hint">Agent is waiting on live odds. Add <code>THE_ODDS_API_KEY</code> to scan for edges.</div>')
        return

    edges = agent_calc.scan_all(
        odds_games, bankroll_cash=kalshi_cash,
        kalshi_probs_by_game=kalshi_probs_by_game or {},
        fallback_book=fallback_book,
        kelly_mult=kelly_mult, min_edge_pct=min_edge,
        min_books=min_books, max_bet_pct=max_bet,
        markets=("h2h", "spreads", "totals"),
    )
    summary = agent_calc.summarize_edges(edges)
    metric_row([
        {"label": "Edges found", "value": str(summary["count"]), "foot": f"min edge {min_edge*100:.1f}%", "tone": "good" if summary["count"] else ""},
        {"label": "Best edge", "value": f"{(summary['best_edge'] or 0)*100:+.2f} pts" if summary["best_edge"] else "—", "foot": "vs no-vig fair"},
        {"label": "Total stake", "value": f"${summary['total_stake']:,.2f}", "foot": f"{kelly_mult*100:.0f}% Kelly · capped {max_bet*100:.1f}%/bet"},
        {"label": "Avg edge", "value": f"{(summary['avg_edge'] or 0)*100:+.2f} pts" if summary["avg_edge"] else "—", "foot": "across found edges"},
    ])

    if not edges:
        html('<div class="hint">No edges meet the threshold right now. Lower the edge requirement in the sidebar to see thinner opportunities.</div>')
        return

    # Compact table of top edges, with the exact $ amount to bet.
    rows = []
    for e in edges[:20]:
        edge_pct = e["edge"] * 100.0
        tone = "ev-plus" if edge_pct > 0 else "ev-minus"
        pt = e.get("point")
        market = (e.get("market") or "").lower()
        # Format the full pick in a human / Kalshi-like way.
        if market == "spreads" and pt is not None:
            try:
                pt_f = float(pt)
                pt_str = f" {pt_f:+g}" if abs(pt_f - round(pt_f)) > 0.49 else f" {pt_f:+.1f}"
            except (TypeError, ValueError):
                pt_str = ""
            full_pick = f"{e['side_label']}{pt_str}"
        elif market == "totals" and pt is not None:
            # side_label is already "Over" / "Under"
            try:
                pt_f = float(pt)
            except (TypeError, ValueError):
                pt_f = 0
            full_pick = f"{e['side_label']} {pt_f:g}"
        else:
            full_pick = e["side_label"]
        fair_src = e.get("fair_source") or "—"
        fair_badge_cls = "accent" if "kalshi" in fair_src.lower() else ("good" if "pinnacle" in fair_src.lower() else "")
        rows.append(
            f'<tr>'
            f'<td><strong>{e["matchup"]}</strong></td>'
            f'<td class="mono" style="color:var(--muted);">{market.upper()}</td>'
            f'<td><strong>{full_pick}</strong></td>'
            f'<td class="num mono">{format_american(e["price"])}</td>'
            f'<td class="mono">{e["book"]}</td>'
            f'<td class="num mono">{e["book_implied"]*100:.1f}%</td>'
            f'<td class="num mono" style="color:var(--accent);">{e["fair_prob"]*100:.1f}%</td>'
            f'<td><span class="chip {fair_badge_cls}" style="padding:1px 6px; font-size:9px;">{fair_src}</span></td>'
            f'<td class="num mono {tone}"><strong>{edge_pct:+.2f}</strong></td>'
            f'<td class="num mono"><strong>${e["stake_dollars"]:,.2f}</strong></td>'
            f'</tr>'
        )
    html(
        '<table class="ledger" style="margin-top:4px;">'
        '<thead><tr>'
        '<th>Matchup</th><th>Market</th><th>Side</th>'
        '<th class="num">Price</th><th>Book</th>'
        '<th class="num" title="Vig-inclusive implied probability from that book">Book IP</th>'
        '<th class="num" title="Fair implied probability – from Kalshi, fallback book, or consensus">Fair IP</th>'
        '<th>Fair src</th>'
        '<th class="num">Edge (pts)</th><th class="num">Stake</th>'
        '</tr></thead>'
        f'<tbody>{"".join(rows)}</tbody>'
        '</table>'
    )
    html(
        '<div class="hint" style="margin-top:10px;">'
        f'Stakes = <strong>{kelly_mult*100:.0f}%-Kelly</strong> × Kalshi cash '
        f'(<strong>${kalshi_cash:,.2f}</strong>), capped at <strong>{max_bet*100:.1f}%</strong>/bet. '
        '<strong>Fair IP</strong> = Pinnacle no-vig (sharpest book) → Kalshi YES → fallback book → '
        'consensus. The agent compares <em>every</em> book (including Kalshi) to that fair prob. '
        '<strong>Edge (pts)</strong> = Fair − Book IP. '
        '<em>Details on the Settings tab.</em>'
        '</div>'
    )


def _render_quick_calculator(merged, unit_pct, bankroll_units, sport):
    """Compact bar: pick a game, see live EV table for both sides."""
    if not merged:
        return
    labels = [
        f"{ev['away']['short'] or ev['away']['name']} @ {ev['home']['short'] or ev['home']['name']}"
        for ev, _odds_meta, _exch in merged
    ]
    html('<h3>Quick-glance EV scanner</h3>')
    pick_idx = st.selectbox(
        "Pick a game", options=list(range(len(labels))),
        format_func=lambda i: labels[i], key="quick_pick", label_visibility="collapsed",
    )
    ev, odds_meta, _exch_probs = merged[pick_idx]
    projected_home = st.slider(
        "Your projected home win probability",
        min_value=0.0, max_value=1.0, value=0.55, step=0.01,
        key="quick_proj",
    )
    projected_away = 1.0 - projected_home

    h2h = (odds_meta or {}).get("h2h") or {}
    hp = h2h.get("home_price")
    ap = h2h.get("away_price")

    cols = st.columns(2)
    for col, side, name, prob, price in [
        (cols[0], "Away", ev["away"]["name"], projected_away, ap),
        (cols[1], "Home", ev["home"]["name"], projected_home, hp),
    ]:
        with col:
            imp = implied_prob_american(price)
            evp = ev_percent(prob, price)
            tone = "good" if (evp or 0) > 0 else ("bad" if (evp or 0) < 0 else "")
            ku = units_from_kelly(kelly_fraction(prob, price, multiplier=0.5), unit_pct)
            imp_s = f"{imp*100:.1f}%" if imp is not None else "—"
            evp_s = f"{evp*100:+.2f}%" if evp is not None else "—"
            price_s = format_american(price)
            html(f"""
            <div class="calc-wrap">
              <div style="display:flex; justify-content:space-between; align-items:baseline;">
                <div><span class="tag model">{side}</span> &nbsp;<strong>{name or '-'}</strong></div>
                <div class="mono">{price_s}</div>
              </div>
              <div class="calc-grid" style="margin-top:10px;">
                <div><div class="label mono" style="font-size:10px; color:var(--muted); letter-spacing:0.12em;">IMPLIED</div><div class="mono" style="font-size:16px;">{imp_s}</div></div>
                <div><div class="label mono" style="font-size:10px; color:var(--muted); letter-spacing:0.12em;">YOUR PROB</div><div class="mono" style="font-size:16px;">{prob*100:.1f}%</div></div>
                <div><div class="label mono" style="font-size:10px; color:var(--muted); letter-spacing:0.12em;">EV / $100</div><div class="mono {tone}" style="font-size:16px;">{evp_s}</div></div>
                <div><div class="label mono" style="font-size:10px; color:var(--muted); letter-spacing:0.12em;">HALF-KELLY</div><div class="mono" style="font-size:16px;">{format_units(ku)}</div></div>
              </div>
            </div>
            """)


def _add_pick_control(event, odds_meta, unit_pct, bankroll_units):
    """Collapsible 'Log pick' control under each game card."""
    key_prefix = f"pick_{event.get('id','?')}"
    with st.expander("＋ Log pick for this game", expanded=False):
        h2h = (odds_meta or {}).get("h2h") or {}
        spr = (odds_meta or {}).get("spreads") or {}
        tot = (odds_meta or {}).get("totals") or {}

        market = st.selectbox(
            "Market", options=["h2h", "spreads", "totals"],
            format_func=lambda m: {"h2h": "Moneyline", "spreads": "Spread", "totals": "Total"}[m],
            key=f"{key_prefix}_market",
        )
        home = event.get("home") or {}
        away = event.get("away") or {}

        if market == "h2h":
            side = st.radio(
                "Side", options=[away.get("name"), home.get("name")],
                key=f"{key_prefix}_side", horizontal=True,
            )
            default_price = h2h.get("away_price") if side == away.get("name") else h2h.get("home_price")
            point = None
        elif market == "spreads":
            side = st.radio(
                "Side", options=[away.get("name"), home.get("name")],
                key=f"{key_prefix}_side", horizontal=True,
            )
            default_point = spr.get("away_point") if side == away.get("name") else spr.get("home_point")
            point = st.number_input("Point", value=float(default_point or 0.0), step=0.5, key=f"{key_prefix}_pt")
            default_price = spr.get("away_price") if side == away.get("name") else spr.get("home_price")
        else:
            side = st.radio("Side", options=["Over", "Under"], key=f"{key_prefix}_side", horizontal=True)
            default_point = tot.get("home_point") or tot.get("away_point") or 0.0
            point = st.number_input("Total", value=float(default_point or 0.0), step=0.5, key=f"{key_prefix}_pt")
            default_price = tot.get("home_price") if side == "Over" else tot.get("away_price")

        price = st.number_input(
            "American price", value=float(default_price or -110),
            step=5.0, format="%.0f", key=f"{key_prefix}_price",
        )
        projected = st.slider(
            "Your projected win probability", 0.0, 1.0,
            value=float(implied_prob_american(price) or 0.5), step=0.01,
            key=f"{key_prefix}_proj",
        )
        methodology = st.selectbox(
            "Methodology", options=["Model Edge", "Line Move", "Situational", "Fade the Public"],
            key=f"{key_prefix}_meth",
        )
        # Breakdown field is hidden until the user explicitly asks for it.
        show_breakdown_key = f"{key_prefix}_show_notes"
        if st.session_state.get(show_breakdown_key):
            notes = st.text_area(
                "Quantitative breakdown",
                placeholder="Why this pick? EV, line move, situational angle, model vs. market...",
                key=f"{key_prefix}_notes", height=100,
            )
            if st.button("Hide breakdown", key=f"{key_prefix}_hide_notes"):
                st.session_state[show_breakdown_key] = False
                st.rerun()
        else:
            notes = st.session_state.get(f"{key_prefix}_notes", "")
            if st.button("＋ Add quantitative breakdown", key=f"{key_prefix}_add_notes"):
                st.session_state[show_breakdown_key] = True
                st.rerun()

        evp = ev_percent(projected, price)
        units_rec = recommend_units(evp)
        imp = implied_prob_american(price)

        c1, c2, c3 = st.columns(3)
        c1.metric("EV / $100", f"{(evp or 0)*100:+.2f}%" if evp is not None else "—")
        c2.metric("Implied", f"{(imp or 0)*100:.1f}%" if imp is not None else "—")
        c3.metric("Rec. units", format_units(units_rec))

        risk_u = st.number_input(
            "Risk units", min_value=0.0, max_value=5.0, step=0.5, value=float(units_rec),
            key=f"{key_prefix}_risk",
        )
        win_u = 0.0
        if risk_u > 0:
            stake_dollars = risk_u * unit_pct * 100.0  # per-$100-bankroll units
            prof = profit_on_win(stake_dollars, price)
            if prof is not None and stake_dollars > 0:
                win_u = risk_u * (prof / stake_dollars)

        if st.button("Save pick", key=f"{key_prefix}_save", disabled=(risk_u <= 0)):
            pick = {
                "sport": event.get("sport"),
                "event_id": event.get("id"),
                "event_name": event.get("name"),
                "market": market,
                "selection": side,
                "point": point,
                "price_american": price,
                "projected_prob": projected,
                "implied_prob": imp,
                "risk_units": risk_u,
                "win_units": win_u,
                "methodology": methodology,
                "notes": notes,
            }
            saved = picks_store.add_pick(pick)
            st.success(f"Pick saved: {side} {('@ ' + format_american(price)) if market=='h2h' else ''}")
            st.cache_data.clear()


# ============================================================================
# TAB 2 - Pro Handicapping Tools
# ============================================================================

def render_pro_tools(settings: dict):
    brand_bar("Pro Handicapping Tools")

    unit_pct = settings.get("unit_pct") or 0.01
    bankroll_units = settings.get("bankroll_units") or 100.0

    st.markdown("### Odds translator")
    st.caption("Enter a price in any format — the other formats and implied probability update instantly.")
    c1, c2, c3 = st.columns(3)
    with c1:
        am = st.number_input("American", value=-110.0, step=5.0, format="%.0f", key="pt_am")
    with c2:
        dec_val = american_to_decimal(am) or 1.0
        dec_in = st.number_input("Decimal", value=float(dec_val), step=0.01, format="%.4f", key="pt_dec")
    with c3:
        frac = american_to_fractional(am) or "—"
        st.text_input("Fractional", value=frac, disabled=True, key="pt_frac")

    # if user edited decimal, convert back
    if abs(dec_in - (dec_val or 0)) > 1e-4:
        am = decimal_to_american(dec_in) or am

    imp = implied_prob_american(am)
    html(f"""
    <table class="ledger" style="margin-top:14px;">
      <thead><tr><th>Format</th><th class="num">Value</th><th>Note</th></tr></thead>
      <tbody>
        <tr><td>American</td><td class="num mono">{format_american(am)}</td><td class="mono" style="color:var(--muted);">standard US price</td></tr>
        <tr><td>Decimal</td><td class="num mono">{american_to_decimal(am):.4f}</td><td class="mono" style="color:var(--muted);">payout multiplier incl. stake</td></tr>
        <tr><td>Fractional</td><td class="num mono">{american_to_fractional(am) or '—'}</td><td class="mono" style="color:var(--muted);">UK-style</td></tr>
        <tr><td><strong>Implied probability</strong></td><td class="num mono"><strong>{(imp*100):.2f}%</strong></td><td class="mono" style="color:var(--muted);">vig-inclusive</td></tr>
      </tbody>
    </table>
    """)

    st.markdown("### True Value calculator")
    st.caption("Compare your projected win probability to the market's implied price. Soft-green when EV > 0.")
    c1, c2, c3 = st.columns([1, 1, 1])
    tv_price = c1.number_input("Market price (American)", value=-110.0, step=5.0, format="%.0f", key="tv_price")
    tv_proj = c2.slider("Your projected win %", 0.0, 1.0, 0.55, 0.01, key="tv_proj")
    tv_stake = c3.number_input("Stake ($)", value=100.0, min_value=1.0, step=10.0, key="tv_stake")

    ev_dollars = expected_value(tv_proj, tv_stake, tv_price)
    evp = ev_percent(tv_proj, tv_price)
    tv_imp = implied_prob_american(tv_price)
    edge_val = calc_edge(tv_proj, tv_price)
    prof = profit_on_win(tv_stake, tv_price)

    tone = "good" if (evp or 0) > 0 else ("bad" if (evp or 0) < 0 else "")
    bg_tint = "color-mix(in oklab, var(--good) 10%, var(--card))" if tone == "good" else (
        "color-mix(in oklab, var(--bad) 10%, var(--card))" if tone == "bad" else "var(--card)"
    )
    ev_display = f"{ev_dollars:+.2f}" if ev_dollars is not None else "—"
    evp_display = f"{evp*100:+.2f}%" if evp is not None else "—"
    edge_display = f"{edge_val*100:+.2f} pts" if edge_val is not None else "—"
    prof_display = f"{prof:+.2f}" if prof is not None else "—"
    imp_display = f"{tv_imp*100:.2f}%" if tv_imp is not None else "—"

    html(f"""
    <div class="calc-wrap" style="background:{bg_tint};">
      <div class="calc-grid">
        <div><div class="label mono" style="font-size:10px; color:var(--muted); letter-spacing:0.12em;">EV ($)</div><div class="mono {tone}" style="font-size:22px;">{ev_display}</div></div>
        <div><div class="label mono" style="font-size:10px; color:var(--muted); letter-spacing:0.12em;">EV (%)</div><div class="mono {tone}" style="font-size:22px;">{evp_display}</div></div>
        <div><div class="label mono" style="font-size:10px; color:var(--muted); letter-spacing:0.12em;">EDGE vs. MARKET</div><div class="mono" style="font-size:22px;">{edge_display}</div></div>
        <div><div class="label mono" style="font-size:10px; color:var(--muted); letter-spacing:0.12em;">PROFIT ON WIN</div><div class="mono" style="font-size:22px;">${prof_display}</div></div>
        <div><div class="label mono" style="font-size:10px; color:var(--muted); letter-spacing:0.12em;">IMPLIED</div><div class="mono" style="font-size:22px;">{imp_display}</div></div>
      </div>
    </div>
    """)

    st.markdown("### Kelly bet-sizer")
    st.caption("Full Kelly is aggressive. Most pros bet 1/4 to 1/2 Kelly to survive variance.")
    c1, c2, c3 = st.columns([1, 1, 1])
    k_price = c1.number_input("Market price (American)", value=-110.0, step=5.0, format="%.0f", key="k_price")
    k_proj = c2.slider("Your win probability", 0.0, 1.0, 0.55, 0.01, key="k_proj")
    k_mult = c3.selectbox(
        "Kelly fraction", options=[1.0, 0.5, 0.25, 0.125],
        format_func=lambda v: {1.0: "Full Kelly", 0.5: "Half Kelly", 0.25: "Quarter Kelly", 0.125: "Eighth Kelly"}[v],
        index=1, key="k_mult",
    )
    bk_units = st.number_input("Bankroll (units)", value=float(bankroll_units), min_value=1.0, step=1.0, key="k_bk")
    bk_dollars = bk_units * unit_pct * 100.0

    kf = kelly_fraction(k_proj, k_price, k_mult) or 0.0
    stake_dollars = bk_dollars * kf
    stake_units = units_from_kelly(kelly_fraction(k_proj, k_price, k_mult), unit_pct)

    html(f"""
    <div class="calc-wrap">
      <div class="calc-grid">
        <div><div class="label mono" style="font-size:10px; color:var(--muted); letter-spacing:0.12em;">KELLY FRACTION</div><div class="mono" style="font-size:22px;">{kf*100:.2f}%</div></div>
        <div><div class="label mono" style="font-size:10px; color:var(--muted); letter-spacing:0.12em;">UNITS TO RISK</div><div class="mono accent" style="font-size:22px;">{format_units(stake_units)}</div></div>
        <div><div class="label mono" style="font-size:10px; color:var(--muted); letter-spacing:0.12em;">RAW STAKE</div><div class="mono" style="font-size:22px;">${stake_dollars:,.2f}</div></div>
      </div>
    </div>
    """)

    st.markdown("### No-vig fair line")
    st.caption("Strip the house margin from a two-way market to see the market's \"true\" prices.")
    c1, c2 = st.columns(2)
    nv_home = c1.number_input("Home American", value=-150.0, step=5.0, format="%.0f", key="nv_home")
    nv_away = c2.number_input("Away American", value=130.0, step=5.0, format="%.0f", key="nv_away")
    ph_fair, pa_fair = no_vig_two_way(nv_home, nv_away)
    if ph_fair is None:
        st.info("Enter valid two-way prices.")
    else:
        fair_home_am = decimal_to_american(1.0 / ph_fair)
        fair_away_am = decimal_to_american(1.0 / pa_fair)
        html(f"""
        <table class="ledger" style="margin-top:10px;">
          <thead><tr><th>Side</th><th class="num">Market</th><th class="num">Fair</th><th class="num">Vig cost</th></tr></thead>
          <tbody>
            <tr><td>Home</td><td class="num mono">{format_american(nv_home)}</td><td class="num mono accent">{format_american(fair_home_am)}</td><td class="num mono">{(implied_prob_american(nv_home)-ph_fair)*100:+.2f} pts</td></tr>
            <tr><td>Away</td><td class="num mono">{format_american(nv_away)}</td><td class="num mono accent">{format_american(fair_away_am)}</td><td class="num mono">{(implied_prob_american(nv_away)-pa_fair)*100:+.2f} pts</td></tr>
          </tbody>
        </table>
        """)


# ============================================================================
# TAB 3 - Verified Picks Ledger
# ============================================================================

def render_picks(settings: dict):
    brand_bar("Verified Picks Ledger")

    cols = st.columns([1, 1, 1, 2])
    with cols[0]:
        if st.button("↻ Auto-settle from ESPN"):
            n = picks_store.auto_settle()
            st.cache_data.clear()
            st.success(f"Settled {n} pick(s).")
    with cols[1]:
        filter_status = st.selectbox(
            "Filter", options=["All", "Open", "Won", "Lost", "Push"],
            key="ledger_filter", label_visibility="collapsed",
        )
    with cols[2]:
        filter_sport = st.selectbox(
            "Sport", options=["All"] + sorted({p.get("sport") or "" for p in picks_store.list_picks() if p.get("sport")}),
            key="ledger_sport_filter", label_visibility="collapsed",
        )

    picks = picks_store.list_picks()
    s = picks_store.summarize(picks)

    net_tone = "good" if (s["net_units"] or 0) > 0 else ("bad" if (s["net_units"] or 0) < 0 else "")
    roi_display = f"{(s['roi'] or 0)*100:+.2f}%" if s["roi"] is not None else "—"
    win_pct = f"{(s['win_pct'] or 0)*100:.1f}%" if s["win_pct"] is not None else "—"

    metric_row([
        {"label": "Open", "value": str(s["open"])},
        {"label": "Record", "value": f"{s['wins']}-{s['losses']}-{s['pushes']}"},
        {"label": "Win %", "value": win_pct, "foot": f"{s['wins']+s['losses']} decided"},
        {"label": "Risked", "value": f"{s['risked_units']:.2f}u"},
        {"label": "Net", "value": f"{s['net_units']:+.2f}u", "tone": net_tone},
        {"label": "ROI", "value": roi_display, "tone": net_tone},
        {"label": "Avg CLV", "value": f"{(s['avg_clv_pct'] or 0)*100:+.2f}%" if s["avg_clv_pct"] is not None else "—"},
    ])

    # apply filters
    def _keep(p):
        status = p.get("status") or "open"
        if filter_status == "Open" and status != "open":
            return False
        if filter_status == "Won" and status != "win":
            return False
        if filter_status == "Lost" and status != "loss":
            return False
        if filter_status == "Push" and status != "push":
            return False
        if filter_sport != "All" and p.get("sport") != filter_sport:
            return False
        return True
    rows = [p for p in picks if _keep(p)]

    if not rows:
        html('<div class="hint">No picks match the current filter. Log one from the Dashboard tab.</div>')
        return

    # Render table
    html("""
    <table class="ledger">
      <thead>
        <tr>
          <th>Logged</th><th>Sport</th><th>Match-up</th><th>Market</th>
          <th>Selection</th><th class="num">Price</th><th class="num">Imp</th>
          <th class="num">Proj</th><th>Method</th><th class="num">Risk</th>
          <th class="num">To Win</th><th>Status</th><th class="num">CLV</th><th>Action</th>
        </tr>
      </thead>
      <tbody>
    """)
    for p in sorted(rows, key=lambda x: x.get("created_at") or "", reverse=True):
        status = p.get("status") or "open"
        status_cls = {
            "open": "", "win": "good", "loss": "bad", "push": "warn", "void": "",
        }.get(status, "")
        status_lbl = status.upper()
        clv = p.get("clv_pct")
        clv_s = f"{clv*100:+.2f}%" if clv is not None else "—"
        meth = p.get("methodology") or ""
        tag_cls = {
            "Model Edge": "model", "Line Move": "line",
            "Situational": "sit", "Fade the Public": "fade",
        }.get(meth, "model")
        html(f"""
        <tr>
          <td class="mono" style="font-size:11px; color:var(--muted);">{(p.get('created_at') or '')[:16]}</td>
          <td class="mono" style="color:var(--muted);">{(p.get('sport') or '').upper()}</td>
          <td>{p.get('event_name') or '-'}</td>
          <td class="mono" style="color:var(--muted);">{(p.get('market') or '').upper()}</td>
          <td><strong>{p.get('selection') or '-'}</strong>{(' ' + (f"{p.get('point'):+g}" if p.get('point') is not None else '')) if p.get('point') is not None else ''}</td>
          <td class="num mono">{format_american(p.get('price_american'))}</td>
          <td class="num mono">{(p.get('implied_prob') or 0)*100:.1f}%</td>
          <td class="num mono">{(p.get('projected_prob') or 0)*100:.1f}%</td>
          <td><span class="tag {tag_cls}">{meth}</span></td>
          <td class="num mono">{(p.get('risk_units') or 0):.2f}u</td>
          <td class="num mono">{(p.get('win_units') or 0):.2f}u</td>
          <td><span class="chip {status_cls}">{status_lbl}</span></td>
          <td class="num mono">{clv_s}</td>
          <td class="mono" style="font-size:11px;"></td>
        </tr>
        """)
    html("</tbody></table>")

    with st.expander("Pick notes & manual actions"):
        for p in sorted(rows, key=lambda x: x.get("created_at") or "", reverse=True):
            st.markdown(f"**{p.get('event_name') or '-'}** — {p.get('selection')} ({format_american(p.get('price_american'))})")
            if p.get("notes"):
                st.caption(p.get("notes"))
            c1, c2, c3, c4 = st.columns([2, 1, 1, 1])
            with c1:
                closing = st.number_input(
                    "Closing American price", value=float(p.get("closing_price_american") or p.get("price_american") or -110),
                    step=5.0, format="%.0f", key=f"close_{p.get('id')}",
                )
            if c2.button("Save CLV", key=f"savecl_{p.get('id')}"):
                picks_store.update_closing_price(p.get("id"), closing)
                st.cache_data.clear()
                st.success("CLV saved.")
            if c3.button("Mark void", key=f"void_{p.get('id')}"):
                p["status"] = "void"
                import json as _j, os as _o
                here = _o.path.dirname(_o.path.dirname(_o.path.abspath(__file__)))
                with open(_o.path.join(here, "picks.json"), "w", encoding="utf-8") as f:
                    _j.dump(picks_store.list_picks(), f, indent=2, default=str)
                # safer: just use delete+add
                picks_store.delete_pick(p.get("id"))
                picks_store.add_pick(p)
                st.cache_data.clear()
                st.success("Marked void.")
            if c4.button("Delete", key=f"del_{p.get('id')}"):
                picks_store.delete_pick(p.get("id"))
                st.cache_data.clear()
                st.success("Deleted.")
            st.divider()


# ============================================================================
# TAB 4 - Settings
# ============================================================================

def render_settings(settings: dict):
    brand_bar("Settings · configuration & API usage")
    saved = settings_store.load()

    # ----- Credit meter -----
    st.markdown("### Odds API usage")
    credits = odds_api.get_credit_status()
    remaining = credits.get("remaining")
    used = credits.get("used_total")
    last = credits.get("last_used")

    budget = 20_000
    pct_used = None
    if remaining is not None:
        try:
            pct_used = max(0.0, min(1.0, 1.0 - (int(remaining) / budget)))
        except (TypeError, ValueError):
            pct_used = None

    cards = [
        {"label": "Remaining credits", "value": str(remaining) if remaining is not None else "—",
         "foot": "20k monthly budget", "tone": "good" if (remaining or 0) > 2000 else ("warn" if (remaining or 0) > 500 else "bad")},
        {"label": "Last request", "value": str(last) if last is not None else "—",
         "foot": "cost of last call"},
        {"label": "Total used", "value": str(used) if used is not None else "—",
         "foot": "this billing cycle"},
    ]
    if pct_used is not None:
        cards.append({"label": "% used", "value": f"{pct_used*100:.1f}%", "tone": "warn" if pct_used > 0.75 else ""})
    metric_row(cards)

    st.caption("Credits are read from The Odds API's response headers after each call. "
               "On-disk cache is at `.odds_cache/` — historical snapshots are cached permanently, "
               "live odds cache for 5 min by default.")

    # ----- Live Kalshi balance -----
    st.markdown("### Kalshi")
    balance = load_kalshi_balance() or {"usd": None, "source": None}
    live_usd = balance.get("usd")
    live_src = balance.get("source")
    if live_src == "live":
        html(f'<div class="hint" style="background:color-mix(in oklab, var(--good) 10%, var(--card));">'
             f'<strong>Live balance:</strong> ${live_usd:,.2f} · source: Kalshi API</div>')
    elif live_src == "manual":
        html(f'<div class="hint warn"><strong>Manual balance:</strong> ${live_usd:,.2f} · '
             f'connect the API in the sidebar for live.</div>')
    else:
        html('<div class="hint warn">Not connected. Enter credentials in the sidebar.</div>')

    unit_dollars = settings.get("unit_dollars") or 0.0
    unit_pct = settings.get("unit_pct") or 0.10
    cash = settings.get("kalshi_cash") or 0.0
    metric_row([
        {"label": "Kalshi cash", "value": f"${cash:,.2f}", "foot": settings.get("kalshi_source") or "unknown"},
        {"label": "1 unit", "value": f"${unit_dollars:,.2f}", "foot": f"{unit_pct*100:.1f}% of cash", "tone": "accent"},
        {"label": "Bankroll (units)", "value": f"{settings.get('bankroll_units') or 0:.1f}u"},
    ])

    # ----- Agent reference -----
    st.markdown("### Agent configuration")
    primary = settings.get("primary_book") or "kalshi"
    fallback = saved.get("fallback_book") or "DraftKings"
    html(
        f'<div class="hint">'
        f'<strong>Fair-probability source (in order):</strong> '
        f'1) Pinnacle no-vig (the sharpest book — best proxy for "true" probability), '
        f'2) Kalshi YES-mid (exchange, no vig, moneyline only), '
        f'3) <strong>{fallback}</strong> no-vig, '
        f'4) multi-book consensus no-vig. '
        f'The agent then scans every bookmaker <em>and Kalshi</em> for prices that imply '
        f'a probability lower than that fair prob. '
        f'An edge of +2 pts means the fair prob is 2 percentage points above what that book is pricing.'
        f'</div>'
    )

    # Allow configuration here too (duplicates sidebar for permanence).
    c1, c2, c3 = st.columns(3)
    with c1:
        new_fallback = st.selectbox(
            "Fallback book (when Pinnacle & Kalshi both miss)",
            options=["DraftKings", "FanDuel", "BetMGM", "Caesars"],
            index=["DraftKings","FanDuel","BetMGM","Caesars"].index(fallback) if fallback in ["DraftKings","FanDuel","BetMGM","Caesars"] else 0,
            key="set_fallback_book",
        )
        if new_fallback != fallback:
            settings_store.save({"fallback_book": new_fallback})
            st.success(f"Fallback book saved: {new_fallback}")
    with c2:
        current_regions = saved.get("odds_regions") or "us,eu"
        region_options = ["us", "us,eu", "us,us2,eu", "us,eu,uk,au"]
        new_regions = st.selectbox(
            "Odds API regions",
            options=region_options,
            index=region_options.index(current_regions) if current_regions in region_options else 1,
            key="set_regions",
            help="Pinnacle (the sharpest book) lives in the 'eu' region. "
                 "us,eu costs 2× credits per fetch but unlocks Pinnacle fair.",
        )
        if new_regions != current_regions:
            settings_store.save({"odds_regions": new_regions})
            st.cache_data.clear()
            st.success(f"Regions saved: {new_regions}. Credit cost per fetch is {len(new_regions.split(','))}×3 = {len(new_regions.split(','))*3}.")
    with c3:
        if st.button("Clear Odds API disk cache"):
            import shutil, os
            here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            cache = os.path.join(here, ".odds_cache")
            try:
                shutil.rmtree(cache, ignore_errors=True)
                st.cache_data.clear()
                st.success("Disk cache cleared. Next refresh will re-fetch.")
            except Exception as e:
                st.error(str(e))

    # ----- Explanations -----
    st.markdown("### What the columns mean")
    html(
        '<div class="hint">'
        '<strong>Fair IP</strong> (fair implied probability) = the probability the agent considers '
        '"true" for a given side. It comes from <strong>Pinnacle no-vig</strong> first — Pinnacle is '
        'the sharpest book in the industry; stripping its small vig gives the tightest proxy for the '
        'market\'s true probability. If Pinnacle isn\'t in the feed for a game, it falls to '
        'Kalshi (exchange, moneyline only), then your fallback book, then consensus. '
        '<br><br><strong>Book IP</strong> = the implied probability a specific sportsbook — '
        'including Kalshi — is offering (vig-inclusive for sportsbooks; raw price for Kalshi). '
        '<br><strong>Edge (pts)</strong> = Fair IP − Book IP, in percentage points. '
        'A +4 pts edge means the sharp consensus considers the outcome 4 points more likely than '
        'this book is pricing it, so you expect long-run profit at that price.'
        '</div>'
    )

    # ----- Saved settings dump -----
    with st.expander("Current saved settings (JSON on disk)"):
        st.json(saved)


# ============================================================================
# TAB 5 - Backtest
# ============================================================================

def render_backtest(settings: dict):
    brand_bar("Backtest · 30-day simulated performance")

    all_sports = ["nfl","nba","mlb","nhl","ncaaf","ncaab","wnba","mls"]
    c1, c2, c3, c4 = st.columns([2, 1, 1, 1])
    with c1:
        picked_sports = st.multiselect(
            "Sports to include",
            options=all_sports,
            default=["nfl", "nba", "mlb", "nhl"],
            format_func=lambda k: espn.DISPLAY_NAMES.get(k, k.upper()),
            key="bt_sports",
        )
    with c2:
        start_bankroll = st.number_input("Starting bankroll ($)", 10.0, 100000.0, 100.0, 10.0, key="bt_bank")
    with c3:
        days = st.number_input("Days back", 1, 90, 30, 1, key="bt_days")
    with c4:
        top_n = st.number_input("Top-N picks/day", 1, 30, 5, 1, key="bt_topn")

    # ----- credit preflight -----
    cost_per_snapshot = odds_history.estimated_credit_cost_per_snapshot("us", "h2h,spreads,totals")
    est_requests = len(picked_sports) * int(days)
    est_credits = est_requests * cost_per_snapshot
    credit_status = odds_api.get_credit_status()
    remaining = credit_status.get("remaining")

    # Cached snapshots don't re-charge; estimate assumes everything is new.
    tone = "warn" if (remaining is not None and est_credits > remaining * 0.5) else ""
    if remaining is not None and est_credits > remaining:
        tone = "bad"

    metric_row([
        {"label": "Est. credits", "value": str(est_credits),
         "foot": f"{est_requests} snapshots × {cost_per_snapshot} ea", "tone": tone},
        {"label": "Remaining budget", "value": str(remaining) if remaining is not None else "—"},
        {"label": "Cached snapshots", "value": _count_cached_snapshots(),
         "foot": "cost 0 on reruns"},
    ])
    html(
        '<div class="hint warn">Historical odds cost <strong>10× live odds</strong> per snapshot '
        '(us × h2h,spreads,totals = <strong>30 credits</strong> each). '
        'We cache every snapshot to disk permanently, so re-running this backtest later costs 0 credits. '
        'Still, keep <strong>sports × days × 30</strong> comfortably under your remaining budget before running.</div>'
    )

    if st.button("▶ Run backtest", type="primary", disabled=not picked_sports):
        progress = st.progress(0, text="starting…")
        def _cb(frac, msg):
            progress.progress(min(1.0, max(0.0, frac)), text=msg)
        result = backtest_engine.run(
            sports=picked_sports,
            start_bankroll=float(start_bankroll),
            days=int(days),
            top_n_per_day=int(top_n),
            kelly_mult=settings.get("agent_kelly") or 0.25,
            min_edge_pct=settings.get("agent_min_edge") or 0.02,
            max_bet_pct=settings.get("agent_max_bet_pct") or 0.10,
            fallback_book=settings.get("fallback_book") or "DraftKings",
            progress_cb=_cb,
        )
        progress.empty()
        st.session_state["bt_last_result"] = result

    result = st.session_state.get("bt_last_result")
    if not result:
        return

    # Render summary
    for w in (result.warnings or []):
        html(f'<div class="hint err">{w}</div>')

    net = result.end_bankroll - result.start_bankroll
    wins = sum(1 for p in result.picks if p.outcome == "win")
    losses = sum(1 for p in result.picks if p.outcome == "loss")
    pushes = sum(1 for p in result.picks if p.outcome == "push")
    ungraded = sum(1 for p in result.picks if p.outcome in ("nograde", "pending"))
    roi = (net / result.start_bankroll) if result.start_bankroll else None
    tone = "good" if net > 0 else ("bad" if net < 0 else "")

    metric_row([
        {"label": "Starting", "value": f"${result.start_bankroll:,.2f}"},
        {"label": "Ending", "value": f"${result.end_bankroll:,.2f}", "tone": tone},
        {"label": "Net", "value": f"${net:+,.2f}", "tone": tone},
        {"label": "ROI", "value": f"{(roi or 0)*100:+.1f}%" if roi is not None else "—", "tone": tone},
        {"label": "Record", "value": f"{wins}-{losses}-{pushes}", "foot": f"{ungraded} ungraded"},
        {"label": "Picks placed", "value": str(len(result.picks))},
    ])

    # Daily bankroll chart
    if result.daily:
        try:
            import pandas as pd
            df = pd.DataFrame(result.daily).set_index("date")
            st.line_chart(df["bankroll_eod"], height=240)
        except ImportError:
            # Fallback: render dates/bankroll as a table.
            lines = "<br>".join(f"{d['date']}  ${d['bankroll_eod']:,.2f}" for d in result.daily)
            html(f'<div class="hint">{lines}</div>')

    # Picks table
    st.markdown("### Simulated picks")
    rows = []
    for p in sorted(result.picks, key=lambda x: x.date, reverse=True)[:200]:
        outcome_tone = {"win": "ev-plus", "loss": "ev-minus", "push": "", "nograde": "", "pending": ""}.get(p.outcome, "")
        rows.append(
            f'<tr>'
            f'<td class="mono" style="font-size:11px;">{p.date}</td>'
            f'<td class="mono" style="color:var(--muted);">{p.sport.upper()}</td>'
            f'<td>{p.matchup}</td>'
            f'<td class="mono" style="color:var(--muted);">{p.market.upper()}</td>'
            f'<td><strong>{p.side_label}</strong>{(" "+str(p.point)) if p.point is not None else ""}</td>'
            f'<td class="num mono">{format_american(p.price)}</td>'
            f'<td class="mono" style="color:var(--muted);">{p.book}</td>'
            f'<td class="num mono">{p.edge_pct:+.2f}</td>'
            f'<td class="num mono">${p.stake:,.2f}</td>'
            f'<td class="mono {outcome_tone}"><strong>{p.outcome.upper()}</strong></td>'
            f'<td class="num mono {outcome_tone}">${p.profit:+,.2f}</td>'
            f'</tr>'
        )
    html(
        '<table class="ledger"><thead><tr>'
        '<th>Date</th><th>Sport</th><th>Matchup</th><th>Mkt</th><th>Side</th>'
        '<th class="num">Price</th><th>Book</th>'
        '<th class="num">Edge pts</th><th class="num">Stake</th>'
        '<th>Result</th><th class="num">P&amp;L</th>'
        '</tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table>'
    )


def _count_cached_snapshots() -> str:
    import os
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cache_dir = os.path.join(here, ".odds_cache")
    try:
        return str(len([f for f in os.listdir(cache_dir) if f.endswith(".json")]))
    except (OSError, FileNotFoundError):
        return "0"
