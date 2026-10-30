"""Picks ledger: local JSON persistence with ESPN auto-settlement.

Picks live in `picks.json` next to app.py by default. A pick has:
  id, created_at, sport, event_id, event_name,
  selection (team name or 'Over'/'Under'/'Home +X.5'/etc),
  market ('h2h' / 'spreads' / 'totals'),
  price_american, implied_prob, projected_prob,
  risk_units, win_units, methodology, notes,
  closing_price_american (filled on auto-settle),
  status ('open' | 'win' | 'loss' | 'push' | 'void'),
  result_payload (ESPN final scores).
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from typing import List, Optional

from apis import espn
from calc.odds import profit_on_win, implied_prob_american
from calc.clv import clv_percent


def _default_path() -> str:
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(here, "picks.json")


def _load(path) -> List[dict]:
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        return []
    except (OSError, json.JSONDecodeError, ValueError):
        return []


def _save(path, picks) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(picks, f, indent=2, default=str)
    except OSError:
        pass


def list_picks(path: Optional[str] = None) -> List[dict]:
    return _load(path or _default_path())


def add_pick(pick: dict, path: Optional[str] = None) -> dict:
    """Insert a new pick with an auto-generated id. Returns the stored row."""
    p = dict(pick or {})
    p.setdefault("id", uuid.uuid4().hex[:12])
    p.setdefault("created_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    p.setdefault("status", "open")
    # Fill implied prob if the caller didn't.
    if p.get("price_american") is not None and p.get("implied_prob") is None:
        p["implied_prob"] = implied_prob_american(p["price_american"])
    file_path = path or _default_path()
    picks = _load(file_path)
    picks.append(p)
    _save(file_path, picks)
    return p


def delete_pick(pick_id: str, path: Optional[str] = None) -> bool:
    file_path = path or _default_path()
    picks = _load(file_path)
    new = [p for p in picks if p.get("id") != pick_id]
    if len(new) == len(picks):
        return False
    _save(file_path, new)
    return True


def _settle_h2h(pick, final):
    """Set status/result_payload on a moneyline pick given ESPN final scores."""
    home_won = final.get("home_won")
    if home_won is None:
        return
    selection = (pick.get("selection") or "").strip().lower()
    home_name = (final.get("home_name") or "").strip().lower()
    away_name = (final.get("away_name") or "").strip().lower()
    picked_home = home_name and selection == home_name
    picked_away = away_name and selection == away_name
    if not (picked_home or picked_away):
        # fall back to loose match
        picked_home = home_name and selection in home_name
        picked_away = away_name and selection in away_name
    if picked_home:
        pick["status"] = "win" if home_won else "loss"
    elif picked_away:
        pick["status"] = "win" if not home_won else "loss"


def _settle_totals(pick, final):
    """Settle an over/under pick. pick['point'] must be set."""
    hs = final.get("home_score")
    ascore = final.get("away_score")
    if hs is None or ascore is None:
        return
    total = hs + ascore
    point = pick.get("point")
    if point is None:
        return
    try:
        p = float(point)
    except (TypeError, ValueError):
        return
    sel = (pick.get("selection") or "").strip().lower()
    if "over" in sel:
        if total > p:
            pick["status"] = "win"
        elif total < p:
            pick["status"] = "loss"
        else:
            pick["status"] = "push"
    elif "under" in sel:
        if total < p:
            pick["status"] = "win"
        elif total > p:
            pick["status"] = "loss"
        else:
            pick["status"] = "push"


def _settle_spreads(pick, final):
    """Settle a point-spread pick. pick['point'] is the selected side's number."""
    hs = final.get("home_score")
    ascore = final.get("away_score")
    if hs is None or ascore is None:
        return
    point = pick.get("point")
    if point is None:
        return
    try:
        p = float(point)
    except (TypeError, ValueError):
        return
    sel = (pick.get("selection") or "").strip().lower()
    home_name = (final.get("home_name") or "").strip().lower()
    away_name = (final.get("away_name") or "").strip().lower()
    picked_home = home_name and (sel == home_name or sel.startswith(home_name))
    picked_away = away_name and (sel == away_name or sel.startswith(away_name))
    if picked_home:
        margin = hs - ascore + p
    elif picked_away:
        margin = ascore - hs + p
    else:
        return
    if margin > 0:
        pick["status"] = "win"
    elif margin < 0:
        pick["status"] = "loss"
    else:
        pick["status"] = "push"


def auto_settle(path: Optional[str] = None) -> int:
    """Query ESPN for each open pick's event; settle finals. Returns # settled."""
    file_path = path or _default_path()
    picks = _load(file_path)
    settled = 0
    for p in picks:
        if p.get("status") not in (None, "open"):
            continue
        sport = p.get("sport")
        event_id = p.get("event_id")
        if not sport or not event_id:
            continue
        final = espn.is_event_final(sport, event_id)
        if not final or not final.get("is_final"):
            continue
        market = (p.get("market") or "h2h").lower()
        if market == "h2h":
            _settle_h2h(p, final)
        elif market == "totals":
            _settle_totals(p, final)
        elif market == "spreads":
            _settle_spreads(p, final)
        p["result_payload"] = {
            "home_score": final.get("home_score"),
            "away_score": final.get("away_score"),
            "home_won": final.get("home_won"),
            "settled_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        if p.get("status") in ("win", "loss", "push"):
            settled += 1
    _save(file_path, picks)
    return settled


def update_closing_price(pick_id: str, closing_american, path: Optional[str] = None) -> bool:
    """Record a closing American price on an existing pick for CLV tracking."""
    file_path = path or _default_path()
    picks = _load(file_path)
    changed = False
    for p in picks:
        if p.get("id") == pick_id:
            p["closing_price_american"] = closing_american
            p["clv_pct"] = clv_percent(p.get("price_american"), closing_american)
            changed = True
            break
    if changed:
        _save(file_path, picks)
    return changed


def summarize(picks) -> dict:
    """Record and ROI summary across settled picks (in units, not dollars)."""
    wins = losses = pushes = voids = 0
    net_units = 0.0
    risked_units = 0.0
    clv_sum = 0.0
    clv_count = 0
    open_count = 0
    for p in picks or []:
        status = p.get("status") or "open"
        risk = float(p.get("risk_units") or 0)
        win_u = float(p.get("win_units") or 0)
        if status == "open":
            open_count += 1
            continue
        risked_units += risk
        if status == "win":
            wins += 1
            net_units += win_u
        elif status == "loss":
            losses += 1
            net_units -= risk
        elif status == "push":
            pushes += 1
        elif status == "void":
            voids += 1
        clv = p.get("clv_pct")
        if clv is not None:
            try:
                clv_sum += float(clv)
                clv_count += 1
            except (TypeError, ValueError):
                pass
    total = wins + losses + pushes
    roi = (net_units / risked_units) if risked_units else None
    return {
        "open": open_count,
        "wins": wins,
        "losses": losses,
        "pushes": pushes,
        "voids": voids,
        "total_settled": total,
        "risked_units": risked_units,
        "net_units": net_units,
        "roi": roi,
        "win_pct": (wins / (wins + losses)) if (wins + losses) else None,
        "avg_clv_pct": (clv_sum / clv_count) if clv_count else None,
    }
