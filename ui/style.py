"""Modern financial-dashboard aesthetic.

Inspiration: Bloomberg Terminal's density + Linear's airiness. Dark-first,
cool-neutral grays, a single confident accent (sky blue), semantic greens and
reds for P&L. Everything key is still monospace + tabular — numbers must line
up like a trading screen.
"""

CUSTOM_CSS = r"""
<style>
:root {
  /* Court Side v2.1 — deep navy canvas, electric emerald accent, magenta for live.
     Reads as a premium sports analytics product; distinct from the red/green
     semantics used for P&L and win/loss. */
  --bg: #0A1220;
  --surface: #0F1A2E;
  --card: #131F35;
  --card-elev: #192842;
  --rule: #1E2E4C;
  --rule-strong: #2B4068;
  /* Text ladder */
  --ink: #F1F6FF;
  --ink-dim: #C6D1E4;
  --muted: #7A8CAA;
  --muted-dim: #4F5D7A;
  /* Emerald accent — electric, pops on navy without the "neon crypto" vibe. */
  --accent: #10E0A1;
  --accent-ink: #06180F;
  /* Semantic */
  --good: #10E0A1;
  --warn: #FFB84D;
  --bad: #FF4477;
  --info: #5AB7FF;
  --live: #FF3D7F;
  --chip: #1A2745;
  --card-shadow: 0 1px 2px rgba(0,0,0,.4), 0 18px 40px rgba(6,16,32,.45);
}

@media (prefers-color-scheme: light) {
  :root:not([data-theme="dark"]) {
    --bg: #F8FAFC;
    --surface: #FFFFFF;
    --card: #FFFFFF;
    --card-elev: #F1F5F9;
    --rule: #E2E8F0;
    --rule-strong: #CBD5E1;
    --ink: #0F172A;
    --ink-dim: #334155;
    --muted: #64748B;
    --muted-dim: #94A3B8;
    --accent: #0A8F6A;
    --accent-ink: #FFFFFF;
    --good: #0A8F6A;
    --warn: #B45309;
    --bad: #B91C1C;
    --info: #0369A1;
    --live: #BE123C;
    --chip: #F1F5F9;
    --card-shadow: 0 1px 2px rgba(0,0,0,.04), 0 2px 10px rgba(15,23,42,.06);
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

/* tabs — pill-shaped, modern, no faux-browser underlines */
.stTabs [data-baseweb="tab-list"] {
  gap: 4px; border-bottom: 1px solid var(--rule);
  margin-bottom: 22px; padding-bottom: 2px;
}
.stTabs [data-baseweb="tab"] {
  padding: 9px 16px; background: transparent;
  color: var(--muted); font-weight: 500;
  border-radius: 8px;
  border: 1px solid transparent;
  font-family: "Public Sans", -apple-system, sans-serif;
  font-size: 13.5px; letter-spacing: 0.005em;
  transition: color 160ms ease, background 160ms ease;
}
.stTabs [data-baseweb="tab"]:hover { color: var(--ink-dim); }
.stTabs [aria-selected="true"] {
  color: var(--ink); font-weight: 600;
  background: var(--card);
  border-color: var(--rule);
  box-shadow: inset 0 -2px 0 var(--accent);
}

/* ===== brand header ===== */
.brand-bar {
  display: flex; align-items: baseline; justify-content: space-between;
  gap: 16px; margin: 0 0 18px;
  padding-bottom: 14px; border-bottom: 1px solid var(--rule);
}
.brand {
  display: flex; align-items: baseline; gap: 12px;
}
.brand .dot {
  width: 10px; height: 10px; border-radius: 50%;
  background: var(--accent); display: inline-block;
  box-shadow: 0 0 12px color-mix(in oklab, var(--accent) 50%, transparent);
}
.brand .name {
  font-family: "Fraunces", Georgia, serif !important;
  font-style: italic; font-size: 30px; font-weight: 400;
  color: var(--ink); letter-spacing: -0.015em;
}
.brand .ver {
  font-family: "JetBrains Mono", monospace;
  font-size: 10px; letter-spacing: 0.14em;
  color: var(--muted); padding: 3px 10px;
  border: 1px solid var(--rule); border-radius: 999px;
  text-transform: uppercase;
  background: var(--chip);
}
.brand-right {
  font-family: "JetBrains Mono", monospace;
  font-size: 11px; letter-spacing: 0.1em;
  color: var(--muted); text-transform: uppercase;
}

/* ===== metric row ===== */
.metric-grid {
  display: grid; gap: 12px;
  grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
  margin: 10px 0 22px;
}
.metric-card {
  background: var(--card); border: 1px solid var(--rule);
  border-radius: 12px; padding: 14px 16px;
  box-shadow: var(--card-shadow);
  transition: border-color 160ms ease, transform 160ms ease;
}
.metric-card:hover { border-color: var(--rule-strong); }
.metric-card .label {
  font-family: "JetBrains Mono", monospace;
  font-size: 10px; letter-spacing: 0.14em;
  text-transform: uppercase; color: var(--muted);
  margin-bottom: 6px;
}
.metric-card .value {
  font-family: "JetBrains Mono", monospace;
  font-size: 24px; font-weight: 500; color: var(--ink);
  font-variant-numeric: tabular-nums; letter-spacing: -0.01em;
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

/* ledger table — modern financial grid */
table.ledger {
  width: 100%; border-collapse: collapse; font-size: 13px;
  background: var(--card); border-radius: 10px; overflow: hidden;
  box-shadow: var(--card-shadow);
}
table.ledger th, table.ledger td {
  padding: 10px 12px; border-bottom: 1px solid var(--rule);
  text-align: left; vertical-align: middle;
}
table.ledger thead th {
  color: var(--muted); font-size: 10px; letter-spacing: 0.12em;
  text-transform: uppercase; font-weight: 600;
  background: color-mix(in oklab, var(--card-elev) 60%, var(--card));
  border-bottom: 1px solid var(--rule-strong);
}
table.ledger td.num {
  font-family: "JetBrains Mono", monospace;
  font-variant-numeric: tabular-nums; text-align: right;
}
table.ledger tbody tr {
  transition: background 120ms ease;
}
table.ledger tbody tr:hover {
  background: color-mix(in oklab, var(--accent) 6%, transparent);
}
table.ledger tbody tr:last-child td { border-bottom: none; }

/* buttons — matched to new chrome */
.stButton>button, .stDownloadButton>button {
  background: var(--card-elev); color: var(--ink);
  border: 1px solid var(--rule); border-radius: 8px;
  font-family: "Public Sans", sans-serif; font-weight: 500;
  padding: 8px 14px;
  transition: background 160ms ease, border-color 160ms ease, color 160ms ease;
}
.stButton>button:hover, .stDownloadButton>button:hover {
  border-color: var(--accent); color: var(--accent);
  background: color-mix(in oklab, var(--accent) 8%, var(--card));
}
.stButton>button[kind="primary"] {
  background: var(--accent); color: var(--accent-ink);
  border-color: var(--accent); font-weight: 600;
}
.stButton>button[kind="primary"]:hover {
  background: color-mix(in oklab, var(--accent) 85%, white);
  color: var(--accent-ink);
}

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
  content: "Open Settings/Tools" !important;
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
/* `display: contents` makes the toolbar's own box disappear while keeping its
   children in flow - this is critical because the Expand-sidebar pill lives
   inside the toolbar, and `display: none` on the parent would kill the pill
   even though it's position:fixed. Individual toolbar items we don't want
   (Deploy button, decoration bar) are hidden by name below. */
[data-testid="stToolbar"] { display: contents !important; }
button[kind="headerDeploy"] { display: none !important; }
[data-testid="stAppDeployButton"] { display: none !important; }
/* Metric grid: let it fit more cards per row before wrapping. */
.metric-grid { grid-template-columns: repeat(auto-fit, minmax(145px, 1fr)); }
/* Tighten the Streamlit date picker so it doesn't truncate at narrow widths. */
[data-testid="stDateInput"] input { font-size: 13px; }

/* =============================================================
   Breaking News hero (bento grid with player portraits + ticker)
   ============================================================= */
.news-head{
  display:flex; justify-content:space-between; align-items:center;
  margin: 2px 0 12px; gap:12px; flex-wrap:wrap;
}
.news-head .news-title{display:flex; align-items:center; gap:12px}
.news-head .news-title h2{
  font-family:"Fraunces", serif; font-weight:500; font-size:24px;
  margin:0; letter-spacing:-0.015em;
}
.news-head .news-title .count-pill{
  font-family:"JetBrains Mono", monospace; font-size:10.5px;
  letter-spacing:0.16em; text-transform:uppercase;
  color: var(--accent); background: color-mix(in oklab, var(--accent) 12%, transparent);
  border:1px solid color-mix(in oklab, var(--accent) 40%, transparent);
  padding:4px 10px; border-radius:999px;
  display:inline-flex; align-items:center; gap:7px;
}
.news-head .news-title .count-pill .cdot{
  width:6px; height:6px; border-radius:50%;
  background: var(--accent); box-shadow: 0 0 8px var(--accent);
}
.news-head .news-filters{display:flex; gap:6px; flex-wrap:wrap;}
.news-head .chip-filter{
  font-family:"JetBrains Mono", monospace; font-size:10.5px;
  letter-spacing:0.08em; text-transform:uppercase;
  color: var(--muted); background: var(--chip);
  border:1px solid var(--rule); border-radius:8px;
  padding:6px 11px; transition: all 180ms;
  display:inline-block; white-space:nowrap;
}
.news-head .chip-filter.active{
  color: var(--accent-ink); background: var(--accent);
  border-color: var(--accent); font-weight:600;
}

/* Board header (The Board · Full Slate) */
.board-hero-head{
  display:flex; justify-content:space-between; align-items:center;
  margin: 24px 0 14px; gap:12px; flex-wrap:wrap;
}
.board-hero-head .board-title{display:flex; align-items:center; gap:12px}
.board-hero-head .board-title h2{
  font-family:"Fraunces", serif; font-weight:500; font-size:24px;
  margin:0; letter-spacing:-0.015em;
}
.board-hero-head .board-title .board-count{
  font-family:"JetBrains Mono", monospace; font-size:10.5px;
  letter-spacing:0.16em; text-transform:uppercase;
  color: var(--ink-dim); background: var(--chip);
  border:1px solid var(--rule-strong);
  padding:4px 10px; border-radius:999px;
}
.board-hero-head .board-title .board-count b{color: var(--accent); font-weight:600;}
.board-hero-head .sort-select{
  font-family:"JetBrains Mono", monospace; font-size:11px;
  letter-spacing:0.06em; color: var(--ink-dim); background: var(--card);
  border:1px solid var(--rule); border-radius:8px;
  padding:7px 32px 7px 11px; cursor:pointer; appearance:none;
  background-image:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='10' height='6' viewBox='0 0 10 6'><path d='M1 1l4 4 4-4' stroke='%2310E0A1' stroke-width='1.5' fill='none' stroke-linecap='round'/></svg>");
  background-repeat:no-repeat; background-position:right 11px center;
  min-width:220px;
}
.bento{
  display:grid; gap:12px;
  grid-template-columns: repeat(6, 1fr);
  grid-auto-rows: minmax(130px, auto);
  margin-bottom: 14px;
}
.news-tile{
  position:relative; overflow:hidden;
  border-radius:14px; border:1px solid var(--rule);
  box-shadow: var(--card-shadow);
  transition: border-color 200ms, transform 200ms;
  min-height:180px;
  isolation:isolate;
  background: linear-gradient(135deg, var(--c1, #131F35) 0%, var(--c2, #0a1220) 100%);
}
.news-tile:hover{border-color: var(--rule-strong); transform: translateY(-2px);}
.news-tile .portrait{
  position:absolute; inset:0 0 0 auto;
  width:95%; max-width:620px;
  background-size: contain; background-position: top right;
  background-repeat: no-repeat; z-index:0;
  -webkit-mask-image: linear-gradient(90deg, transparent 0%, rgba(0,0,0,.4) 8%, #000 18%, #000 100%);
          mask-image: linear-gradient(90deg, transparent 0%, rgba(0,0,0,.4) 8%, #000 18%, #000 100%);
}
.news-tile.featured{grid-column: span 3; grid-row: span 2; min-height:380px}
.news-tile.featured .portrait{
  width:80%; max-width:900px;
  background-position: top right; background-size: contain;
}
.news-tile.mid{grid-column: span 2; min-height:220px}
.news-tile.wide{grid-column: span 2; min-height:220px}
.news-tile .scrim-left{
  position:absolute; inset:0; z-index:0;
  background: linear-gradient(90deg,
    color-mix(in oklab, var(--c1, #0a1220) 92%, transparent) 0%,
    color-mix(in oklab, var(--c1, #0a1220) 80%, transparent) 20%,
    color-mix(in oklab, var(--c1, #0a1220) 50%, transparent) 40%,
    transparent 70%);
}
.news-tile.featured .scrim-left{
  background: linear-gradient(90deg,
    color-mix(in oklab, var(--c1, #0a1220) 92%, transparent) 0%,
    color-mix(in oklab, var(--c1, #0a1220) 82%, transparent) 25%,
    color-mix(in oklab, var(--c1, #0a1220) 55%, transparent) 50%,
    transparent 80%);
}
.news-tile .scrim-bottom{
  position:absolute; left:0; right:0; bottom:0; height:55%; z-index:0;
  background: linear-gradient(0deg, rgba(5,10,20,.65) 0%, transparent 100%);
}
.news-tile .accent-bar{
  position:absolute; left:0; top:0; bottom:0; width:3px;
  background: var(--team-color, var(--rule-strong)); z-index:2;
}
.news-tile .team-logos{
  position:absolute; right:12px; top:50%;
  transform: translateY(-50%);
  display:flex; align-items:center; gap:10px;
  z-index:0; opacity:.95;
  filter: drop-shadow(0 4px 14px rgba(0,0,0,.5));
}
.news-tile .team-logos img{width:92px; height:92px; object-fit:contain;}
.news-tile .team-logos .vs{
  font-family:"Fraunces", serif; font-style:italic;
  font-size:22px; color: rgba(255,255,255,.4);
}
.news-tile .body{
  position:relative; z-index:1;
  padding:16px 18px;
  display:flex; flex-direction:column; gap:10px;
  height:100%;
}
.news-tile.featured .body{padding:20px 22px}
.news-tile .head{display:flex; justify-content:space-between; align-items:flex-start; gap:10px;}
.news-tile .sev{
  display:inline-flex; align-items:center; gap:7px;
  padding:4px 10px; border-radius:999px;
  font-family:"JetBrains Mono", monospace; font-size:9.5px;
  letter-spacing:0.18em; text-transform:uppercase; font-weight:600;
  white-space:nowrap; backdrop-filter: blur(8px);
}
.news-tile .sev.out{background: rgba(255,68,119,.2); color:#ff8aad; border:1px solid rgba(255,68,119,.5);}
.news-tile .sev.gtd{background: rgba(255,184,77,.2); color:#ffd08a; border:1px solid rgba(255,184,77,.5);}
.news-tile .sev.weather{background: rgba(90,183,255,.2); color:#a8d4ff; border:1px solid rgba(90,183,255,.5);}
.news-tile .sev.line{background: rgba(16,224,161,.2); color:#67f0c7; border:1px solid rgba(16,224,161,.5);}
.news-tile .sev.news{background: rgba(255,255,255,.12); color: var(--ink-dim); border:1px solid rgba(255,255,255,.2);}
.news-tile .ts{
  font-family:"JetBrains Mono", monospace; font-size:10px;
  color:#cbd6ea; letter-spacing:0.08em;
  background: rgba(10,18,32,.55); padding:2px 8px; border-radius:4px;
  backdrop-filter: blur(8px);
}
.news-tile .text-col{max-width:52%; display:flex; flex-direction:column; gap:10px; flex:1;}
.news-tile.featured .text-col{max-width:50%}
.news-tile h3{
  font-family:"Fraunces", serif; font-weight:500;
  letter-spacing:-0.01em; color:#fff;
  margin:0; line-height:1.18;
  text-shadow: 0 2px 12px rgba(0,0,0,.6);
}
.news-tile.featured h3{font-size:28px; line-height:1.1}
.news-tile.mid h3, .news-tile.wide h3{font-size:16.5px}
.news-tile .snippet{color:#dbe3f1; font-size:13px; line-height:1.5; text-shadow: 0 1px 6px rgba(0,0,0,.5);}
.news-tile .foot{
  margin-top:auto; display:flex; gap:12px; flex-wrap:wrap;
  padding-top:8px; border-top:1px solid rgba(255,255,255,.08);
  font-family:"JetBrains Mono", monospace; font-size:10.5px;
  color:#aab7d2; letter-spacing:0.06em;
}
.news-tile .foot a{color: var(--accent); text-decoration:none;}

/* Line Moves ticker */
.news-tile.ticker{
  grid-column: span 6; min-height:68px;
  background:
    linear-gradient(90deg,
      rgba(16,224,161,.14) 0%,
      rgba(16,224,161,.04) 12%,
      rgba(10,18,32,.6) 50%,
      rgba(16,224,161,.04) 88%,
      rgba(16,224,161,.14) 100%),
    var(--card-elev);
  border-color: rgba(16,224,161,.45);
  box-shadow:
    0 0 0 1px rgba(16,224,161,.25),
    0 0 24px rgba(16,224,161,.18),
    0 10px 30px rgba(0,0,0,.4);
  position:relative; overflow:hidden;
  display:flex; flex-direction:row; align-items:center; gap:18px;
  padding:14px 24px;
}
.news-tile.ticker::before, .news-tile.ticker::after{
  content:""; position:absolute; left:0; right:0; height:2px;
  background: linear-gradient(90deg, transparent, var(--accent), transparent);
  box-shadow: 0 0 12px var(--accent);
  animation: tickerBeam 4s ease-in-out infinite;
}
.news-tile.ticker::before{top:0}
.news-tile.ticker::after{bottom:0; animation-direction: reverse;}
@keyframes tickerBeam{0%,100%{opacity:.5} 50%{opacity:1}}
.news-tile.ticker .portrait, .news-tile.ticker .scrim-left, .news-tile.ticker .scrim-bottom{display:none}
.ticker-label{
  display:inline-flex; align-items:center; gap:8px;
  font-family:"JetBrains Mono", monospace; font-size:11.5px;
  letter-spacing:0.26em; text-transform:uppercase;
  color: var(--accent-ink); background: var(--accent);
  padding:7px 14px 7px 12px; border-radius:6px;
  white-space:nowrap; font-weight:700;
  box-shadow: 0 0 18px rgba(16,224,161,.55), 0 4px 14px rgba(0,0,0,.4);
}
.ticker-label .bolt{display:inline-block; font-size:14px; animation: bolt 1.4s ease-in-out infinite;}
@keyframes bolt{0%,100%{opacity:1; transform:scale(1)} 50%{opacity:.4; transform:scale(.8)}}
.ticker-track{
  overflow:hidden; flex:1; white-space:nowrap;
  mask-image: linear-gradient(90deg, transparent 0, #000 40px, #000 calc(100% - 40px), transparent 100%);
}
.ticker-run{
  display:inline-flex; gap:0; animation: tick 42s linear infinite;
  padding-left:100%;
  font-family:"JetBrains Mono", monospace; font-size:14.5px; color: var(--ink);
  letter-spacing:0.02em; font-weight:500;
}
.ticker-run .item{display:inline-flex; align-items:center; gap:10px; padding:0 22px; position:relative;}
.ticker-run .item + .item::before{
  content:""; position:absolute; left:0; top:50%;
  transform: translateY(-50%);
  width:1px; height:18px; background: rgba(16,224,161,.35);
}
.ticker-run .matchup{color:#fff; font-weight:600; letter-spacing:0.06em}
.ticker-run .up{color: var(--good); font-weight:700; text-shadow: 0 0 10px rgba(16,224,161,.5);}
.ticker-run .dn{color: var(--bad); font-weight:700; text-shadow: 0 0 10px rgba(255,68,119,.5);}
.ticker-run .pm{color: var(--muted)}
@keyframes tick{from{transform:translateX(0)} to{transform:translateX(-50%)}}
.news-empty{
  padding: 18px 20px; border:1px dashed var(--rule-strong);
  border-radius:12px; color: var(--muted);
  font-size:13px; text-align:center; margin-bottom:14px;
}

/* bottom snapshot strip (demoted bankroll/slate/picks/exposure) */
.snap-strip{
  display:grid; grid-template-columns: repeat(4, 1fr); gap:10px;
  margin: 20px 0 10px;
}
.snap-strip .cell{
  background: var(--card); border:1px solid var(--rule);
  border-radius:12px; padding:12px 15px;
  display:flex; justify-content:space-between; align-items:baseline;
}
.snap-strip .k{font-family:"JetBrains Mono", monospace; font-size:10px; letter-spacing:0.14em; text-transform:uppercase; color: var(--muted);}
.snap-strip .v{font-family:"JetBrains Mono", monospace; font-size:16px; color: var(--ink); font-variant-numeric: tabular-nums; letter-spacing:-0.01em;}
.snap-strip .v.accent{color: var(--accent)}
.snap-strip .v.good{color: var(--good)}

/* ============================================================
   Sidebar nav header (added when tabs moved into the sidebar)
   ============================================================ */
.nav-head .nav-kicker{
  font-family:"JetBrains Mono", monospace; font-size:10px;
  letter-spacing:0.22em; text-transform:uppercase; color:var(--accent);
}
.nav-head h3{
  font-family:"Fraunces", serif; font-weight:500;
  letter-spacing:-0.015em; font-size:19px; color:var(--ink);
}
/* Streamlit's radio becomes a vertical nav list — larger hit targets on mobile. */
[data-testid="stSidebar"] [data-testid="stRadio"] label{
  padding: 10px 8px; border-radius: 8px;
  transition: background 150ms, color 150ms;
  margin: 1px 0; font-weight: 500; font-size: 14px;
}
[data-testid="stSidebar"] [data-testid="stRadio"] label:hover{
  background: color-mix(in oklab, var(--accent) 10%, transparent);
  color: var(--accent);
}

/* Section kicker for the filter-above-the-board */
.board-filters-head{margin: 26px 0 6px}
.section-kicker{
  font-family:"JetBrains Mono", monospace; font-size:10.5px;
  letter-spacing:0.22em; text-transform:uppercase; color: var(--muted);
}

/* ============================================================
   The Board — full-slate table, Court Side look
   ============================================================ */
.board-wrap{
  background: var(--card); border:1px solid var(--rule);
  border-radius:14px; overflow:hidden;
  box-shadow: var(--card-shadow);
  margin-top: 6px;
}
table.board-table{
  width:100%; border-collapse: collapse; font-size:13px;
  background: transparent;
}
table.board-table thead th{
  background: var(--card-elev);
  color: var(--muted); font-family:"JetBrains Mono", monospace;
  font-size:10px; letter-spacing:0.14em; text-transform:uppercase; font-weight:600;
  padding:12px 14px; text-align:left;
  border-bottom:1px solid var(--rule-strong);
  white-space:nowrap;
}
table.board-table thead th.num{text-align:right;}
table.board-table tbody td{
  padding:12px 14px; border-bottom:1px solid var(--rule);
  color: var(--ink); vertical-align:middle;
}
table.board-table tbody tr:hover{background: color-mix(in oklab, var(--accent) 5%, transparent);}
table.board-table tbody tr:last-child td{border-bottom:none;}
table.board-table tbody tr.row-divider td{
  background: var(--surface); padding:8px 14px;
  font-family:"JetBrains Mono", monospace; font-size:10px;
  letter-spacing:0.16em; text-transform:uppercase; color: var(--muted);
  border-bottom:1px solid var(--rule-strong);
}
table.board-table .rank-cell{
  font-family:"JetBrains Mono", monospace; color: var(--muted-dim);
  font-size:11px; width:40px;
}
table.board-table .tier-cell{width:52px;}
.tier-badge{
  display:inline-flex; align-items:center; justify-content:center;
  width:22px; height:22px; border-radius:6px;
  font-family:"JetBrains Mono", monospace; font-size:11px; font-weight:700;
  line-height:1;
}
.tier-badge.a{background: rgba(16,224,161,.2); color: var(--accent); border:1px solid rgba(16,224,161,.5);}
.tier-badge.b{background: rgba(90,183,255,.15); color: var(--info); border:1px solid rgba(90,183,255,.4);}
.tier-badge.c{background: rgba(255,184,77,.15); color: var(--warn); border:1px solid rgba(255,184,77,.4);}
.tier-badge.w{background: var(--chip); color: var(--muted); border:1px solid var(--rule-strong);}
.matchup-cell{font-weight:500;}
.matchup-cell .chip-row{display:flex; align-items:center; gap:7px; flex-wrap:wrap;}
.matchup-cell .chip-row .team-chip{
  display:inline-block; padding:2px 7px; border-radius:4px;
  font-family:"JetBrains Mono", monospace; font-size:10px; font-weight:700;
  color:#fff; letter-spacing:0.04em;
  background: var(--team-color, var(--rule-strong));
  box-shadow: 0 2px 6px rgba(0,0,0,.3);
}
.matchup-cell .chip-row .team-chip.light{color:#002D62;}
.matchup-cell .chip-row .at{color: var(--muted); font-family:"Fraunces", serif; font-style:italic; font-size:11px;}
.matchup-cell .time{
  font-family:"JetBrains Mono", monospace; font-size:10.5px;
  color: var(--muted); letter-spacing:0.05em; display:block; margin-top:4px;
}
.side-cell{display:flex; flex-direction:column; gap:2px;}
.side-cell .mkt{font-family:"JetBrains Mono", monospace; font-size:9.5px; letter-spacing:0.14em; text-transform:uppercase; color: var(--muted);}
.side-cell .pick{font-weight:600; color: var(--ink); font-size:13px;}
.book-cell{font-family:"JetBrains Mono", monospace; font-size:11.5px; color: var(--ink-dim); white-space:nowrap;}
.book-cell .n{color: var(--accent); font-weight:600;}
td.num-cell{font-family:"JetBrains Mono", monospace; font-size:13px; font-variant-numeric: tabular-nums; text-align:right; letter-spacing:-0.01em;}
td.num-cell.good{color: var(--good); font-weight:600;}
td.num-cell.bad{color: var(--bad);}
td.num-cell.accent{color: var(--accent); font-weight:600;}
td.num-cell.muted{color: var(--muted);}
.stake-cell{display:flex; flex-direction:column; align-items:flex-end; gap:1px;}
.stake-cell .u{font-family:"JetBrains Mono", monospace; font-size:13.5px; color: var(--accent); font-weight:600; font-variant-numeric: tabular-nums;}
.stake-cell .d{font-family:"JetBrains Mono", monospace; font-size:10.5px; color: var(--muted); font-variant-numeric: tabular-nums;}
.stake-cell .u.watch{color: var(--muted);}
.method-tag{
  display:inline-block; padding:3px 9px; border-radius:4px;
  font-family:"JetBrains Mono", monospace; font-size:9.5px;
  letter-spacing:0.1em; text-transform:uppercase; font-weight:600;
}
.method-tag.model{background: rgba(16,224,161,.15); color: var(--accent);}
.method-tag.line{background: rgba(255,184,77,.15); color: var(--warn);}
.method-tag.sit{background: rgba(90,183,255,.15); color: var(--info);}
.method-tag.fade{background: rgba(255,68,119,.15); color: var(--bad);}
.method-tag.watch{background: var(--chip); color: var(--muted); border:1px solid var(--rule-strong);}

/* Board on mobile: horizontal scroll inside the card so the layout holds */
@media (max-width: 900px){
  .board-wrap{overflow-x: auto;}
  table.board-table{min-width: 820px;}
  table.board-table thead th, table.board-table tbody td{padding:9px 10px; font-size:12px;}
}

/* ============================================================
   Mobile-first polish (phones + tall tablets)
   ============================================================ */
@media (max-width: 900px){
  section.main > div.block-container{
    padding-left: 10px; padding-right: 10px;
    padding-top: 0.7rem;
    max-width: 100%;
  }
  .brand-bar{flex-direction: column; align-items: flex-start; gap: 4px;}
  .brand .name{font-size: 24px;}
  .brand-right{font-size: 10px;}
  /* bento stacks to a single column */
  .bento{grid-template-columns: 1fr; grid-auto-rows: auto; gap: 10px;}
  .news-tile.featured, .news-tile.mid, .news-tile.wide, .news-tile.ticker{grid-column: 1;}
  .news-tile{min-height: 160px;}
  .news-tile.featured{min-height: 260px;}
  .news-tile .portrait{width: 55%; max-width: 280px; opacity: .55}
  .news-tile.featured .portrait{width: 60%; max-width: 360px;}
  .news-tile .text-col, .news-tile.featured .text-col{max-width: 70%;}
  .news-tile h3{font-size: 15px;}
  .news-tile.featured h3{font-size: 22px;}
  .news-tile .snippet{font-size: 12.5px;}
  .news-tile .team-logos img{width: 48px; height: 48px;}
  .news-tile .team-logos{right: 8px;}
  .news-tile.ticker{min-height: 54px; padding: 10px 12px;}
  .ticker-label{font-size: 10px; padding: 5px 10px;}
  .ticker-run{font-size: 12.5px;}
  /* Snap strip: two cells per row on phones so nothing truncates */
  .snap-strip{grid-template-columns: repeat(2, 1fr); gap: 8px;}
  .snap-strip .cell{padding: 10px 12px;}
  .snap-strip .v{font-size: 14px;}
  /* Metric grid: two columns, no narrow-column overflow */
  .metric-grid{grid-template-columns: repeat(2, 1fr) !important; gap: 8px;}
  .metric-card{padding: 10px 12px;}
  .metric-card .value{font-size: 16px;}
  /* Game cards collapse the 3-column lines row */
  .gc-lines{grid-template-columns: 1fr !important;}
  .gc-matchup{grid-template-columns: 1fr;}
  .gc-matchup .gc-at{grid-column: 1;}
  /* Ledger tables: smaller padding, horizontal scroll if needed */
  table.ledger{display: block; overflow-x: auto; white-space: nowrap;}
  table.ledger th, table.ledger td{padding: 6px 8px; font-size: 11.5px;}
  /* Tab bars inherit a horizontal scroll fallback */
  .stTabs [data-baseweb="tab-list"]{overflow-x: auto; scrollbar-width: none;}
  .stTabs [data-baseweb="tab-list"]::-webkit-scrollbar{display: none;}
  /* Bigger touch targets everywhere */
  .stButton>button, .stDownloadButton>button{min-height: 42px; font-size: 14px;}
  /* The floating Settings/Tools pill grows to a tap-friendly size */
  [data-testid="stExpandSidebarButton"]{
    min-height: 44px !important;
    min-width: 170px !important;
    top: 8px !important; left: 8px !important;
  }
}
/* Very narrow phones */
@media (max-width: 420px){
  .snap-strip{grid-template-columns: 1fr;}
  .news-tile .portrait{opacity: .4}
  .news-tile .text-col, .news-tile.featured .text-col{max-width: 100%;}
}

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
  --bg: #F8FAFC;
  --surface: #FFFFFF;
  --card: #FFFFFF;
  --card-elev: #F1F5F9;
  --rule: #E2E8F0;
  --rule-strong: #CBD5E1;
  --ink: #0F172A;
  --ink-dim: #334155;
  --muted: #64748B;
  --muted-dim: #94A3B8;
  --accent: #0A8F6A;
  --accent-ink: #FFFFFF;
  --good: #0A8F6A;
  --warn: #B45309;
  --bad: #B91C1C;
  --info: #0369A1;
  --live: #BE123C;
  --chip: #F1F5F9;
  --card-shadow: 0 1px 2px rgba(0,0,0,.04), 0 2px 10px rgba(15,23,42,.06);
}
</style>
"""


def inject(theme: str = "dark"):
    import streamlit as st
    st.markdown(CUSTOM_CSS, unsafe_allow_html=True)
    if (theme or "dark").lower() == "light":
        st.markdown(LIGHT_THEME_CSS, unsafe_allow_html=True)
    st.markdown(RESPONSIVE_CSS, unsafe_allow_html=True)
