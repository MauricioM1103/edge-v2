"""Check every Kalshi price the Board would show against Kalshi itself.

    python tools/audit_kalshi.py            # all sports with odds fetched in the last 90 minutes
    python tools/audit_kalshi.py nba mls    # just these

For each sport it replays the Board's matching (ESPN pass, then the name pass)
on the odds already in the on-disk cache, runs the Kalshi scanner, and then
looks each priced market up again on Kalshi by its ticker and checks,
independently of the matching code:

  * the market's name is the team the row says it is,
  * the other market in that event names the opponent,
  * the event's date is within a day of the game,
  * the price on the row is the market's current ask (plus fee).

Uses Kalshi's free public API and no Odds API credits.
"""
import collections
import datetime as dt
import glob
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from apis import espn, kalshi  # noqa: E402
from apis import kalshi_match as km  # noqa: E402
from apis.team_mapping import canonical  # noqa: E402
from calc import agent  # noqa: E402

SPORT_OF = {
    "soccer_usa_mls": "mls", "soccer_epl": "epl", "basketball_nba": "nba", "icehockey_nhl": "nhl",
    "basketball_ncaab": "ncaab", "americanfootball_nfl": "nfl", "americanfootball_ncaaf": "ncaaf",
    "soccer_spain_la_liga": "la_liga", "soccer_germany_bundesliga": "bundesliga",
    "soccer_italy_serie_a": "serie_a", "soccer_france_ligue_one": "ligue_1", "soccer_mexico_ligamx": "liga_mx",
    "mma_mixed_martial_arts": "ufc", "boxing_boxing": "boxing",
}
MAX_AGE_MIN = 90


def _words(name):
    return [w for w in canonical(name).split() if w not in {"fc", "sc", "cf", "ac", "st", "state", "the"}]


def _shares_a_word(sub, team):
    """Independent, deliberately crude check: one distinctive word in common
    (or the row's truncated initial matches)."""
    a, b = _words(sub), _words(team)
    if set(a) & set(b):
        return True
    return bool(a and b and len(a[-1]) <= 2 and any(w.startswith(a[-1]) for w in b) and a[0] in b)


def main():
    now = dt.datetime.now(dt.timezone.utc)
    games = collections.defaultdict(dict)
    for f in glob.glob(os.path.join(ROOT, ".odds_cache", "*.json")):
        if (now.timestamp() - os.path.getmtime(f)) / 60 > MAX_AGE_MIN:
            continue
        try:
            d = json.load(open(f, encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for g in (d if isinstance(d, list) else []):
            if isinstance(g, dict) and g.get("sport_key") in SPORT_OF and g.get("bookmakers"):
                games[SPORT_OF[g["sport_key"]]][g["id"]] = dict(g, _sport=SPORT_OF[g["sport_key"]])
    sports = sys.argv[1:] or sorted(games)
    if not sports:
        print(f"no odds fetched in the last {MAX_AGE_MIN} minutes; open the Board first")
        return 1
    total = bad = 0
    for sp in sports:
        gl = [g for g in games.get(sp, {}).values()
              if dt.datetime.fromisoformat(g["commence_time"].replace("Z", "+00:00")) > now]
        ms = kalshi.fetch_sport_markets(sp) or []
        by_game = {}
        days = sorted({dt.datetime.fromisoformat(g["commence_time"].replace("Z", "+00:00"))
                       .astimezone(dt.timezone(dt.timedelta(hours=-6))).strftime("%Y%m%d") for g in gl})[:2]
        for d8 in days:
            try:
                evs = espn.get_scoreboard(sp, date=d8) or []
            except Exception:
                evs = []
            for ev in evs:
                h, a = ev.get("home") or {}, ev.get("away") or {}
                em = km.find_event_markets(ms, ev.get("start_utc"), h.get("abbr") or "", a.get("abbr") or "",
                                           h.get("name"), a.get("name"))
                if em:
                    by_game[(h.get("name"), a.get("name"))] = em
        claimed = {m.get("event_ticker") for v in by_game.values() for m in v}
        by_game.update(km.match_markets_to_games([m for m in ms if m.get("event_ticker") not in claimed], gl))
        rows = agent.scan_kalshi_vs_pinnacle(by_game, gl, 500.0, sport_key=sp, min_edge_pct=-1.0, shop_books=[])
        priced = [r for r in rows if r.get("kalshi_price_cents") is not None]
        problems = []
        by_event = km._group_markets_by_event(ms)
        for r in priced:
            total += 1
            g = next(x for x in gl if x["home_team"] == r["home_team"] and x["away_team"] == r["away_team"])
            live = kalshi.get_market(r["kalshi_ticker"]) or {}
            sub = live.get("yes_sub_title") or ""
            event = live.get("event_ticker") or ""
            issues = []
            if r["side_label"] == "Draw":
                if not km.is_tie_market(live):
                    issues.append(f"row says Draw, market says '{sub}'")
            else:
                team = r["home_team"] if r["picked_side_key"] == "home" else r["away_team"]
                opp = r["away_team"] if r["picked_side_key"] == "home" else r["home_team"]
                if not _shares_a_word(sub, team):
                    issues.append(f"market '{sub}' is not {team}")
                others = [m.get("yes_sub_title") or "" for m in by_event.get(event, [])
                          if m.get("ticker") != r["kalshi_ticker"] and not km.is_tie_market(m)]
                if not any(_shares_a_word(o, opp) for o in others):
                    issues.append(f"opponent {opp} not in event ({others})")
            tdate = km._ticker_date(event)
            gdate = dt.datetime.fromisoformat(g["commence_time"].replace("Z", "+00:00")).date()
            if tdate is None or abs((gdate - tdate).days) > 1:
                issues.append(f"event date {tdate} vs game {gdate}")
            ask = kalshi.market_yes_ask_prob(live)
            if ask is None or abs(round(ask * 100) - r["kalshi_price_cents"]) > 3:
                issues.append(f"row shows {r['kalshi_price_cents']}c, Kalshi ask now {None if ask is None else round(ask * 100)}c")
            if abs(r["fair_prob"] - r["kalshi_implied"]) > agent.KALSHI_MAX_FAIR_GAP + 0.03:
                issues.append(f"Kalshi {r['kalshi_implied'] * 100:.0f}% vs fair {r['fair_prob'] * 100:.0f}%")
            if issues:
                bad += 1
                problems.append(f"   {r['matchup']} - {r['side_label']} [{r['kalshi_ticker']}]: " + "; ".join(issues))
        print(f"{sp:11s} games {len(gl):3d}  kalshi events {len(by_event):3d}  attached {len(by_game):3d}  "
              f"priced rows {len(priced):3d}  problems {len(problems)}")
        for p in problems[:10]:
            print(p)
    print(f"\n{total} Kalshi-priced rows checked, {bad} with problems")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
