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


def _dedup_key(p: dict) -> tuple:
    """Stable key for exact-match deduplication (same side, same price slot)."""
    return (
        (p.get("sport") or "").lower(),
        str(p.get("event_id") or ""),
        (p.get("market") or "h2h").lower(),
        (p.get("selection") or "").strip().lower(),
    )


def _market_slot(p: dict) -> tuple:
    """Group key for a single two-way market slot.

    Two picks that share this slot are opposite sides of the same market
    (e.g. "Lakers wins" vs "Celtics wins" on the same h2h, or "Over 45.5"
    vs "Under 45.5" on the same total). Point normalization:
     - h2h / h2h_h1: point ignored (always None).
     - spreads: home_pt = -away_pt, so key on abs(point) so "Lakers -3.5"
       and "Celtics +3.5" collapse. Different numbers (-3.5 vs -7.5) stay
       as distinct slots.
     - totals: Over/Under share the same point, so point is used as-is.
    """
    sport = (p.get("sport") or "").lower()
    event = str(p.get("event_id") or "")
    market = (p.get("market") or "h2h").lower()
    pt = p.get("point")
    if market in ("spreads", "spreads_h1") and pt is not None:
        try:
            pt = abs(float(pt))
        except (TypeError, ValueError):
            pass
    if market in ("h2h", "h2h_h1"):
        pt = None  # moneyline is always two-sided with no point
    return (sport, event, market, pt)


def upsert_agent_pick(pick: dict, path: Optional[str] = None) -> Optional[dict]:
    """Insert an agent pick, deduping against both exact and opposing-side matches.

    Returns the stored row on insert, None when skipped. Rules:
     - Exact-match (same side) is a no-op skip.
     - Opposing-side match (same market slot, other side) keeps whichever
       side has the higher Pinnacle fair prob. If the stored row is a loser,
       it gets replaced by the better side; otherwise the incoming pick is
       dropped. This prevents the ledger from ever carrying both sides of
       the same two-way market, which was a user-reported bug.
    """
    p = dict(pick or {})
    p.setdefault("source", "agent-auto")
    file_path = path or _default_path()
    picks = _load(file_path)

    exact = _dedup_key(p)
    slot = _market_slot(p)
    new_fp = float(p.get("projected_prob") or 0)

    for i, existing in enumerate(picks):
        # Only compare against still-open or just-settled agent rows; manually
        # logged rows are the user's intentional decisions and we never touch them.
        if (existing.get("source") or "manual") != "agent-auto":
            continue
        if _dedup_key(existing) == exact:
            return None  # exact-side duplicate
        if _market_slot(existing) != slot:
            continue
        # Opposing-side hit on the same market slot. Keep the Pinnacle-favored
        # side; if it's already there, drop the incoming pick.
        existing_fp = float(existing.get("projected_prob") or 0)
        if new_fp > existing_fp + 1e-9:
            # Replace in place so dates/CLV history continue to live under one id.
            picks[i] = {**existing, **p, "id": existing.get("id") or p.get("id")}
            _save(file_path, picks)
            return picks[i]
        return None
    return add_pick(p, path=file_path)


def collapse_duplicate_sides(path: Optional[str] = None) -> int:
    """One-shot cleanup: fold opposing agent-logged sides into one row.

    Picks logged before the two-way collapse fix landed can still show both
    sides on the ledger. Group by market slot and keep the side with the
    higher Pinnacle fair prob (projected_prob); drop the rest. Only touches
    rows tagged source="agent-auto" — manual logs are left alone.
    Returns the number of rows removed.
    """
    file_path = path or _default_path()
    picks = _load(file_path)
    by_slot: dict = {}
    others: list = []
    for p in picks:
        if (p.get("source") or "manual") != "agent-auto":
            others.append(p)
            continue
        slot = _market_slot(p)
        prev = by_slot.get(slot)
        if prev is None:
            by_slot[slot] = p
            continue
        prev_fp = float(prev.get("projected_prob") or 0)
        cur_fp = float(p.get("projected_prob") or 0)
        if cur_fp > prev_fp + 1e-9:
            by_slot[slot] = p
        # else keep prev; cur is dropped
    kept_agent = list(by_slot.values())
    removed = len(picks) - len(others) - len(kept_agent)
    if removed > 0:
        _save(file_path, others + kept_agent)
    return removed


def auto_log_agent_edges(edges: list, settings: dict, path: Optional[str] = None) -> int:
    """Write every funded edge into the ledger (dedup-aware), then settle.

    Returns the number of new rows inserted. Called from the dashboard every
    time the agent panel re-renders, so the ledger ends up being a complete
    audit trail of what the agent recommended — not just the picks the user
    bothered to click "Log" on.
    """
    if not edges:
        return 0
    kalshi_cash = float(settings.get("kalshi_cash") or 0) or 1.0
    unit_pct = float(settings.get("unit_pct") or 0.01) or 0.01
    unit_dollars = kalshi_cash * unit_pct
    inserted = 0
    for e in edges:
        if (e.get("stake_dollars") or 0) <= 0:
            continue  # don't track cap-dropped / rounded-to-zero rows
        espn_eid = e.get("espn_event_id") or e.get("event_id")
        matchup = e.get("matchup") or ""
        best_book = e.get("book") or "?"
        edge_pct = (e.get("edge") or 0) * 100.0
        stake_dollars = float(e.get("stake_dollars") or 0)
        risk_units = round(stake_dollars / max(1e-6, unit_dollars), 2)
        row = {
            "sport": (e.get("_sport") or "").lower(),
            "event_id": espn_eid,
            "kalshi_ticker": e.get("kalshi_ticker"),
            "event_name": matchup,
            "market": e.get("market") or "h2h",
            "selection": e.get("side_label") or "",
            "point": e.get("point"),
            "price_american": e.get("price"),
            "projected_prob": e.get("fair_prob"),
            "implied_prob": e.get("book_implied"),
            "risk_units": risk_units,
            "win_units": None,
            "methodology": "Agent · Pinnacle fair",
            "source": "agent-auto",
            "notes": (
                f"{best_book} @ {e.get('price')} vs Pinnacle · "
                f"edge {edge_pct:+.2f}pts · "
                f"Kalshi {int(e.get('kalshi_price_cents') or 0)}¢"
                if e.get("kalshi_price_cents") is not None else
                f"{best_book} @ {e.get('price')} vs Pinnacle · edge {edge_pct:+.2f}pts"
            ),
        }
        # If the game is already final/live, carry the current grade across so
        # the Ledger shows a status without waiting for the next auto-settle.
        grade = e.get("grade")
        if grade in ("win", "loss", "push"):
            row["status"] = grade
        if upsert_agent_pick(row, path=path):
            inserted += 1
    # One auto-settle sweep — grabs any games finished since the last tick.
    try:
        auto_settle(path=path)
    except Exception:
        pass
    return inserted


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
    """Query ESPN for each open pick's event; settle finals. Returns # settled.

    Each pick's `created_at` provides a date hint we pass to is_event_final so
    historical events are looked up on the right dated scoreboard. The ESPN
    "today" endpoint rolls old games off, so without this hint anything older
    than the current day never settles.
    """
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
        # Hint ESPN with the pick's date (Central-time YYYYMMDD) so dated
        # historical scoreboards are hit without the 7-day fallback sweep.
        date_hint = None
        created = p.get("created_at") or ""
        if len(created) >= 10:
            try:
                dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                date_hint = dt.strftime("%Y%m%d")
            except ValueError:
                date_hint = None
        final = espn.is_event_final(sport, event_id, date=date_hint)
        if not final:
            # Fall back to the no-date lookup so games on the current scoreboard
            # (or in the 7-day sweep) still resolve when the hint missed.
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
