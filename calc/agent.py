"""Automated edge scanner.

Given a list of games with full bookmaker odds from The Odds API, finds
+EV plays by comparing each book's price to the no-vig consensus of all
books combined. Returns bet-size recommendations in actual dollars using
the user's live Kalshi cash and a Kelly multiplier.

How the edge is detected:
  1. For each market (h2h / spreads / totals), collect every book's prices
     for both sides.
  2. Compute each book's own two-way no-vig fair probability.
  3. Compute a consensus "true" probability = average of all books' no-vig
     fair probabilities (less noisy than any single book).
  4. For each book, each side: if that book's offered price implies a
     probability LOWER than the consensus fair prob, you are getting +EV
     on that side at that book.
  5. Edge = consensus_fair_prob - book_implied_prob.
  6. Kelly stake = kelly_fraction(consensus_fair_prob, book_price, kelly_mult)
     * bankroll_cash.

Thresholds:
  min_edge_pct    - skip edges smaller than this (default 2%).
  min_books       - require at least N books in the consensus (default 3).
  max_bet_pct     - clamp any single stake to X% of bankroll (default 5%).
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from calc.odds import (
    implied_prob_american, no_vig_two_way, american_to_decimal, format_american,
)
from calc.kelly import kelly_fraction


def _no_vig_prob(home_price, away_price) -> Tuple[Optional[float], Optional[float]]:
    return no_vig_two_way(home_price, away_price)


def _consensus_fair(prices_pairs: List[Tuple[float, float]]) -> Tuple[Optional[float], Optional[float]]:
    """Average of per-book no-vig fair probabilities. (home, away) or (None, None)."""
    hs, as_ = [], []
    for h, a in prices_pairs:
        ph, pa = no_vig_two_way(h, a)
        if ph is not None and pa is not None:
            hs.append(ph)
            as_.append(pa)
    if not hs:
        return (None, None)
    return (sum(hs) / len(hs), sum(as_) / len(as_))


def scan_game(
    game: dict,
    market_key: str,
    bankroll_cash: float,
    kelly_mult: float = 0.25,
    min_edge_pct: float = 0.02,
    min_books: int = 3,
    max_bet_pct: float = 0.05,
) -> List[dict]:
    """Return list of edge dicts for one game, one market."""
    if not game or not isinstance(game, dict):
        return []
    home_team = game.get("home_team")
    away_team = game.get("away_team")
    books = game.get("bookmakers") or []
    if not books:
        return []

    # 1. Collect each book's (home_price, away_price) for this market.
    per_book: Dict[str, Dict[str, float]] = {}
    for b in books:
        bname = b.get("title") or b.get("key") or ""
        for m in (b.get("markets") or []):
            if m.get("key") != market_key:
                continue
            hp = ap = None
            hpt = apt = None
            for o in (m.get("outcomes") or []):
                price = o.get("price")
                point = o.get("point")
                if price is None:
                    continue
                try:
                    price = float(price)
                except (TypeError, ValueError):
                    continue
                if o.get("name") == home_team:
                    hp = price
                    hpt = point
                elif o.get("name") == away_team:
                    ap = price
                    apt = point
                # for totals the names are "Over"/"Under"
                elif o.get("name") == "Over":
                    hp = price  # treat Over as "home" side for consensus math
                    hpt = point
                elif o.get("name") == "Under":
                    ap = price
                    apt = point
            if hp is not None and ap is not None:
                per_book[bname] = {"home": hp, "away": ap, "home_pt": hpt, "away_pt": apt}

    if len(per_book) < min_books:
        return []

    pairs = [(d["home"], d["away"]) for d in per_book.values()]
    cfh, cfa = _consensus_fair(pairs)
    if cfh is None:
        return []

    results = []
    max_stake = bankroll_cash * max_bet_pct

    for bname, d in per_book.items():
        for side_key, side_label, book_price, fair_prob in [
            ("home", home_team if market_key != "totals" else "Over", d["home"], cfh),
            ("away", away_team if market_key != "totals" else "Under", d["away"], cfa),
        ]:
            book_imp = implied_prob_american(book_price)
            if book_imp is None:
                continue
            edge = fair_prob - book_imp
            if edge < min_edge_pct:
                continue
            kf = kelly_fraction(fair_prob, book_price, kelly_mult) or 0.0
            raw_stake = bankroll_cash * kf
            stake = min(raw_stake, max_stake)
            results.append({
                "book": bname,
                "side_key": side_key,
                "side_label": side_label or side_key.upper(),
                "price": book_price,
                "book_implied": book_imp,
                "fair_prob": fair_prob,
                "edge": edge,
                "edge_pct": edge * 100.0,
                "kelly_frac": kf,
                "stake_dollars": stake,
                "point": d.get(f"{side_key}_pt"),
                "market": market_key,
            })
    results.sort(key=lambda r: -r["edge"])
    return results


def scan_all(
    odds_games: list,
    bankroll_cash: float,
    kelly_mult: float = 0.25,
    min_edge_pct: float = 0.02,
    min_books: int = 3,
    max_bet_pct: float = 0.05,
    markets: Tuple[str, ...] = ("h2h", "spreads", "totals"),
) -> List[dict]:
    """Scan every game, every market, return flat list of edge opportunities.

    Each item carries the game identity so the UI can show matchup + bookmaker.
    """
    try:
        bankroll_cash = float(bankroll_cash) if bankroll_cash is not None else 0.0
    except (TypeError, ValueError):
        bankroll_cash = 0.0
    if bankroll_cash <= 0:
        return []
    out = []
    for g in odds_games or []:
        for mk in markets:
            edges = scan_game(
                g, mk, bankroll_cash,
                kelly_mult=kelly_mult,
                min_edge_pct=min_edge_pct,
                min_books=min_books,
                max_bet_pct=max_bet_pct,
            )
            for e in edges:
                e["event_id"] = g.get("id")
                e["commence_time"] = g.get("commence_time")
                e["home_team"] = g.get("home_team")
                e["away_team"] = g.get("away_team")
                e["matchup"] = f"{g.get('away_team','?')} @ {g.get('home_team','?')}"
                out.append(e)
    out.sort(key=lambda r: -r["edge"])
    return out


def summarize_edges(edges: List[dict]) -> dict:
    if not edges:
        return {"count": 0, "best_edge": None, "total_stake": 0.0, "avg_edge": None}
    total = sum(e.get("stake_dollars", 0.0) for e in edges)
    best = max(e["edge"] for e in edges)
    avg = sum(e["edge"] for e in edges) / len(edges)
    return {
        "count": len(edges),
        "best_edge": best,
        "avg_edge": avg,
        "total_stake": total,
    }
