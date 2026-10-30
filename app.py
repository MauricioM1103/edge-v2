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
from storage import picks as picks_store
from ui.style import inject as inject_style
from ui.pages import render_dashboard, render_pro_tools, render_picks, load_kalshi_balance


def _sidebar_settings() -> dict:
    with st.sidebar:
        st.markdown("### Settings")

        # ----- SPORT PICKER (grouped) -----
        group_labels = [g[0] for g in espn.SPORT_GROUPS]
        group_name = st.selectbox(
            "Sport group", options=group_labels, index=0, key="sb_group",
        )
        group_sports = next((g[1] for g in espn.SPORT_GROUPS if g[0] == group_name), ["nfl"])
        sport = st.selectbox(
            "Sport / league",
            options=group_sports,
            format_func=lambda k: espn.DISPLAY_NAMES.get(k, k.upper()),
            index=0,
            key="sb_sport",
        )
        st.markdown("---")

        # ----- KALSHI CASH -----
        st.markdown("#### Kalshi cash")
        balance = load_kalshi_balance() or {"usd": None, "source": None}
        live_usd = balance.get("usd")
        live_src = balance.get("source")
        if live_src == "live":
            st.success(f"Live balance: ${live_usd:,.2f}")
        elif live_src == "manual":
            st.info(f"Manual balance (env): ${live_usd:,.2f}")
        else:
            st.caption("No live Kalshi connection — enter cash manually below.")

        kalshi_cash = st.number_input(
            "Available Kalshi cash ($)",
            min_value=0.0, max_value=10_000_000.0,
            value=float(live_usd) if live_usd is not None else 500.0,
            step=25.0,
            key="sb_kalshi_cash",
            help="The agent sizes recommendations against this balance. Comes from "
                 "the live Kalshi API when a key pair is configured, otherwise enter manually.",
        )
        kalshi_source = live_src or ("manual" if kalshi_cash else None)

        # ----- CLASSIC UNITS -----
        st.markdown("#### Unit sizing")
        bankroll_units = st.number_input(
            "Bankroll (units)",
            min_value=1.0, max_value=10000.0, value=100.0, step=1.0,
            key="sb_bank",
        )
        unit_pct = st.slider(
            "Unit size (% of bankroll)",
            min_value=0.25, max_value=5.0, value=1.0, step=0.25,
            key="sb_unit",
        ) / 100.0

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
            min_value=0.5, max_value=10.0, value=2.0, step=0.5,
            key="sb_agent_min_edge",
            help="Skip opportunities where consensus-fair prob beats book by less than this.",
        ) / 100.0
        agent_min_books = st.slider(
            "Min books for consensus",
            min_value=2, max_value=15, value=3, step=1,
            key="sb_agent_min_books",
        )
        agent_max_bet_pct = st.slider(
            "Max bet per edge (%)",
            min_value=0.5, max_value=15.0, value=5.0, step=0.5,
            key="sb_agent_max_bet",
            help="Hard cap on any single stake, as % of Kalshi cash.",
        ) / 100.0

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

    return {
        "sport": sport,
        "unit_pct": unit_pct,
        "bankroll_units": bankroll_units,
        "kalshi_cash": kalshi_cash,
        "kalshi_source": kalshi_source,
        "agent_kelly": agent_kelly,
        "agent_min_edge": agent_min_edge,
        "agent_min_books": agent_min_books,
        "agent_max_bet_pct": agent_max_bet_pct,
    }


def main():
    st.set_page_config(
        page_title="Edge v2.0 — sports betting dashboard",
        page_icon="🎯",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    inject_style()
    settings = _sidebar_settings()

    tab_dash, tab_pro, tab_picks = st.tabs([
        "Dashboard · Daily Slate",
        "Pro Handicapping Tools",
        "Verified Picks Ledger",
    ])
    with tab_dash:
        render_dashboard(settings)
    with tab_pro:
        render_pro_tools(settings)
    with tab_picks:
        render_picks(settings)


if __name__ == "__main__":
    main()
# reload-marker: 1
