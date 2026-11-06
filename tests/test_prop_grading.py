"""End-to-end check for player-prop grading from ESPN box scores.

Run from the repo root (no UI, no API key):

    python tests/test_prop_grading.py

1. Pure-function checks: Over/Under rule, name normalization, stat parsing.
2. Live check: pull a recent final NBA game from the ESPN scoreboard, read
   its box score, and grade synthetic agent prop rows through
   storage.picks.auto_settle against a temp picks file:
     - an Over that hit            -> win
     - an Under that missed        -> loss
     - a player who did not play   -> void
     - a first-initial player name -> still matches and grades
   plus a manual prop row and a game-line row on the same event: the manual
   row must be left open, the game line must still grade from the scoreboard.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from apis import espn, espn_boxscore as bx  # noqa: E402
from storage import picks as picks_store  # noqa: E402

FAILS = 0


def check(label, cond, detail=""):
    global FAILS
    mark = "PASS" if cond else "FAIL"
    if not cond:
        FAILS += 1
    print(f"  [{mark}] {label}{(' -- ' + str(detail)) if detail else ''}")


def unit_tests():
    print("== pure functions")
    check("over hits", picks_store.grade_prop(27, 26.5, "over") == "win")
    check("over misses", picks_store.grade_prop(26, 26.5, "over") == "loss")
    check("under hits", picks_store.grade_prop(26, 26.5, "under") == "win")
    check("under misses", picks_store.grade_prop(27, 26.5, "under") == "loss")
    check("equal is push", picks_store.grade_prop(27, 27, "under") == "push")
    check("missing stat -> None", picks_store.grade_prop(None, 27, "over") is None)
    check("accents stripped", bx.normalize_name("Luka Dončić") == "luka doncic")
    check("initials/periods", bx.normalize_name("T.J. McConnell") == "tj mcconnell")
    check("suffix dropped", bx.normalize_name("Labaron Philon Jr.") == "labaron philon")
    check("hyphen -> space", bx.normalize_name("Kentavious Caldwell-Pope") == "kentavious caldwell pope")
    cands = [{"id": "1", "name": "LeBron James", "short": "L. James"},
             {"id": "2", "name": "Jabari Walker", "short": "J. Walker"},
             {"id": "3", "name": "Jalen Williams", "short": "J. Williams"},
             {"id": "4", "name": "Jaylin Williams", "short": "J. Williams"}]
    check("exact match", bx.match_player("Lebron James", cands)[0]["id"] == "1")
    check("initial fallback", bx.match_player("L. James", cands)[0]["id"] == "1")
    check("ambiguous initials refused", bx.match_player("J. Williams", cands) == (None, "ambiguous"))
    check("unknown player", bx.match_player("Stephen Curry", cands) == (None, ""))
    check("made-att split", bx._part("7-11", 0) == 7 and bx._part("10/21", 1) == 21)
    check("innings -> outs", bx._outs_from_ip("5.2") == 17 and bx._outs_from_ip("7") == 21)
    check("total-bases text", [bx._bases_from_text(t) for t in (
        "Turang doubled to center.", "Chourio singled to left.", "X tripled to right.",
        "Y homered to left (412 feet).", "Z grounded into double play.")] == [2, 1, 3, 4, 0])
    check("side parsed from selection",
          picks_store._prop_side({"selection": "Devin Booker Under 26.5"}) == "under")
    check("player parsed from selection",
          picks_store._prop_player({"selection": "Devin Booker Under 26.5"}) == "Devin Booker")
    check("game line has no prop side", picks_store._prop_side({"selection": "Lakers -3.5"}) is None)

    print("== moneyline labels")
    final = {"home_won": True, "home_name": "Phoenix Suns", "away_name": "Orlando Magic",
             "home_score": 117, "away_score": 104}

    def h2h(selection, fin=final):
        row = {"selection": selection, "status": "open"}
        picks_store._settle_h2h(row, fin)
        return row["status"]

    check("'Team wins' label, home winner", h2h("Phoenix Suns wins") == "win")
    check("'Team wins' label, away loser", h2h("Orlando Magic wins") == "loss")
    check("bare team name still grades", h2h("Phoenix Suns") == "win")
    check("'to win' / 'ML' tails stripped",
          h2h("Orlando Magic to win") == "loss" and h2h("Phoenix Suns ML") == "win")
    la_final = {"home_won": False, "home_name": "Los Angeles Clippers", "away_name": "Denver Nuggets"}
    check("Odds API vs ESPN naming via same_team", h2h("LA Clippers wins", la_final) == "loss")
    check("unknown team stays open", h2h("Boston Celtics wins") == "open")
    check("no winner yet stays open", h2h("Phoenix Suns wins", {"home_won": None}) == "open")

    print("== spread labels")
    sp_final = {"home_name": "Arizona Wildcats", "away_name": "Cal Poly Mustangs",
                "home_score": 100, "away_score": 60}

    def spread(selection, point, fin=sp_final):
        row = {"selection": selection, "point": point, "status": "open"}
        picks_store._settle_spreads(row, fin)
        return row["status"]

    check("spread label strip",
          picks_store._spread_team("Arizona Wildcats -31") == "Arizona Wildcats"
          and picks_store._spread_team("Celtics +3.5") == "Celtics")
    check("home spread covers", spread("Arizona Wildcats -31", -31) == "win")
    check("home spread misses", spread("Arizona Wildcats -45.5", -45.5) == "loss")
    check("away spread with plus line", spread("Cal Poly Mustangs +45.5", 45.5) == "win")
    check("whole-number line pushes", spread("Arizona Wildcats -40", -40) == "push")
    clip_final = {"home_name": "Los Angeles Clippers", "away_name": "Denver Nuggets",
                  "home_score": 110, "away_score": 100}
    check("Odds API vs ESPN naming on spreads", spread("LA Clippers -3.5", -3.5, clip_final) == "win")
    check("unknown team spread stays open", spread("Boston Celtics -2.5", -2.5) == "open")

    print("== BTTS labels")

    def btts(selection, fin):
        row = {"selection": selection, "status": "open"}
        picks_store._settle_btts(row, fin)
        return row["status"]

    both_scored = {"home_score": 2, "away_score": 1}
    one_blank = {"home_score": 3, "away_score": 0}
    goalless = {"home_score": 0, "away_score": 0}
    check("BTTS side parse: Yes", picks_store._btts_side("Both to score: Yes") == "yes")
    check("BTTS side parse: No", picks_store._btts_side("BTTS No") == "no")
    check("BTTS side parse: ambiguous -> None", picks_store._btts_side("yes or no") is None)
    check("BTTS Yes wins when both score", btts("Both to score: Yes", both_scored) == "win")
    check("BTTS Yes loses on a blank sheet", btts("Both to score: Yes", one_blank) == "loss")
    check("BTTS No wins when one is blank", btts("Both to score: No", one_blank) == "win")
    check("BTTS No wins on 0-0", btts("Both to score: No", goalless) == "win")
    check("BTTS without score stays open",
          btts("Both to score: Yes", {"home_score": None, "away_score": 2}) == "open")

    print("== Draw No Bet labels")

    def dnb(selection, fin):
        row = {"selection": selection, "status": "open"}
        picks_store._settle_dnb(row, fin)
        return row["status"]

    # Simulated soccer finals (ESPN fields).
    arsenal_win = {"home_name": "Arsenal", "away_name": "Chelsea",
                   "home_score": 2, "away_score": 1, "home_won": True}
    chelsea_win = {"home_name": "Arsenal", "away_name": "Chelsea",
                   "home_score": 0, "away_score": 1, "home_won": False}
    draw = {"home_name": "Arsenal", "away_name": "Chelsea",
            "home_score": 1, "away_score": 1, "home_won": None}
    check("DNB label strip: '(DNB)'", picks_store._dnb_team("Arsenal (DNB)") == "Arsenal")
    check("DNB label strip: 'DNB' tail", picks_store._dnb_team("Arsenal DNB") == "Arsenal")
    check("DNB home win", dnb("Arsenal (DNB)", arsenal_win) == "win")
    check("DNB home loss", dnb("Arsenal (DNB)", chelsea_win) == "loss")
    check("DNB away win", dnb("Chelsea (DNB)", chelsea_win) == "win")
    check("DNB draw -> push (either side)",
          dnb("Arsenal (DNB)", draw) == "push" and dnb("Chelsea (DNB)", draw) == "push")
    # Naming drift still resolves through same_team.
    man_city_draw = {"home_name": "Manchester City", "away_name": "Liverpool",
                     "home_score": 1, "away_score": 1, "home_won": None}
    check("DNB Odds API vs ESPN naming on draw",
          dnb("Man City (DNB)", man_city_draw) == "push")
    man_city_win = {"home_name": "Manchester City", "away_name": "Liverpool",
                    "home_score": 2, "away_score": 1, "home_won": True}
    check("DNB Odds API vs ESPN naming on win",
          dnb("Man City (DNB)", man_city_win) == "win")
    check("DNB unknown team stays open",
          dnb("Real Madrid (DNB)", arsenal_win) == "open")


def find_recent_final_nba():
    today = datetime.now(timezone.utc).date()
    for n in range(0, 8):
        d = (today - timedelta(days=n)).strftime("%Y%m%d")
        for ev in espn.get_scoreboard("nba", date=d):
            if ev.get("is_final"):
                return ev, d
    return None, None


def live_test():
    print("== live: recent final NBA game")
    ev, date = find_recent_final_nba()
    if not ev:
        check("found a final NBA game in the last 7 days", False, "no finals on ESPN")
        return
    print(f"  game: {ev['name']} (event {ev['id']}, {date}) -- {ev['away']['score']}-{ev['home']['score']}")
    summary = bx.fetch_summary("nba", ev["id"])
    check("summary fetched", bool(summary))
    rows = bx._rows_from_boxscore(summary or {})
    played = [r for r in rows if not r["dnp"]]
    dnp = [r for r in rows if r["dnp"]]
    check("box score has players", len(played) > 5, f"{len(played)} played, {len(dnp)} DNP")
    if not played:
        return
    # Highest scorer -> Over that hit / Under that missed at (pts - 0.5).
    top = max(played, key=lambda r: bx._num(r["stats"].get("points")) or 0)
    pts = bx._num(top["stats"].get("points"))
    line = pts - 0.5
    print(f"  top scorer: {top['name']} {pts:g} pts -> synthetic line {line:g}")
    if dnp:
        print(f"  DNP player: {dnp[0]['name']} ({dnp[0]['raw'].get('reason') or 'no stats'})")

    created = datetime.strptime(date, "%Y%m%d").replace(tzinfo=timezone.utc).isoformat(timespec="seconds")

    def row(selection, player, market="player_points", point=None, **extra):
        base = {
            "sport": "nba", "event_id": ev["id"], "event_name": ev["name"],
            "market": market, "market_family": "prop", "player": player,
            "selection": selection, "point": point,
            "price_american": -110, "implied_prob": 0.524, "projected_prob": 0.58,
            "risk_units": 1.0, "win_units": 0.91, "source": "agent-auto",
            "created_at": created, "pick_date": created[:10], "status": "open",
        }
        base.update(extra)
        return base

    fd, path = tempfile.mkstemp(prefix="picks_test_", suffix=".json")
    os.close(fd)
    try:
        picks_store.add_pick(row(f"{top['name']} Over {line:g}", top["name"], point=line, id="over_hit"), path=path)
        picks_store.add_pick(row(f"{top['name']} Under {line:g}", top["name"], point=line, id="under_miss"), path=path)
        if dnp:
            picks_store.add_pick(row(f"{dnp[0]['name']} Over 0.5", dnp[0]["name"], point=0.5, id="dnp_void"), path=path)
        # A first-initial name (how some books abbreviate) should still match.
        toks = top["name"].split()
        short = f"{toks[0][0]}. {' '.join(toks[1:])}"
        picks_store.add_pick(row(f"{short} Over {line:g}", short, point=line, id="initial_match"), path=path)
        # Manual prop row: must be left open.
        picks_store.add_pick(row(f"{top['name']} Over {line:g}", top["name"], point=line,
                                 id="manual_prop", source="manual"), path=path)
        # Game-line row on the same event: grades via the scoreboard as before.
        hs, as_ = ev["home"]["score"], ev["away"]["score"]
        picks_store.add_pick({
            "id": "game_total", "sport": "nba", "event_id": ev["id"], "event_name": ev["name"],
            "market": "totals", "market_family": "game", "selection": "Over", "point": (hs + as_) - 0.5,
            "price_american": -110, "projected_prob": 0.55, "risk_units": 1.0, "win_units": 0.91,
            "source": "agent-auto", "created_at": created, "pick_date": created[:10], "status": "open",
        }, path=path)

        n = picks_store.auto_settle(path=path)
        print(f"  auto_settle settled {n} row(s)")
        by_id = {p["id"]: p for p in picks_store.list_picks(path)}

        def rp(i):
            return (by_id[i].get("result_payload") or {})

        check("Over that hit -> win", by_id["over_hit"]["status"] == "win",
              f"stat={rp('over_hit').get('stat')} vs {rp('over_hit').get('point')} ({rp('over_hit').get('stat_label')})")
        check("Under that missed -> loss", by_id["under_miss"]["status"] == "loss",
              f"stat={rp('under_miss').get('stat')} vs {rp('under_miss').get('point')}")
        check("result_payload carries stat/point/side/settled_at",
              all(k in rp("over_hit") for k in ("stat", "point", "side", "settled_at")),
              json.dumps(rp("over_hit"))[:160])
        if dnp:
            check("DNP -> void", by_id["dnp_void"]["status"] == "void", rp("dnp_void").get("reason"))
        check("first-initial name still grades", by_id["initial_match"]["status"] == "win",
              f"{short!r} -> {rp('initial_match').get('player')!r}")
        check("manual prop left open", by_id["manual_prop"]["status"] == "open")
        check("game total still grades from scoreboard", by_id["game_total"]["status"] == "win",
              f"{as_}+{hs} vs {by_id['game_total']['point']}")
        # Idempotent: a second sweep changes nothing.
        before = json.dumps(picks_store.list_picks(path), sort_keys=True, default=str)
        picks_store.auto_settle(path=path)
        after = json.dumps(picks_store.list_picks(path), sort_keys=True, default=str)
        check("second sweep is a no-op", before == after)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


if __name__ == "__main__":
    unit_tests()
    live_test()
    print(f"\n{'ALL PASS' if FAILS == 0 else f'{FAILS} FAILED'}")
    sys.exit(1 if FAILS else 0)
