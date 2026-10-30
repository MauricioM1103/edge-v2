# Edge v2.0 — sport-agnostic betting dashboard

A premium sportsbook-style dashboard that merges **ESPN** schedules/scores with
**The Odds API** moneylines, spreads, and totals, and layers rigorous
handicapping math on top: implied probability, no-vig fair lines, EV,
Kelly sizing, unit management, line-movement tracking, and CLV.

Three tabs:

1. **Dashboard · Daily Slate** — multi-column grid of today's matchups with
   live consensus lines, line-move indicators, implied probabilities, a quick
   EV scanner, and a one-click pick-logger per game.
2. **Pro Handicapping Tools** — side-by-side American / Decimal / Fractional
   odds converter, True Value EV calculator (soft-green when +EV), Kelly
   bet-sizer (Full / Half / Quarter / Eighth), no-vig fair-line stripper.
3. **Verified Picks Ledger** — JSON-persisted picks, auto-settled against ESPN
   finals; W/L/P record, net units, ROI, average CLV; filter by sport/status;
   edit closing prices to update CLV; delete or void picks.

Supported sports (both APIs cover these): NFL, NBA, MLB, NHL, NCAAF, NCAAB,
WNBA, MLS.

## Quick start

```bash
cd betting-dashboard-v2
pip install -r requirements.txt
streamlit run app.py
```

Then open http://localhost:8501.

## API keys

**ESPN** is free with no key.

**The Odds API** needs a free key (500 requests/mo on the free tier):

- Sign up at https://the-odds-api.com
- Either export an env var:
  ```bash
  export THE_ODDS_API_KEY=xxxxxxxxxxxx     # Linux/macOS
  setx THE_ODDS_API_KEY "xxxxxxxxxxxx"     # Windows (new shells)
  ```
- Or copy `.streamlit/secrets.toml.example` to `.streamlit/secrets.toml`
  and paste your key there.

Without the Odds API key, the dashboard still renders the ESPN schedule (with
scores, live states, broadcasts) — EV / Kelly / line move panels show a soft
"add your key" hint instead of failing.

## Design

- **Bankroll in units, never dollars.** The sidebar sets `bankroll_units` and
  `unit_pct`. All pick sizes land in `0.5u – 3u` increments.
- **EV formula** `(p_win × profit) − (p_lose × stake)`. Both $ and % variants.
- **Kelly fraction** `(b·p − q) / b` with `b = decimal_odds − 1`. Full, Half,
  Quarter, Eighth multipliers.
- **CLV** computed on implied probabilities so pricing conventions don't skew it.
- **Line movement** compares opening to current implied probability — the app
  tags each move as `shortening` / `lengthening` / `flat`.
- **Methodology tags** on every pick: `Model Edge`, `Line Move`, `Situational`,
  `Fade the Public`.
- **Resilience** — every API call is wrapped in try/except. Missing fields,
  timeouts, rate limits, empty responses all render a clean "no data" state
  rather than crashing.

## Files

```
betting-dashboard-v2/
├── app.py                     # Streamlit entrypoint, sidebar, 3 tabs
├── requirements.txt
├── README.md
├── picks.json                 # auto-created local picks store
├── .streamlit/
│   ├── config.toml            # dark theme defaults
│   └── secrets.toml.example   # template for API key
├── apis/
│   ├── espn.py                # ESPN scoreboard client (no key)
│   ├── odds.py                # The Odds API v4 client
│   └── team_mapping.py        # name normalization across both APIs
├── calc/
│   ├── odds.py                # American/Decimal/Fractional + implied prob + no-vig
│   ├── ev.py                  # EV ($/%, edge vs market)
│   ├── kelly.py               # Kelly fraction & stake with multiplier
│   ├── units.py               # 0.5u-3u recommendation from EV%
│   └── clv.py                 # Line movement & CLV
├── storage/
│   └── picks.py               # JSON ledger + ESPN auto-settlement
└── ui/
    ├── style.py               # Premium dark-sportsbook CSS
    ├── components.py          # Reusable widgets (game card, metric row)
    └── pages.py               # Dashboard / Pro Tools / Ledger renderers
```

## Caching

`st.cache_data` wraps ESPN and Odds API calls (TTL 2–3 minutes) to stay
inside the free-tier request budget. The sidebar has a **Clear cache &
reload** button for forcing a fresh pull.

## Picks ledger

Stored in `betting-dashboard-v2/picks.json` as a plain JSON list — human
readable, easy to back up, portable to any later tooling. The ledger
**auto-settles** on the ESPN event ID attached at log time:

- `h2h`     — settles by winner.
- `spreads` — settles by `(score_diff + point)` sign.
- `totals`  — settles by `(home + away) vs. point`.

Edit closing prices directly in the ledger to populate CLV on past picks.

## Credits

- Original MLB Elo+SP model lives in the parent folder (`mlb_model.py`,
  `mlb_ui.py`). Those are preserved untouched and can still be run as-is.
  The v2.0 app is sport-agnostic and sources team/game data from ESPN
  instead of MLB-only statsapi.
