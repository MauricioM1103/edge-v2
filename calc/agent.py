"""Automated edge scanner.

Approach (per user spec, oct-2026):
  * Fair probability = Pinnacle's no-vig two-way probability.
    Pinnacle is the sharpest sportsbook in the industry - its line is the
    tightest proxy we have for the market's "true" probability. Stripping
    the small vig off Pinnacle gives a clean fair reference.
  * Fallbacks (in order) when Pinnacle isn't in the Odds API response for
    this game or market:
       1. Kalshi YES-mid (exchange, no vig)   -- h2h only
       2. Fallback book (default DraftKings)  no-vig
       3. Multi-book consensus                no-vig
  * Scan every bookmaker, INCLUDING Kalshi (synthesized from kalshi_prob_home).
    An "edge" exists when a book's offered price implies a probability LOWER
    than the fair prob. Edge = fair_prob - book_implied_prob.
  * Stake = kelly_fraction(fair_prob, book_price) * bankroll_cash * kelly_mult,
    capped at max_bet_pct of bankroll.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from calc.odds import (
    implied_prob_american, no_vig_two_way, format_american,
)
from calc.kelly import kelly_fraction


def _no_vig(home_price, away_price) -> Tuple[Optional[float], Optional[float]]:
    return no_vig_two_way(home_price, away_price)


def _consensus_fair(prices_pairs: List[Tuple[float, float]]) -> Tuple[Optional[float], Optional[float]]:
    hs, as_ = [], []
    for h, a in prices_pairs:
        ph, pa = no_vig_two_way(h, a)
        if ph is not None and pa is not None:
            hs.append(ph)
            as_.append(pa)
    if not hs:
        return (None, None)
    return (sum(hs) / len(hs), sum(as_) / len(as_))


def _book_prices(game: dict, book_name_or_key: str, market_key: str) -> Optional[dict]:
    """Pull a specific book's prices from a game (home/away in native order)."""
    if not game or not book_name_or_key:
        return None
    target = book_name_or_key.lower()
    home_team = game.get("home_team")
    away_team = game.get("away_team")
    for b in (game.get("bookmakers") or []):
        bk = (b.get("key") or "").lower()
        bt = (b.get("title") or "").lower()
        if target != bk and target != bt and target not in bk and target not in bt:
            continue
        for m in (b.get("markets") or []):
            if m.get("key") != market_key:
                continue
            hp = ap = None
            hpt = apt = None
            for o in (m.get("outcomes") or []):
                price, point, name = o.get("price"), o.get("point"), o.get("name")
                if price is None:
                    continue
                try:
                    price = float(price)
                except (TypeError, ValueError):
                    continue
                if name == home_team or name == "Over":
                    hp, hpt = price, point
                elif name == away_team or name == "Under":
                    ap, apt = price, point
            return {"home": hp, "away": ap, "home_pt": hpt, "away_pt": apt}
    return None


def _fair_prob_for(
    game: dict, market_key: str,
    kalshi_prob_home: Optional[float], fallback_book: str,
) -> Tuple[Optional[float], Optional[float], str]:
    """Return (fair_home, fair_away, source_label).

    Preference order:
      1. Pinnacle no-vig  (sharpest book - our best fair reference)
      2. Kalshi YES-mid   (h2h only; exchange, no vig)
      3. fallback_book no-vig (default DraftKings)
      4. multi-book consensus no-vig
    """
    # 1. Pinnacle no-vig fair (works for every market).
    pin = _book_prices(game, "Pinnacle", market_key)
    if pin and pin.get("home") is not None and pin.get("away") is not None:
        ph, pa = no_vig_two_way(pin["home"], pin["away"])
        if ph is not None:
            return (ph, pa, "Pinnacle no-vig")

    # 2. Kalshi (moneyline only - Kalshi has no spreads/totals markets).
    if market_key == "h2h" and kalshi_prob_home is not None:
        return (kalshi_prob_home, 1.0 - kalshi_prob_home, "Kalshi")

    # 3. Fallback book's no-vig fair
    fb = _book_prices(game, fallback_book, market_key)
    if fb and fb.get("home") is not None and fb.get("away") is not None:
        ph, pa = no_vig_two_way(fb["home"], fb["away"])
        if ph is not None:
            return (ph, pa, f"{fallback_book} no-vig")

    # 4. Multi-book consensus
    books = game.get("bookmakers") or []
    pairs = []
    for b in books:
        for m in (b.get("markets") or []):
            if m.get("key") != market_key:
                continue
            hp = ap = None
            for o in (m.get("outcomes") or []):
                price = o.get("price")
                if price is None:
                    continue
                try:
                    price = float(price)
                except (TypeError, ValueError):
                    continue
                nm = o.get("name")
                if nm == game.get("home_team") or nm == "Over":
                    hp = price
                elif nm == game.get("away_team") or nm == "Under":
                    ap = price
            if hp is not None and ap is not None:
                pairs.append((hp, ap))
    ph, pa = _consensus_fair(pairs)
    if ph is None:
        return (None, None, "none")
    return (ph, pa, f"{len(pairs)}-book consensus")


def scan_game(
    game: dict,
    market_key: str,
    bankroll_cash: float,
    kalshi_prob_home: Optional[float] = None,
    fallback_book: str = "DraftKings",
    kelly_mult: float = 0.25,
    min_edge_pct: float = 0.02,
    min_books: int = 1,
    max_bet_pct: float = 0.05,
) -> List[dict]:
    if not game or not isinstance(game, dict):
        return []
    home_team = game.get("home_team")
    away_team = game.get("away_team")
    books = game.get("bookmakers") or []
    if not books:
        return []

    fair_h, fair_a, fair_source = _fair_prob_for(
        game, market_key, kalshi_prob_home, fallback_book,
    )
    if fair_h is None:
        return []

    # per-book prices for this market
    per_book: Dict[str, Dict[str, float]] = {}
    for b in books:
        bname = b.get("title") or b.get("key") or ""
        for m in (b.get("markets") or []):
            if m.get("key") != market_key:
                continue
            hp = ap = None
            hpt = apt = None
            for o in (m.get("outcomes") or []):
                price, point, name = o.get("price"), o.get("point"), o.get("name")
                if price is None:
                    continue
                try:
                    price = float(price)
                except (TypeError, ValueError):
                    continue
                if name == home_team or name == "Over":
                    hp, hpt = price, point
                elif name == away_team or name == "Under":
                    ap, apt = price, point
            if hp is not None and ap is not None:
                per_book[bname] = {"home": hp, "away": ap, "home_pt": hpt, "away_pt": apt}

    # Synthesize Kalshi as a scannable "book" when we have a Kalshi probability.
    # Kalshi only has moneyline (no spreads/totals), so skip it for those markets.
    if market_key == "h2h" and kalshi_prob_home is not None and 0 < kalshi_prob_home < 1:
        try:
            from calc.odds import decimal_to_american
            k_home = decimal_to_american(1.0 / kalshi_prob_home)
            k_away = decimal_to_american(1.0 / (1.0 - kalshi_prob_home))
            if k_home is not None and k_away is not None:
                per_book["Kalshi"] = {
                    "home": float(k_home), "away": float(k_away),
                    "home_pt": None, "away_pt": None,
                }
        except Exception:
            pass

    if len(per_book) < max(1, min_books):
        return []

    results = []
    max_stake = bankroll_cash * max_bet_pct

    for bname, d in per_book.items():
        for side_key, side_label, book_price, fair_prob in [
            ("home", home_team if market_key != "totals" else "Over", d["home"], fair_h),
            ("away", away_team if market_key != "totals" else "Under", d["away"], fair_a),
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
                "fair_source": fair_source,
                "edge": edge,
                "edge_pct": edge * 100.0,
                "kelly_frac": kf,
                "stake_dollars": stake,
                "point": d.get(f"{side_key}_pt"),
                "market": market_key,
            })
    results.sort(key=lambda r: -r["edge"])
    return results


def _is_half_point(point) -> bool:
    """True when point exists and ends in .5 (Kalshi-style; no push risk)."""
    if point is None:
        return False
    try:
        return abs(float(point) - round(float(point))) > 0.49
    except (TypeError, ValueError):
        return False


def _dedupe_best(edges: List[dict]) -> List[dict]:
    """Collapse edges that share (event, market, side, point) to the single best price.

    "Best" = largest edge (longest odds, since fair prob is fixed per side-point).
    Keeps the one row that lets you shop the top number across all books.
    """
    best: Dict[tuple, dict] = {}
    for e in edges:
        key = (
            e.get("event_id"),
            e.get("market"),
            (e.get("side_label") or "").lower(),
            e.get("point"),
        )
        cur = best.get(key)
        if cur is None or e.get("edge", 0) > cur.get("edge", 0):
            best[key] = e
    return list(best.values())


def scan_all(
    odds_games: list,
    bankroll_cash: float,
    kalshi_probs_by_game: Optional[Dict[str, float]] = None,
    fallback_book: str = "DraftKings",
    kelly_mult: float = 0.25,
    min_edge_pct: float = 0.02,
    min_books: int = 1,
    max_bet_pct: float = 0.05,
    markets: Tuple[str, ...] = ("h2h", "spreads", "totals"),
    half_points_only: bool = True,
    dedupe_best_price: bool = True,
) -> List[dict]:
    """Scan every game, every market.

    kalshi_probs_by_game: {game_id: P(home_wins_per_Kalshi)}.
    half_points_only: when True, drop spreads/totals whose point is a whole number
      (eliminates push risk; mirrors how Kalshi lists its margin markets).
    dedupe_best_price: when True, collapse identical (side, point) picks across
      books down to the single best-priced row so the agent table doesn't repeat.
    """
    try:
        bankroll_cash = float(bankroll_cash) if bankroll_cash is not None else 0.0
    except (TypeError, ValueError):
        bankroll_cash = 0.0
    if bankroll_cash <= 0:
        return []
    kalshi_probs_by_game = kalshi_probs_by_game or {}
    out = []
    for g in odds_games or []:
        kp = kalshi_probs_by_game.get(g.get("id"))
        for mk in markets:
            edges = scan_game(
                g, mk, bankroll_cash,
                kalshi_prob_home=kp,
                fallback_book=fallback_book,
                kelly_mult=kelly_mult,
                min_edge_pct=min_edge_pct,
                min_books=min_books,
                max_bet_pct=max_bet_pct,
            )
            for e in edges:
                # Keep h2h always; filter spreads/totals to half-points if asked.
                if half_points_only and mk in ("spreads", "totals") and not _is_half_point(e.get("point")):
                    continue
                e["event_id"] = g.get("id")
                e["commence_time"] = g.get("commence_time")
                e["home_team"] = g.get("home_team")
                e["away_team"] = g.get("away_team")
                e["_sport"] = g.get("_sport")
                e["matchup"] = f"{g.get('away_team','?')} @ {g.get('home_team','?')}"
                out.append(e)
    if dedupe_best_price:
        out = _dedupe_best(out)
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
