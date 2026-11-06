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

Agent-logged player props add market_family='prop', player, point and a
prop `market` key (player_points, batter_hits, ...). They grade from the
ESPN box score (apis.espn_boxscore) once the game is final; their
result_payload carries {stat, point, side, player, stat_label, settled_at}
so the Ledger can show the actual number against the line.
"""
from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from typing import List, Optional

from apis import espn
from apis import espn_boxscore
from apis.team_mapping import same_team
from calc.odds import profit_on_win, implied_prob_american
from calc.clv import clv_percent

# Central US slate anchor. A pick's slate date is derived from the game's
# start time in CT so that late-night games (00:30 UTC on 10-26 = 19:30 CT on
# 10-25) correctly roll back to the Oct 25 slate, which is how sportsbooks
# and bettors talk about days.
try:
    from zoneinfo import ZoneInfo
    _CT = ZoneInfo("America/Chicago")
except Exception:
    _CT = timezone.utc


def _slate_date_from(pick: dict) -> Optional[str]:
    """Resolve the Central-US slate date for a pick as YYYY-MM-DD.

    Preference order:
      1. Pre-set `pick_date` (caller passed it).
      2. Game start (`start_utc` or `commence_time`): the slate the game
         actually belongs to — survives timezone math around midnight UTC.
      3. `created_at`: when the row was written. Fallback only; a pick logged
         late at night from a West-coast user could otherwise land on the
         wrong Central day.
    """
    for src in ("pick_date",):
        v = pick.get(src)
        if v:
            if isinstance(v, str) and len(v) >= 10:
                return v[:10]
    for src in ("start_utc", "commence_time"):
        v = pick.get(src)
        if v is None:
            continue
        try:
            if isinstance(v, datetime):
                dt = v
            else:
                dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
            return dt.astimezone(_CT).strftime("%Y-%m-%d")
        except (ValueError, TypeError, AttributeError):
            continue
    created = pick.get("created_at")
    if created:
        try:
            dt = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
            return dt.astimezone(_CT).strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            pass
    return None


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
    # Anchor the pick to a Central-US slate date so look-back by date works.
    if not p.get("pick_date"):
        sd = _slate_date_from(p)
        if sd:
            p["pick_date"] = sd
    file_path = path or _default_path()
    picks = _load(file_path)
    picks.append(p)
    _save(file_path, picks)
    return p


def backfill_pick_dates(path: Optional[str] = None) -> int:
    """One-shot migration: fill pick_date on any legacy row missing it.

    Returns the number of rows updated. Idempotent — rows that already have
    pick_date are untouched, so this is safe to call on every dashboard load.
    """
    file_path = path or _default_path()
    picks = _load(file_path)
    changed = 0
    for p in picks:
        if p.get("pick_date"):
            continue
        sd = _slate_date_from(p)
        if sd:
            p["pick_date"] = sd
            changed += 1
    if changed:
        _save(file_path, picks)
    return changed


def group_by_date(picks: Optional[List[dict]] = None, path: Optional[str] = None) -> List[dict]:
    """Return per-slate-date summaries, newest first.

    Each summary carries: date (YYYY-MM-DD), count, record (W-L-P), risk/net
    in units, roi, mean CLV%, and the raw picks list for drill-down. Pure
    aggregation — reads from the ledger, does not mutate it.
    """
    rows = picks if picks is not None else list_picks(path)
    buckets: dict = {}
    for p in rows:
        d = p.get("pick_date") or _slate_date_from(p)
        if not d:
            continue
        buckets.setdefault(d, []).append(p)
    summaries = []
    for date, items in buckets.items():
        wins = sum(1 for p in items if p.get("status") == "win")
        losses = sum(1 for p in items if p.get("status") == "loss")
        pushes = sum(1 for p in items if p.get("status") == "push")
        opens = sum(1 for p in items if (p.get("status") or "open") == "open")
        risked = 0.0
        net = 0.0
        for p in items:
            try:
                risk_u = float(p.get("risk_units") or 0)
                win_u = float(p.get("win_units") or 0)
            except (TypeError, ValueError):
                continue
            status = p.get("status") or "open"
            if status in ("win", "loss", "push"):
                risked += risk_u
            if status == "win":
                net += win_u
            elif status == "loss":
                net -= risk_u
        clv_vals = [float(p["clv_pct"]) for p in items if p.get("clv_pct") is not None]
        summaries.append({
            "date": date,
            "count": len(items),
            "wins": wins, "losses": losses, "pushes": pushes, "open": opens,
            "risked_units": round(risked, 2),
            "net_units": round(net, 2),
            "roi": (net / risked) if risked > 0 else None,
            "avg_clv_pct": (sum(clv_vals) / len(clv_vals)) if clv_vals else None,
            "picks": items,
        })
    summaries.sort(key=lambda s: s["date"], reverse=True)
    return summaries


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
    # Player props: the slot is per player — "LeBron Over 24.5" and "Curry
    # Over 24.5" share event/market/point but are not opposing sides.
    player = (p.get("player") or "").strip().lower() or None
    return (sport, event, market, pt, player)


def upsert_agent_pick(pick: dict, path: Optional[str] = None) -> Optional[dict]:
    """Insert an agent pick, deduping against both exact and opposing-side matches.

    Returns the stored row on insert, None when skipped. Rules:
     - Exact-match (same side) refreshes `last_seen_on_board` so the Results
       tab can tell "still on the Board" from "dropped off"; no new row.
     - Opposing-side match (same market slot, other side) keeps whichever
       side has the higher Pinnacle fair prob. If the stored row is a loser,
       it gets replaced by the better side; otherwise the incoming pick is
       dropped. This prevents the ledger from ever carrying both sides of
       the same two-way market, which was a user-reported bug.
    """
    p = dict(pick or {})
    p.setdefault("source", "agent-auto")
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    p.setdefault("first_seen_on_board", now_iso)
    p.setdefault("last_seen_on_board", now_iso)
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
            # Same side logged again this scan: treat it as "still on the
            # board" rather than a duplicate silently dropped. Refresh
            # last_seen so Results can flag picks that later drop off.
            existing["last_seen_on_board"] = p.get("last_seen_on_board") or now_iso
            if not existing.get("first_seen_on_board"):
                existing["first_seen_on_board"] = p.get("first_seen_on_board") or existing.get("created_at") or now_iso
            _save(file_path, picks)
            return None
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


#: Agent-pick tier cutoffs: edge in [0,1] space (not percent). A ≥ 2%, B ≥ 1%,
#: C > 0, else Watch. Only A-tier picks enter the ledger - everything below
#: that is still scanned, still shown on the Board, just not recorded, so the
#: ledger is a working record of the agent's strongest signals.
TIER_A_EDGE = 0.020


def _edge_from_pick(pick: dict) -> float:
    """Edge (fair_prob - book_implied) reconstructed from a stored pick row.

    Agent rows carry both; a missing/zero field gives 0 so the pick is not
    counted toward tier A on incomplete data. Props store the same fields,
    so they tier the same way.
    """
    try:
        fair = float(pick.get("projected_prob") or 0)
        imp = float(pick.get("implied_prob") or 0)
    except (TypeError, ValueError):
        return 0.0
    return fair - imp


def is_tier_a(pick: dict) -> bool:
    """True when a stored agent pick would have been A-tier when logged."""
    return _edge_from_pick(pick) >= TIER_A_EDGE


#: How long an A-tier pick can go unseen on the Board before Results flags
#: it as "removed from board". Agent scans run every 30s by default, so a
#: 10-minute grace is well past any transient scan hiccup — a gap this big
#: means the pick really did drop off (line moved, odds pulled, or edge
#: collapsed below tier A).
BOARD_STALE_MINUTES = 10


def was_removed_from_board(pick: dict, now: Optional[datetime] = None,
                           grace_minutes: int = BOARD_STALE_MINUTES) -> bool:
    """True when an agent pick was logged, then dropped off the Board.

    Only meaningful for still-open rows — settled rows naturally fall out
    of the Board regardless of whether their line survived. Returns False
    when `last_seen_on_board` is missing (legacy rows logged before the
    timestamp existed) so we don't retroactively label every old pick.
    """
    status = (pick.get("status") or "open").lower()
    if status != "open":
        return False
    last_seen = pick.get("last_seen_on_board")
    if not last_seen:
        return False
    try:
        if isinstance(last_seen, str):
            last_seen = datetime.fromisoformat(last_seen.replace("Z", "+00:00"))
        if last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError, AttributeError):
        return False
    now = now or datetime.now(timezone.utc)
    return (now - last_seen).total_seconds() / 60.0 > grace_minutes


def auto_log_agent_edges(edges: list, settings: dict, path: Optional[str] = None) -> int:
    """Write every A-tier funded edge into the ledger, then settle.

    Returns the number of new rows inserted. Only tier A (edge >= 2%) is
    recorded so the ledger stays a high-signal record of the agent's best
    picks; B / C / Watch rows still show on the Board but aren't stored.
    Called from the dashboard every time the agent panel re-renders.

    Every A-tier row on the board also refreshes its stored
    `last_seen_on_board` timestamp (via upsert_agent_pick) so the Results
    tab can distinguish picks still live on the Board from ones that have
    since dropped off — the line moved, the game started, or the edge
    collapsed below tier A — regardless of whether the drop happened
    pre-game or in the run of play.
    """
    if not edges:
        return 0
    kalshi_cash = float(settings.get("kalshi_cash") or 0) or 1.0
    unit_pct = float(settings.get("unit_pct") or 0.01) or 0.01
    unit_dollars = kalshi_cash * unit_pct
    inserted = 0
    scan_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for e in edges:
        if (e.get("stake_dollars") or 0) <= 0:
            continue  # don't track cap-dropped / rounded-to-zero rows
        if float(e.get("edge") or 0) < TIER_A_EDGE:
            continue  # tier-B / C / Watch — kept on the Board, kept out of the ledger
        espn_eid = e.get("espn_event_id") or e.get("event_id")
        matchup = e.get("matchup") or ""
        best_book = e.get("book") or "?"
        edge_pct = (e.get("edge") or 0) * 100.0
        stake_dollars = float(e.get("stake_dollars") or 0)
        risk_units = round(stake_dollars / max(1e-6, unit_dollars), 2)
        # Pass start_utc / commence_time through so _slate_date_from can
        # anchor the pick to the correct Central-US slate even if the row is
        # written hours after kickoff.
        start_val = e.get("start_utc") or e.get("commence_time")
        if isinstance(start_val, datetime):
            start_val = start_val.isoformat()
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
            "start_utc": start_val,
            # pick_type: "pre_game" when the agent surfaced it before kickoff,
            # "live_pick" when the game was already underway at scan time.
            # Results carries this forward so in-play agent decisions stay
            # visually distinct from pre-game ones in the audit trail.
            "pick_type": "live_pick" if (e.get("status_state") == "in") else "pre_game",
            # Board-presence timestamps: every scan that still finds this
            # pick in the A-tier set bumps last_seen_on_board. Results uses
            # a stale last_seen on an open row to flag "removed from board".
            "first_seen_on_board": scan_iso,  # kept only on first insert
            "last_seen_on_board": scan_iso,
            # Player props carry the player so the ledger's opposing-side
            # dedup keys per player, and so Results can label the row. The
            # scoreboard has no player stats, so auto_settle grades them from
            # the ESPN box score (apis.espn_boxscore) once the game is final.
            "market_family": "prop" if e.get("is_prop") else "game",
            "player": e.get("prop_player"),
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


_H2H_LABEL_TAIL = re.compile(r"\s+(?:wins?|to win|ml|moneyline)\s*$", re.I)


def _h2h_team(selection) -> str:
    """Team named by a moneyline selection, without the label tail.

    Agent rows are logged as "Phoenix Suns wins" (Kalshi-style label); manual
    rows and the offshore scanner use the bare team name. Both must resolve
    to the team so the ESPN final can be matched against it.
    """
    return _H2H_LABEL_TAIL.sub("", (selection or "").strip()).strip()


def _match_side(team, home_name, away_name) -> Optional[str]:
    """Which side of an ESPN final a team label names: "home", "away" or None.

    Match order: exact name -> apis.team_mapping.same_team (tolerates Odds
    API vs ESPN naming, "LA Clippers" vs "Los Angeles Clippers") -> loose
    containment either direction. A tier only decides when exactly one side
    matches; a label that fits both is passed down and finally left
    unresolved rather than guessed.
    """
    team = (team or "").strip().lower()
    home_name = (home_name or "").strip().lower()
    away_name = (away_name or "").strip().lower()
    if not team:
        return None
    tiers = (
        (bool(home_name) and team == home_name,
         bool(away_name) and team == away_name),
        (bool(home_name) and same_team(team, home_name),
         bool(away_name) and same_team(team, away_name)),
        (bool(home_name) and (team in home_name or home_name in team),
         bool(away_name) and (team in away_name or away_name in team)),
    )
    for is_home, is_away in tiers:
        if is_home != is_away:
            return "home" if is_home else "away"
    return None


def _settle_h2h(pick, final):
    """Set status on a moneyline pick given ESPN final scores."""
    home_won = final.get("home_won")
    if home_won is None:
        return
    side = _match_side(_h2h_team(pick.get("selection")), final.get("home_name"), final.get("away_name"))
    if side == "home":
        pick["status"] = "win" if home_won else "loss"
    elif side == "away":
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


_SPREAD_LABEL_TAIL = re.compile(r"\s*[+\-−]?\s*\d+(?:\.\d+)?\s*$")


def _spread_team(selection) -> str:
    """Team named by a spread selection, without the trailing line."""
    return _SPREAD_LABEL_TAIL.sub("", (selection or "").strip()).strip()


_DNB_LABEL_TAIL = re.compile(r"\s*\(\s*dnb\s*\)\s*$|\s+(?:dnb|draw\s*no\s*bet)\s*$", re.I)


def _dnb_team(selection) -> str:
    """Team named by a draw-no-bet selection, without the '(DNB)' marker.

    The scanner labels DNB rows as e.g. "Arsenal (DNB)" or "Arsenal" -
    strip that tail so the same team-matching ladder as moneylines decides
    which side of the ESPN final we're on.
    """
    sel = _DNB_LABEL_TAIL.sub("", (selection or "").strip()).strip()
    return sel


def _btts_side(selection) -> Optional[str]:
    """Which half of BTTS a selection names: "yes" | "no" | None.

    Tolerates the human labels ("Both to score: Yes", "BTTS No") and bare
    "Yes" / "No" rows. A selection that matches neither word returns None
    so the pick stays open rather than being guessed into a side.
    """
    sel = (selection or "").lower()
    has_yes = re.search(r"\byes\b", sel) is not None
    has_no = re.search(r"\bno\b", sel) is not None
    if has_yes and not has_no:
        return "yes"
    if has_no and not has_yes:
        return "no"
    return None


def _settle_btts(pick, final):
    """Settle a Both Teams To Score pick from a soccer final."""
    hs = final.get("home_score")
    ascore = final.get("away_score")
    if hs is None or ascore is None:
        return
    side = _btts_side(pick.get("selection"))
    if side is None:
        return
    try:
        both_scored = int(hs) > 0 and int(ascore) > 0
    except (TypeError, ValueError):
        return
    if side == "yes":
        pick["status"] = "win" if both_scored else "loss"
    else:  # "no"
        pick["status"] = "win" if not both_scored else "loss"


def _settle_dnb(pick, final):
    """Settle a Draw No Bet pick. Draws are a push (stake refunded)."""
    hs = final.get("home_score")
    ascore = final.get("away_score")
    if hs is None or ascore is None:
        return
    if hs == ascore:
        pick["status"] = "push"
        return
    home_won = final.get("home_won")
    if home_won is None:
        return
    side = _match_side(_dnb_team(pick.get("selection")),
                       final.get("home_name"), final.get("away_name"))
    if side == "home":
        pick["status"] = "win" if home_won else "loss"
    elif side == "away":
        pick["status"] = "win" if not home_won else "loss"


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
    # Selection carries the line ("Arizona Wildcats -31", "Celtics +3.5");
    # strip it and resolve the team the same way moneylines do, so Odds API
    # vs ESPN naming drift can't leave a spread open forever.
    side = _match_side(_spread_team(pick.get("selection")), final.get("home_name"), final.get("away_name"))
    if side == "home":
        margin = hs - ascore + p
    elif side == "away":
        margin = ascore - hs + p
    else:
        return
    if margin > 0:
        pick["status"] = "win"
    elif margin < 0:
        pick["status"] = "loss"
    else:
        pick["status"] = "push"


def grade_prop(stat, point, side: str) -> Optional[str]:
    """Over/Under rule for a player prop: 'win' | 'loss' | 'push' | None.

    Over wins when stat > point, Under wins when stat < point, equal is a
    push. None when any input is missing/unparseable (caller leaves it open).
    """
    if stat is None or point is None:
        return None
    try:
        s = float(stat)
        pt = float(point)
    except (TypeError, ValueError):
        return None
    side = (side or "").strip().lower()
    if side not in ("over", "under"):
        return None
    if abs(s - pt) < 1e-9:
        return "push"
    if side == "over":
        return "win" if s > pt else "loss"
    return "win" if s < pt else "loss"


_PROP_SIDE_RE = re.compile(r"\b(over|under)\b", re.I)
_PROP_SELECTION_RE = re.compile(r"^(?P<player>.+?)\s+(?P<side>over|under)\s+[-+]?\d+(?:\.\d+)?\s*$", re.I)


def _prop_side(pick: dict) -> Optional[str]:
    side = (pick.get("side") or "").strip().lower()
    if side in ("over", "under"):
        return side
    m = _PROP_SIDE_RE.search(pick.get("selection") or "")
    return m.group(1).lower() if m else None


def _prop_player(pick: dict) -> str:
    player = (pick.get("player") or "").strip()
    if player:
        return player
    m = _PROP_SELECTION_RE.match((pick.get("selection") or "").strip())
    return m.group("player").strip() if m else ""


def _settle_prop(pick: dict, final: dict) -> bool:
    """Grade one agent-logged player prop from the ESPN box score.

    Called only once the game is final. Returns True when the row changed
    (win / loss / push / void). Leaves the row open — returning False — when
    the box score isn't readable yet so the next sweep retries. Void is
    reserved for cases where there is no number to grade against: the player
    did not play / is not on the box score, or ESPN has no column for the
    stat; the reason is kept in result_payload so the Ledger can show it.
    """
    sport = (pick.get("sport") or "").lower()
    event_id = str(pick.get("event_id") or "")
    market = (pick.get("market") or "").lower()
    side = _prop_side(pick)
    player = _prop_player(pick)
    point = pick.get("point")
    if not (sport and event_id and market and side and player) or point is None:
        return False
    try:
        res = espn_boxscore.lookup_player_stat(sport, event_id, player, market)
    except Exception:
        return False
    status = res.get("status")
    if status == "unavailable":
        return False
    payload = {
        "stat": res.get("value"),
        "point": point,
        "side": side,
        "player": res.get("player") or player,
        "stat_label": res.get("label") or "",
        "reason": res.get("detail") or "",
        "home_score": final.get("home_score"),
        "away_score": final.get("away_score"),
        "settled_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if status == "ok":
        grade = grade_prop(res.get("value"), point, side)
        if grade is None:
            return False
        pick["status"] = grade
    elif status in ("dnp", "not_found", "unsupported"):
        pick["status"] = "void"
    else:
        return False
    pick["result_payload"] = payload
    return True


def auto_settle(path: Optional[str] = None) -> int:
    """Query ESPN for each open pick's event; settle finals. Returns # settled.

    Each pick's `created_at` provides a date hint we pass to is_event_final so
    historical events are looked up on the right dated scoreboard. The ESPN
    "today" endpoint rolls old games off, so without this hint anything older
    than the current day never settles.

    Game lines (h2h / spreads / totals) grade from the scoreboard. Agent-logged
    player props (market_family == "prop") grade from the ESPN box score via
    _settle_prop once the same final check passes; manually logged props are
    the user's own records and are never auto-graded. Idempotent: only rows
    still 'open' are considered.
    """
    file_path = path or _default_path()
    picks = _load(file_path)
    settled = 0
    # One scoreboard lookup per (sport, event, date) per sweep — several props
    # on the same game would otherwise each re-fetch the same scoreboard.
    finals_memo: dict = {}
    for p in picks:
        if p.get("status") not in (None, "open"):
            continue
        is_prop = (p.get("market_family") or "game") == "prop"
        sport = p.get("sport")
        event_id = p.get("event_id")
        if not sport or not event_id:
            continue
        if is_prop:
            if (p.get("source") or "manual") != "agent-auto":
                continue  # manual prop rows are the user's own decisions
            if not str(event_id).isdigit():
                continue  # Odds API id (no ESPN match at scan time) — can't look it up
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
        memo_key = (str(sport).lower(), str(event_id), date_hint)
        if memo_key in finals_memo:
            final = finals_memo[memo_key]
        else:
            final = espn.is_event_final(sport, event_id, date=date_hint)
            if not final:
                # Fall back to the no-date lookup so games on the current scoreboard
                # (or in the 7-day sweep) still resolve when the hint missed.
                final = espn.is_event_final(sport, event_id)
            finals_memo[memo_key] = final
        if not final or not final.get("is_final"):
            continue
        if is_prop:
            if _settle_prop(p, final) and p.get("status") in ("win", "loss", "push"):
                settled += 1
            continue
        market = (p.get("market") or "h2h").lower()
        if market == "h2h":
            _settle_h2h(p, final)
        elif market == "totals":
            _settle_totals(p, final)
        elif market == "spreads":
            _settle_spreads(p, final)
        elif market == "btts":
            _settle_btts(p, final)
        elif market == "draw_no_bet":
            _settle_dnb(p, final)
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
