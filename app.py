"""Edge v2.0 - sport-agnostic betting dashboard.

Run:
    cd betting-dashboard-v2
    streamlit run app.py

Add The Odds API key to enable live lines, EV, Kelly, CLV:
    * env var:    THE_ODDS_API_KEY=xxxxxxxx
    * or secrets: .streamlit/secrets.toml   -> the_odds_api_key = "xxxxxxxx"

Everything else (ESPN schedules & scores) is free and needs no key.
"""
from __future__ import annotations

import importlib
import sys
import streamlit as st

# Dev-only: force fresh reimport of local modules so edits land on rerun.
for _m in [
    "ui.style", "ui.components", "ui.pages",
    "apis.espn", "apis.odds", "apis.team_mapping",
    "calc.odds", "calc.ev", "calc.kelly", "calc.units", "calc.clv",
    "storage.picks",
]:
    if _m in sys.modules:
        try:
            importlib.reload(sys.modules[_m])
        except Exception:
            pass

from apis import espn, odds as odds_api, kalshi
from storage import picks as picks_store, settings as settings_store
from ui.style import inject as inject_style
from ui.pages import (
    render_dashboard, render_analysis, render_pro_tools, render_picks,
    render_settings, render_backtest, load_kalshi_balance,
    render_performance, render_calibration, render_results,
)


def _sidebar_settings() -> dict:
    saved = settings_store.load()
    with st.sidebar:
        st.markdown("### Settings")

        # Sports selection moved to the Board itself (the Sports/Markets filter
        # strip above the agent table). The sidebar now holds only connection
        # + sizing + tuning concerns; the slate choice lives where the user
        # looks at results.

        # ----- KALSHI LIVE CONNECTION -----
        st.markdown("#### Kalshi")
        with st.expander("Connect Kalshi (live cash)", expanded=False):
            st.caption(
                "Credentials live in this browser session only — never written to disk. "
                "Get keys at kalshi.com → Account → API Keys."
            )
            kid_in = st.text_input(
                "Access key ID",
                value=st.session_state.get("_kalshi_rt_key_id", ""),
                key="sb_k_kid", type="password",
            )
            pem_in = st.text_area(
                "RSA private key (PEM)",
                value=st.session_state.get("_kalshi_rt_pem", ""),
                key="sb_k_pem", height=120,
                placeholder="-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----",
            )
            c_a, c_b = st.columns(2)
            if c_a.button("Connect", use_container_width=True, type="primary"):
                if kid_in.strip() and pem_in.strip():
                    kalshi.set_runtime_credentials(kid_in.strip(), pem_in.strip())
                    result = kalshi.test_credentials()
                    if result.get("ok"):
                        # Persist to secrets.toml so the connection survives restarts.
                        settings_store.save_kalshi_keys(kid_in.strip(), pem_in.strip())
                        st.cache_data.clear()
                        st.success(f"✓ Connected · Balance: ${result['balance_usd']:,.2f} · Keys saved")
                        st.rerun()
                    else:
                        # Keep creds in session so user can tweak; show detailed error.
                        st.error(result.get("error") or "Connect failed.")
                        hint = result.get("hint")
                        if hint:
                            st.caption(hint)
                else:
                    st.warning("Both the access key ID and PEM are required.")
            if c_b.button("Disconnect", use_container_width=True):
                kalshi.clear_runtime_credentials()
                settings_store.clear_kalshi_keys()
                st.cache_data.clear()
                st.success("Credentials cleared from session and local secrets.")
                st.rerun()

        # Resolve current balance (session creds take priority, then env/secrets).
        # Manual cash entry removed — the ONLY bankroll source is the Kalshi
        # connection above. When no credentials are set, we fall back to a
        # saved placeholder so sizing math still works without crashing.
        balance = load_kalshi_balance() or {"usd": None, "source": None}
        live_usd = balance.get("usd")
        live_src = balance.get("source")
        if live_src == "live":
            st.success(f"Live Kalshi balance: ${live_usd:,.2f}")
        elif live_src == "manual":
            st.info(f"Env balance: ${live_usd:,.2f}")
        else:
            st.caption("Not connected — bankroll sizing uses the last known value.")

        kalshi_cash = float(live_usd) if live_usd is not None else float(saved.get("manual_kalshi_cash") or 500.0)
        kalshi_source = live_src or ("saved" if kalshi_cash else None)

        # Unit = X% of (live) Kalshi cash. 1% follows pro convention.
        unit_pct_of_kalshi = st.slider(
            "1 unit = % of Kalshi cash",
            min_value=0.25, max_value=10.0,
            value=float(saved.get("unit_pct_of_kalshi", 0.01)) * 100,
            step=0.25,
            key="sb_unit_pct_kalshi",
            help="Pro convention is 1u = 1% of bankroll. Typical plays range 0.5-3u, "
                 "max-confidence plays hit 5u.",
        ) / 100.0
        if abs(unit_pct_of_kalshi - saved.get("unit_pct_of_kalshi", 0.01)) > 1e-6:
            settings_store.save({"unit_pct_of_kalshi": unit_pct_of_kalshi})
        unit_dollars = kalshi_cash * unit_pct_of_kalshi
        st.caption(f"1u = **${unit_dollars:,.2f}** · max bet {(settings_store.load().get('agent_max_bet_pct', 0.025))/unit_pct_of_kalshi:.1f}u · daily cap {(settings_store.load().get('daily_exposure_cap_pct', 0.25))/unit_pct_of_kalshi:.1f}u")

        # ----- PRIMARY BOOK (sets which book shows in each game card's lines block) -----
        st.markdown("#### Lines display")
        primary_book = st.selectbox(
            "Primary book (shown on game cards)",
            options=["kalshi", "pinnacle", "draftkings", "consensus"],
            format_func=lambda k: {
                "kalshi": "Kalshi (prediction market)",
                "pinnacle": "Pinnacle (sharpest book)",
                "draftkings": "DraftKings",
                "consensus": "Consensus (all books)",
            }[k],
            index=0,
            key="sb_primary_book",
        )
        # ----- BOARD BOOK FILTER (sets what the "Best Book" column shows) -----
        # Default: Kalshi. Picks that don't have a Kalshi market fall back to the
        # shop-best price so no row shows an empty cell. "Best book" is the
        # legacy behavior (whichever offshore book has the longest price).
        board_book_mode = st.radio(
            "Board · Best Book column",
            options=["kalshi", "best"],
            format_func=lambda v: {"kalshi": "Kalshi (default)", "best": "Best Book (shop)"}[v],
            index=["kalshi", "best"].index(saved.get("board_book_mode", "kalshi")),
            horizontal=True, key="sb_board_book_mode",
            help="'Kalshi' shows the Kalshi price on every row, with a shop fallback for "
                 "markets Kalshi doesn't have. 'Best Book' shows whichever book had the longest price.",
        )
        if board_book_mode != saved.get("board_book_mode", "kalshi"):
            settings_store.save({"board_book_mode": board_book_mode})
        # Default: show ONLY Kalshi in the prob-strip. Toggle others on explicitly.
        extra_sources = st.multiselect(
            "Also show in implied-prob strip",
            options=["pinnacle", "draftkings", "polymarket", "betfair", "consensus"],
            format_func=lambda k: {
                "pinnacle":   "Pinnacle (sharp)",
                "draftkings": "DraftKings",
                "polymarket": "Polymarket (exchange)",
                "betfair":    "Betfair (exchange)",
                "consensus":  "Consensus (all books)",
            }[k],
            default=[],
            key="sb_extra_sources",
            help="Kalshi always shows. Pick any additional sources to compare side-by-side.",
        )

        # Legacy sizing kept for ledger history compatibility.
        bankroll_units = kalshi_cash / unit_dollars if unit_dollars > 0 else 0
        unit_pct = unit_pct_of_kalshi

        # ----- AGENT TUNING -----
        st.markdown("---")
        st.markdown("#### Agent")
        agent_kelly = st.selectbox(
            "Kelly multiplier",
            options=[0.125, 0.25, 0.5, 1.0],
            format_func=lambda v: {0.125: "Eighth", 0.25: "Quarter", 0.5: "Half", 1.0: "Full"}[v],
            index=1, key="sb_agent_kelly",
        )
        agent_min_edge = st.slider(
            "Minimum edge (%)",
            min_value=0.1, max_value=10.0,
            value=float(saved.get("agent_min_edge", 0.01)) * 100,
            step=0.1,
            key="sb_agent_min_edge",
            help="Kalshi vs Pinnacle produces smaller edges (~0.5–2%) than scanning retail books. "
                 "1% is a good starting threshold.",
        ) / 100.0
        if abs(agent_min_edge - saved.get("agent_min_edge", 0.01)) > 1e-6:
            settings_store.save({"agent_min_edge": agent_min_edge})
        agent_min_books = st.slider(
            "Min books for consensus",
            min_value=2, max_value=15, value=3, step=1,
            key="sb_agent_min_books",
        )
        agent_max_bet_pct = st.slider(
            "Max bet per edge (%)",
            min_value=0.5, max_value=15.0,
            value=float(saved.get("agent_max_bet_pct", 0.025)) * 100,
            step=0.5,
            key="sb_agent_max_bet",
            help="Hard cap on any single stake, as % of Kalshi cash. "
                 "2.5% keeps the 25% daily cap funding 10 picks.",
        ) / 100.0
        if abs(agent_max_bet_pct - saved.get("agent_max_bet_pct", 0.025)) > 1e-6:
            settings_store.save({"agent_max_bet_pct": agent_max_bet_pct})

        agent_picks_shown = st.select_slider(
            "Picks shown on main board",
            options=[5, 10, 15, 20, 25],
            value=int(saved.get("agent_picks_shown", 15)),
            key="sb_agent_picks_shown",
            help="Top-N picks surfaced in the main agent table and analysis boards. Extras go to the overflow expander.",
        )
        if agent_picks_shown != saved.get("agent_picks_shown", 15):
            settings_store.save({"agent_picks_shown": int(agent_picks_shown)})

        st.markdown("---")
        st.markdown("#### Status")
        key_ok = odds_api.get_api_key() is not None
        if key_ok:
            st.success("The Odds API: connected")
        else:
            st.warning("The Odds API: no key")
        st.caption("ESPN: free, no key required")
        if live_src == "live":
            st.success("Kalshi: live connection")
        else:
            st.caption("Kalshi: manual / not connected")

        if st.button("Clear cache & reload", use_container_width=True):
            st.cache_data.clear()
            st.rerun()

        with st.expander("Auto-settle picks"):
            if st.button("Settle from ESPN finals", use_container_width=True):
                n = picks_store.auto_settle()
                st.cache_data.clear()
                st.success(f"Settled {n} pick(s).")

        st.markdown("---")
        st.caption("Edge v2.0 · ESPN + The Odds API + Kalshi")

    # `sport` now comes from the Sports filter above the Board (render_dashboard
    # writes it into session_state so the Analysis tab sees the same selection).
    # Fall back to the persisted group if the Board hasn't rendered yet.
    board_sports = st.session_state.get("board_sports")
    if not board_sports:
        saved_group = saved.get("sport_group") or "All sports"
        board_sports = next(
            (g[1] for g in espn.SPORT_GROUPS if g[0] == saved_group),
            espn.SPORT_GROUPS[0][1],
        )
    return {
        "sport": board_sports,
        "unit_pct": unit_pct,
        "unit_dollars": unit_dollars,
        "bankroll_units": bankroll_units,
        "kalshi_cash": kalshi_cash,
        "kalshi_source": kalshi_source,
        "primary_book": primary_book,
        "board_book_mode": board_book_mode,
        "extra_sources": extra_sources,
        "fallback_book": saved.get("fallback_book", "DraftKings"),
        "agent_kelly": agent_kelly,
        "agent_min_edge": agent_min_edge,
        "agent_min_books": agent_min_books,
        "agent_max_bet_pct": agent_max_bet_pct,
        "agent_picks_shown": int(agent_picks_shown),
    }


def main():
    st.set_page_config(
        page_title="Edge v2.0 — sports betting dashboard",
        page_icon="🎯",
        layout="wide",
        initial_sidebar_state="collapsed",
    )
    saved_theme = settings_store.load().get("theme", "dark")
    inject_style(theme=saved_theme)

    # Navigation moved out of the top tab bar into the sidebar: on a laptop it
    # saves vertical space (the news hero + board now reach higher on the
    # viewport), and on phone it collapses into the hamburger menu so the
    # board is the only thing on screen. Board stays the default view.
    with st.sidebar:
        st.markdown(
            '<div class="nav-head">'
            '<span class="nav-kicker">◆ View</span>'
            '<h3 style="margin:2px 0 8px;">Pages</h3>'
            '</div>',
            unsafe_allow_html=True,
        )
        page = st.radio(
            "Page", options=[
                "Board", "Results", "Analysis", "Tools", "Ledger",
                "Performance", "Calibration", "Backtest", "Settings",
            ],
            index=0, key="nav_page", label_visibility="collapsed",
        )
        st.markdown("---")

    settings = _sidebar_settings()

    # Only the selected page renders. Board is the default and the only view
    # with a "tab" in the top chrome (via a styled brand bar inside its
    # renderer); everything else shows only when picked from the sidebar.
    if page == "Board":
        render_dashboard(settings)
    elif page == "Results":
        render_results(settings)
    elif page == "Analysis":
        render_analysis(settings)
    elif page == "Tools":
        render_pro_tools(settings)
    elif page == "Ledger":
        render_picks(settings)
    elif page == "Performance":
        render_performance(settings)
    elif page == "Calibration":
        render_calibration(settings)
    elif page == "Backtest":
        render_backtest(settings)
    elif page == "Settings":
        render_settings(settings)


if __name__ == "__main__":
    main()
# reload-marker: 1
