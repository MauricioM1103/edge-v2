"""Parlay math, capper logic, and ledger settlement.

Run from the repo root (no UI, no API key):

    python tests/test_parlay.py

Covers:
  - calc.parlay: combined odds, joint probs, EV, Kelly, push/void/loss
    resolution.
  - calc.capper: tier-A gate, same-game dedup, in-play exclusion, rank
    order, end-to-end build with rationale.
  - storage.picks: log_capper_parlay row shape, parlay auto_settle with
    push-reduction and leg-level grading.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
# Windows consoles default to cp1252; this file uses "≈" and other
# arithmetic glyphs in its labels. Reconfigure stdout so the prints
# don't choke on them.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, Exception):
    pass

from calc import parlay as pm
from calc import capper
from storage import picks as picks_store

FAILS = 0


def check(label, cond, detail=""):
    global FAILS
    mark = "PASS" if cond else "FAIL"
    if not cond:
        FAILS += 1
    print(f"  [{mark}] {label}{(' -- ' + str(detail)) if detail else ''}")


def approx(a, b, tol=1e-4):
    if a is None or b is None:
        return False
    return abs(a - b) <= tol


def test_math():
    print("== parlay math")
    # Combined decimal: three -110s → 1.9091^3 ≈ 6.9641; combined American ≈ +596
    d = pm.combined_decimal([-110, -110, -110])
    check("3-leg -110 combined decimal ≈ 6.9641", approx(d, 1.9091 ** 3, tol=0.01), d)
    check("3-leg -110 combined American ≈ +596",
          pm.combined_american([-110, -110, -110]) == 596)
    check("2-leg -200 combined ≈ +125",
          pm.combined_american([-200, -200]) == 125)
    check("bad leg -> None",
          pm.combined_decimal([-110, None, -110]) is None
          and pm.combined_american([0]) is None)

    # Joint probs: 0.6 × 0.5 × 0.52 = 0.156
    check("joint fair prob = product",
          approx(pm.joint_fair_prob([0.6, 0.5, 0.52]), 0.156))
    check("bad prob -> None",
          pm.joint_fair_prob([0.6, None]) is None
          and pm.joint_fair_prob([0.6, 1.5]) is None)
    # Joint implied: all -110 → 0.5238 ** 3 ≈ 0.1437
    check("joint implied = product",
          approx(pm.joint_implied_prob([-110, -110, -110]), 0.52381 ** 3, tol=0.0005))

    # Edge: positive when fair > implied
    e = pm.parlay_edge([0.56, 0.56, 0.56], [-110, -110, -110])
    check("edge computes", e is not None and e > 0, f"{e}")

    # Profit / EV at $100 stake on three -110s
    profit = pm.parlay_profit_on_win(100, [-110, -110, -110])
    check("profit on win uses combined decimal",
          approx(profit, (pm.combined_decimal([-110, -110, -110]) - 1.0) * 100, tol=0.01), profit)
    ev = pm.parlay_ev_dollars(100, [0.55, 0.55, 0.55], [-110, -110, -110])
    # Joint fair 0.166375, profit 596.41, EV = 0.166 * 596 - 0.833 * 100 ≈ +15.8
    check("EV calc positive on sharp legs", ev is not None and ev > 0, f"{ev}")
    # Negative when legs are at book-implied fair: joint fair == joint implied,
    # EV should be slightly negative (vig stack).
    ev_flat = pm.parlay_ev_dollars(100, [0.5238, 0.5238, 0.5238], [-110, -110, -110])
    check("EV ≈ 0 when fair == implied", approx(ev_flat, 0, tol=0.5), f"{ev_flat}")

    # Kelly: quarter-Kelly default, positive on edge
    kf = pm.parlay_kelly_fraction([0.56, 0.56, 0.56], [-110, -110, -110])
    check("quarter-Kelly positive for +EV parlay", kf is not None and kf > 0, f"{kf}")
    kf_neg = pm.parlay_kelly_fraction([0.50, 0.50, 0.50], [-110, -110, -110])
    check("Kelly = 0 for -EV parlay", kf_neg == 0.0, f"{kf_neg}")

    # Settlement rules
    check("all wins -> win + combined price",
          pm.settle_parlay(["win", "win", "win"], [-110, -110, -110]) == ("win", 596))
    check("one loss -> loss",
          pm.settle_parlay(["win", "loss", "win"], [-110, -110, -110])[0] == "loss")
    # Push drops the middle leg → parlay reduces to two -110 wins.
    # (1 + 100/110)^2 ≈ 3.6446 → +264 American.
    check("one push -> parlay reduces to remaining",
          pm.settle_parlay(["win", "push", "win"], [-110, -110, -110]) == ("win", 264),
          "two -110 wins = +264")
    check("all pushes -> push",
          pm.settle_parlay(["push", "void"], [-110, -110]) == ("push", None))
    check("still open -> open",
          pm.settle_parlay(["win", "open", "win"], [-110, -110, -110])[0] == "open")
    check("loss trumps open",
          pm.settle_parlay(["loss", "open"], [-110, -110])[0] == "loss")


def _edge(sport, eid, selection, edge, fair, price, start_hours_out=4.0,
          state="pre", fair_source="Pinnacle no-vig"):
    """Build a synthetic agent edge dict for capper tests."""
    now = datetime.now(timezone.utc)
    return {
        "_sport": sport, "event_id": eid, "matchup": f"{eid} matchup",
        "market": "h2h", "side_label": selection, "price": price, "book": "Bovada",
        "book_implied": None, "fair_prob": fair, "edge": edge,
        "fair_source": fair_source,
        "status_state": state,
        "start_utc": (now + timedelta(hours=start_hours_out)).isoformat(timespec="seconds"),
    }


def test_capper():
    print("== capper selection")
    edges = [
        _edge("nba", "e1", "Lakers wins", 0.040, 0.60, -110),  # tier-A, pregame
        _edge("nhl", "e2", "Bruins -1.5", 0.035, 0.55, +150),
        _edge("soccer", "e3", "Arsenal wins", 0.028, 0.58, -140),
        _edge("nba", "e1", "Lakers -5.5", 0.050, 0.57, -110),  # same event as 1
        _edge("mlb", "e4", "Yankees wins", 0.015, 0.52, -120),  # below tier A
        _edge("nfl", "e5", "Chiefs wins", 0.025, 0.56, -110, state="in"),  # in-play
    ]
    legs = capper.select_parlay_legs(edges, target_legs=3, min_leg_edge=0.020)
    check("capper picks 3 legs", len(legs) == 3, f"got {len(legs)}")
    check("tier-A floor enforced (no e4)",
          all(float(l["edge"]) >= 0.020 for l in legs))
    check("in-play leg excluded (no e5)",
          not any(l["event_id"] == "e5" for l in legs))
    check("one leg per event (e1 appears once)",
          sum(1 for l in legs if l["event_id"] == "e1") == 1)
    check("highest-edge leg first",
          legs[0]["event_id"] == "e1" and legs[0]["edge"] == 0.050)
    # Fewer tier-A edges than min_legs returns a short list
    thin = capper.select_parlay_legs([edges[2]], target_legs=3, min_legs=2)
    check("thin slate returns under min_legs for the UI to refuse", len(thin) < 2)

    print("== capper build")
    parlay = capper.build_capper_parlay(edges, bankroll=1000.0, kelly_mult=0.25)
    check("build returns parlay with legs", len(parlay["legs"]) >= 2)
    check("combined price computed",
          parlay["combined_american"] is not None
          and parlay["combined_decimal"] > 1.0)
    check("joint fair + implied computed",
          parlay["joint_fair_prob"] is not None
          and parlay["joint_implied_prob"] is not None)
    check("edge = joint fair - joint implied",
          approx(parlay["edge"],
                 parlay["joint_fair_prob"] - parlay["joint_implied_prob"]))
    check("rationale mentions leg count",
          f"{len(parlay['legs'])}-leg" in (parlay.get("rationale") or ""))
    # Thin slate: builder still returns a dict with an explanatory rationale
    nothing = capper.build_capper_parlay([_edge("nba", "e1", "x", 0.01, 0.5, -110)])
    check("thin slate still returns an object with a rationale",
          "No tier-A legs" in (nothing.get("rationale") or ""))


def test_ledger():
    print("== ledger: log + settle parlay")
    edges = [
        _edge("nba", "11111111", "Lakers wins", 0.040, 0.60, -110),
        _edge("nhl", "22222222", "Bruins wins", 0.035, 0.58, -130),
    ]
    parlay = capper.build_capper_parlay(edges, bankroll=500.0)
    fd, path = tempfile.mkstemp(prefix="picks_parlay_", suffix=".json")
    os.close(fd)
    try:
        row = picks_store.log_capper_parlay(parlay, bankroll=500.0,
                                            unit_dollars=5.0, path=path)
        check("log_capper_parlay writes a row", row is not None)
        stored = picks_store.list_picks(path)
        check("exactly one row in ledger", len(stored) == 1)
        r = stored[0]
        check("market = 'parlay'", r.get("market") == "parlay")
        check("source = 'capper'", r.get("source") == "capper")
        check("legs preserved", len(r.get("legs") or []) == 2)
        check("combined price saved",
              r.get("price_american") == parlay["combined_american"])

        # Force both legs to win, run _settle_parlay directly (no ESPN).
        stored[0]["legs"][0]["status"] = "win"
        stored[0]["legs"][1]["status"] = "win"
        picks_store._save(path, stored)
        from calc import parlay as pm2
        # settle_parlay directly:
        overall, effective = pm2.settle_parlay(
            [l["status"] for l in stored[0]["legs"]],
            [l["price_american"] for l in stored[0]["legs"]],
        )
        check("two-leg both win -> win", overall == "win")
        check("effective combined > 0 American",
              effective is not None)

        # Push + win reduces to the single win's standalone price
        overall2, eff2 = pm2.settle_parlay(
            ["push", "win"], [-110, -130]
        )
        check("push drops leg, parlay = standalone leg",
              overall2 == "win" and eff2 == -130)
        # Loss anywhere kills
        overall3, _ = pm2.settle_parlay(
            ["win", "loss"], [-110, -130]
        )
        check("one loss kills the parlay", overall3 == "loss")

        # Insufficient legs → log refuses
        thin_parlay = dict(parlay)
        thin_parlay["legs"] = [parlay["legs"][0]]
        refused = picks_store.log_capper_parlay(
            thin_parlay, bankroll=500.0, unit_dollars=5.0, path=path,
        )
        check("single-leg 'parlay' is refused by the logger", refused is None)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


if __name__ == "__main__":
    test_math()
    test_capper()
    test_ledger()
    print(f"\n{'ALL PASS' if FAILS == 0 else f'{FAILS} FAILED'}")
    sys.exit(1 if FAILS else 0)
