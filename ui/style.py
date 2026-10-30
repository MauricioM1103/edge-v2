"""Premium sportsbook aesthetic.

Inspiration: Action Network's deep-charcoal cards, Pinnacle's precise typographic
hierarchy, VSiN's monospace stat framing. Streamlit's default theme is replaced
with CSS variables that respond to the user's dark/light preference.
"""

CUSTOM_CSS = r"""
<style>
:root {
  --bg: #0b0f17;
  --surface: #141a26;
  --card: #1b2232;
  --card-elev: #212a3d;
  --rule: #2a3348;
  --rule-strong: #3a4660;
  --ink: #ECEFF4;
  --ink-dim: #B8C0CC;
  --muted: #7A8396;
  --muted-dim: #5A6378;
  --accent: #4FD1C5;
  --accent-ink: #0b0f17;
  --good: #4ADE80;
  --warn: #F59E0B;
  --bad: #F87171;
  --live: #EF4444;
  --chip: #2a3348;
}

@media (prefers-color-scheme: light) {
  :root:not([data-theme="dark"]) {
    --bg: #F6F5F0;
    --surface: #FFFFFF;
    --card: #FFFFFF;
    --card-elev: #FBFAF4;
    --rule: #E3E0D4;
    --rule-strong: #CBC7B6;
    --ink: #1A1F2C;
    --ink-dim: #3F4654;
    --muted: #6B7284;
    --muted-dim: #9099A7;
    --accent: #0E7C7B;
    --accent-ink: #FFFFFF;
    --good: #138A4B;
    --warn: #B37100;
    --bad: #B91C1C;
    --live: #DC2626;
    --chip: #F0EDE1;
  }
}

/* ===== Streamlit chrome overrides ===== */
.stApp { background: var(--bg); }
section.main > div.block-container { padding-top: 1.2rem; padding-bottom: 3rem; max-width: 1320px; }
.stApp, .stMarkdown, .stText, .stCaption, p, div, label, li {
  font-family: "Public Sans", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  color: var(--ink);
}
/* Hand Streamlit's icon spans back to the Material Symbols font.
   Streamlit's expander labels contain a span whose text is a Material-ligature
   name (e.g. 'keyboard_arrow_right'); we select it broadly here. */
[class*="material-symbols"],
[class*="material-icon"],
[data-testid*="Icon"],
[data-testid*="Icon"] span,
[data-testid*="Collapsed"] span,
[data-testid*="Expanded"] span,
details > summary span:first-child,
[data-testid="stExpander"] summary span:first-child,
[data-testid="stExpanderToggleIcon"],
.stExpander span[data-testid="stMarkdownContainer"] + span,
button[kind="header"] span,
button[kind="headerNoPadding"] span,
button[data-testid*="baseButton"] svg + span {
  font-family: "Material Symbols Rounded", "Material Symbols Outlined",
               "Material Icons", sans-serif !important;
  font-feature-settings: "liga" 1 !important;
}
/* Safety-net: any span whose literal text looks like a Material ligature
   (lowercase_with_underscores) and is one word gets the icon font too. */
span:is(:not([class]), [class=""]) { font-family: inherit; }
h1, h2, h3, h4 {
  font-family: "Fraunces", "Playfair Display", Georgia, serif !important;
  letter-spacing: -0.015em; color: var(--ink);
  font-weight: 500;
}
h1 { font-size: clamp(28px, 3.6vw, 44px); line-height: 1.1; }
h2 { font-size: 22px; }
h3 { font-size: 17px; }

/* mono numbers everywhere that matters */
.vsin, code, .mono {
  font-family: "JetBrains Mono", ui-monospace, "SF Mono", Menlo, monospace !important;
  font-variant-numeric: tabular-nums;
}

[data-testid="stSidebar"] {
  background: var(--surface);
  border-right: 1px solid var(--rule);
}
[data-testid="stSidebar"] * { color: var(--ink); }

/* tabs — big and clean */
.stTabs [data-baseweb="tab-list"] {
  gap: 6px; border-bottom: 1px solid var(--rule);
  margin-bottom: 20px;
}
.stTabs [data-baseweb="tab"] {
  padding: 10px 16px; background: transparent;
  color: var(--muted); font-weight: 500;
  border-radius: 8px 8px 0 0;
  border: 1px solid transparent;
  border-bottom: 2px solid transparent;
}
.stTabs [aria-selected="true"] {
  color: var(--ink);
  border-bottom-color: var(--accent);
  background: var(--surface);
}

/* ===== brand header ===== */
.brand-bar {
  display: flex; align-items: baseline; justify-content: space-between;
  gap: 16px; margin-bottom: 10px;
  padding-bottom: 14px; border-bottom: 1px solid var(--rule);
}
.brand {
  display: flex; align-items: baseline; gap: 10px;
}
.brand .dot {
  width: 10px; height: 10px; border-radius: 50%;
  background: var(--accent); display: inline-block;
}
.brand .name {
  font-family: "Fraunces", Georgia, serif !important;
  font-style: italic; font-size: 28px; font-weight: 400;
  color: var(--ink); letter-spacing: -0.01em;
}
.brand .ver {
  font-family: "JetBrains Mono", monospace;
  font-size: 10px; letter-spacing: 0.14em;
  color: var(--muted); padding: 3px 8px;
  border: 1px solid var(--rule); border-radius: 999px;
  text-transform: uppercase;
}
.brand-right {
  font-family: "JetBrains Mono", monospace;
  font-size: 11px; letter-spacing: 0.1em;
  color: var(--muted); text-transform: uppercase;
}

/* ===== metric row ===== */
.metric-grid {
  display: grid; gap: 10px;
  grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
  margin: 10px 0 22px;
}
.metric-card {
  background: var(--card); border: 1px solid var(--rule);
  border-radius: 10px; padding: 14px 16px;
}
.metric-card .label {
  font-family: "JetBrains Mono", monospace;
  font-size: 10px; letter-spacing: 0.12em;
  text-transform: uppercase; color: var(--muted);
  margin-bottom: 6px;
}
.metric-card .value {
  font-family: "JetBrains Mono", monospace;
  font-size: 24px; font-weight: 500; color: var(--ink);
  font-variant-numeric: tabular-nums;
}
.metric-card .value.good { color: var(--good); }
.metric-card .value.bad { color: var(--bad); }
.metric-card .value.accent { color: var(--accent); }
.metric-card .foot { color: var(--muted); font-size: 11px; margin-top: 4px; font-family: "JetBrains Mono", monospace; }

/* ===== game card ===== */
.game-card {
  background: var(--card); border: 1px solid var(--rule);
  border-radius: 12px; padding: 16px 18px 14px;
  display: flex; flex-direction: column; gap: 12px;
  margin-bottom: 12px;
}
.game-card:hover { border-color: var(--rule-strong); }
.gc-head {
  display: flex; justify-content: space-between; align-items: center;
  gap: 8px; flex-wrap: wrap;
}
.gc-time { font-family: "JetBrains Mono", monospace; font-size: 12.5px; font-weight: 500; color: var(--ink); font-variant-numeric: tabular-nums; }
.chip {
  font-family: "JetBrains Mono", monospace;
  font-size: 10px; letter-spacing: 0.1em;
  text-transform: uppercase;
  color: var(--muted); padding: 3px 8px;
  border: 1px solid var(--rule); border-radius: 999px;
  background: var(--chip);
}
.chip.live { color: var(--live); border-color: var(--live); animation: pulse 1.6s ease infinite; }
.chip.final { color: var(--muted); }
.chip.good { color: var(--good); border-color: color-mix(in oklab, var(--good) 45%, var(--rule)); }
.chip.bad { color: var(--bad); border-color: color-mix(in oklab, var(--bad) 45%, var(--rule)); }
.chip.warn { color: var(--warn); border-color: color-mix(in oklab, var(--warn) 45%, var(--rule)); }
@keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.5; } }

.gc-matchup {
  display: grid; grid-template-columns: 1fr auto 1fr; gap: 10px; align-items: center;
}
.gc-team {
  display: flex; flex-direction: column; gap: 2px;
}
.gc-team .name { font-size: 17px; font-weight: 600; color: var(--ink); letter-spacing: -0.01em; }
.gc-team .record { font-family: "JetBrains Mono", monospace; font-size: 11px; color: var(--muted); font-variant-numeric: tabular-nums; }
.gc-team.away { text-align: left; }
.gc-team.home { text-align: right; align-items: flex-end; }
.gc-at { color: var(--muted); font-family: "Fraunces", serif; font-style: italic; font-size: 13px; padding: 0 6px; }

.gc-lines {
  display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px;
  border-top: 1px dashed var(--rule); padding-top: 10px;
}
.gc-line {
  background: var(--card-elev); border: 1px solid var(--rule);
  border-radius: 8px; padding: 8px 10px;
  display: flex; flex-direction: column; gap: 2px;
}
.gc-line .label { font-family: "JetBrains Mono", monospace; font-size: 9px; letter-spacing: 0.14em; text-transform: uppercase; color: var(--muted); }
.gc-line .row { display: flex; justify-content: space-between; align-items: baseline; gap: 6px; font-family: "JetBrains Mono", monospace; font-size: 12.5px; color: var(--ink); font-variant-numeric: tabular-nums; }
.gc-line .row .side { color: var(--muted); font-size: 10.5px; }
.gc-line .row .price { font-weight: 500; }
.gc-line .row .imp { color: var(--muted); font-size: 10.5px; margin-left: 4px; }

.gc-foot {
  display: flex; gap: 10px; flex-wrap: wrap;
  color: var(--muted); font-size: 11px;
}
.gc-foot .item { display: inline-flex; align-items: center; gap: 4px; }

/* ===== pro tools calculator ===== */
.calc-wrap {
  background: var(--card); border: 1px solid var(--rule);
  border-radius: 12px; padding: 20px 22px;
  margin-bottom: 16px;
}
.calc-wrap h3 { margin: 0 0 12px; }
.calc-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 10px; }

/* badges & tags */
.tag {
  display: inline-block; padding: 2px 8px; border-radius: 4px;
  font-size: 10px; font-family: "JetBrains Mono", monospace;
  letter-spacing: 0.08em; text-transform: uppercase; font-weight: 500;
}
.tag.model { background: color-mix(in oklab, var(--accent) 20%, transparent); color: var(--accent); }
.tag.line { background: color-mix(in oklab, var(--warn) 20%, transparent); color: var(--warn); }
.tag.sit { background: color-mix(in oklab, var(--good) 20%, transparent); color: var(--good); }
.tag.fade { background: color-mix(in oklab, var(--bad) 20%, transparent); color: var(--bad); }

/* ev highlight */
.ev-plus { color: var(--good); font-weight: 600; }
.ev-minus { color: var(--bad); font-weight: 600; }

/* ledger table */
table.ledger { width: 100%; border-collapse: collapse; font-size: 13px; }
table.ledger th, table.ledger td { padding: 8px 10px; border-bottom: 1px solid var(--rule); text-align: left; vertical-align: middle; }
table.ledger th { color: var(--muted); font-size: 10px; letter-spacing: 0.1em; text-transform: uppercase; font-weight: 500; }
table.ledger td.num { font-family: "JetBrains Mono", monospace; font-variant-numeric: tabular-nums; text-align: right; }
table.ledger tr:hover { background: color-mix(in oklab, var(--rule) 30%, transparent); }

/* buttons – let Streamlit's own buttons harmonize */
.stButton>button, .stDownloadButton>button {
  background: var(--card-elev); color: var(--ink);
  border: 1px solid var(--rule-strong); border-radius: 8px;
  font-family: "Public Sans", sans-serif; font-weight: 500;
  padding: 8px 14px;
}
.stButton>button:hover { border-color: var(--accent); color: var(--accent); background: var(--card); }

/* alerts */
.hint {
  padding: 10px 14px; border-radius: 8px;
  background: color-mix(in oklab, var(--accent) 10%, var(--card));
  border: 1px solid color-mix(in oklab, var(--accent) 30%, var(--rule));
  color: var(--ink-dim); font-size: 13px; margin: 10px 0;
}
.hint.warn {
  background: color-mix(in oklab, var(--warn) 10%, var(--card));
  border-color: color-mix(in oklab, var(--warn) 30%, var(--rule));
}
.hint.err {
  background: color-mix(in oklab, var(--bad) 10%, var(--card));
  border-color: color-mix(in oklab, var(--bad) 30%, var(--rule));
}

/* Clean up Streamlit's sidebar collapsed-state controls (prevents the "double arrow"
   that appears when both the collapse control AND the brand mark land near the top-left). */
/* Modern Streamlit renders the sidebar expand control as data-testid=
   "stExpandSidebarButton" with 0x0 dimensions, so the only way to re-open
   the sidebar is a near-invisible 1px wide click target. Force it to be a
   large floating pill the user can't miss. Also target legacy testids. */
[data-testid="stExpandSidebarButton"],
[data-testid="collapsedControl"],
[data-testid="stSidebarCollapsedControl"],
button[aria-label="Expand sidebar"],
button[aria-label*="idebar" i][kind="headerNoPadding"] {
  display: inline-flex !important;
  align-items: center !important;
  justify-content: center !important;
  gap: 8px !important;
  padding: 10px 18px 10px 14px !important;
  position: fixed !important;
  top: 12px !important;
  left: 12px !important;
  z-index: 2147483647 !important; /* highest safe z-index */
  background: var(--accent) !important;
  border: 1px solid var(--accent) !important;
  border-radius: 999px !important;
  box-shadow: 0 4px 14px rgba(0,0,0,0.45) !important;
  color: var(--accent-ink) !important;
  cursor: pointer !important;
  transition: transform 160ms ease, box-shadow 160ms ease !important;
  min-height: 40px !important;
  min-width: 150px !important;
  height: auto !important;
  width: auto !important;
  opacity: 1 !important;
  visibility: visible !important;
  pointer-events: auto !important;
  overflow: visible !important;
  transform: none !important;
}
[data-testid="stExpandSidebarButton"]:hover,
[data-testid="collapsedControl"]:hover,
[data-testid="stSidebarCollapsedControl"]:hover,
button[aria-label="Expand sidebar"]:hover {
  transform: translateY(-1px) !important;
  box-shadow: 0 6px 18px rgba(0,0,0,0.55) !important;
}
[data-testid="stExpandSidebarButton"] svg,
[data-testid="collapsedControl"] svg,
[data-testid="stSidebarCollapsedControl"] svg,
button[aria-label="Expand sidebar"] svg {
  height: 18px !important;
  width: 18px !important;
  color: var(--accent-ink) !important;
  fill: currentColor !important;
  display: inline-block !important;
}
[data-testid="stExpandSidebarButton"]::after,
[data-testid="collapsedControl"]::after,
[data-testid="stSidebarCollapsedControl"]::after,
button[aria-label="Expand sidebar"]::after {
  content: "Open Settings" !important;
  font-family: "Public Sans", -apple-system, sans-serif !important;
  font-size: 13px !important;
  font-weight: 600 !important;
  letter-spacing: 0.02em !important;
  color: var(--accent-ink) !important;
  pointer-events: none !important;
  white-space: nowrap !important;
}
/* The Streamlit parent wrapper also gets 0x0 - force it to flow normally. */
[data-testid="stExpandSidebarButton"] * {
  display: inline-block !important;
  opacity: 1 !important;
  visibility: visible !important;
}
header[data-testid="stHeader"] { background: transparent; }
/* Hide Streamlit's own deploy promo + decoration bars. */
[data-testid="stDecoration"] { display: none; }
[data-testid="stToolbar"] { display: none; }
button[kind="headerDeploy"] { display: none !important; }
[data-testid="stAppDeployButton"] { display: none !important; }
/* Metric grid: let it fit more cards per row before wrapping. */
.metric-grid { grid-template-columns: repeat(auto-fit, minmax(145px, 1fr)); }
/* Tighten the Streamlit date picker so it doesn't truncate at narrow widths. */
[data-testid="stDateInput"] input { font-size: 13px; }

/* fonts (incl. Material Symbols so Streamlit's icon ligatures resolve to glyphs) */
@import url('https://fonts.googleapis.com/css2?family=Fraunces:ital,wght@0,400;0,500;1,400&family=Public+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&family=Material+Symbols+Rounded:opsz,wght,FILL,GRAD@24,400,0,0&display=swap');
</style>
"""


RESPONSIVE_CSS = r"""
<style>
@media (max-width: 760px) {
  section.main > div.block-container { padding-left: 10px; padding-right: 10px; }
  .brand-bar { flex-direction: column; align-items: flex-start; gap: 6px; }
  .brand .name { font-size: 22px; }
  .metric-grid { grid-template-columns: repeat(2, 1fr); gap: 8px; }
  .metric-card { padding: 10px 12px; }
  .metric-card .value { font-size: 18px; }
  .gc-lines { grid-template-columns: 1fr !important; }
  .gc-matchup { grid-template-columns: 1fr; }
  .gc-matchup .gc-at { grid-column: 1; }
  table.ledger th, table.ledger td { padding: 5px 6px; font-size: 11px; }
  .stTabs [data-baseweb="tab-list"] { overflow-x: auto; scrollbar-width: none; }
  .stTabs [data-baseweb="tab-list"]::-webkit-scrollbar { display: none; }
}
</style>
"""

LIGHT_THEME_CSS = r"""
<style>
:root { color-scheme: light; }
:root {
  --bg: #F6F5F0;
  --surface: #FFFFFF;
  --card: #FFFFFF;
  --card-elev: #FBFAF4;
  --rule: #E3E0D4;
  --rule-strong: #CBC7B6;
  --ink: #1A1F2C;
  --ink-dim: #3F4654;
  --muted: #6B7284;
  --muted-dim: #9099A7;
  --accent: #0E7C7B;
  --accent-ink: #FFFFFF;
  --good: #138A4B;
  --warn: #B37100;
  --bad: #B91C1C;
  --live: #DC2626;
  --chip: #F0EDE1;
}
</style>
"""


def inject(theme: str = "dark"):
    import streamlit as st
    st.markdown(CUSTOM_CSS, unsafe_allow_html=True)
    if (theme or "dark").lower() == "light":
        st.markdown(LIGHT_THEME_CSS, unsafe_allow_html=True)
    st.markdown(RESPONSIVE_CSS, unsafe_allow_html=True)
