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

@st.cache_data(ttl=60, show_spinner=False)
def load_espn_scoreboard(sport_key: str, date_str: Optional[str] = None):
    """ESPN scoreboard - 60s TTL so live scores stay fresh. ESPN is free, so no credit worry."""
    try:
        return espn.get_scoreboard(sport_key, date_str)
    except Exception:
        return []


def _central_today_str() -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("America/Chicago")).strftime("%Y%m%d")


@st.cache_data(ttl=3600, show_spinner=False)
def load_odds(sport_key: str, regions: str = "eu", markets: str = "h2h", cache_ttl: int = 3600):
    """Fetch live odds for one sport. Default: Pinnacle only (eu), moneyline only (h2h)
    = 1 credit per sport. Set regions/markets wider in Settings for more coverage."""
    odds_sport = espn.ODDS_API_SPORT_KEYS.get(sport_key)
    if not odds_sport:
        return []
    try:
        return odds_api.get_odds(odds_sport, regions=regions, markets=markets, cache_ttl=cache_ttl)
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

    sport_setting = settings.get("sport") or "nfl"
    # Normalize to a list so single-sport and all-sports modes share code.
    sports_list = sport_setting if isinstance(sport_setting, list) else [sport_setting]
    multi_sport_mode = len(sports_list) > 1
    unit_pct = settings.get("unit_pct") or 0.01
    bankroll_units = settings.get("bankroll_units") or 100.0
    kalshi_cash = settings.get("kalshi_cash") or 0.0

    # Date picker (Central US). Picks will apply to this date.
    from datetime import date, timedelta
    from zoneinfo import ZoneInfo
    central_today = datetime.now(ZoneInfo("America/Chicago")).date()
    dc1, dc2, dc3, dc4, dc5 = st.columns([1, 2, 1, 1, 5])
    with dc1:
        if st.button("← Prev"):
            cur = st.session_state.get("dash_date") or central_today
            st.session_state["dash_date"] = cur - timedelta(days=1)
            st.rerun()
    with dc2:
        picked_date = st.date_input(
            "Date", value=st.session_state.get("dash_date") or central_today,
            min_value=central_today - timedelta(days=180),
            max_value=central_today + timedelta(days=30),
            key="dash_date", label_visibility="collapsed",
        )
    with dc3:
        if st.button("Next →"):
            cur = st.session_state.get("dash_date") or central_today
            st.session_state["dash_date"] = cur + timedelta(days=1)
            st.rerun()
    with dc4:
        if picked_date != central_today:
            if st.button("Today", key="go_today"):
                st.session_state["dash_date"] = central_today
                st.rerun()

    is_today = picked_date == central_today
    date_str_api = picked_date.strftime("%Y%m%d")
    date_str_pretty = picked_date.strftime("%A, %b %d, %Y")

    # fetch (one or many sports) — credit-conscious defaults.
    saved_cfg = settings_store.load()
    regions = saved_cfg.get("odds_regions") or "eu"
    markets_cfg = saved_cfg.get("odds_markets") or "h2h"
    live_ttl = int(saved_cfg.get("odds_live_ttl") or 3600)

    # Per-sport fetches, aggregated. CREDIT GUARD: only hit the Odds API
    # for sports that actually have ESPN games on the selected date.
    all_events_tagged: List[tuple] = []     # list of (sport_key, event)
    all_odds_games_tagged: List[tuple] = [] # list of (sport_key, odds_game)
    all_merged = []                          # list of (ev, odds_meta, exch_probs)
    for sp in sports_list:
        events_sp = load_espn_scoreboard(sp, date_str=date_str_api)
        for ev in events_sp or []:
            all_events_tagged.append((sp, ev))
        # Skip Odds API fetch entirely when ESPN shows no games on this date for this sport.
        has_games = bool(events_sp)
        odds_games_sp = load_odds(sp, regions=regions, markets=markets_cfg, cache_ttl=live_ttl) if (is_today and has_games) else []
        for og in odds_games_sp or []:
            og["_sport"] = sp
            all_odds_games_tagged.append((sp, og))
        kalshi_markets_sp = load_kalshi_markets(sp) if (is_today and has_games) else []
        poly_sp = load_polymarket_markets() if sp == sports_list[0] else []  # Polymarket fetched once
        merged_sp = _merge_events_with_odds(
            events_sp, odds_games_sp,
            kalshi_markets=kalshi_markets_sp,
            polymarket_markets=poly_sp,
            sport_key=sp,
        )
        for ev, om, ex in merged_sp:
            ev["sport"] = ev.get("sport") or sp
            all_merged.append((ev, om, ex))

    events = [ev for _sp, ev in all_events_tagged]
    odds_games = [og for _sp, og in all_odds_games_tagged]
    merged = all_merged

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

    # Sparkline: tiny bar of per-sport game counts.
    if multi_sport_mode:
        per_sport_counts = {}
        for ev, _om, _ex in merged:
            sp = ev.get("sport") or "?"
            per_sport_counts[sp] = per_sport_counts.get(sp, 0) + 1
        top3 = sorted(per_sport_counts.items(), key=lambda kv: -kv[1])[:3]
        spark = "  ".join(f"{k.upper()}·{v}" for k, v in top3)
        slate_foot = f"{live_count} live · {final_count} final · {spark}"
    else:
        slate_foot = f"{live_count} live · {final_count} final"
    slate_label = f"{len(sports_list)} sports" if multi_sport_mode else sport_setting.upper()
    metric_row([
        {"label": "Kalshi cash", "value": kalshi_display, "foot": (kalshi_source or "not configured"), "tone": "accent" if kalshi_source == "live" else ""},
        {"label": "Bankroll", "value": f"{bankroll_units:.1f}u", "foot": f"1u = {unit_pct*100:.1f}% of roll"},
        {"label": "Open picks", "value": str(s["open"]), "foot": "awaiting result"},
        {"label": "Record", "value": f"{s['wins']}-{s['losses']}-{s['pushes']}", "foot": f"settled: {s['total_settled']}"},
        {"label": "Net units", "value": f"{s['net_units']:+.2f}u", "foot": roi_display + " ROI", "tone": net_tone},
        {"label": "Avg CLV", "value": f"{(s['avg_clv_pct'] or 0)*100:+.2f}%" if s["avg_clv_pct"] is not None else "—", "foot": "closing line value"},
        {"label": f"Slate · {slate_label}", "value": f"{live_count + upcoming + final_count}", "foot": slate_foot},
    ])

    if not api_key_ok:
        html('<div class="hint warn">The Odds API key not set — showing ESPN schedule only. Add <code>THE_ODDS_API_KEY</code> to your environment or <code>.streamlit/secrets.toml</code> to enable live lines, EV, and Kelly suggestions.</div>')

    # Date-mode banner
    if is_today:
        html(f'<div class="hint" style="margin-bottom:6px;"><strong>{date_str_pretty}</strong> (today, Central US) — live odds & agent active</div>')
    elif picked_date < central_today:
        html(f'<div class="hint warn" style="margin-bottom:6px;"><strong>{date_str_pretty}</strong> — past date. Showing ESPN finals only; the agent needs the Backtest tab for historical plays.</div>')
    else:
        html(f'<div class="hint" style="margin-bottom:6px;"><strong>{date_str_pretty}</strong> — upcoming. Live odds appear closer to game time.</div>')

    # Agent recommendations panel.
    # BUG FIX: previously we passed a flat dict across all sports to the scanner,
    # then looped sports_list and re-tagged each edge with whatever sport came first.
    # Teams like "Giants" (SF Giants MLB vs NY Giants NFL) could even collide.
    # Fix: split everything per-sport so each scan sees only that sport's data.
    kalshi_markets_by_sport: Dict[str, dict] = {}
    espn_events_by_sport: Dict[str, dict] = {}
    pinnacle_games_by_sport: Dict[str, list] = {}
    for sp in sports_list:
        if not is_today:
            continue
        espn_events_sp = load_espn_scoreboard(sp, date_str=date_str_api) or []
        espn_by_game = {}
        for ev in espn_events_sp:
            home_name = (ev.get("home") or {}).get("name")
            away_name = (ev.get("away") or {}).get("name")
            if home_name and away_name:
                espn_by_game[(home_name, away_name)] = ev
        espn_events_by_sport[sp] = espn_by_game

        all_markets_sp = load_kalshi_markets(sp)
        kalshi_by_game = {}
        for ev in espn_events_sp:
            home_name = (ev.get("home") or {}).get("name")
            away_name = (ev.get("away") or {}).get("name")
            home_abbr = (ev.get("home") or {}).get("abbr") or ""
            away_abbr = (ev.get("away") or {}).get("abbr") or ""
            if not all_markets_sp:
                continue
            event_markets = find_event_markets(
                all_markets_sp, ev.get("start_utc"),
                home_abbr, away_abbr, home_name, away_name,
            )
            if event_markets:
                kalshi_by_game[(home_name, away_name)] = event_markets
        kalshi_markets_by_sport[sp] = kalshi_by_game

        # Partition the already-fetched pinnacle games by their sport tag.
        pinnacle_games_by_sport[sp] = [og for og in odds_games if og.get("_sport") == sp]

    top_picks = _render_agent_panel(
        odds_games, kalshi_cash, settings,
        kalshi_markets_by_sport=kalshi_markets_by_sport,
        espn_events_by_sport=espn_events_by_sport,
        pinnacle_games_by_sport=pinnacle_games_by_sport,
        sports_list=sports_list,
    ) or []

    # Per-pick quantitative analysis boards.
    if top_picks:
        _render_analysis_boards(top_picks, settings, kalshi_cash)

    if not merged:
        html('<div class="hint">No games on the slate for this sport right now. Try another sport in the sidebar, or check back later.</div>')
        return

    # Everything below is secondary — collapse it so the main agent board stays prominent.
    with st.expander("🎯 Quick-glance EV scanner + today's matchups", expanded=False):
        _quick_label = ",".join(sports_list) if multi_sport_mode else sport_setting
        _render_quick_calculator(merged, unit_pct, bankroll_units, _quick_label)
        _render_matchups_grid(merged, sports_list, settings, unit_pct, bankroll_units, multi_sport_mode)


def _render_matchups_grid(merged, sports_list, settings, unit_pct, bankroll_units, multi_sport_mode):
    """Game card grid - per-sport sections in all-sports mode, flat grid otherwise."""
    st.markdown('### Today\'s matchups')
    primary_book = settings.get("primary_book") or "kalshi"
    extra_sources = set(settings.get("extra_sources") or [])
    visible_sources = {"kalshi"} | extra_sources

    def _render_one_card(ev, odds_meta, exch_probs):
        prob_sources = _prob_sources_for(
            odds_meta,
            kalshi_prob_home=exch_probs.get("kalshi"),
            polymarket_prob_home=exch_probs.get("polymarket"),
            betfair_prob_home=exch_probs.get("betfair"),
        )
        prob_sources_filtered = None
        if prob_sources:
            key_map = {
                "kalshi": "kalshi", "pinnacle": "pinnacle",
                "draftkings": "dk", "polymarket": "polymarket",
                "betfair": "betfair", "consensus": "consensus",
            }
            prob_sources_filtered = {
                key_map[k]: prob_sources.get(key_map[k])
                for k in visible_sources
                if prob_sources.get(key_map.get(k, k))
            } or None
        game_card(
            ev, odds_meta,
            prob_sources=prob_sources_filtered,
            primary_book=primary_book,
            kalshi_prob_home=exch_probs.get("kalshi"),
        )
        _add_pick_control(ev, odds_meta, unit_pct, bankroll_units)

    if multi_sport_mode:
        by_sport_games = {}
        for item in merged:
            sp = (item[0].get("sport") or "other")
            by_sport_games.setdefault(sp, []).append(item)
        for sp in sorted(by_sport_games.keys(), key=lambda k: -len(by_sport_games[k])):
            items = by_sport_games[sp]
            st.markdown(f"#### {espn.DISPLAY_NAMES.get(sp, sp.upper())}  ·  {len(items)} games")
            cols = st.columns(2)
            for i, (ev, odds_meta, exch_probs) in enumerate(items):
                with cols[i % 2]:
                    _render_one_card(ev, odds_meta, exch_probs)
        return

    cols = st.columns(2)
    for i, (ev, odds_meta, exch_probs) in enumerate(merged):
        with cols[i % 2]:
            _render_one_card(ev, odds_meta, exch_probs)


def _render_agent_panel(odds_games, kalshi_cash, settings,
                        kalshi_markets_by_sport=None, sports_list=None,
                        espn_events_by_sport=None, pinnacle_games_by_sport=None):
    """Agent edge-scanner — Kalshi (free) as the book, Pinnacle no-vig as fair."""
    saved_cfg = settings_store.load()
    st.markdown("### Agent · Kalshi vs Pinnacle edge scanner")
    kelly_mult = settings.get("agent_kelly") or 0.25
    min_edge = settings.get("agent_min_edge") or 0.02
    max_bet = settings.get("agent_max_bet_pct") or 0.05
    daily_cap_pct = saved_cfg.get("daily_exposure_cap_pct", 0.25)

    if not kalshi_cash or kalshi_cash <= 0:
        html(
            '<div class="hint warn">Agent is <strong>idle</strong>: configure Kalshi cash so '
            'recommendations can be sized in dollars.</div>'
        )
        return []
    if not odds_games:
        html('<div class="hint">Agent is waiting on Pinnacle odds (used as the fair-prob reference).</div>')
        return []
    kalshi_markets_by_sport = kalshi_markets_by_sport or {}
    espn_events_by_sport = espn_events_by_sport or {}
    pinnacle_games_by_sport = pinnacle_games_by_sport or {}
    # Total matched Kalshi markets across all sports.
    total_matched = sum(len(v) for v in kalshi_markets_by_sport.values())
    if total_matched == 0:
        html(
            '<div class="hint">No matching Kalshi markets found for today\'s games. The agent only '
            'picks spots where it can bet on Kalshi — nothing to show yet.</div>'
        )
        return []
    # Credit tip: us,eu is the preferred combo (gives both Pinnacle fair + offshore shop books).
    # Only warn when the user has gone wider than us,eu, which burns credits with no payoff.
    cur_regions = saved_cfg.get("odds_regions") or "us,eu"
    if cur_regions not in ("us,eu", "eu"):
        html(
            '<div class="hint warn">You are on <code>'
            f'{cur_regions}</code> regions — spending extra credits. '
            '<code>us,eu</code> (2 credits/sport) gives both Pinnacle fair AND the offshore shop books. '
            'Switch in Settings.</div>'
        )

    # Scan ALL sports (we grouped Kalshi markets by game, Pinnacle by sport).
    edges = []
    scan_fn = getattr(agent_calc, "scan_kalshi_vs_pinnacle", None)
    if not callable(scan_fn):
        html('<div class="hint err">Agent scanner missing — try reloading.</div>')
        return []
    shop_books = saved_cfg.get("shop_books") or ["Bovada", "LowVig.ag", "BetOnline.ag", "BetUS", "MyBookie.ag"]
    markets_list = [m.strip() for m in (saved_cfg.get("odds_markets") or "h2h").split(",") if m.strip()]
    scan_other_fn = getattr(agent_calc, "scan_offshore_markets_vs_pinnacle", None)
    for sp in (sports_list or []):
        sp_kalshi = kalshi_markets_by_sport.get(sp, {})
        sp_pin = pinnacle_games_by_sport.get(sp, [])
        sp_espn = espn_events_by_sport.get(sp, {})
        if not sp_pin:
            continue  # no Pinnacle data for this sport today - skip entirely
        want_h2h = "h2h" in markets_list

        # h2h via Kalshi+shop vs Pinnacle - only when Kalshi has markets for this sport.
        if want_h2h and sp_kalshi:
            try:
                sport_edges = scan_fn(
                    kalshi_markets_by_game=sp_kalshi,
                    pinnacle_games=sp_pin,
                    bankroll_cash=kalshi_cash,
                    sport_key=sp,
                    kelly_mult=kelly_mult,
                    min_edge_pct=min_edge,
                    max_bet_pct=max_bet,
                    shop_books=shop_books,
                    espn_events_by_game=sp_espn,
                )
            except Exception:
                sport_edges = []
            edges.extend(sport_edges)

        # Non-h2h markets ALWAYS go through the offshore scanner.
        # When this sport has NO Kalshi (soccer / tennis / F1 / UFC / etc.),
        # ALSO add h2h to the offshore list so we don't silently skip the sport.
        other_markets = [m for m in markets_list if m != "h2h"]
        if want_h2h and not sp_kalshi:
            other_markets = ["h2h"] + other_markets
        if other_markets and callable(scan_other_fn):
            try:
                other_edges = scan_other_fn(
                    pinnacle_games=sp_pin,
                    bankroll_cash=kalshi_cash,
                    sport_key=sp,
                    markets=other_markets,
                    shop_books=shop_books,
                    kelly_mult=kelly_mult,
                    min_edge_pct=min_edge,
                    max_bet_pct=max_bet,
                    espn_events_by_game=sp_espn,
                )
            except Exception:
                other_edges = []
            edges.extend(other_edges)
    # Deduplicate across sports — key by (event, market, side).
    seen = set()
    deduped = []
    for e in edges:
        key = (e.get("event_id"), e.get("market"), e.get("side_label"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(e)
    edges = sorted(deduped, key=lambda e: -e["edge"])

    # Apply total-exposure cap.
    _cap_fn = getattr(agent_calc, "apply_daily_cap", None)
    if callable(_cap_fn):
        try:
            edges = _cap_fn(edges, kalshi_cash, daily_cap_pct)
        except Exception:
            pass
    summary = agent_calc.summarize_edges(edges)
    unit_pct = settings.get("unit_pct") or 0.01
    unit_dollars = kalshi_cash * unit_pct
    # Convert stakes to units, rounded to nearest 0.5u for display.
    def _to_units_raw(dollars):
        return (dollars / unit_dollars) if unit_dollars > 0 else 0.0
    def _to_units(dollars):
        return round(_to_units_raw(dollars) * 2) / 2
    total_stake_units = _to_units(summary['total_stake'])
    cap_units = _to_units(kalshi_cash * daily_cap_pct)
    # Hide picks whose Kelly recommendation rounds to 0u (too small to be actionable).
    funded_pre = [e for e in edges if (e.get("stake_dollars") or 0) > 0]
    for e in funded_pre:
        if _to_units(e.get("stake_dollars") or 0) <= 0:
            e["stake_dollars"] = 0.0
            e["rounded_to_zero"] = True
    exposure_pct = (summary['total_stake'] / kalshi_cash) * 100 if kalshi_cash else 0
    exposure_tone = "warn" if exposure_pct > daily_cap_pct * 100 * 0.9 else ("good" if exposure_pct > 0 else "")
    metric_row([
        {"label": "Edges", "value": str(summary["count"]), "foot": f"≥{min_edge*100:.1f}% edge", "tone": "good" if summary["count"] else ""},
        {"label": "Best", "value": f"{(summary['best_edge'] or 0)*100:+.1f}pts" if summary["best_edge"] else "—"},
        {"label": "Total stake", "value": f"{total_stake_units:.1f}u", "foot": f"of {cap_units:.1f}u cap", "tone": exposure_tone},
        {"label": "Exposure", "value": f"{exposure_pct:.1f}%", "foot": f"{daily_cap_pct*100:.0f}% cap today", "tone": exposure_tone},
        {"label": "Avg edge", "value": f"{(summary['avg_edge'] or 0)*100:+.1f}pts" if summary["avg_edge"] else "—"},
    ])

    if not edges:
        html('<div class="hint">No edges meet the threshold right now. Lower the edge requirement in the Settings tab to see thinner opportunities.</div>')
        return []
    # ----- Filter controls -----
    all_sports = sorted({(e.get("_sport") or "").upper() for e in edges if e.get("_sport")})
    all_markets_present = sorted({e.get("market") or "h2h" for e in edges})
    all_books_present = sorted({e.get("book") or "" for e in edges if e.get("book")})
    all_venues = ["Kalshi", "Offshore shop"]  # grouped venue filter

    fc1, fc2, fc3, fc4 = st.columns([2, 2, 2, 1])
    with fc1:
        f_sports = st.multiselect(
            "Sport", options=all_sports, default=all_sports,
            key="agent_f_sports", label_visibility="collapsed",
            placeholder="Sport filter",
        )
    with fc2:
        f_markets = st.multiselect(
            "Market", options=all_markets_present, default=all_markets_present,
            format_func=lambda m: agent_calc.MARKET_LABELS.get(m, m),
            key="agent_f_markets", label_visibility="collapsed",
            placeholder="Market filter",
        )
    with fc3:
        f_books = st.multiselect(
            "Book", options=all_books_present, default=all_books_present,
            key="agent_f_books", label_visibility="collapsed",
            placeholder="Book filter",
        )
    with fc4:
        f_min_edge = st.number_input(
            "Min edge %", 0.0, 50.0, min_edge * 100, 0.5,
            key="agent_f_min_edge", label_visibility="collapsed",
        )
    # Apply filters - every toggle is independent and multi-select so turning
    # one off doesn't force you to re-tick everything.
    def _keep(e):
        if f_sports and (e.get("_sport") or "").upper() not in f_sports:
            return False
        if f_markets and (e.get("market") or "h2h") not in f_markets:
            return False
        if f_books and (e.get("book") or "") not in f_books:
            return False
        if e.get("edge", 0) * 100 < f_min_edge:
            return False
        return True
    filtered = [e for e in edges if _keep(e)]
    if not filtered:
        html('<div class="hint">Every edge was filtered out. Clear a filter above to see picks again.</div>')
        return []
    # Separate funded picks from cap-dropped picks so the main table only shows actionable rows.
    funded = [e for e in filtered if (e.get("stake_dollars") or 0) > 0]
    dropped = [e for e in filtered if (e.get("stake_dollars") or 0) <= 0]

    # Build a status/score cell for an edge based on ESPN state.
    from datetime import datetime
    from zoneinfo import ZoneInfo
    CT = ZoneInfo("America/Chicago")

    def _status_cell(e):
        state = e.get("status_state")
        hs = e.get("home_score"); as_ = e.get("away_score")
        if state == "post":
            score = f"{as_}–{hs}" if hs is not None and as_ is not None else "final"
            return f'<span class="chip" style="padding:2px 7px; font-size:9px;">FINAL</span><br><span class="mono" style="font-size:11px;">{score}</span>'
        if state == "in":
            score = f"{as_}–{hs}" if hs is not None and as_ is not None else ""
            detail = (e.get("status_detail") or "LIVE")[:18]
            return (
                f'<span class="chip live" style="padding:2px 7px; font-size:9px;">● {detail}</span>'
                + (f'<br><span class="mono" style="font-size:11px;">{score}</span>' if score else '')
            )
        # pre-game: show start time in CT
        start = e.get("start_utc")
        if start:
            try:
                t = start.astimezone(CT).strftime("%I:%M %p").lstrip("0") + " CT"
                return f'<span class="mono" style="font-size:11px; color:var(--muted);">{t}</span>'
            except Exception:
                pass
        return '<span style="color:var(--muted);">—</span>'

    def _grade_cell(e):
        g = e.get("grade")
        if g == "win":    return '<span class="chip good" style="padding:2px 7px; font-size:9px;">✓ WIN</span>'
        if g == "loss":   return '<span class="chip bad"  style="padding:2px 7px; font-size:9px;">✗ LOSS</span>'
        if g == "push":   return '<span class="chip warn" style="padding:2px 7px; font-size:9px;">= PUSH</span>'
        if g == "winning":return '<span class="chip good" style="padding:2px 7px; font-size:9px;">▲ ahead</span>'
        if g == "losing": return '<span class="chip bad"  style="padding:2px 7px; font-size:9px;">▼ behind</span>'
        if g == "tied":   return '<span class="chip" style="padding:2px 7px; font-size:9px;">= tied</span>'
        return '<span style="color:var(--muted); font-size:11px;">—</span>'

    def _row(e, highlight: bool = False):
        edge_pct = e["edge"] * 100.0
        tone = "ev-plus" if edge_pct > 0 else "ev-minus"
        side_label = e.get("side_label", "")
        best_book = e.get("book", "?")
        is_kalshi_best = best_book.lower() == "kalshi"
        best_price = format_american(e.get("price"))
        best_imp = f"{e['book_implied']*100:.1f}%"
        kalshi_cents = e.get("kalshi_price_cents")
        kalshi_cell = (
            f'<span class="mono">{int(kalshi_cents)}¢</span>'
            if kalshi_cents is not None else '<span style="color:var(--muted);">—</span>'
        )
        kalshi_url = e.get("kalshi_url") or "https://kalshi.com/markets"
        if is_kalshi_best:
            link_cell = (
                f'<a href="{kalshi_url}" target="_blank" rel="noopener" '
                'style="color:var(--accent); text-decoration:none; font-size:11px;">Open ↗</a>'
            )
            best_book_html = f'<span class="chip accent" style="padding:2px 7px; font-size:9.5px;">Kalshi</span>'
        else:
            link_cell = f'<span style="color:var(--muted); font-size:11px;">— offshore —</span>'
            best_book_html = f'<span class="chip warn" style="padding:2px 7px; font-size:9.5px;">{best_book}</span>'
        row_style = (
            ' style="background: color-mix(in oklab, var(--good) 10%, transparent);"'
            if highlight else ""
        )
        strong_open = "<strong>" if highlight else ""
        strong_close = "</strong>" if highlight else ""
        sport_label = (e.get("_sport") or "").upper() or "—"
        market_label = e.get("market_label") or agent_calc.MARKET_LABELS.get(e.get("market", "h2h"), "Moneyline")
        return (
            f'<tr{row_style}>'
            f'<td><span class="chip" style="padding:1px 6px; font-size:9.5px;">{sport_label}</span></td>'
            f'<td>{_status_cell(e)}</td>'
            f'<td>{strong_open}{e["matchup"]}{strong_close}</td>'
            f'<td class="mono" style="color:var(--muted); font-size:11px;">{market_label}</td>'
            f'<td>{strong_open}{side_label}{strong_close}</td>'
            f'<td>{best_book_html}</td>'
            f'<td class="num mono"><strong>{best_price}</strong></td>'
            f'<td class="num mono">{best_imp}</td>'
            f'<td class="num" style="text-align:right;">{kalshi_cell}</td>'
            f'<td class="num mono" style="color:var(--good);">{e["fair_prob"]*100:.1f}%</td>'
            f'<td class="num mono {tone}"><strong>{edge_pct:+.2f}</strong></td>'
            f'<td class="num mono"><strong>{_to_units(e["stake_dollars"]):.2f}u</strong></td>'
            f'<td>{_grade_cell(e)}</td>'
            f'<td>{link_cell}</td>'
            f'</tr>'
        )

    header = (
        '<thead><tr>'
        '<th>Sport</th>'
        '<th>Status</th>'
        '<th>Matchup</th>'
        '<th>Market</th>'
        '<th>Bet on</th>'
        '<th>Best book</th>'
        '<th class="num" title="Best price across Kalshi + shop books">Price</th>'
        '<th class="num">Best IP</th>'
        '<th class="num" title="Kalshi\'s YES price for comparison (h2h only)">Kalshi ¢</th>'
        '<th class="num" title="Pinnacle no-vig fair probability">Pinn fair</th>'
        '<th class="num">Edge (pts)</th>'
        '<th class="num" title="Stake in units. 1u = your configured unit size.">Stake (u)</th>'
        '<th>Result</th>'
        '<th></th>'
        '</tr></thead>'
    )

    top = funded[:15]
    overflow_funded = funded[15:]
    # Attach stable anchor ids so the analysis boards can scroll-nav back and forth.
    for i, e in enumerate(top):
        e["_anchor"] = f"pick-{i+1}"
    html('<div id="agent-table-top" style="position:relative; top:-40px;"></div>')
    if not top:
        # Re-scan at progressively looser thresholds to tell the user WHY they see nothing.
        suggestion = ""
        for try_thr in (0.01, 0.005, 0.0):
            try_edges = []
            for sp in (sports_list or []):
                sp_kalshi = kalshi_markets_by_sport.get(sp, {})
                if not sp_kalshi:
                    continue
                try:
                    try_edges += scan_fn(
                        kalshi_markets_by_game=sp_kalshi,
                        pinnacle_games=pinnacle_games_by_sport.get(sp, []),
                        bankroll_cash=kalshi_cash,
                        sport_key=sp, kelly_mult=kelly_mult,
                        min_edge_pct=try_thr, max_bet_pct=max_bet,
                        shop_books=shop_books,
                        espn_events_by_game=espn_events_by_sport.get(sp, {}),
                    )
                except Exception:
                    pass
            # dedupe
            seen2 = set(); de = []
            for e in try_edges:
                k = (e.get("event_id"), e.get("side_label"))
                if k in seen2:
                    continue
                seen2.add(k); de.append(e)
            if de:
                suggestion = (
                    f'<strong>{len(de)} edges exist at {try_thr*100:.1f}%+</strong> — '
                    f'current threshold is {min_edge*100:.1f}%. Lower it in the sidebar '
                    f'"Minimum edge (%)" slider or on the Settings tab.'
                )
                break
        if not suggestion:
            suggestion = (
                "No Kalshi market is mispriced vs Pinnacle today — both are near-fair exchanges, "
                "so this happens on efficient slates. Check back closer to game time."
            )
        html(f'<div class="hint">{suggestion}</div>')
        return []
    html(
        '<table class="ledger" style="margin-top:4px;">'
        + header
        + f'<tbody>{"".join(_row(e) for e in top)}</tbody>'
        + '</table>'
    )
    # One-click "Log pick"
    with st.expander("⚡ One-click log for the top picks", expanded=False):
        for i, e in enumerate(top):
            edge_pct = e["edge"] * 100
            matchup = e.get("matchup", "?")
            side_label = e.get("side_label", "")
            best_book = e.get("book", "?")
            best_price = format_american(e.get("price"))
            kurl = e.get("kalshi_url") or "https://kalshi.com/markets"
            cols = st.columns([5, 1, 1, 1, 1])
            stake_u = _to_units(e.get("stake_dollars") or 0)
            with cols[0]:
                st.markdown(
                    f"**{matchup}** — {side_label}  "
                    f"<span style='color:var(--muted);font-size:12px;'>· {best_book} @ **{best_price}** · "
                    f"<span style='color:var(--good);'>+{edge_pct:.2f}pts</span> · "
                    f"**{stake_u:.2f}u**</span>",
                    unsafe_allow_html=True,
                )
            cols[1].markdown(f"**{best_price}**")
            cols[2].markdown(f"{e.get('_sport', '').upper()}")
            if best_book.lower() == "kalshi":
                cols[3].markdown(f"[Open ↗]({kurl})")
            else:
                cols[3].markdown(f"_{best_book}_")
            if cols[4].button("Log", key=f"agent_log_{i}_{e.get('event_id','?')}_{side_label[:6]}"):
                picks_store.add_pick({
                    "sport": (e.get("_sport") or "").lower(),
                    "event_id": e.get("event_id"),
                    "event_name": matchup,
                    "market": "h2h",
                    "selection": side_label,
                    "point": None,
                    "price_american": e.get("price"),
                    "projected_prob": e.get("fair_prob"),
                    "implied_prob": e.get("book_implied"),
                    "risk_units": round(e.get("stake_dollars", 0) / max(1e-6, kalshi_cash * (settings.get("unit_pct") or 0.1)), 2),
                    "win_units": None,
                    "methodology": "Model Edge",
                    "notes": f"{best_book} @ {best_price} vs Pinnacle · edge {edge_pct:+.2f}pts · Kalshi {int(e.get('kalshi_price_cents') or 0)}¢",
                })
                st.cache_data.clear()
                st.toast(f"Logged: {side_label} @ {best_price} · {stake_u:.2f}u ({best_book})", icon="✅")

    if overflow_funded:
        with st.expander(f"➕ {len(overflow_funded)} more funded picks", expanded=False):
            html(
                '<table class="ledger" style="margin-top:4px;">'
                + header
                + f'<tbody>{"".join(_row(e, highlight=True) for e in overflow_funded)}</tbody>'
                + '</table>'
            )

    if dropped:
        # Dropped picks hit the daily cap - show them separately with $0 stake so
        # the user can see opportunities their cap didn't fund.
        with st.expander(f"⚠ {len(dropped)} edges found but capped out (no stake)", expanded=False):
            by_sport: Dict[str, list] = {}
            for e in dropped:
                key = (e.get("_sport") or "other").upper()
                by_sport.setdefault(key, []).append(e)
            for sport_key in sorted(by_sport.keys(), key=lambda k: -max(ev["edge"] for ev in by_sport[k])):
                sport_edges = by_sport[sport_key]
                st.markdown(f"**{sport_key}** · {len(sport_edges)} edges")
                html(
                    '<table class="ledger" style="margin-top:4px;">'
                    + header
                    + f'<tbody>{"".join(_row(e, highlight=True) for e in sport_edges[:50])}</tbody>'
                    + '</table>'
                )
    html(
        '<div class="hint" style="margin-top:10px;">'
        f'Agent compares <strong>Kalshi</strong> + offshore books '
        f'(<em>Bovada, LowVig.ag, BetOnline.ag, BetUS, MyBookie.ag</em>) against <strong>Pinnacle no-vig</strong>. '
        f'For each outcome, the <strong>best price</strong> wins the row — bet on whichever book is shown. '
        f'Offshore books tolerate winning accounts; retail (DK/FD/MGM) are deliberately excluded. '
        f'Stakes sized at <strong>{kelly_mult*100:.0f}%-Kelly</strong>, 1u = {unit_pct*100:.1f}% of your Kalshi cash, '
        f'per-bet cap <strong>{max_bet/unit_pct:.1f}u</strong>, daily total cap <strong>{daily_cap_pct/unit_pct:.1f}u</strong>.'
        '</div>'
    )

    # Jump-nav to the per-pick analysis boards.
    if top:
        nav_links = " ".join(
            f'<a href="#{e["_anchor"]}" style="padding:3px 10px; background:var(--card-elev); '
            f'border:1px solid var(--rule); border-radius:999px; text-decoration:none; '
            f'color:var(--ink); font-family:JetBrains Mono,monospace; font-size:11px;">'
            f'#{i+1}</a>'
            for i, e in enumerate(top)
        )
        html(
            '<div style="margin-top:14px; padding:10px 14px; background:var(--card); '
            'border:1px solid var(--rule); border-radius:10px; display:flex; align-items:center; '
            'gap:10px; flex-wrap:wrap;">'
            '<span style="font-family:JetBrains Mono,monospace; font-size:10px; letter-spacing:0.14em; '
            'text-transform:uppercase; color:var(--muted);">Jump to analysis →</span>'
            f'{nav_links}'
            '</div>'
        )
    return top


def _render_analysis_boards(top_picks, settings, kalshi_cash):
    """One detailed quantitative-analysis card per top pick, with scroll-nav back to the main table."""
    if not top_picks:
        return
    saved_cfg = settings_store.load()
    unit_pct = settings.get("unit_pct") or 0.01
    unit_dollars = kalshi_cash * unit_pct
    kelly_mult = settings.get("agent_kelly") or 0.25
    max_bet = settings.get("agent_max_bet_pct") or 0.05
    daily_cap_pct = saved_cfg.get("daily_exposure_cap_pct", 0.25)

    def _units_rounded(dollars):
        if unit_dollars <= 0:
            return 0.0
        return round((dollars / unit_dollars) * 2) / 2

    html(
        '<h3 id="analysis-top" style="margin-top:34px; padding-top:10px; '
        'border-top:1px solid var(--rule);">Quantitative analysis</h3>'
        '<p style="color:var(--muted); font-size:13px; margin-top:-6px;">'
        'Each funded pick, decomposed: the fair-price source, the Kelly math, '
        'alternate venues, and current ESPN state.</p>'
    )

    for i, e in enumerate(top_picks):
        _render_single_pick_analysis(e, i + 1, len(top_picks), unit_dollars,
                                     _units_rounded, kelly_mult, max_bet, daily_cap_pct)


def _render_single_pick_analysis(e, idx, total, unit_dollars, _units_fn, kelly_mult, max_bet, daily_cap_pct):
    """Render one pick's detailed analysis card with its own anchor + navigation."""
    from calc.odds import american_to_decimal, implied_prob_american
    anchor = e.get("_anchor") or f"pick-{idx}"
    matchup = e.get("matchup") or "?"
    sport = (e.get("_sport") or "").upper()
    market_label = e.get("market_label") or agent_calc.MARKET_LABELS.get(e.get("market", "h2h"), "Moneyline")
    side_label = e.get("side_label") or ""
    best_book = e.get("book") or "?"
    best_price = format_american(e.get("price"))
    best_imp = e.get("book_implied") or 0
    fair_prob = e.get("fair_prob") or 0
    edge_pct = (e.get("edge") or 0) * 100
    stake_u = _units_fn(e.get("stake_dollars") or 0)
    stake_dollars = e.get("stake_dollars") or 0
    kelly_frac = e.get("kelly_frac") or 0
    status_detail = e.get("status_detail") or ""
    status_state = e.get("status_state") or ""
    hs = e.get("home_score"); as_ = e.get("away_score")
    kalshi_url = e.get("kalshi_url")

    # EV calculation
    dec = american_to_decimal(e.get("price"))
    profit_on_win = (dec - 1) * stake_dollars if dec and stake_dollars else 0
    ev_dollars = (fair_prob * profit_on_win) - ((1 - fair_prob) * stake_dollars)
    ev_pct = (ev_dollars / stake_dollars * 100) if stake_dollars > 0 else 0

    # Alternate books from candidates list (if present)
    candidates = e.get("candidates") or []
    if candidates:
        candidates_sorted = sorted(candidates, key=lambda c: c["implied"])
        best_imp_val = candidates_sorted[0]["implied"]
    else:
        candidates_sorted = []
        best_imp_val = best_imp

    # Prev/next anchors
    prev_a = f"pick-{idx-1}" if idx > 1 else "agent-table-top"
    next_a = f"pick-{idx+1}" if idx < total else "agent-table-top"
    prev_label = f"← Pick #{idx-1}" if idx > 1 else "↑ Main board"
    next_label = f"Pick #{idx+1} →" if idx < total else "↑ Main board"

    # Live/final status chip
    status_chip = ""
    if status_state == "post":
        score = f"{as_}–{hs}" if hs is not None and as_ is not None else "final"
        status_chip = f'<span class="chip" style="padding:3px 10px;">FINAL · {score}</span>'
    elif status_state == "in":
        score = f"{as_}–{hs}" if hs is not None and as_ is not None else ""
        status_chip = f'<span class="chip live" style="padding:3px 10px;">● LIVE · {status_detail} · {score}</span>'
    elif e.get("start_utc"):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        try:
            t = e["start_utc"].astimezone(ZoneInfo("America/Chicago")).strftime("%a %I:%M %p").lstrip("0") + " CT"
            status_chip = f'<span class="chip" style="padding:3px 10px;">{t}</span>'
        except Exception:
            pass

    # Alt-book table
    alt_rows = ""
    for c in candidates_sorted:
        is_best = c["implied"] == best_imp_val
        tone_style = 'style="color:var(--good); font-weight:600;"' if is_best else 'style="color:var(--muted);"'
        alt_rows += (
            f'<tr>'
            f'<td class="mono">{c["book"]}</td>'
            f'<td class="num mono">{format_american(c["price"])}</td>'
            f'<td class="num mono" {tone_style}>{c["implied"]*100:.2f}%</td>'
            f'<td class="num mono" {tone_style}>{(fair_prob - c["implied"])*100:+.2f} pts</td>'
            f'</tr>'
        )
    alt_table = ""
    if alt_rows:
        alt_table = (
            '<div style="margin-top:14px;">'
            '<div class="mono" style="font-size:10px; letter-spacing:0.14em; text-transform:uppercase; '
            'color:var(--muted); margin-bottom:6px;">Price comparison</div>'
            '<table class="ledger" style="margin:0;">'
            '<thead><tr><th>Venue</th><th class="num">Price</th><th class="num">Implied</th><th class="num">Edge vs Pinn</th></tr></thead>'
            f'<tbody>{alt_rows}</tbody></table>'
            '</div>'
        )

    # Open-bet CTA
    cta_html = ""
    if kalshi_url and best_book.lower() == "kalshi":
        cta_html = (
            f'<a href="{kalshi_url}" target="_blank" rel="noopener" '
            'style="display:inline-block; padding:8px 16px; background:var(--accent); color:var(--accent-ink); '
            'border-radius:8px; text-decoration:none; font-weight:600; font-size:13px;">'
            f'Open on Kalshi ↗</a>'
        )

    # Main board
    html(
        f'<div id="{anchor}" style="position:relative; top:-20px;"></div>'
        f'<div style="background:var(--card); border:1px solid var(--rule); border-radius:12px; '
        f'padding:20px 24px; margin-top:16px;">'
        # Header row
        f'<div style="display:flex; justify-content:space-between; align-items:flex-start; '
        f'flex-wrap:wrap; gap:12px; padding-bottom:12px; border-bottom:1px solid var(--rule); margin-bottom:14px;">'
        f'<div>'
        f'<div style="display:flex; align-items:center; gap:8px; margin-bottom:4px;">'
        f'<span class="chip accent" style="padding:3px 10px;">#{idx}</span>'
        f'<span class="chip" style="padding:3px 10px;">{sport}</span>'
        f'<span class="chip" style="padding:3px 10px;">{market_label}</span>'
        f'{status_chip}'
        f'</div>'
        f'<div style="font-family:Fraunces,serif; font-style:italic; font-size:22px; margin-top:4px;">{matchup}</div>'
        f'<div style="font-size:15px; color:var(--ink); margin-top:4px;"><strong>{side_label}</strong></div>'
        f'</div>'
        f'<div style="text-align:right;">'
        f'<div class="mono" style="font-size:10px; color:var(--muted); letter-spacing:0.12em; text-transform:uppercase;">Edge</div>'
        f'<div class="mono ev-plus" style="font-size:28px; font-weight:500;">{edge_pct:+.2f}pts</div>'
        f'{cta_html}'
        f'</div>'
        f'</div>'
        # Metric blocks
        f'<div style="display:grid; grid-template-columns:repeat(auto-fit, minmax(140px, 1fr)); gap:10px;">'
        f'<div class="metric-card"><div class="label">Price</div><div class="value">{best_price}</div><div class="foot">{best_book}</div></div>'
        f'<div class="metric-card"><div class="label">Market IP</div><div class="value">{best_imp*100:.2f}%</div><div class="foot">at that price</div></div>'
        f'<div class="metric-card"><div class="label">Pinnacle fair</div><div class="value accent">{fair_prob*100:.2f}%</div><div class="foot">no-vig consensus</div></div>'
        f'<div class="metric-card"><div class="label">Kelly</div><div class="value">{kelly_frac*100:.2f}%</div><div class="foot">{kelly_mult*100:.0f}% × full Kelly</div></div>'
        f'<div class="metric-card"><div class="label">Stake</div><div class="value accent">{stake_u:.1f}u</div><div class="foot">of {max_bet/0.01:.0f}u max</div></div>'
        f'<div class="metric-card"><div class="label">EV / 100u</div><div class="value {"good" if ev_pct > 0 else ""}">{ev_pct:+.2f}%</div><div class="foot">expected per 100u risked</div></div>'
        f'</div>'
        # Reasoning
        f'<div style="margin-top:16px; padding:12px 14px; background:var(--card-elev); border-radius:8px; '
        f'font-size:13px; color:var(--ink-dim); line-height:1.6;">'
        f'<strong>Rationale.</strong> Pinnacle\'s no-vig fair prob says this outcome happens '
        f'<strong>{fair_prob*100:.2f}%</strong> of the time. {best_book} is offering it at '
        f'<strong>{best_price}</strong> ({best_imp*100:.2f}% implied), underestimating the true prob by '
        f'<strong>{edge_pct:+.2f} pts</strong>. Quarter-Kelly says stake '
        f'<strong>{kelly_frac*100:.2f}%</strong> of bankroll (<strong>{stake_u:.1f}u</strong>), capped by '
        f'the <strong>{max_bet/0.01:.0f}u</strong> per-bet rule and <strong>{daily_cap_pct/0.01:.0f}u</strong> daily cap.'
        f'</div>'
        f'{alt_table}'
        # Nav footer
        f'<div style="margin-top:16px; padding-top:12px; border-top:1px solid var(--rule); '
        f'display:flex; justify-content:space-between; align-items:center;">'
        f'<a href="#{prev_a}" style="color:var(--muted); text-decoration:none; font-size:12px;">{prev_label}</a>'
        f'<a href="#agent-table-top" style="color:var(--accent); text-decoration:none; font-size:12px; font-weight:500;">↑ Main board</a>'
        f'<a href="#{next_a}" style="color:var(--muted); text-decoration:none; font-size:12px;">{next_label}</a>'
        f'</div>'
        f'</div>'
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

    cur_regions = saved.get("odds_regions") or "eu"
    cur_markets = saved.get("odds_markets") or "h2h"
    cur_ttl = int(saved.get("odds_live_ttl") or 3600)
    cost_per_fetch = odds_api.estimated_credit_cost(cur_regions, cur_markets)
    st.caption(
        f"Current cost: **{cost_per_fetch} credits** per sport per fetch "
        f"(regions=`{cur_regions}` · markets=`{cur_markets}` · cache={cur_ttl//60} min). "
        f"On-disk cache at `.odds_cache/` — historical snapshots cached permanently."
    )
    if cost_per_fetch > 1:
        html(
            '<div class="hint warn" style="margin-top:6px;">'
            '<strong>Credit tip:</strong> set regions to <code>eu</code> and markets to <code>h2h</code> '
            'to make each sport cost <strong>1 credit</strong>. Pinnacle lives in <code>eu</code> '
            'and is all the agent needs for fair-prob. You bet on <strong>Kalshi</strong> (free), '
            'so extra US books are optional.'
            '</div>'
        )

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
        region_options = ["us,eu", "eu", "us", "us,us2,eu", "us,eu,uk,au"]
        new_regions = st.selectbox(
            "Odds API regions",
            options=region_options,
            index=region_options.index(current_regions) if current_regions in region_options else 0,
            key="set_regions",
            help="us,eu (recommended) = Pinnacle fair + offshore shop books (Bovada/LowVig/BetOnline), "
                 "2 credits per sport. eu alone = Pinnacle only (1 credit), smaller edges because you "
                 "can only bet on Kalshi.",
        )
        if new_regions != current_regions:
            settings_store.save({"odds_regions": new_regions})
            st.cache_data.clear()
            n = len(new_regions.split(","))
            st.success(f"Regions: {new_regions} → {n * len(cur_markets.split(','))} credits per sport per fetch.")

    # Markets picker — more markets = more edges but linearly more credits.
    st.markdown("### Markets to scan")
    all_markets = ["h2h", "totals", "spreads", "h2h_h1", "totals_h1", "btts"]
    labels = {
        "h2h": "Moneyline (h2h) — required for Kalshi",
        "totals": "Full-game totals (O/U)",
        "spreads": "Point spreads",
        "h2h_h1": "1st-half moneyline",
        "totals_h1": "1st-half totals (O/U)",
        "btts": "Both teams to score (soccer only)",
    }
    cur_markets_list = [m.strip() for m in (saved.get("odds_markets") or "h2h").split(",") if m.strip()]
    new_markets = st.multiselect(
        "Markets",
        options=all_markets,
        default=cur_markets_list,
        format_func=lambda m: labels.get(m, m),
        key="set_markets",
    )
    if "h2h" not in new_markets:
        new_markets = ["h2h"] + new_markets  # always keep h2h for Kalshi
    new_markets_str = ",".join(new_markets)
    n_regions = len(cur_regions.split(","))
    n_markets = len(new_markets)
    new_cost = n_regions * n_markets
    st.caption(
        f"Credit cost preview: **{new_cost} credits** per sport per fetch "
        f"(regions × markets = {n_regions} × {n_markets}). "
        f"ESPN pre-filter already skips sports with no games today."
    )
    if new_markets_str != (saved.get("odds_markets") or "h2h"):
        settings_store.save({"odds_markets": new_markets_str})
        st.cache_data.clear()
        st.success(f"Markets: {new_markets_str}")
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

    # ----- Reset -----
    st.markdown("### Reset")
    rc1, rc2 = st.columns([1, 3])
    with rc1:
        if st.button("Reset to recommended defaults", use_container_width=True, type="primary"):
            import os
            try:
                path = os.path.join(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "settings.json",
                )
                if os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass
            # Clear sidebar session_state so sliders snap back to defaults.
            for k in list(st.session_state.keys()):
                if k.startswith("sb_") or k.startswith("set_") or k.startswith("agent_f_"):
                    del st.session_state[k]
            st.cache_data.clear()
            st.success("Settings reset. Reloading…")
            st.rerun()
    with rc2:
        st.caption(
            "This clears your saved settings.json and resets to the credit-efficient defaults: "
            "regions=eu, markets=h2h, cache=60min, min-edge=1%, unit=10% of Kalshi cash, "
            "daily cap=25%, theme=dark. "
            "Does **not** clear your API keys or Kalshi connection."
        )

    # ----- Theme + exposure cap -----
    st.markdown("### Appearance & risk")
    tc1, tc2 = st.columns(2)
    with tc1:
        theme = st.selectbox(
            "Theme", options=["dark", "light"],
            index=0 if saved.get("theme", "dark") == "dark" else 1,
            format_func=lambda t: t.title(),
            key="set_theme",
        )
        if theme != saved.get("theme", "dark"):
            settings_store.save({"theme": theme})
            st.success(f"Theme set to {theme}. Reloading…")
            st.rerun()
    with tc2:
        cap = st.slider(
            "Daily total-exposure cap (% of Kalshi cash)",
            5, 100, int(saved.get("daily_exposure_cap_pct", 0.25) * 100), 5,
            key="set_cap",
            help="If the agent's total recommended stakes exceed this % of your "
                 "Kalshi cash, every stake is scaled down proportionally.",
        ) / 100.0
        if abs(cap - saved.get("daily_exposure_cap_pct", 0.25)) > 1e-6:
            settings_store.save({"daily_exposure_cap_pct": cap})
            st.success(f"Daily cap: {cap*100:.0f}% of Kalshi cash")

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
    bt_regions = settings_store.load().get("odds_regions", "us,eu")
    cost_per_snapshot = odds_history.estimated_credit_cost_per_snapshot(bt_regions, "h2h,spreads,totals")
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
