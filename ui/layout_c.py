"""Layout C ("Hybrid terminal") styles, layered over the base stylesheet.

Loaded last so it wins the cascade without rewriting the 1,200-line base.
What it changes:
  * one top bar (brand, page tabs, live clock) instead of a brand row plus a
    sidebar page list;
  * a four-number KPI strip under the top bar;
  * the board as the first thing on the page, with a right rail;
  * prediction-market cues on every board row: the price as a button-like
    pill and a bar comparing the book's implied chance with the sharp fair
    chance (borrowed from Kalshi and Polymarket);
  * sans-serif headings throughout (the italic serif read as editorial,
    not as a trading screen);
  * the cold-start / wake-up loading overlay.
"""

LAYOUT_C_CSS = r"""
<style>
/* ---------- page frame ---------- */
section.main > div.block-container,
[data-testid="stMainBlockContainer"]{ padding-top: 14px !important; max-width: 1440px; }
h1, h2, h3, h4{
  font-family: "Open Sauce One", "Inter", -apple-system, "Segoe UI", sans-serif !important;
  font-style: normal !important; font-weight: 700 !important; letter-spacing: -0.02em;
}
h2{ font-size: 19px; } h3{ font-size: 15.5px; }

[data-testid="stElementContainer"]:has(.edge-css-anchor){ display:none; }

/* ---------- top bar ---------- */
.topbar-brand{ display:flex; align-items:center; gap:9px; }
.topbar-brand .mark{
  width:28px; height:28px; border-radius:8px; background:var(--accent); color:var(--accent-ink);
  display:grid; place-items:center; font-weight:900; font-size:15px;
}
.topbar-brand .name{ font-weight:800; font-size:18px; letter-spacing:-0.03em; color:var(--ink); }
.topbar-brand .name i{ color:var(--accent); font-style:normal; }
.topbar-clock{
  display:flex; align-items:center; justify-content:flex-end; gap:8px;
  font-family:"Geist Mono", monospace; font-size:11px; color:var(--muted); white-space:nowrap;
}
.topbar-clock .date{ color:var(--muted-dim); }
.topbar-clock .pulse-dot{
  width:7px; height:7px; border-radius:50%; background:var(--live); display:inline-block;
  box-shadow:0 0 0 0 color-mix(in oklab, var(--live) 60%, transparent); animation: edgePulse 1.8s ease-out infinite;
}
@keyframes edgePulse{ 70%{ box-shadow:0 0 0 7px transparent; } 100%{ box-shadow:0 0 0 0 transparent; } }
.topbar-rule{ height:1px; background:var(--rule); margin:10px 0 14px; }

/* page tabs (st.segmented_control) */
[data-testid="stButtonGroup"]{
  background:var(--surface); border:1px solid var(--rule); border-radius:12px; padding:3px;
  gap:2px !important; flex-wrap:nowrap !important; overflow-x:auto; scrollbar-width:none; width:fit-content; max-width:100%;
}
[data-testid="stButtonGroup"]::-webkit-scrollbar{ display:none; }
[data-testid="stButtonGroup"] button{
  border:0 !important; background:transparent !important; border-radius:9px !important;
  padding:6px 13px !important; min-height:0 !important; flex:0 0 auto;
  color:var(--muted) !important; font-weight:600 !important; font-size:12.5px !important;
}
[data-testid="stButtonGroup"] button p{ color:inherit !important; font-size:12.5px !important; font-weight:600 !important; }
[data-testid="stButtonGroup"] button:hover{ color:var(--ink) !important; }
[data-testid="stButtonGroup"] button[data-testid*="Active"],
[data-testid="stButtonGroup"] button[aria-checked="true"],
[data-testid="stButtonGroup"] button[aria-pressed="true"]{
  background:var(--card-elev) !important; color:var(--ink) !important;
  box-shadow: inset 0 0 0 1px var(--rule-strong);
}

/* sidebar opener: a quiet floating button, out of the top bar's way */
[data-testid="stExpandSidebarButton"],
[data-testid="collapsedControl"],
[data-testid="stSidebarCollapsedControl"],
button[aria-label="Expand sidebar"],
button[aria-label*="idebar" i][kind="headerNoPadding"]{
  top:auto !important; bottom:16px !important; left:16px !important;
  background:var(--card-elev) !important; border:1px solid var(--rule-strong) !important;
  color:var(--ink) !important; min-width:0 !important; min-height:36px !important;
  padding:8px 14px 8px 10px !important;
}
[data-testid="stExpandSidebarButton"] svg,
[data-testid="collapsedControl"] svg,
[data-testid="stSidebarCollapsedControl"] svg,
button[aria-label="Expand sidebar"] svg{ color:var(--accent) !important; }
[data-testid="stExpandSidebarButton"]::after,
[data-testid="collapsedControl"]::after,
[data-testid="stSidebarCollapsedControl"]::after,
button[aria-label="Expand sidebar"]::after{
  content:"Filters & settings" !important; color:var(--ink) !important; font-size:12px !important;
}

/* ---------- KPI strip ---------- */
.kpi-strip{
  display:grid; grid-template-columns:repeat(4, minmax(0,1fr)); gap:1px;
  background:var(--rule); border:1px solid var(--rule); border-radius:12px; overflow:hidden; margin:0 0 16px;
}
.kpi-strip .kpi{ background:var(--surface); padding:11px 16px; display:flex; flex-direction:column; gap:2px; min-width:0; }
.kpi-strip .k{ font-size:10px; letter-spacing:.09em; text-transform:uppercase; color:var(--muted); font-weight:600; }
.kpi-strip .v{
  font-family:"Geist Mono", monospace; font-variant-numeric:tabular-nums; font-size:17px; font-weight:600;
  color:var(--ink); white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
}
.kpi-strip .v.good{ color:var(--accent); }

/* ---------- board ---------- */
.board-hero-head{ margin:0 0 6px !important; padding-top:0 !important; }
[data-testid="stElementContainer"]:has(#agent-table-top){ position:absolute; height:0; overflow:hidden; }
.board-hero-head .board-title .h1{
  font-family:"Inter", sans-serif !important; font-style:normal !important;
  font-size:18px !important; font-weight:700 !important; letter-spacing:-0.02em;
}
.board-wrap{ border-radius:14px; }
table.board-table thead th{ font-size:10px; letter-spacing:.07em; }
table.board-table tbody tr:not(.row-divider):nth-child(even){ background: color-mix(in oklab, var(--surface) 55%, transparent); }

/* The price lives in the pill now; the book cell names the book only. */
table.board-table .book-cell > .n{ display:none; }
table.board-table .matchup-cell .time{ white-space:nowrap; }
table.board-table .method-tag{ white-space:nowrap; }
table.board-table tbody td{ padding-top:9px; padding-bottom:9px; }

/* price pill + fair-vs-book bar */
table.board-table td.pb-cell{ min-width:150px; }
.pb-cell .px{
  display:inline-block; min-width:62px; text-align:center; padding:4px 10px; border-radius:8px;
  font-family:"Geist Mono", monospace; font-variant-numeric:tabular-nums; font-weight:700; font-size:12.5px;
  background:var(--chip); color:var(--ink-dim); border:1px solid var(--rule);
}
.pb-cell.pos .px{
  background: color-mix(in oklab, var(--accent) 14%, transparent); color:var(--accent);
  border-color: color-mix(in oklab, var(--accent) 38%, transparent);
}
.pb-cell .pb-bar{ display:flex; height:4px; border-radius:2px; overflow:hidden; background:var(--rule); margin:6px 0 3px; }
.pb-cell .pb-bar i{ display:block; background:var(--muted-dim); }
.pb-cell.pos .pb-bar i{ background: color-mix(in oklab, var(--accent) 55%, var(--rule)); }
.pb-cell .pb-bar u{ display:block; background:var(--accent); }
.pb-cell .pb-nums{
  display:flex; justify-content:space-between; gap:8px; white-space:nowrap;
  font-family:"Geist Mono", monospace; font-size:10px; color:var(--muted);
}
.pb-cell .pb-nums .accent{ color:var(--accent); }

/* ---------- right rail ---------- */
.rail-box-head{
  font-size:10.5px; letter-spacing:.09em; text-transform:uppercase; font-weight:700;
  color:var(--accent); margin:2px 0 8px;
}
.rail-note{ font-size:12px; line-height:1.5; color:var(--muted); margin:8px 0 18px; }
.rail-card{
  background:var(--surface); border:1px solid var(--rule); border-radius:12px; padding:12px; margin:8px 0 18px;
}
.rail-card ul{ list-style:none; margin:0; padding:0; }
.rail-card li{
  display:flex; justify-content:space-between; gap:10px; align-items:baseline;
  font-size:12px; padding:5px 0; border-bottom:1px dashed var(--rule);
}
.rail-card li:last-child{ border-bottom:0; }
.rail-card ul.legs{ margin-bottom:8px; padding-bottom:4px; border-bottom:1px solid var(--rule-strong); }
.rail-card ul.legs li span:first-child{ font-weight:600; }
.rail-card .k{ color:var(--muted); }
.rail-card .mono.good{ color:var(--accent); } .rail-card .mono.bad{ color:var(--bad); } .rail-card .mono.warn{ color:var(--warn); }
.rail-card .why{ font-size:11.5px; line-height:1.5; color:var(--muted); margin:10px 0 0; }

/* buttons */
.stButton > button{ border-radius:10px !important; font-weight:650 !important; }
.stButton > button[kind="secondary"]{
  background: color-mix(in oklab, var(--accent) 12%, transparent) !important;
  border:1px solid color-mix(in oklab, var(--accent) 40%, transparent) !important; color:var(--accent) !important;
}
.stButton > button[kind="secondary"] p{ color:var(--accent) !important; }
.stButton > button[kind="secondary"]:hover{ background: color-mix(in oklab, var(--accent) 20%, transparent) !important; }

/* The live-refresh timer is an invisible component; stop it taking a row. */
[data-testid="stElementContainer"]:has(iframe[title*="autorefresh"]){ position:absolute; height:0; overflow:hidden; }

/* news now follows the board; headlines drop the italic serif */
section.bento *, .news-head .news-title .h1{ font-family:"Open Sauce One", "Inter", -apple-system, "Segoe UI", sans-serif !important; font-style:normal !important; }
section.bento .mono, section.bento code{ font-family:"Geist Mono", monospace !important; }
section.bento{ margin-top:14px; }
.news-head{ margin-top:26px; }
.news-head .news-title .h1{ font-size:18px !important; font-weight:700 !important; letter-spacing:-0.02em; }

/* ---------- loading overlay ---------- */
.edge-loader{
  position:fixed; inset:0; z-index:999990; background:var(--bg);
  display:grid; place-items:center; overflow:hidden;
  animation: edgeLoaderOut .5s ease var(--failsafe, 45s) forwards;
}
@keyframes edgeLoaderOut{ to{ opacity:0; visibility:hidden; } }
.edge-loader-glow{
  position:absolute; width:560px; height:560px; border-radius:50%;
  background: radial-gradient(circle, color-mix(in oklab, var(--accent) 16%, transparent), transparent 65%);
  animation: edgeBreathe 3s ease-in-out infinite;
}
.edge-loader-in{ position:relative; width:min(480px, 86vw); text-align:center; }
.edge-loader-mark{
  width:58px; height:58px; margin:0 auto 14px; border-radius:16px; background:var(--accent); color:var(--accent-ink);
  display:grid; place-items:center; font-weight:900; font-size:26px; animation: edgePop 1.6s ease-in-out infinite;
}
.edge-loader-title{ font-size:22px; font-weight:800; letter-spacing:-0.02em; color:var(--ink); }
.edge-loader-sub{ font-size:12px; color:var(--muted); margin:4px 0 16px; }
.edge-loader-track{ height:4px; border-radius:2px; background:var(--rule); overflow:hidden; margin-bottom:8px; }
.edge-loader-track i{
  display:block; height:100%; border-radius:2px; transition: width .5s ease;
  background: linear-gradient(90deg, var(--accent) 0%, color-mix(in oklab, var(--accent) 55%, #fff) 50%, var(--accent) 100%);
  background-size:200% 100%; animation: edgeShim 1.2s linear infinite;
}
.edge-loader-steps{ display:flex; justify-content:space-between; gap:8px; font-size:10.5px; color:var(--muted-dim); margin-bottom:18px; }
.edge-loader-steps .done{ color:var(--accent); }
.edge-loader-steps .now{ color:var(--ink); font-weight:600; }
.edge-loader-sk{
  height:34px; border-radius:9px; margin-bottom:7px;
  background: linear-gradient(90deg, var(--surface) 25%, var(--card-elev) 50%, var(--surface) 75%);
  background-size:200% 100%; animation: edgeShim 1.3s linear infinite;
}
@keyframes edgeShim{ to{ background-position:-200% 0; } }
@keyframes edgePop{ 0%,100%{ transform:scale(1); } 50%{ transform:scale(1.08); } }
@keyframes edgeBreathe{ 0%,100%{ opacity:.6; } 50%{ opacity:1; } }
@media (prefers-reduced-motion: reduce){
  .edge-loader-mark, .edge-loader-glow, .edge-loader-sk, .edge-loader-track i, .topbar-clock .pulse-dot{ animation:none; }
}

/* ---------- phone ---------- */
@media (max-width: 900px){
  .topbar-clock{ display:none; }
  .topbar-rule{ margin:8px 0 12px; }
  [data-testid="stButtonGroup"]{ width:100%; }
  .kpi-strip{ grid-template-columns:repeat(2, minmax(0,1fr)); }
  .kpi-strip .kpi{ padding:9px 12px; }
  .kpi-strip .v{ font-size:15px; }
  .board-hero-head .board-title{ flex-wrap:wrap; gap:8px; }
  .board-hero-head .board-title .board-count{ white-space:nowrap; font-size:9.5px; padding:4px 9px; }
  table.board-table td.pb-cell{ min-width:138px; }
  /* Phone: the sidebar opener shrinks to an icon so it covers less of the board. */
  [data-testid="stExpandSidebarButton"]::after,
  [data-testid="collapsedControl"]::after,
  [data-testid="stSidebarCollapsedControl"]::after,
  button[aria-label="Expand sidebar"]::after{ content:"" !important; }
  [data-testid="stExpandSidebarButton"],
  [data-testid="collapsedControl"],
  [data-testid="stSidebarCollapsedControl"],
  button[aria-label="Expand sidebar"]{ padding:9px 11px !important; bottom:12px !important; left:12px !important; }
}
</style>
"""
