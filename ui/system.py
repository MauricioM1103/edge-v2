"""Design-system layer: tokens and the shared components every tab uses.

Loaded last (after style.py, the responsive rules and layout_c.py) so it is
the single place that decides how a KPI strip, a section header, a note, an
expander, a button, an input and a data table look. Page code should use
these classes and the tokens below rather than inline one-off values.

Direction: a quant desk screen. Hairline borders instead of shadows, one
accent (green) reserved for positive edge / actionable / the primary
action, monospace tabular numbers, small quiet labels.
"""

SYSTEM_CSS = r"""
<style>
:root{
  /* spacing, radius and type scales */
  --sp-1:4px; --sp-2:8px; --sp-3:12px; --sp-4:16px; --sp-5:24px;
  --r-sm:6px; --r-md:10px;
  --fs-xs:10.5px; --fs-sm:12px; --fs-md:13.5px; --fs-lg:16px; --fs-xl:20px;
  --hair:1px solid var(--rule);
  /* flat surfaces: depth comes from the hairline, not a drop shadow */
  --card-shadow:none;
}

/* ---------- page frame ---------- */
section.main > div.block-container, [data-testid="stMainBlockContainer"]{ max-width:1440px; }
.metric-card, .board-wrap, .calc-wrap, .rail-card, table.ledger, .game-card{ box-shadow:none !important; }

/* ---------- section headers ----------
   Markdown headings were rendering at browser-default sizes (28px bold),
   louder than the data under them. A section header is a small label on a
   hairline. */
[data-testid="stMarkdownContainer"] h1{ font-size:var(--fs-xl) !important; font-weight:650 !important; letter-spacing:-.02em; margin:0 0 var(--sp-2) !important; padding:0 !important; }
[data-testid="stMarkdownContainer"] h2,
[data-testid="stMarkdownContainer"] h3{
  font-size:13px !important; font-weight:650 !important; letter-spacing:.01em !important; line-height:1.3 !important;
  color:var(--ink) !important; margin:var(--sp-5) 0 var(--sp-2) !important; padding:var(--sp-3) 0 0 !important;
  border-top:var(--hair);
}
[data-testid="stMarkdownContainer"] h4{ font-size:var(--fs-sm) !important; font-weight:650 !important; color:var(--ink-dim) !important; margin:var(--sp-4) 0 var(--sp-1) !important; padding:0 !important; }
[data-testid="stMarkdownContainer"] h2 + p, [data-testid="stMarkdownContainer"] h3 + p{ margin-top:0; }
/* headings that live inside a custom component keep that component's look */
.calc-wrap h3, .rail-card h3, .news-head h3, .board-hero-head h3, [data-testid="stSidebar"] h3,
[data-testid="stSidebar"] h4{ border-top:0 !important; padding-top:0 !important; margin-top:0 !important; }
[data-testid="stSidebar"] [data-testid="stMarkdownContainer"] h4{
  font-size:var(--fs-xs) !important; letter-spacing:.09em !important; text-transform:uppercase; color:var(--muted) !important; margin:var(--sp-4) 0 var(--sp-1) !important;
}
[data-testid="stCaptionContainer"], .stCaption{ font-size:var(--fs-sm) !important; color:var(--muted) !important; }

/* ---------- KPI strip (every tab) ----------
   .metric-grid used to be a row of tall separate cards. It is now one
   joined strip of cells divided by hairlines, the same as the Board's. */
.metric-grid{
  display:grid !important; grid-template-columns:repeat(auto-fit, minmax(148px, 1fr)) !important; gap:0 !important;
  border:var(--hair); border-radius:var(--r-md); overflow:hidden; background:var(--surface); margin:var(--sp-1) 0 var(--sp-3);
}
.metric-card{
  background:var(--surface) !important; border:0 !important; border-radius:0 !important;
  padding:10px 16px 11px !important; min-width:0; box-shadow:none !important;
  /* hairlines between cells: each cell draws its left and top edge and is
     pulled 1px up and left, so the outer two are clipped by the strip. */
  border-left:1px solid var(--rule) !important; border-top:1px solid var(--rule) !important;
  margin:-1px 0 0 -1px !important;
}
.metric-card:hover{ background:var(--card) !important; }
.metric-card .label{ font-family:inherit !important; font-size:10px !important; letter-spacing:.09em !important; text-transform:uppercase; color:var(--muted) !important; font-weight:600 !important; margin:0 0 3px !important; }
.metric-card .value{ font-family:"Geist Mono", monospace !important; font-variant-numeric:tabular-nums; font-size:17px !important; font-weight:600 !important; letter-spacing:-.02em; line-height:1.25 !important; }
.metric-card .foot{ font-size:10.5px !important; margin-top:2px !important; color:var(--muted) !important; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }

/* ---------- notes ----------
   Informational text is a quiet line with a 2px rule, not a tinted box.
   Tinted boxes of equal weight stacked three deep buried the data. */
.hint{
  background:transparent !important; border:0 !important; border-left:2px solid var(--rule-strong) !important; border-radius:0 !important;
  padding:3px 0 3px 12px !important; margin:var(--sp-2) 0 !important;
  color:var(--ink-dim) !important; font-size:12.5px !important; line-height:1.55 !important;
}
.hint strong{ color:var(--ink); font-weight:600; }
.hint.warn{ border-left-color:var(--warn) !important; }
.hint.err{ border-left-color:var(--bad) !important; }
.auto-log-caption{ background:transparent !important; border:0 !important; padding:2px 0 !important; margin:var(--sp-2) 0 0 !important; font-size:10.5px !important; letter-spacing:.08em !important; color:var(--muted) !important; }
.auto-log-caption .check{ color:var(--good); }
[data-testid="stAlert"]{ background:var(--surface) !important; border:var(--hair) !important; border-radius:var(--r-sm) !important; padding:8px 12px !important; }
[data-testid="stAlert"] p{ font-size:12.5px !important; color:var(--ink-dim) !important; }
[data-testid="stAlert"] [data-testid="stAlertContentSuccess"] p{ color:var(--good) !important; }
[data-testid="stAlert"] [data-testid="stAlertContentError"] p{ color:var(--bad) !important; }
[data-testid="stAlert"] [data-testid="stAlertContentWarning"] p{ color:var(--warn) !important; }

/* ---------- drawers ---------- */
[data-testid="stExpander"] details{ border:var(--hair) !important; border-radius:var(--r-sm) !important; background:var(--card) !important; box-shadow:none !important; }
[data-testid="stExpander"] summary{ padding:9px 12px !important; min-height:0 !important; }
[data-testid="stExpander"] summary p{ font-size:13px !important; font-weight:600 !important; color:var(--ink) !important; }
[data-testid="stExpander"] summary:hover p{ color:var(--accent) !important; }

/* ---------- controls ---------- */
.stButton > button, .stDownloadButton > button{
  border-radius:var(--r-sm) !important; font-weight:600 !important; font-size:13px !important;
  min-height:36px !important; padding:6px 14px !important; box-shadow:none !important;
}
.stButton > button[kind="secondary"], .stDownloadButton > button{
  background:var(--card) !important; border:1px solid var(--rule-strong) !important; color:var(--ink) !important;
}
.stButton > button[kind="secondary"] p, .stDownloadButton > button p{ color:var(--ink) !important; }
.stButton > button[kind="secondary"]:hover, .stDownloadButton > button:hover{ background:var(--surface) !important; border-color:var(--ink-dim) !important; }
.stButton > button[kind="primary"]{ background:var(--accent) !important; border:1px solid var(--accent) !important; color:var(--accent-ink) !important; }
.stButton > button[kind="primary"] p{ color:var(--accent-ink) !important; }
.stButton > button[kind="primary"]:hover{ filter:brightness(.94); }
[data-baseweb="input"], [data-baseweb="base-input"], [data-baseweb="select"] > div, [data-baseweb="textarea"]{
  background:var(--card) !important; border-color:var(--rule) !important; border-radius:var(--r-sm) !important;
}
[data-baseweb="input"]:focus-within, [data-baseweb="select"] > div:focus-within{ border-color:var(--accent) !important; }
[data-testid="stNumberInput"] input, [data-testid="stTextInput"] input{ font-family:"Geist Mono", monospace !important; font-variant-numeric:tabular-nums; font-size:13px !important; }
[data-testid="stWidgetLabel"] p{ font-size:11.5px !important; font-weight:600 !important; color:var(--ink-dim) !important; }
[data-baseweb="tag"]{ border-radius:5px !important; font-size:11.5px !important; }

/* ---------- data tables ---------- */
table.ledger{ border:var(--hair) !important; border-radius:var(--r-md) !important; overflow:hidden; border-collapse:separate !important; border-spacing:0; width:100%; }
table.ledger thead th{
  font-family:inherit !important; font-size:10px !important; letter-spacing:.08em !important; text-transform:uppercase;
  color:var(--muted) !important; font-weight:600 !important; background:var(--surface) !important;
  padding:8px 12px !important; border-bottom:var(--hair) !important;
}
table.ledger tbody td{ padding:8px 12px !important; font-size:12.5px; border-bottom:var(--hair); }
table.ledger tbody tr:last-child td{ border-bottom:0; }
table.ledger tbody tr:hover td{ background:var(--surface); }
table.ledger td.num, table.ledger td.mono, .num, .mono{ font-variant-numeric:tabular-nums; }
.chip{ border-radius:5px !important; }
.calc-wrap{ border:var(--hair) !important; border-radius:var(--r-md) !important; }

@media (max-width: 899px){
  .metric-grid{ grid-template-columns:repeat(2, minmax(0, 1fr)) !important; }
  .metric-card{ padding:9px 12px !important; }
  .metric-card .value{ font-size:15px !important; }
  [data-testid="stMarkdownContainer"] h2, [data-testid="stMarkdownContainer"] h3{ margin-top:var(--sp-4) !important; }
}
</style>
"""
