"""Kalshi market expansion: three-way moneylines, name matching, settlement.

    python tests/test_markets.py
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from apis import kalshi
from apis.kalshi_match import same_contestant, match_markets_to_games, is_tie_market
from calc import agent
from calc.odds import no_vig_multi
from storage import picks as picks_store

FAILS = 0


def check(label, cond, detail=""):
    global FAILS
    if not cond:
        FAILS += 1
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}{(' -- ' + str(detail)) if detail else ''}")


def km(event, sub, bid, ask):
    return {"event_ticker": event, "ticker": f"{event}-{sub[:3].upper()}",
            "yes_sub_title": sub, "yes_bid_dollars": f"{bid:.4f}", "yes_ask_dollars": f"{ask:.4f}"}


def book(title, key, outcomes):
    return {"title": title, "key": key,
            "markets": [{"key": "h2h", "outcomes": [{"name": n, "price": p} for n, p in outcomes]}]}


def test_math_and_names():
    print("== no-vig and names")
    f = no_vig_multi([150, 240, 190])
    check("three-way fair sums to 1", f is not None and abs(sum(f) - 1) < 1e-9)
    check("three-way home fair below a coin flip", f[0] < 0.5, f"{f[0]:.3f}")
    check("bad price -> None", no_vig_multi([150, None]) is None)
    check("initial vs full name", same_contestant("Frances Tiafoe", "F. Tiafoe"))
    check("accents ignored", same_contestant("Jiří Procházka", "Jiri Prochazka"))
    check("club short name", same_contestant("Tottenham", "Tottenham Hotspur"))
    check("same surname, different player", not same_contestant("Alexander Zverev", "Mischa Zverev"))
    check("different clubs, shared city", not same_contestant("Manchester City", "Manchester United"))
    check("tie market detected", is_tie_market({"yes_sub_title": "Tie"}))


def test_matching():
    print("== Kalshi -> sharp slate matching")
    markets = [
        km("KXATPMATCH-26NOV17MEDTIA", "Frances Tiafoe", 0.30, 0.31),
        km("KXATPMATCH-26NOV17MEDTIA", "Daniil Medvedev", 0.69, 0.70),
        km("KXATPMATCH-26NOV17ALCCOB", "Carlos Alcaraz", 0.80, 0.82),
        km("KXATPMATCH-26NOV17ALCCOB", "Flavio Cobolli", 0.18, 0.20),
    ]
    games = [
        {"id": "g1", "home_team": "Daniil Medvedev", "away_team": "Frances Tiafoe",
         "commence_time": "2026-11-17T13:30:00Z"},
        {"id": "g2", "home_team": "Jannik Sinner", "away_team": "Taylor Fritz",
         "commence_time": "2026-11-17T19:00:00Z"},
        {"id": "g3", "home_team": "Carlos Alcaraz", "away_team": "Flavio Cobolli",
         "commence_time": "2026-11-27T13:30:00Z"},  # same pairing, 10 days later
    ]
    m = match_markets_to_games(markets, games)
    check("tennis match attached by player names", ("Daniil Medvedev", "Frances Tiafoe") in m)
    check("unlisted match not attached", ("Jannik Sinner", "Taylor Fritz") not in m)
    check("wrong-date rematch not attached", ("Carlos Alcaraz", "Flavio Cobolli") not in m)


def test_scanners():
    print("== tennis via Kalshi scanner")
    game = {"id": "g1", "home_team": "Daniil Medvedev", "away_team": "Frances Tiafoe",
            "commence_time": "2026-11-17T13:30:00Z",
            "bookmakers": [book("Pinnacle", "pinnacle", [("Daniil Medvedev", -185), ("Frances Tiafoe", 160)])]}
    markets = [km("KXATPMATCH-26NOV17MEDTIA", "Frances Tiafoe", 0.30, 0.31),
               km("KXATPMATCH-26NOV17MEDTIA", "Daniil Medvedev", 0.69, 0.70)]
    edges = agent.scan_kalshi_vs_pinnacle(
        kalshi_markets_by_game={("Daniil Medvedev", "Frances Tiafoe"): markets},
        pinnacle_games=[game], bankroll_cash=500.0, sport_key="atp", min_edge_pct=0.02)
    check("tennis edge found on the underpriced player",
          len(edges) == 1 and edges[0]["side_label"] == "Frances Tiafoe wins",
          [e["side_label"] for e in edges])
    if edges:
        e = edges[0]
        check("fair is Pinnacle two-way no-vig", abs(e["fair_prob"] - 0.3721) < 0.002, f"{e['fair_prob']:.4f}")
        check("book is Kalshi with its ticker", e["book"] == "Kalshi" and bool(e["kalshi_ticker"]))
        check("matchup reads A vs B for players", " vs " in e["matchup"])

    print("== soccer three-way")
    sg = {"id": "s1", "home_team": "Tottenham Hotspur", "away_team": "Fulham",
          "commence_time": "2026-12-03T15:00:00Z",
          "bookmakers": [
              book("Pinnacle", "pinnacle", [("Tottenham Hotspur", 105), ("Fulham", 265), ("Draw", 255)]),
              book("Bovada", "bovada", [("Tottenham Hotspur", 100), ("Fulham", 250), ("Draw", 300)]),
          ]}
    off = agent.scan_offshore_markets_vs_pinnacle(
        [sg], bankroll_cash=500.0, sport_key="epl", markets=["h2h"], min_edge_pct=-1.0)
    by = {e["side_label"]: e for e in off}
    check("offshore scanner emits home / away / draw", set(by) == {"Tottenham Hotspur", "Fulham", "Draw"}, list(by))
    if len(by) == 3:
        total = sum(e["fair_prob"] for e in off)
        check("three fair probs sum to 1", abs(total - 1) < 1e-6, f"{total:.4f}")
        check("home fair is three-way (~46%), not two-way (~64%)",
              0.44 < by["Tottenham Hotspur"]["fair_prob"] < 0.48, f"{by['Tottenham Hotspur']['fair_prob']:.3f}")
        check("only the genuinely long Draw price shows an edge",
              by["Draw"]["edge"] > 0 and by["Tottenham Hotspur"]["edge"] < 0)
    no_draw = dict(sg, bookmakers=[book("Pinnacle", "pinnacle", [("Tottenham Hotspur", 105), ("Fulham", 265)]),
                                   book("Bovada", "bovada", [("Tottenham Hotspur", 100), ("Fulham", 250)])])
    check("soccer moneyline without a sharp draw price is skipped",
          agent.scan_offshore_markets_vs_pinnacle([no_draw], 500.0, sport_key="epl", markets=["h2h"],
                                                  min_edge_pct=-1.0) == [])
    kms = [km("KXEPLGAME-26DEC03TOTFUL", "Tottenham", 0.44, 0.46),
           km("KXEPLGAME-26DEC03TOTFUL", "Fulham", 0.24, 0.26),
           km("KXEPLGAME-26DEC03TOTFUL", "Tie", 0.20, 0.22)]
    ke = agent.scan_kalshi_vs_pinnacle(
        kalshi_markets_by_game={("Tottenham Hotspur", "Fulham"): kms},
        pinnacle_games=[sg], bankroll_cash=500.0, sport_key="epl", min_edge_pct=0.02)
    labels = [e["side_label"] for e in ke]
    check("Kalshi tie market priced as the Draw", "Draw" in labels, labels)
    grade = agent._grade_offshore_pick
    check("three-way: level final beats a team pick",
          grade("h2h", "home", None, 1, 1, is_final=True, three_way=True) == "loss")
    check("three-way: level final wins the Draw",
          grade("h2h", "draw", None, 1, 1, is_final=True, three_way=True) == "win")
    check("two-way: level final still pushes", grade("h2h", "home", None, 1, 1, is_final=True) == "push")


def test_line_matching():
    print("== spreads and totals shop only at the sharp line")
    def sp(title, key, home_pt, home_px, away_px):
        return {"title": title, "key": key, "markets": [{"key": "spreads", "outcomes": [
            {"name": "Pittsburgh Penguins", "point": home_pt, "price": home_px},
            {"name": "St Louis Blues", "point": -home_pt, "price": away_px}]}]}
    g = {"id": "n1", "home_team": "Pittsburgh Penguins", "away_team": "St Louis Blues",
         "commence_time": "2099-01-01T00:00:00Z",
         "bookmakers": [sp("Pinnacle", "pinnacle", 1.5, -263, 225),
                        sp("BetUS", "betus", -1.5, 236, -280),      # other side of the puck line
                        sp("Bovada", "bovada", 1.5, -270, 220)]}    # same line as Pinnacle
    e = agent.scan_offshore_markets_vs_pinnacle([g], 500.0, sport_key="nhl", markets=["spreads"],
                                                min_edge_pct=-1.0)
    check("a book hanging the opposite puck line is not shopped",
          all(x["book"] != "BetUS" for x in e), [(x["book"], x["side_label"]) for x in e])
    check("no phantom double-digit edge", all(abs(x["edge"]) < 0.05 for x in e),
          [round(x["edge"], 3) for x in e])
    check("the book on the same line is still shopped", any(x["book"] == "Bovada" for x in e))


def test_settlement():
    print("== settlement")
    level = {"home_name": "Tottenham Hotspur", "away_name": "Fulham",
             "home_score": 1, "away_score": 1, "home_won": None}
    home = {"home_name": "Tottenham Hotspur", "away_name": "Fulham",
            "home_score": 2, "away_score": 0, "home_won": True}

    def h2h(sel, fin, sport="epl"):
        row = {"selection": sel, "sport": sport, "status": "open"}
        picks_store._settle_h2h(row, fin)
        return row["status"]

    check("soccer team pick loses on a draw", h2h("Tottenham Hotspur wins", level) == "loss")
    check("soccer Draw pick wins on a draw", h2h("Draw", level) == "win")
    check("soccer Draw pick loses when a team wins", h2h("Draw", home) == "loss")
    check("soccer team pick wins normally", h2h("Tottenham Hotspur", home) == "win")
    check("unknown team on a draw stays open", h2h("Arsenal wins", level) == "open")
    check("two-way sport level final stays open",
          h2h("Boston Bruins wins", dict(level, home_name="Boston Bruins"), sport="nhl") == "open")

    real = kalshi.market_result
    try:
        kalshi.market_result = lambda t: {"T-YES": "yes", "T-NO": "no"}.get(t)
        a = {"kalshi_ticker": "T-YES", "status": "open"}
        b = {"kalshi_ticker": "T-NO", "status": "open"}
        c = {"kalshi_ticker": "T-OPEN", "status": "open"}
        d = {"status": "open"}
        check("Kalshi result yes -> win", picks_store._settle_from_kalshi(a) and a["status"] == "win")
        check("Kalshi result no -> loss", picks_store._settle_from_kalshi(b) and b["status"] == "loss")
        check("unresolved market stays open", not picks_store._settle_from_kalshi(c) and c["status"] == "open")
        check("no ticker stays open", not picks_store._settle_from_kalshi(d))
    finally:
        kalshi.market_result = real


def test_ledger_safety():
    print("== ledger cannot be wiped by a failed read")
    import json, tempfile
    fd, path = tempfile.mkstemp(prefix="ledger_safe_", suffix=".json")
    os.close(fd)
    try:
        rows = [{"id": f"r{i}", "selection": f"Pick {i}", "status": "win"} for i in range(5)]
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rows, f)
        # A save with fewer rows than the file holds is refused.
        picks_store._save(path, rows[:1])
        check("shrinking save is refused", len(picks_store.list_picks(path)) == 5)
        # An explicit delete may shrink it.
        picks_store.delete_pick("r0", path=path)
        check("explicit delete still works", len(picks_store.list_picks(path)) == 4)
        # Unreadable file: writers refuse, display code sees empty, file untouched.
        good = open(path, encoding="utf-8").read()
        with open(path, "w", encoding="utf-8") as f:
            f.write(good[: len(good) // 2])  # torn write
        raised = False
        try:
            picks_store.add_pick({"selection": "New pick"}, path=path)
        except picks_store.LedgerUnavailable:
            raised = True
        check("adding to an unreadable ledger raises instead of saving", raised)
        check("the unreadable file was not overwritten",
              open(path, encoding="utf-8").read() == good[: len(good) // 2])
        check("display read of an unreadable ledger is just empty", picks_store.list_picks(path) == [])
        # Once readable again, everything is still there.
        with open(path, "w", encoding="utf-8") as f:
            f.write(good)
        picks_store.add_pick({"selection": "New pick"}, path=path)
        check("rows survive and the new pick is appended", len(picks_store.list_picks(path)) == 5)
        check("set_status edits in place",
              picks_store.set_status("r1", "void", path=path)
              and any(p.get("id") == "r1" and p.get("status") == "void" for p in picks_store.list_picks(path)))
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def test_edge_audit_fixes():
    """Regressions from the 2026-11-19 edge audit."""
    print("== edge audit regressions")
    from calc import agent as agent_calc
    from apis import kalshi as kalshi_api
    side = agent_calc._side_of
    check("exact name beats the loose matcher (Owls vs Owls)",
          side("Rice Owls", "Temple Owls", "Rice Owls") == "away"
          and side("Temple Owls", "Temple Owls", "Rice Owls") == "home")
    check("a name that fits both sides is refused, not guessed",
          side("Milan", "Inter Milan", "AC Milan") is None)
    check("an alias that fits one side still resolves",
          side("Lakers", "Los Angeles Lakers", "Boston Celtics") == "home")
    # Same-nickname game used to be skipped entirely (both names -> home).
    def _bk(title, hp, ap):
        return {"title": title, "markets": [{"key": "h2h", "outcomes": [
            {"name": "Temple Owls", "price": hp}, {"name": "Rice Owls", "price": ap}]}]}
    g = {"id": "owls", "home_team": "Temple Owls", "away_team": "Rice Owls",
         "bookmakers": [_bk("Pinnacle", -150, 130), _bk("Bovada", -160, 150)]}
    rows = agent_calc.scan_offshore_markets_vs_pinnacle(
        [g], 500.0, sport_key="ncaaf", markets=["h2h"], shop_books=["Bovada"], min_edge_pct=-1.0)
    by = {r["side_label"]: r for r in rows}
    check("same-nickname game is priced on both sides", set(by) == {"Temple Owls", "Rice Owls"}, str(list(by)))
    if "Rice Owls" in by:
        check("underdog edge uses the underdog's own fair and price",
              abs(by["Rice Owls"]["fair_prob"] - (100/230) / (150/250 + 100/230)) < 1e-9
              and by["Rice Owls"]["price"] == 150)
    # Kalshi: cost to buy is the ask plus the taker fee, never the midpoint.
    m = {"yes_bid_dollars": "0.56", "yes_ask_dollars": "0.60"}
    ask = kalshi_api.market_yes_ask_prob(m)
    check("Kalshi buy price is the ask", ask == 0.60, str(ask))
    check("Kalshi taker fee is 7% of p(1-p)", abs(kalshi_api.taker_fee_prob(0.60) - 0.0168) < 1e-9)
    check("an empty book (ask 0 or 1) is not a price",
          kalshi_api.market_yes_ask_prob({"yes_ask_dollars": "1.00"}) is None
          and kalshi_api.market_yes_ask_prob({"yes_bid_dollars": "0.40"}) is None)


def test_stale_sharp_quote():
    """In-play, Pinnacle's feed trails the shop books; a stale fair is not an edge."""
    print("== stale sharp quotes")
    from calc import agent as agent_calc
    def game(pin_at, shop_at, commence):
        def bk(title, at, hp, ap):
            return {"title": title, "markets": [{"key": "h2h", "last_update": at, "outcomes": [
                {"name": "Vegas Golden Knights", "price": hp}, {"name": "Dallas Stars", "price": ap}]}]}
        return {"id": "g", "home_team": "Vegas Golden Knights", "away_team": "Dallas Stars",
                "commence_time": commence,
                "bookmakers": [bk("Pinnacle", pin_at, -132, 113), bk("Bovada", shop_at, -900, 500)]}
    def scan(g):
        return agent_calc.scan_offshore_markets_vs_pinnacle(
            [g], 500.0, sport_key="nhl", markets=["h2h"], shop_books=["Bovada"], min_edge_pct=0.02)
    live_stale = game("2026-11-20T05:06:45Z", "2026-11-20T05:17:34Z", "2026-11-20T03:08:35Z")
    check("in-play: an 11-minute-old Pinnacle line prices nothing", scan(live_stale) == [], str(len(scan(live_stale))))
    live_fresh = game("2026-11-20T05:17:00Z", "2026-11-20T05:17:34Z", "2026-11-20T03:08:35Z")
    check("in-play: even a 34-second-old line prices nothing (live pricing is off)", scan(live_fresh) == [])
    pre = game("2026-11-19T20:00:00Z", "2026-11-19T20:06:00Z", "2026-11-20T03:08:35Z")
    check("pre-game: a 6-minute lag is fine", len(scan(pre)) == 1)
    pre_old = game("2026-11-19T19:00:00Z", "2026-11-19T20:06:00Z", "2026-11-20T03:08:35Z")
    check("pre-game: an hour-old Pinnacle line prices nothing", scan(pre_old) == [])
    check("missing timestamps are not judged", agent_calc._sharp_is_fresh(None, None, "2026-11-20T03:08:35Z"))


def test_scan_failures_are_reported():
    """A scanner crash must be visible, not an empty Board."""
    print("== scanner failures")
    from calc import agent as agent_calc
    check("no failures gives no message", agent_calc.describe_scan_failures([]) == "")
    msg = agent_calc.describe_scan_failures([("nba", "moneyline scan", KeyError("price")),
                                             ("nhl", "player-prop scan", ValueError("bad point"))])
    check("message counts the failures", msg.startswith("2 scans failed"), msg)
    check("message names each sport and scanner",
          "NBA moneyline scan" in msg and "NHL player-prop scan" in msg, msg)
    check("message carries the error type", "KeyError" in msg and "ValueError: bad point" in msg, msg)


def test_kalshi_event_must_be_this_game():
    """A Kalshi price is only used when the event is shown to be this game."""
    print("== Kalshi event validation")
    from apis.kalshi_match import event_side_map, find_event_markets, names_team
    from datetime import datetime, timezone
    # The live case from 2026-11-25: Strasbourg-Lorient (Dec 5) was attached to
    # Strasbourg @ Le Havre (Nov 28) because "ac" sits inside "Alsace".
    other_week = [km("KXLIGUE1GAME-26DEC05RCSFCL", "Strasbourg Alsace", 0.25, 0.26),
                  km("KXLIGUE1GAME-26DEC05RCSFCL", "Lorient", 0.48, 0.50),
                  km("KXLIGUE1GAME-26DEC05RCSFCL", "Tie", 0.25, 0.27)]
    check("another week's game with one shared team is rejected",
          event_side_map(other_week, "Le Havre AC", "Strasbourg", "2026-11-28T18:00:00Z") is None)
    check("old name fallback no longer attaches it",
          find_event_markets(other_week, datetime(2026, 11, 28, 18, tzinfo=timezone.utc), "HAC", "STR",
                             "Le Havre AC", "Strasbourg") == [])
    check("right teams on the wrong date are rejected",
          event_side_map(other_week, "Strasbourg", "Lorient", "2026-11-28T18:00:00Z") is None)
    right = event_side_map(other_week, "Strasbourg", "Lorient", "2026-12-05T18:00:00Z")
    check("the real fixture maps home, away and draw",
          right is not None and sorted(right.values()) == ["away", "draw", "home"], str(right))
    # Overlapping abbreviations: "SA" is inside "SAC", so the ticker alone
    # matched the Sacramento-Utah event to San Antonio @ Sacramento.
    sac_uta = [km("KXNBAGAME-26NOV29SACUTA", "Sacramento", 0.55, 0.57), km("KXNBAGAME-26NOV29SACUTA", "Utah", 0.43, 0.45)]
    check("same-day game sharing an abbreviation is rejected",
          find_event_markets(sac_uta, datetime(2026, 11, 29, 23, tzinfo=timezone.utc), "SAC", "SA",
                             "Sacramento Kings", "San Antonio Spurs") == [])
    # The scanner refuses it even if something upstream attached it anyway.
    g = {"id": "x", "home_team": "Le Havre AC", "away_team": "Strasbourg", "commence_time": "2026-11-28T18:00:00Z",
         "bookmakers": [book("Pinnacle", "pinnacle", [("Le Havre AC", 180), ("Strasbourg", 150), ("Draw", 230)])]}
    rows = agent.scan_kalshi_vs_pinnacle({("Le Havre AC", "Strasbourg"): other_week}, [g], 500.0,
                                         sport_key="ligue_1", min_edge_pct=-1.0)
    check("scanner prices nothing from a mismatched event", rows == [], str(len(rows)))
    # Kalshi's spellings still resolve when the game is right.
    check("'Kansas St.' names Kansas State", names_team("Kansas St.", "Kansas State Wildcats"))
    check("'Los Angeles R' names the Rams, not the Chargers",
          names_team("Los Angeles R", "Los Angeles Rams") and not names_team("Los Angeles R", "Los Angeles Chargers"))
    check("'Salt Lake' names Real Salt Lake", names_team("Salt Lake", "Real Salt Lake"))
    check("'Saint Louis' names St. Louis City SC", names_team("Saint Louis", "St. Louis City SC"))
    check("a bare 'FC' names nobody", not names_team("FC", "Toronto FC"))
    fla = [km("KXNCAAFGAME-26NOV27FLAFSU", "Florida St.", 0.60, 0.62), km("KXNCAAFGAME-26NOV27FLAFSU", "Florida", 0.38, 0.40)]
    fla[0]["ticker"], fla[1]["ticker"] = "KXNCAAFGAME-26NOV27FLAFSU-FSU", "KXNCAAFGAME-26NOV27FLAFSU-FLA"
    m = event_side_map(fla, "Florida State Seminoles", "Florida Gators", "2026-11-27T20:00:00Z")
    check("Florida vs Florida State resolves each market to the right school",
          m is not None and m.get(fla[0]["ticker"]) == "home" and m.get(fla[1]["ticker"]) == "away", str(m))
    # A Kalshi number far from the sharp fair is not offered as a price.
    g2 = {"id": "y", "home_team": "Houston Dynamo", "away_team": "San Jose Earthquakes", "commence_time": "2026-11-28T22:30:00Z",
          "bookmakers": [book("Pinnacle", "pinnacle", [("Houston Dynamo", -105), ("San Jose Earthquakes", 280), ("Draw", 270)])]}
    odd = [km("KXMLSGAME-26NOV28HOUSJ", "Houston", 0.25, 0.26), km("KXMLSGAME-26NOV28HOUSJ", "San Jose", 0.25, 0.26),
           km("KXMLSGAME-26NOV28HOUSJ", "Tie", 0.24, 0.25)]
    rows = agent.scan_kalshi_vs_pinnacle({("Houston Dynamo", "San Jose Earthquakes"): odd}, [g2], 500.0,
                                         sport_key="mls", min_edge_pct=-1.0)
    hou = [r for r in rows if r["side_label"].startswith("Houston")]
    check("26c against a ~48% fair is not shown as a Kalshi edge",
          all(r["book"] != "Kalshi" for r in hou) and all(r.get("kalshi_price_cents") is None for r in hou),
          str([(r["book"], r.get("kalshi_price_cents"), round(r["fair_prob"], 3)) for r in hou]))


if __name__ == "__main__":
    test_math_and_names()
    test_matching()
    test_scanners()
    test_line_matching()
    test_settlement()
    test_ledger_safety()
    test_edge_audit_fixes()
    test_stale_sharp_quote()
    test_scan_failures_are_reported()
    test_kalshi_event_must_be_this_game()
    print(f"\n{'ALL PASS' if FAILS == 0 else f'{FAILS} FAILED'}")
    sys.exit(1 if FAILS else 0)
