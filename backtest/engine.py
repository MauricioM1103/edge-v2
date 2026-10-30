"""Backtest engine.

For each day in [start_date, end_date):
  1. Fetch historical odds snapshot ~1 hour before the first game of that day
     for the selected sport(s). Requires The Odds API's historical tier.
  2. Run calc.agent.scan_all on that snapshot, bankroll_cash = running balance.
  3. Rank edges by edge_pct descending; take top-N picks.
  4. Grade each pick by looking up final scores from ESPN for that day.
  5. Update running balance by stake * profit (if win) or -stake (if loss).

Caveats:
  - h2h: graded by winner.
  - spreads: graded by (margin + spread_point) sign.
  - totals: graded by (home_score + away_score) vs point.
  - Pushes (margin exactly 0 after spread) return the stake.
  - Games we can't find a final for are skipped (not counted W or L).
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from apis import odds_history, espn
from apis.team_mapping import canonical
from calc import agent as agent_calc
from calc.odds import profit_on_win, implied_prob_american


@dataclass
class BacktestPick:
    date: str
    sport: str
    matchup: str
    market: str
    side_label: str
    price: float
    book: str
    edge_pct: float
    stake: float
    fair_prob: float
    point: Optional[float] = None
    outcome: str = "pending"  # win | loss | push | nograde
    profit: float = 0.0

    def to_dict(self): return asdict(self)


@dataclass
class BacktestResult:
    start_bankroll: float
    end_bankroll: float
    picks: List[BacktestPick] = field(default_factory=list)
    daily: List[dict] = field(default_factory=list)  # [{date, bankroll_eod}]
    warnings: List[str] = field(default_factory=list)


def _snapshot_iso(d: datetime, hour_utc: int = 16) -> str:
    """ISO 8601 Z string at `hour_utc` on date d. 16:00 UTC = noon ET (most pre-game windows)."""
    s = d.replace(hour=hour_utc, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
    return s.strftime("%Y-%m-%dT%H:%M:%SZ")


def _settle_pick(pick: BacktestPick, final: dict) -> None:
    """Mutate pick.outcome + pick.profit based on ESPN final score."""
    hs = final.get("home_score")
    as_ = final.get("away_score")
    if hs is None or as_ is None:
        pick.outcome = "nograde"
        return

    sel = (pick.side_label or "").strip().lower()
    home_name = (final.get("home_name") or "").strip().lower()
    away_name = (final.get("away_name") or "").strip().lower()
    picked_home = home_name and (sel == home_name or sel.startswith(home_name))
    picked_away = away_name and (sel == away_name or sel.startswith(away_name))

    def _apply(won: bool, push: bool = False):
        if push:
            pick.outcome = "push"
            pick.profit = 0.0
            return
        if won:
            pick.outcome = "win"
            pick.profit = profit_on_win(pick.stake, pick.price) or 0.0
        else:
            pick.outcome = "loss"
            pick.profit = -pick.stake

    if pick.market == "h2h":
        if picked_home:
            _apply(hs > as_)
        elif picked_away:
            _apply(as_ > hs)
        else:
            pick.outcome = "nograde"
    elif pick.market == "spreads":
        p = pick.point
        if p is None:
            pick.outcome = "nograde"
            return
        if picked_home:
            m = (hs - as_) + float(p)
        elif picked_away:
            m = (as_ - hs) + float(p)
        else:
            pick.outcome = "nograde"
            return
        _apply(m > 0, push=(m == 0))
    elif pick.market == "totals":
        p = pick.point
        if p is None:
            pick.outcome = "nograde"
            return
        total = hs + as_
        p = float(p)
        if "over" in sel:
            _apply(total > p, push=(total == p))
        elif "under" in sel:
            _apply(total < p, push=(total == p))
        else:
            pick.outcome = "nograde"
    else:
        pick.outcome = "nograde"


def run(
    sports: List[str],
    start_bankroll: float = 100.0,
    days: int = 30,
    top_n_per_day: int = 5,
    kelly_mult: float = 0.25,
    min_edge_pct: float = 0.02,
    max_bet_pct: float = 0.10,
    unit_pct_of_bankroll: float = 0.10,
    fallback_book: str = "DraftKings",
    snapshot_hour_utc: int = 16,
    progress_cb=None,
) -> BacktestResult:
    """Run a backtest. Picks top `top_n_per_day` edges per day across sports."""
    bankroll = float(start_bankroll)
    result = BacktestResult(start_bankroll=bankroll, end_bankroll=bankroll)

    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=days)

    for i in range(days):
        d = start + timedelta(days=i)
        day_dt = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
        snapshot = _snapshot_iso(day_dt, snapshot_hour_utc)
        date_yyyymmdd = d.strftime("%Y%m%d")
        date_str = d.isoformat()

        all_edges = []
        for sp in sports:
            odds_sport_key = espn.ODDS_API_SPORT_KEYS.get(sp)
            if not odds_sport_key:
                continue
            snap = odds_history.historical_odds(odds_sport_key, snapshot)
            status = snap.get("status")
            if status == "no_key":
                result.warnings.append("No Odds API key configured; backtest cannot run.")
                return result
            if status in ("http_401", "http_402"):
                result.warnings.append(
                    "Your Odds API plan doesn't include historical data "
                    "(got {}). Backtest requires the historical-odds endpoint "
                    "(typically $30/mo+ tier).".format(status)
                )
                return result
            if status != "ok":
                result.warnings.append(f"[{date_str} {sp}] fetch failed ({status})")
                continue
            games = snap.get("data") or []
            edges = agent_calc.scan_all(
                games, bankroll_cash=bankroll,
                kalshi_probs_by_game={},  # no historical Kalshi; agent falls to DK/consensus
                fallback_book=fallback_book,
                kelly_mult=kelly_mult, min_edge_pct=min_edge_pct,
                min_books=1, max_bet_pct=max_bet_pct,
                markets=("h2h", "spreads", "totals"),
            )
            for e in edges:
                e["_sport"] = sp
            all_edges.extend(edges)

        all_edges.sort(key=lambda e: -e["edge"])
        day_picks = all_edges[:top_n_per_day]

        # Grade each pick
        finals_by_sport: Dict[str, dict] = {}
        for e in day_picks:
            sp = e["_sport"]
            if sp not in finals_by_sport:
                finals_by_sport[sp] = odds_history.grading_scores_from_espn(sp, date_yyyymmdd)
            key = (canonical(e.get("home_team")), canonical(e.get("away_team")))
            final = finals_by_sport[sp].get(key)
            pick = BacktestPick(
                date=date_str, sport=sp, matchup=e.get("matchup", ""),
                market=e.get("market", ""), side_label=e.get("side_label", ""),
                price=float(e.get("price", 0) or 0), book=e.get("book", ""),
                edge_pct=float(e.get("edge_pct", 0) or 0),
                stake=min(float(e.get("stake_dollars", 0) or 0), bankroll * max_bet_pct),
                fair_prob=float(e.get("fair_prob", 0) or 0),
                point=e.get("point"),
            )
            if final:
                _settle_pick(pick, final)
                if pick.outcome in ("win", "loss", "push"):
                    bankroll += pick.profit
            else:
                pick.outcome = "nograde"
            result.picks.append(pick)

        result.daily.append({"date": date_str, "bankroll_eod": round(bankroll, 2)})
        if progress_cb:
            try:
                progress_cb((i + 1) / days, f"Day {i+1}/{days}: {date_str}  bankroll ${bankroll:,.2f}")
            except Exception:
                pass

    result.end_bankroll = round(bankroll, 2)
    return result
