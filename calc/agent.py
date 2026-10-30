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


def _grade_offshore_pick(market, side_key, point, home_score, away_score,
                         is_final=False, is_live=False) -> Optional[str]:
    """Grade a non-h2h pick against a running or final score.

    Returns one of win / loss / push for finals, winning / losing / tied for
    live games, or None when we lack the data to decide. Handles h2h (fallback
    when the main scanner didn't run), spreads (home/away ± point), and totals
    (over/under combined score). 1H markets are left ungraded since ESPN
    only exposes a running whole-game score, which can be misleading mid-game.
    """
    if home_score is None or away_score is None:
        return None
    if not (is_final or is_live):
        return None
    mk = (market or "").lower()
    try:
        pt = float(point) if point is not None else None
    except (TypeError, ValueError):
        pt = None

    def _label(final_label, live_label, tied_label):
        if is_final:
            return final_label
        return live_label

    if mk == "h2h":
        if home_score == away_score:
            return "push" if is_final else "tied"
        picked_home = side_key == "home"
        home_leads = home_score > away_score
        winning = (home_leads == picked_home)
        return _label("win" if winning else "loss",
                      "winning" if winning else "losing",
                      "tied")
    if mk in ("spreads", "spreads_h1"):
        if pt is None:
            return None
        if side_key == "home":
            margin = home_score - away_score + pt
        else:
            margin = away_score - home_score + pt
        if abs(margin) < 1e-9:
            return "push" if is_final else "tied"
        winning = margin > 0
        return _label("win" if winning else "loss",
                      "winning" if winning else "losing",
                      "tied")
    if mk in ("totals", "totals_h1"):
        if pt is None:
            return None
        total = home_score + away_score
        # In two-way totals, home side = Over, away side = Under (per _collect_market).
        picked_over = side_key == "home"
        if abs(total - pt) < 1e-9:
            return "push" if is_final else "tied"
        over_hit = total > pt
        winning = (over_hit == picked_over)
        return _label("win" if winning else "loss",
                      "winning" if winning else "losing",
                      "tied")
    return None


def _find_espn_ev(espn_events_by_game: Optional[dict], home_name, away_name) -> dict:
    """Resolve an ESPN event record, tolerant to naming differences between feeds.

    The agent sees Pinnacle (and Kalshi) team names, which often differ from the
    ESPN scoreboard names ("Washington Commanders" vs. "Washington" vs. "WAS").
    An exact tuple lookup misses those; we fall back to canonical same_team
    matching so start times and live scores still attach.
    """
    if not espn_events_by_game:
        return {}
    # Fast exact lookups first - cheap when names already align.
    exact = (
        espn_events_by_game.get((home_name, away_name))
        or espn_events_by_game.get((away_name, home_name))
    )
    if exact:
        return exact
    try:
        from apis.team_mapping import same_team as _same
    except Exception:
        return {}
    for (h_key, a_key), ev in espn_events_by_game.items():
        if _same(h_key, home_name) and _same(a_key, away_name):
            return ev
        if _same(h_key, away_name) and _same(a_key, home_name):
            return ev
    return {}


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


def scan_kalshi_vs_pinnacle(
    kalshi_markets_by_game: dict,     # {(home_name,away_name): [kalshi_markets...]}
    pinnacle_games: List[dict],       # Odds API games with Pinnacle + offshore quotes
    bankroll_cash: float,
    sport_key: str = "",
    kelly_mult: float = 0.25,
    min_edge_pct: float = 0.02,
    max_bet_pct: float = 0.05,
    shop_books: Optional[List[str]] = None,  # Extra books to scan against Pinnacle
    espn_events_by_game: Optional[dict] = None,  # {(home_name,away_name): espn_event_dict}
) -> List[dict]:
    """Multi-book shop: Kalshi + offshore books vs Pinnacle no-vig fair.

    For each game outcome, we compare the price offered on:
      * Kalshi (exchange, no vig)
      * Each book in `shop_books` (default: Bovada, LowVig.ag, BetOnline.ag, BetUS, MyBookie.ag)
    against Pinnacle's no-vig fair prob. We keep ONE row per (game, side) =
    whichever source has the biggest edge, so you bet where the price is best.

    Returns edge dicts shaped like scan_all's rows plus:
      * `best_book`, `best_price_american`, `best_implied`
      * `kalshi_price_cents`, `kalshi_implied` (always included for comparison
        when Kalshi had a quote on this market, even if not the best price)
      * `kalshi_url` for direct-click when Kalshi is the best book
    """
    from apis.team_mapping import canonical, same_team
    from apis import kalshi as kalshi_api
    from calc.odds import decimal_to_american, implied_prob_american

    out = []
    if not pinnacle_games or not kalshi_markets_by_game:
        return out
    try:
        bankroll_cash = float(bankroll_cash)
    except (TypeError, ValueError):
        return out
    if bankroll_cash <= 0:
        return out

    shop_books = shop_books or ["Bovada", "LowVig.ag", "BetOnline.ag", "BetUS", "MyBookie.ag"]
    shop_books_lower = {b.lower() for b in shop_books}
    max_stake = bankroll_cash * max_bet_pct
    espn_events_by_game = espn_events_by_game or {}

    # Lookup Pinnacle games by canonical team pair.
    pin_by_key = {}
    for g in pinnacle_games:
        home = g.get("home_team") or ""
        away = g.get("away_team") or ""
        if home and away:
            pin_by_key[(canonical(home), canonical(away))] = g

    def _book_h2h_prices(game, book_name_or_key):
        """Return (home_price, away_price) for one book in h2h, or (None, None)."""
        target = book_name_or_key.lower()
        hp = ap = None
        for b in (game.get("bookmakers") or []):
            bk = (b.get("key") or "").lower()
            bt = (b.get("title") or "").lower()
            if target != bk and target != bt and target not in bk and target not in bt:
                continue
            for m in (b.get("markets") or []):
                if m.get("key") != "h2h":
                    continue
                for o in (m.get("outcomes") or []):
                    price = o.get("price")
                    try:
                        price = float(price) if price is not None else None
                    except (TypeError, ValueError):
                        price = None
                    if price is None:
                        continue
                    if same_team(o.get("name"), game.get("home_team")):
                        hp = price
                    elif same_team(o.get("name"), game.get("away_team")):
                        ap = price
            break
        return hp, ap

    for (home_name, away_name), markets in kalshi_markets_by_game.items():
        if not markets:
            continue
        pin_game = pin_by_key.get((canonical(home_name), canonical(away_name)))
        if not pin_game:
            pin_game = pin_by_key.get((canonical(away_name), canonical(home_name)))
            if not pin_game:
                continue
        # Pinnacle fair prob.
        pin_h, pin_a = _book_h2h_prices(pin_game, "Pinnacle")
        if pin_h is None or pin_a is None:
            continue
        fair_home, fair_away = no_vig_two_way(pin_h, pin_a)
        if fair_home is None:
            continue
        pin_home_name = pin_game.get("home_team") or home_name
        pin_away_name = pin_game.get("away_team") or away_name

        # Pre-extract shop-book prices for this game (home_price, away_price).
        shop_prices = {}
        for b in (pin_game.get("bookmakers") or []):
            btitle = b.get("title") or b.get("key") or ""
            if btitle.lower() not in shop_books_lower:
                continue
            for m in (b.get("markets") or []):
                if m.get("key") != "h2h":
                    continue
                hp = ap = None
                for o in (m.get("outcomes") or []):
                    price = o.get("price")
                    try:
                        price = float(price) if price is not None else None
                    except (TypeError, ValueError):
                        price = None
                    if price is None:
                        continue
                    if same_team(o.get("name"), pin_home_name):
                        hp = price
                    elif same_team(o.get("name"), pin_away_name):
                        ap = price
                shop_prices[btitle] = (hp, ap)

        # Build per-outcome candidate list: (book, american_price, implied_prob, kalshi_ticker_or_None)
        # One iteration per Kalshi market (yes side); fair prob derived from side.
        for market in markets:
            yes_prob = kalshi_api.market_implied_yes_prob(market)
            yes_team = (market.get("yes_sub_title") or "").strip()
            if not yes_team:
                continue
            if same_team(yes_team, pin_home_name):
                fair_for_yes = fair_home
                yes_side_name = pin_home_name
                side_key = "home"
            elif same_team(yes_team, pin_away_name):
                fair_for_yes = fair_away
                yes_side_name = pin_away_name
                side_key = "away"
            else:
                continue

            # Candidate list: start with Kalshi, add offshore books.
            candidates = []
            kalshi_price_american = None
            kalshi_implied = None
            kalshi_ticker = market.get("ticker") or ""
            if yes_prob is not None and 0 < yes_prob < 1:
                try:
                    kalshi_price_american = decimal_to_american(1.0 / yes_prob)
                except Exception:
                    kalshi_price_american = None
                kalshi_implied = yes_prob
                if kalshi_price_american is not None:
                    candidates.append({
                        "book": "Kalshi",
                        "price": kalshi_price_american,
                        "implied": yes_prob,
                    })
            for bname, (hp, ap) in shop_prices.items():
                price = hp if side_key == "home" else ap
                if price is None:
                    continue
                imp = implied_prob_american(price)
                if imp is None:
                    continue
                candidates.append({"book": bname, "price": price, "implied": imp})

            if not candidates:
                continue
            # Best price = lowest implied prob (longest odds) for the SAME outcome.
            best = min(candidates, key=lambda c: c["implied"])
            edge = fair_for_yes - best["implied"]
            if edge < min_edge_pct:
                continue
            kf = kelly_fraction(fair_for_yes, best["price"], kelly_mult) or 0.0
            stake = min(bankroll_cash * kf, max_stake)
            kalshi_url = (
                f"https://kalshi.com/markets/{kalshi_ticker.lower()}" if kalshi_ticker else "https://kalshi.com/markets"
            )
            # Attach ESPN status/score/time for in-table grading + live tracking.
            # Try both the Kalshi-side names and the Pinnacle-side names; fall
            # back to canonical same_team matching so start times still resolve
            # when the feeds spell the team differently.
            espn_ev = (
                _find_espn_ev(espn_events_by_game, home_name, away_name)
                or _find_espn_ev(espn_events_by_game, pin_home_name, pin_away_name)
            )
            home_score = (espn_ev.get("home") or {}).get("score") if espn_ev else None
            away_score = (espn_ev.get("away") or {}).get("score") if espn_ev else None
            # Grade the pick side vs the current score (useful for live + final).
            # Pick is "YES on <team>" — team has to be winning (or won) to be ahead.
            grade = None  # "winning" | "losing" | "tied" | "win" | "loss" | "push"
            if home_score is not None and away_score is not None:
                try:
                    hs, as_ = int(home_score), int(away_score)
                except (TypeError, ValueError):
                    hs, as_ = None, None
                if hs is not None:
                    picked_home = side_key == "home"
                    if espn_ev.get("is_final"):
                        if hs == as_:
                            grade = "push"
                        elif (hs > as_) == picked_home:
                            grade = "win"
                        else:
                            grade = "loss"
                    elif espn_ev.get("is_live"):
                        if hs == as_:
                            grade = "tied"
                        elif (hs > as_) == picked_home:
                            grade = "winning"
                        else:
                            grade = "losing"
            out.append({
                "_sport": sport_key,
                # Kalshi ticker is the venue id; keep it separately so logged
                # picks can still auto-settle via ESPN below.
                "event_id": market.get("event_ticker"),
                "espn_event_id": espn_ev.get("id") if espn_ev else None,
                "home_team": pin_home_name,
                "away_team": pin_away_name,
                "matchup": f"{pin_away_name} @ {pin_home_name}",
                "market": "h2h",
                "side_label": f"{yes_side_name} wins",
                "point": None,
                # Headline numbers point to the BEST book
                "price": best["price"],
                "price_cents": int(round(best["implied"] * 100)),
                "book": best["book"],
                "book_implied": best["implied"],
                "fair_prob": fair_for_yes,
                "fair_source": "Pinnacle no-vig",
                "edge": edge,
                "edge_pct": edge * 100.0,
                "kelly_frac": kf,
                "stake_dollars": stake,
                # Kalshi comparison
                "kalshi_price_cents": int(round(yes_prob * 100)) if yes_prob else None,
                "kalshi_implied": kalshi_implied,
                "kalshi_ticker": kalshi_ticker,
                "kalshi_url": kalshi_url,
                "candidates": candidates,
                # ESPN status/score for live grading. Fall back to the Odds API
                # commence_time when ESPN hasn't matched the event (keeps the time
                # column alive for sports we don't pull ESPN scores for).
                "start_utc": espn_ev.get("start_utc"),
                "commence_time": pin_game.get("commence_time"),
                "status_state": espn_ev.get("status_state"),  # pre | in | post
                "status_detail": espn_ev.get("status_detail"),
                "home_score": home_score,
                "away_score": away_score,
                "picked_side_key": side_key,  # home | away (which Pinnacle side the pick is on)
                "grade": grade,
            })
    out.sort(key=lambda r: -r["edge"])
    return out


MARKET_LABELS = {
    "h2h": "Moneyline",
    "totals": "Total",
    "spreads": "Spread",
    "h2h_h1": "1H Moneyline",
    "totals_h1": "1H Total",
    "spreads_h1": "1H Spread",
    "btts": "BTTS",
}


def scan_offshore_markets_vs_pinnacle(
    pinnacle_games: List[dict],
    bankroll_cash: float,
    sport_key: str = "",
    markets: Optional[List[str]] = None,
    shop_books: Optional[List[str]] = None,
    kelly_mult: float = 0.25,
    min_edge_pct: float = 0.02,
    max_bet_pct: float = 0.05,
    espn_events_by_game: Optional[dict] = None,
    half_points_only: bool = True,
) -> List[dict]:
    """Shop offshore books vs Pinnacle no-vig fair on markets Kalshi doesn't carry
    (totals, 1H h2h, 1H totals, BTTS, spreads).

    Only two-way markets handled — totals/BTTS/moneyline/spreads all fit the
    (outcome_A, outcome_B) structure that no_vig_two_way works on.
    """
    from apis.team_mapping import same_team
    from calc.odds import implied_prob_american

    out = []
    if not pinnacle_games or not markets:
        return out
    try:
        bankroll_cash = float(bankroll_cash)
    except (TypeError, ValueError):
        return out
    if bankroll_cash <= 0:
        return out

    shop_books = shop_books or ["Bovada", "LowVig.ag", "BetOnline.ag", "BetUS", "MyBookie.ag"]
    shop_books_lower = {b.lower() for b in shop_books}
    max_stake = bankroll_cash * max_bet_pct
    espn_events_by_game = espn_events_by_game or {}

    def _is_half_pt(pt):
        if pt is None:
            return False
        try:
            return abs(float(pt) - round(float(pt))) > 0.49
        except (TypeError, ValueError):
            return False

    def _collect_market(game, market_key):
        """Return {book_name: {home: price, away: price, home_pt, away_pt, labels}} for one market."""
        home_team = game.get("home_team")
        away_team = game.get("away_team")
        out = {}
        for b in (game.get("bookmakers") or []):
            bk_title = b.get("title") or b.get("key") or ""
            for m in (b.get("markets") or []):
                if m.get("key") != market_key:
                    continue
                hp = ap = None
                hpt = apt = None
                hlabel = alabel = None
                for o in (m.get("outcomes") or []):
                    price = o.get("price")
                    point = o.get("point")
                    name = o.get("name") or ""
                    try:
                        price = float(price) if price is not None else None
                    except (TypeError, ValueError):
                        price = None
                    if price is None:
                        continue
                    name_l = name.lower()
                    # Two-way outcome names across markets we support:
                    # h2h / h2h_h1 / spreads  -> team names
                    # totals / totals_h1      -> "Over" / "Under"
                    # btts                    -> "Yes" / "No"
                    if name_l == "over":
                        hp = price; hpt = point; hlabel = "Over"
                    elif name_l == "under":
                        ap = price; apt = point; alabel = "Under"
                    elif name_l == "yes":
                        hp = price; hlabel = "Yes"
                    elif name_l == "no":
                        ap = price; alabel = "No"
                    elif same_team(name, home_team):
                        hp = price; hpt = point; hlabel = home_team
                    elif same_team(name, away_team):
                        ap = price; apt = point; alabel = away_team
                if hp is not None and ap is not None:
                    out[bk_title] = {
                        "home": hp, "away": ap,
                        "home_pt": hpt, "away_pt": apt,
                        "home_label": hlabel or "home",
                        "away_label": alabel or "away",
                    }
        return out

    for g in pinnacle_games:
        for mk in markets:
            # h2h is normally handled by scan_kalshi_vs_pinnacle. We also handle it
            # here when the caller passes it in (used for sports without Kalshi
            # markets like soccer/tennis - the h2h scanner can't run there).
            all_books = _collect_market(g, mk)
            if not all_books:
                continue
            # Pinnacle sets fair.
            pin_entry = None
            for bk_name, data in all_books.items():
                if bk_name.lower() == "pinnacle":
                    pin_entry = data
                    break
            if not pin_entry:
                continue
            fair_home, fair_away = no_vig_two_way(pin_entry["home"], pin_entry["away"])
            if fair_home is None:
                continue
            # For totals / spreads, require half-point lines (no push risk).
            if mk in ("spreads", "totals", "totals_h1", "spreads_h1") and half_points_only:
                if not (_is_half_pt(pin_entry.get("home_pt")) or _is_half_pt(pin_entry.get("away_pt"))):
                    continue
            # Pre-attach ESPN state for grading. Tolerant to feed name differences.
            home_name = g.get("home_team")
            away_name = g.get("away_team")
            espn_ev = _find_espn_ev(espn_events_by_game, home_name, away_name)
            # Precompute shared live/final scores + market-wide grading state.
            hs_raw = (espn_ev.get("home") or {}).get("score") if espn_ev else None
            as_raw = (espn_ev.get("away") or {}).get("score") if espn_ev else None
            try:
                hs_i = int(hs_raw) if hs_raw is not None else None
                as_i = int(as_raw) if as_raw is not None else None
            except (TypeError, ValueError):
                hs_i, as_i = None, None
            is_final = bool(espn_ev.get("is_final")) if espn_ev else False
            is_live = bool(espn_ev.get("is_live")) if espn_ev else False
            # Shop best price per side across offshore books.
            for side_key, fair_prob, label_fallback, point_key in [
                ("home", fair_home, pin_entry.get("home_label"), "home_pt"),
                ("away", fair_away, pin_entry.get("away_label"), "away_pt"),
            ]:
                candidates = []
                for bk_name, data in all_books.items():
                    if bk_name.lower() not in shop_books_lower:
                        continue
                    price = data.get(side_key)
                    if price is None:
                        continue
                    imp = implied_prob_american(price)
                    if imp is None:
                        continue
                    candidates.append({"book": bk_name, "price": price, "implied": imp,
                                       "point": data.get(point_key)})
                if not candidates:
                    continue
                best = min(candidates, key=lambda c: c["implied"])
                edge = fair_prob - best["implied"]
                if edge < min_edge_pct:
                    continue
                kf = kelly_fraction(fair_prob, best["price"], kelly_mult) or 0.0
                stake = min(bankroll_cash * kf, max_stake)
                side_label = label_fallback or side_key.upper()
                pt = best.get("point")
                if mk in ("totals", "totals_h1") and pt is not None:
                    side_label = f"{side_label} {pt}"
                elif mk in ("spreads", "spreads_h1") and pt is not None:
                    side_label = f"{side_label} {pt:+g}"
                elif mk == "spreads" and pt is not None:
                    try:
                        pt_f = float(pt)
                        side_label = f"{side_label} {pt_f:+g}"
                    except (TypeError, ValueError):
                        pass
                # Grade the pick: ✓/✗/= for finals, ▲/▼/= for in-progress.
                grade = _grade_offshore_pick(
                    mk, side_key, pt, hs_i, as_i,
                    is_final=is_final, is_live=is_live,
                )
                out.append({
                    "_sport": sport_key,
                    "event_id": g.get("id"),
                    "espn_event_id": espn_ev.get("id") if espn_ev else None,
                    "home_team": home_name,
                    "away_team": away_name,
                    "matchup": f"{away_name} @ {home_name}",
                    "market": mk,
                    "market_label": MARKET_LABELS.get(mk, mk),
                    "side_label": side_label,
                    "point": pt,
                    "price": best["price"],
                    "price_cents": int(round(best["implied"] * 100)),
                    "book": best["book"],
                    "book_implied": best["implied"],
                    "fair_prob": fair_prob,
                    "fair_source": "Pinnacle no-vig",
                    "edge": edge,
                    "edge_pct": edge * 100.0,
                    "kelly_frac": kf,
                    "stake_dollars": stake,
                    # Kalshi not applicable for non-h2h markets.
                    "kalshi_price_cents": None,
                    "kalshi_implied": None,
                    "kalshi_ticker": None,
                    "kalshi_url": None,
                    # Keep every candidate book so the quant breakdown can show
                    # the full probability ladder, not just the best price.
                    "candidates": candidates,
                    # ESPN state, with Odds-API commence_time as a fallback.
                    "start_utc": espn_ev.get("start_utc"),
                    "commence_time": g.get("commence_time"),
                    "status_state": espn_ev.get("status_state"),
                    "status_detail": espn_ev.get("status_detail"),
                    "home_score": hs_i,
                    "away_score": as_i,
                    "picked_side_key": side_key,
                    "grade": grade,
                })
    out.sort(key=lambda r: -r["edge"])
    return out


def apply_daily_cap(edges: List[dict], bankroll_cash: float, daily_cap_pct: float = 0.25) -> List[dict]:
    """Top-N selection: take picks in edge-rank order until the cap is used.

    daily_cap_pct = fraction of bankroll that may be in play across all picks today.
    Picks that don't fit have `stake_dollars` set to 0 (so they still appear in
    the UI as "overflow" without recommending real money). The last pick that
    partially fits gets scaled down to use the remaining budget exactly.
    """
    if not edges:
        return edges
    try:
        bk = float(bankroll_cash)
        cap_pct = float(daily_cap_pct)
    except (TypeError, ValueError):
        return edges
    if bk <= 0 or cap_pct <= 0:
        return edges
    cap_dollars = bk * cap_pct
    # Sort by edge descending (defensive; scan_all already sorts).
    ranked = sorted(edges, key=lambda e: -float(e.get("edge", 0) or 0))
    remaining = cap_dollars
    for e in ranked:
        s = float(e.get("stake_dollars", 0.0) or 0.0)
        e["stake_dollars_raw"] = s
        if remaining <= 0:
            e["stake_dollars"] = 0.0
            e["dropped_by_cap"] = True
            continue
        if s <= remaining:
            e["stake_dollars"] = s
            remaining -= s
        else:
            e["stake_dollars"] = remaining  # partial fill of the last pick
            e["partial_cap"] = True
            remaining = 0.0
    return ranked


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
