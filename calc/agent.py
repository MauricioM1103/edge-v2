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
                         is_final=False, is_live=False, three_way=False) -> Optional[str]:
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

    if mk == "h2h" and (three_way or side_key == "draw"):
        # Three-way moneyline: a level score wins the Draw and beats both teams.
        if side_key == "draw":
            winning = home_score == away_score
        else:
            winning = home_score != away_score and ((home_score > away_score) == (side_key == "home"))
        return _label("win" if winning else "loss",
                      "winning" if winning else "losing",
                      "tied")
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
    if mk == "btts":
        # home side = Yes, away side = No (per _collect_market).
        picked_yes = side_key == "home"
        both_scored = (home_score > 0) and (away_score > 0)
        winning = (both_scored == picked_yes)
        # Live BTTS can flip once the trailing side scores, so "ahead" stays
        # meaningful: Yes is winning once both have scored, No is winning as
        # long as at least one has zero.
        return _label("win" if winning else "loss",
                      "winning" if winning else "losing",
                      "tied")
    if mk == "draw_no_bet":
        # Draw refunds the stake — push on finals, tied in play.
        if home_score == away_score:
            return "push" if is_final else "tied"
        picked_home = side_key == "home"
        home_leads = home_score > away_score
        winning = (home_leads == picked_home)
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


#: How far Pinnacle's quote may trail the price being shopped. Before a game
#: lines drift slowly, so ten minutes is fine. In play they move by the
#: second, and Pinnacle's feed often trails the shop books by ten minutes or
#: more: pricing a live +500 against a ten-minute-old +113 "fair" produced
#: 10-28 point phantom edges that were logged as tier-A picks.
PRE_GAME_MAX_LAG_S = 600
#: Kalshi is read live while the odds feed may come from the on-disk cache
#: (60 minutes by default), so before a game its price is allowed to be
#: newer than Pinnacle's by up to the cache lifetime plus a margin.
KALSHI_PRE_GAME_MAX_LAG_S = 75 * 60
IN_PLAY_MAX_LAG_S = 90
#: In-play pricing is off. Even quotes whose timestamps agree to the second
#: were seen carrying pre-game numbers late in a game (Under 5.5 at a 58 %
#: fair with 51 seconds left and five goals scored), and the odds feed is
#: cached for up to an hour. Nothing here is fast enough to bet live.
ALLOW_IN_PLAY = False
#: Largest believable gap between Kalshi's midpoint and the sharp fair value.
KALSHI_MAX_FAIR_GAP = 0.10


def _ts(value):
    """Parse an Odds API timestamp ("2026-11-20T05:06:45Z") to aware UTC."""
    if not value:
        return None
    try:
        from datetime import datetime, timezone
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _market_updated(book: dict, market: dict):
    return _ts((market or {}).get("last_update") or (book or {}).get("last_update"))


def _sharp_is_fresh(sharp_updated, quote_updated, commence_time, pre_game_max_lag_s: int = PRE_GAME_MAX_LAG_S) -> bool:
    """True when the sharp quote is recent enough to price `quote` against.

    quote_updated is when the price being shopped was last refreshed (for a
    Kalshi price read just now, pass the current time). Missing timestamps
    cannot be judged and are allowed, so hand-built test games still scan.
    """
    sharp, quote = _ts(sharp_updated), _ts(quote_updated)
    if sharp is None or quote is None:
        return True
    lag = (quote - sharp).total_seconds()
    start = _ts(commence_time)
    in_play = start is not None and quote >= start
    if in_play:
        return ALLOW_IN_PLAY and abs(lag) <= IN_PLAY_MAX_LAG_S
    return lag <= pre_game_max_lag_s


def describe_scan_failures(failures) -> str:
    """One readable line for scanner crashes, or "" when there were none.

    `failures` is a list of (sport, scanner_label, exception). The Board used
    to catch a scanner exception and carry on with an empty list, so a crash
    made a whole sport vanish with no sign anything was wrong.
    """
    if not failures:
        return ""
    parts = []
    for sport, label, ex in failures:
        detail = f"{type(ex).__name__}: {ex}" if str(ex) else type(ex).__name__
        parts.append(f"{str(sport).upper()} {label} ({detail[:120]})")
    n = len(failures)
    return f"{n} scan{'s' if n != 1 else ''} failed and {'are' if n != 1 else 'is'} missing from the Board: " + "; ".join(parts)


def _side_of(name, home, away, loose=None) -> Optional[str]:
    """Which side of a game an outcome name refers to: "home", "away" or None.

    Exact (case-insensitive) equality wins: inside one Odds API event every
    book spells the two teams exactly as home_team / away_team. The loose
    alias matcher is only a fallback, and only counts when it picks ONE
    side. It matches on a shared last word, so it cannot tell Rice Owls
    from Temple Owls, Inter Milan from AC Milan or Toronto FC from Atlanta
    United FC; an ambiguous name is refused rather than guessed, because a
    wrong guess prices one team against the other team's fair value.
    """
    n = (name or "").strip().lower()
    if not n:
        return None
    if n == (home or "").strip().lower():
        return "home"
    if n == (away or "").strip().lower():
        return "away"
    if loose is None:
        from apis.team_mapping import same_team as loose
    mh, ma = bool(loose(name, home)), bool(loose(name, away))
    if mh and not ma:
        return "home"
    if ma and not mh:
        return "away"
    return None


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
    from apis.team_mapping import canonical, same_team as _same_team_strict
    from apis import kalshi as kalshi_api
    from apis.kalshi_match import is_tie_market, same_contestant, event_side_map
    from calc.odds import decimal_to_american, implied_prob_american, no_vig_multi

    three_way_sport = sport_key in kalshi_api.THREE_WAY_SPORTS

    def same_team(a, b):
        # Team aliases first; fall back to the person/club matcher so
        # "F. Tiafoe" and "Tottenham" resolve against the sharp-book names.
        return _same_team_strict(a, b) or same_contestant(a, b)

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
        """Return (home_price, away_price, draw_price, updated) for one book in h2h."""
        target = book_name_or_key.lower()
        hp = ap = dp = None
        updated = None
        for b in (game.get("bookmakers") or []):
            bk = (b.get("key") or "").lower()
            bt = (b.get("title") or "").lower()
            if target != bk and target != bt and target not in bk and target not in bt:
                continue
            for m in (b.get("markets") or []):
                if m.get("key") != "h2h":
                    continue
                updated = _market_updated(b, m)
                for o in (m.get("outcomes") or []):
                    price = o.get("price")
                    try:
                        price = float(price) if price is not None else None
                    except (TypeError, ValueError):
                        price = None
                    if price is None:
                        continue
                    if (o.get("name") or "").strip().lower() == "draw":
                        dp = price
                        continue
                    which = _side_of(o.get("name"), game.get("home_team"), game.get("away_team"), same_team)
                    if which == "home":
                        hp = price
                    elif which == "away":
                        ap = price
            break
        return hp, ap, dp, updated

    for (home_name, away_name), markets in kalshi_markets_by_game.items():
        if not markets:
            continue
        pin_game = pin_by_key.get((canonical(home_name), canonical(away_name)))
        if not pin_game:
            pin_game = pin_by_key.get((canonical(away_name), canonical(home_name)))
            if not pin_game:
                continue
        # Pinnacle fair prob.
        pin_h, pin_a, pin_d, pin_updated = _book_h2h_prices(pin_game, "Pinnacle")
        _commence = pin_game.get("commence_time")
        if pin_h is None or pin_a is None:
            continue
        fair_draw = None
        if pin_d is not None:
            # Three-way moneyline: strip the vig across all three outcomes.
            # Normalising only home/away would hand both teams the draw's
            # share of probability and invent an edge on every side.
            fairs = no_vig_multi([pin_h, pin_a, pin_d])
            if not fairs:
                continue
            fair_home, fair_away, fair_draw = fairs
        elif three_way_sport:
            continue  # soccer without a sharp draw price cannot be priced
        else:
            fair_home, fair_away = no_vig_two_way(pin_h, pin_a)
            if fair_home is None:
                continue
        pin_home_name = pin_game.get("home_team") or home_name
        pin_away_name = pin_game.get("away_team") or away_name
        # Last gate before any Kalshi price is used: the event must be shown
        # to be THIS game (right date, both teams named, one way to fit them).
        # Whatever attached these markets upstream, a different game's price
        # never reaches the Board.
        side_by_ticker = event_side_map(markets, pin_home_name, pin_away_name, pin_game.get("commence_time"))
        if side_by_ticker is None:
            continue

        # Pre-extract shop-book prices for this game (home_price, away_price).
        shop_prices = {}
        for b in (pin_game.get("bookmakers") or []):
            btitle = b.get("title") or b.get("key") or ""
            if btitle.lower() not in shop_books_lower:
                continue
            for m in (b.get("markets") or []):
                if m.get("key") != "h2h":
                    continue
                hp = ap = dp = None
                for o in (m.get("outcomes") or []):
                    price = o.get("price")
                    try:
                        price = float(price) if price is not None else None
                    except (TypeError, ValueError):
                        price = None
                    if price is None:
                        continue
                    if (o.get("name") or "").strip().lower() == "draw":
                        dp = price
                        continue
                    which = _side_of(o.get("name"), pin_home_name, pin_away_name, same_team)
                    if which == "home":
                        hp = price
                    elif which == "away":
                        ap = price
                if _sharp_is_fresh(pin_updated, _market_updated(b, m), _commence):
                    shop_prices[btitle] = {"home": hp, "away": ap, "draw": dp}

        # Build per-outcome candidate list: (book, american_price, implied_prob, kalshi_ticker_or_None)
        # One iteration per Kalshi market (yes side); fair prob derived from side.
        for market in markets:
            # What it costs to BUY Yes: the ask plus Kalshi's taker fee. The
            # bid/ask midpoint is not a price anyone can trade at, and on a
            # 2-4 cent spread it overstated every Kalshi edge by 1-2 points.
            yes_ask = kalshi_api.market_yes_ask_prob(market)
            yes_fee = kalshi_api.taker_fee_prob(yes_ask)
            yes_prob = (yes_ask + yes_fee) if yes_ask is not None else None
            yes_team = (market.get("yes_sub_title") or "").strip()
            if not yes_team:
                continue
            _which = side_by_ticker.get(market.get("ticker") or yes_team)
            if _which == "draw":
                if fair_draw is None:
                    continue
                fair_for_yes = fair_draw
                yes_side_name = "Draw"
                side_key = "draw"
            elif _which == "home":
                fair_for_yes = fair_home
                yes_side_name = pin_home_name
                side_key = "home"
            elif _which == "away":
                fair_for_yes = fair_away
                yes_side_name = pin_away_name
                side_key = "away"
            else:
                continue
            # Sanity check on the number itself. Kalshi's game markets track
            # the sharp line closely; when its midpoint sits more than ten
            # points from the fair value, the far likelier explanation is a
            # stale line or a mismatched market than a real mispricing, so
            # Kalshi is not offered as a price for this side.
            _mid = kalshi_api.market_implied_yes_prob(market)
            kalshi_suspect = _mid is not None and abs(_mid - fair_for_yes) > KALSHI_MAX_FAIR_GAP
            if kalshi_suspect:
                yes_prob = None
                yes_ask = None

            # Candidate list: start with Kalshi, add offshore books.
            candidates = []
            kalshi_price_american = None
            kalshi_implied = None
            kalshi_ticker = market.get("ticker") or ""
            # Kalshi's price is live as of this scan, so Pinnacle has to be too.
            from datetime import datetime as _dt, timezone as _tz
            if not _sharp_is_fresh(pin_updated, _dt.now(_tz.utc), _commence, KALSHI_PRE_GAME_MAX_LAG_S):
                yes_prob = None
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
            for bname, sp_prices in shop_prices.items():
                price = sp_prices.get(side_key)
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
                if hs is not None and (side_key == "draw" or fair_draw is not None):
                    # Three-way: a level score wins the Draw and beats both teams.
                    if side_key == "draw":
                        ahead = hs == as_
                    else:
                        ahead = hs != as_ and ((hs > as_) == (side_key == "home"))
                    if espn_ev.get("is_final"):
                        grade = "win" if ahead else "loss"
                    elif espn_ev.get("is_live"):
                        grade = "winning" if ahead else "losing"
                elif hs is not None:
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
                "matchup": (
                    f"{pin_away_name} vs {pin_home_name}"
                    if sport_key in kalshi_api.INDIVIDUAL_SPORTS
                    else f"{pin_away_name} @ {pin_home_name}"
                ),
                "market": "h2h",
                "side_label": "Draw" if side_key == "draw" else f"{yes_side_name} wins",
                "point": None,
                "three_way": fair_draw is not None,
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
                "kalshi_price_cents": int(round(yes_ask * 100)) if yes_ask else None,
                "kalshi_fee_cents": round(yes_fee * 100, 2) if yes_ask else None,
                "kalshi_suspect": bool(kalshi_suspect),
                "kalshi_implied": kalshi_implied,
                "kalshi_ticker": kalshi_ticker,
                "kalshi_url": kalshi_url,
                "candidates": candidates,
                # ESPN status/score for live grading. Fall back to the Odds API
                # commence_time when ESPN hasn't matched the event (keeps the time
                # column alive for sports we don't pull ESPN scores for).
                "start_utc": espn_ev.get("start_utc"),
                "commence_time": pin_game.get("commence_time"),
                "home_abbr": ((espn_ev.get("home") or {}).get("abbr") or "").upper(),
                "away_abbr": ((espn_ev.get("away") or {}).get("abbr") or "").upper(),
                # Team primary colors (hex, from ESPN scoreboard metadata) —
                # used by the board's team-chip renderer so chips take the team's
                # jersey color instead of a uniform gray.
                "home_color": (espn_ev.get("home") or {}).get("color") or "",
                "away_color": (espn_ev.get("away") or {}).get("color") or "",
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
    "draw_no_bet": "Draw No Bet",
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
    from apis.kalshi import THREE_WAY_SPORTS
    from calc.odds import implied_prob_american, no_vig_multi

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
                hp = ap = dp = None
                hpt = apt = None
                hlabel = alabel = None
                _updated = _market_updated(b, m)
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
                    elif name_l == "draw":
                        dp = price
                    else:
                        which = _side_of(name, home_team, away_team, same_team)
                        if which == "home":
                            hp = price; hpt = point; hlabel = home_team
                        elif which == "away":
                            ap = price; apt = point; alabel = away_team
                if hp is not None and ap is not None:
                    out[bk_title] = {
                        "home": hp, "away": ap, "draw": dp,
                        "home_pt": hpt, "away_pt": apt,
                        "home_label": hlabel or "home",
                        "away_label": alabel or "away",
                        "updated": _updated,
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
            fair_draw = None
            if mk in ("h2h", "h2h_h1") and pin_entry.get("draw") is not None:
                # Three-way moneyline (soccer): strip the vig across home /
                # draw / away. Normalising only the two teams would hand
                # them the draw's share and invent double-digit "edges".
                fairs = no_vig_multi([pin_entry["home"], pin_entry["away"], pin_entry["draw"]])
                if not fairs:
                    continue
                fair_home, fair_away, fair_draw = fairs
            elif mk in ("h2h", "h2h_h1") and sport_key in THREE_WAY_SPORTS:
                continue  # soccer moneyline without a sharp draw price
            else:
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
            _sides = [
                ("home", fair_home, pin_entry.get("home_label"), "home_pt"),
                ("away", fair_away, pin_entry.get("away_label"), "away_pt"),
            ]
            if fair_draw is not None:
                _sides.append(("draw", fair_draw, "Draw", "draw_pt"))
            for side_key, fair_prob, label_fallback, point_key in _sides:
                candidates = []
                for bk_name, data in all_books.items():
                    if bk_name.lower() not in shop_books_lower:
                        continue
                    price = data.get(side_key)
                    if price is None:
                        continue
                    if not _sharp_is_fresh(pin_entry.get("updated"), data.get("updated"), g.get("commence_time")):
                        continue
                    # A spread or total is only the same bet at the same
                    # number. Books split on which side of a puck line or
                    # run line they hang (PIT +1.5 at one, PIT -1.5 at
                    # another); pricing one against the other's fair
                    # manufactures a 40-point "edge".
                    if mk in ("spreads", "spreads_h1", "totals", "totals_h1"):
                        pin_pt, bk_pt = pin_entry.get(point_key), data.get(point_key)
                        try:
                            if pin_pt is None or bk_pt is None or abs(float(pin_pt) - float(bk_pt)) > 1e-9:
                                continue
                        except (TypeError, ValueError):
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
                    side_label = f"{side_label} {float(pt):g}"
                elif mk in ("spreads", "spreads_h1") and pt is not None:
                    side_label = f"{side_label} {pt:+g}"
                elif mk == "spreads" and pt is not None:
                    try:
                        pt_f = float(pt)
                        side_label = f"{side_label} {pt_f:+g}"
                    except (TypeError, ValueError):
                        pass
                elif mk == "btts":
                    # "Yes" / "No" alone is ambiguous on a mixed-market board;
                    # the storage layer still keys off `market`, this just
                    # reads better in the Board rows.
                    side_label = f"Both to score: {side_label}"
                elif mk == "draw_no_bet":
                    side_label = f"{side_label} (DNB)"
                # Grade the pick: ✓/✗/= for finals, ▲/▼/= for in-progress.
                grade = _grade_offshore_pick(
                    mk, side_key, pt, hs_i, as_i,
                    is_final=is_final, is_live=is_live,
                    three_way=fair_draw is not None,
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
                    "three_way": fair_draw is not None,
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
                    "home_abbr": ((espn_ev.get("home") or {}).get("abbr") or "").upper(),
                    "away_abbr": ((espn_ev.get("away") or {}).get("abbr") or "").upper(),
                    "home_color": (espn_ev.get("home") or {}).get("color") or "",
                    "away_color": (espn_ev.get("away") or {}).get("color") or "",
                    "status_state": espn_ev.get("status_state"),
                    "status_detail": espn_ev.get("status_detail"),
                    "home_score": hs_i,
                    "away_score": as_i,
                    "picked_side_key": side_key,
                    "grade": grade,
                })
    out.sort(key=lambda r: -r["edge"])
    return out


# ---------------------------------------------------------------------------
# Player props
# ---------------------------------------------------------------------------
# Odds API market key -> short label. Only Over/Under (two-way) props are
# listed: those have a fair reference (Pinnacle no-vig, else book consensus).
# Yes-only props (anytime TD, first basket) are deliberately absent.
PROP_MARKET_LABELS = {
    # basketball
    "player_points": "Points", "player_rebounds": "Rebounds", "player_assists": "Assists",
    "player_threes": "3PM", "player_blocks": "Blocks", "player_steals": "Steals",
    "player_turnovers": "Turnovers", "player_points_rebounds_assists": "PRA",
    "player_points_rebounds": "Pts+Reb", "player_points_assists": "Pts+Ast",
    "player_rebounds_assists": "Reb+Ast", "player_blocks_steals": "Blk+Stl",
    # football
    "player_pass_yds": "Pass Yds", "player_pass_tds": "Pass TDs", "player_pass_attempts": "Pass Att",
    "player_pass_completions": "Completions", "player_pass_interceptions": "INTs",
    "player_rush_yds": "Rush Yds", "player_rush_attempts": "Rush Att",
    "player_reception_yds": "Rec Yds", "player_receptions": "Receptions",
    "player_rush_reception_yds": "Rush+Rec Yds", "player_kicking_points": "Kicking Pts",
    # baseball
    "batter_hits": "Hits", "batter_total_bases": "Total Bases", "batter_home_runs": "Home Runs",
    "batter_rbis": "RBIs", "batter_runs_scored": "Runs", "batter_hits_runs_rbis": "H+R+RBI",
    "batter_strikeouts": "Batter Ks", "batter_walks": "Walks",
    "pitcher_strikeouts": "Pitcher Ks", "pitcher_outs": "Outs", "pitcher_earned_runs": "Earned Runs",
    "pitcher_hits_allowed": "Hits Allowed", "pitcher_walks": "Pitcher BB",
    # hockey (player_points / player_assists shared with basketball labels)
    "player_goals": "Goals", "player_shots_on_goal": "SOG", "player_blocked_shots": "Blocked Shots",
    "player_power_play_points": "PP Points", "player_total_saves": "Saves",
    # soccer
    "player_shots_on_target": "Shots on Target", "player_shots": "Shots",
}

# Which O/U prop markets each sport supports on The Odds API. The Settings
# picker builds from this so users can only choose keys the API will accept.
PROP_CATALOG = {
    "nba": ["player_points", "player_rebounds", "player_assists", "player_threes",
            "player_blocks", "player_steals", "player_turnovers",
            "player_points_rebounds_assists", "player_points_rebounds",
            "player_points_assists", "player_rebounds_assists", "player_blocks_steals"],
    "wnba": ["player_points", "player_rebounds", "player_assists", "player_threes",
             "player_points_rebounds_assists"],
    "ncaab": ["player_points", "player_rebounds", "player_assists", "player_threes"],
    "nfl": ["player_pass_yds", "player_pass_tds", "player_pass_attempts", "player_pass_completions",
            "player_pass_interceptions", "player_rush_yds", "player_rush_attempts",
            "player_reception_yds", "player_receptions", "player_rush_reception_yds",
            "player_kicking_points"],
    "ncaaf": ["player_pass_yds", "player_pass_tds", "player_rush_yds", "player_reception_yds",
              "player_receptions"],
    "cfl": ["player_pass_yds", "player_rush_yds", "player_reception_yds", "player_receptions"],
    "mlb": ["batter_hits", "batter_total_bases", "batter_home_runs", "batter_rbis",
            "batter_runs_scored", "batter_hits_runs_rbis", "batter_strikeouts", "batter_walks",
            "pitcher_strikeouts", "pitcher_outs", "pitcher_earned_runs", "pitcher_hits_allowed",
            "pitcher_walks"],
    "nhl": ["player_points", "player_assists", "player_goals", "player_shots_on_goal",
            "player_blocked_shots", "player_power_play_points", "player_total_saves"],
    "epl": ["player_shots_on_target", "player_shots", "player_assists"],
    "la_liga": ["player_shots_on_target", "player_shots", "player_assists"],
    "bundesliga": ["player_shots_on_target", "player_shots", "player_assists"],
    "serie_a": ["player_shots_on_target", "player_shots", "player_assists"],
    "ligue_1": ["player_shots_on_target", "player_shots", "player_assists"],
    "mls": ["player_shots_on_target", "player_shots", "player_assists"],
}


def prop_market_label(market_key: str) -> str:
    return PROP_MARKET_LABELS.get(market_key) or market_key.replace("player_", "").replace("_", " ").title()


def _is_alternate_market(market_key: str) -> bool:
    """True when a prop market is the Odds API 'ladder' variant.

    Alt markets are one-sided (Over only) with many lines per player, so
    fair is derived from a vig-padded consensus of other books at the same
    line rather than from two-way no-vig removal.
    """
    k = (market_key or "").lower()
    return k.endswith("_alternate")


def _primary_market_key(market_key: str) -> str:
    """Strip the '_alternate' suffix so primary + alt at the same line
    collapse into one slot for grouping/dedup."""
    k = (market_key or "").lower()
    if k.endswith("_alternate"):
        return k[:-len("_alternate")]
    return k


def scan_player_props_vs_pinnacle(
    pinnacle_games: List[dict],
    bankroll_cash: float,
    sport_key: str = "",
    prop_markets: Optional[List[str]] = None,
    shop_books: Optional[List[str]] = None,
    kelly_mult: float = 0.25,
    min_edge_pct: float = 0.01,
    max_bet_pct: float = 0.05,
    espn_events_by_game: Optional[dict] = None,
    min_consensus_books: int = 3,
    consensus_min_edge_pct: float = 0.03,
    alt_min_edge_pct: float = 0.03,
) -> List[dict]:
    """Shop offshore books vs a fair line on player Over/Under props.

    Handles two market shapes:

    1. Primary two-way markets (e.g. player_points): outcomes come as
       {Over, Under} at one standard line per book. Fair is Pinnacle no-vig
       at that line, else the no-vig consensus of >= min_consensus_books
       books at the same line.

    2. Odds API "_alternate" ladders (e.g. player_points_alternate): Over
       only, many lines per player per book. No Under means no no-vig fair
       can be computed, so fair is taken as the median Over-implied across
       reference books (shop books excluded) at the same line. That number
       is still vig-padded, so alt rows must clear a higher edge floor
       (alt_min_edge_pct, default 5 %) to surface.

    Emits edge dicts shaped like the offshore scanner's rows plus
    is_prop=True, prop_player and a "Prop · <stat>" market_label. Grade is
    always None — ESPN's scoreboard carries no player stats.
    """
    from calc.odds import implied_prob_american

    out: List[dict] = []
    if not pinnacle_games or not prop_markets:
        return out
    try:
        bankroll_cash = float(bankroll_cash)
    except (TypeError, ValueError):
        return out
    if bankroll_cash <= 0:
        return out
    shop_books = shop_books or ["Bovada", "LowVig.ag", "BetOnline.ag", "BetUS", "MyBookie.ag"]
    shop_lower = {b.lower() for b in shop_books}
    max_stake = bankroll_cash * max_bet_pct
    espn_events_by_game = espn_events_by_game or {}

    def _is_half(pt) -> bool:
        try:
            return abs(float(pt) - round(float(pt))) > 0.49
        except (TypeError, ValueError):
            return False

    for g in pinnacle_games:
        home_name = g.get("home_team")
        away_name = g.get("away_team")
        espn_ev = _find_espn_ev(espn_events_by_game, home_name, away_name)
        for mk in prop_markets:
            is_alt = _is_alternate_market(mk)
            # Alt ladders are Over-only, so their vig cannot be measured
            # directly. Estimate it from this game's two-way primary market
            # (Over implied + Under implied at reference books) and divide
            # it out of the alt consensus below; assume 7 % when the primary
            # market was not fetched.
            alt_overround = 1.07
            if is_alt:
                _prim = _primary_market_key(mk)
                _ov = []
                for b in (g.get("bookmakers") or []):
                    if (b.get("title") or b.get("key") or "").lower() in shop_lower:
                        continue
                    for m in (b.get("markets") or []):
                        if m.get("key") != _prim:
                            continue
                        _pair: Dict[tuple, dict] = {}
                        for o in (m.get("outcomes") or []):
                            _s = (o.get("name") or "").strip().lower()
                            if _s in ("over", "under"):
                                _pair.setdefault(((o.get("description") or ""), o.get("point")), {})[_s] = o.get("price")
                        for d in _pair.values():
                            io, iu = implied_prob_american(d.get("over")), implied_prob_american(d.get("under"))
                            if io is not None and iu is not None and 1.0 < io + iu < 1.25:
                                _ov.append(io + iu)
                if _ov:
                    alt_overround = sum(_ov) / len(_ov)
            # lines[(player, point)] = {book_title: {"over": price, "under": price}}
            lines: Dict[tuple, Dict[str, dict]] = {}
            for b in (g.get("bookmakers") or []):
                btitle = b.get("title") or b.get("key") or ""
                for m in (b.get("markets") or []):
                    if m.get("key") != mk:
                        continue
                    for o in (m.get("outcomes") or []):
                        player = (o.get("description") or "").strip()
                        side = (o.get("name") or "").strip().lower()
                        if not player or side not in ("over", "under"):
                            continue
                        try:
                            price = float(o.get("price"))
                            pt = float(o.get("point"))
                        except (TypeError, ValueError):
                            continue
                        _slot = lines.setdefault((player, pt), {}).setdefault(btitle, {})
                        _slot[side] = price
                        _slot["updated"] = _market_updated(b, m)
            for (player, pt), by_book in lines.items():
                if not _is_half(pt):
                    continue  # whole-number lines carry push risk; skip like totals
                # Fair at this exact line — three paths:
                #   1. Primary market with Pinnacle both sides -> no-vig fair
                #   2. Primary market with >=min_consensus_books two-way ->
                #      no-vig consensus
                #   3. _alternate market (Over-only) -> median reference
                #      Over-implied (vig-padded, higher edge floor applies)
                pin = next((d for t, d in by_book.items() if t.lower() == "pinnacle"), None)
                fair_over = fair_under = None
                fair_source = ""
                is_alt_fair = False
                if not is_alt and pin and pin.get("over") is not None and pin.get("under") is not None:
                    fair_over, fair_under = no_vig_two_way(pin["over"], pin["under"])
                    fair_source = "Pinnacle no-vig"
                if fair_over is None and not is_alt:
                    two_way = {t: (d["over"], d["under"]) for t, d in by_book.items()
                               if d.get("over") is not None and d.get("under") is not None}
                    ref_pairs = [p for t, p in two_way.items() if t.lower() not in shop_lower]
                    pairs = ref_pairs if len(ref_pairs) >= min_consensus_books else list(two_way.values())
                    if len(pairs) >= min_consensus_books:
                        fair_over, fair_under = _consensus_fair(pairs)
                        fair_source = f"{len(pairs)}-book consensus"
                if fair_over is None and is_alt:
                    # Alt markets are Over-only, so no no-vig removal is
                    # possible. Use the median reference-book Over implied
                    # as the fair estimate; the one-sided nature means it
                    # still carries vig, hence the stricter edge floor
                    # below. Under side is never shoppable here.
                    ref_overs = [d["over"] for t, d in by_book.items()
                                 if d.get("over") is not None and t.lower() not in shop_lower]
                    pool_overs = ref_overs if len(ref_overs) >= min_consensus_books else [
                        d["over"] for d in by_book.values() if d.get("over") is not None
                    ]
                    implieds = sorted(
                        p for p in (implied_prob_american(x) for x in pool_overs) if p is not None
                    )
                    if len(implieds) >= min_consensus_books:
                        mid = implieds[len(implieds) // 2] / alt_overround
                        fair_over = mid
                        fair_under = 1.0 - mid  # not actually shoppable
                        fair_source = f"{len(implieds)}-book alt consensus"
                        is_alt_fair = True
                if fair_over is None:
                    continue
                sides = (("over", fair_over),) if is_alt_fair else (("over", fair_over), ("under", fair_under))
                for side, fair_prob in sides:
                    candidates = []
                    for btitle, d in by_book.items():
                        if btitle.lower() not in shop_lower:
                            continue
                        price = d.get(side)
                        if price is None:
                            continue
                        # Pinnacle fair must be current; a consensus fair has no
                        # single timestamp, so it is only trusted before the game.
                        _q = d.get("updated")
                        if fair_source.startswith("Pinnacle"):
                            if not _sharp_is_fresh((pin or {}).get("updated"), _q, g.get("commence_time")):
                                continue
                        else:
                            _start = _ts(g.get("commence_time"))
                            if _start is not None and _q is not None and _q >= _start:
                                continue
                        imp = implied_prob_american(price)
                        if imp is None:
                            continue
                        candidates.append({"book": btitle, "price": price, "implied": imp, "point": pt})
                    if not candidates:
                        continue
                    best = min(candidates, key=lambda c: c["implied"])
                    edge = fair_prob - best["implied"]
                    # Edge floor varies by fair-source credibility:
                    #   Pinnacle no-vig: min_edge_pct (sharpest reference)
                    #   N-book consensus: consensus_min_edge_pct (retail
                    #     avg with vig removed)
                    #   Alt consensus: alt_min_edge_pct (vig-padded median,
                    #     highest floor since the Under side was unavailable)
                    if is_alt_fair:
                        floor = max(min_edge_pct, alt_min_edge_pct)
                    elif fair_source.startswith("Pinnacle"):
                        floor = min_edge_pct
                    else:
                        floor = max(min_edge_pct, consensus_min_edge_pct)
                    if edge < floor:
                        continue
                    kf = kelly_fraction(fair_prob, best["price"], kelly_mult) or 0.0
                    stake = min(bankroll_cash * kf, max_stake)
                    stat = prop_market_label(mk)
                    out.append({
                        "_sport": sport_key,
                        "event_id": g.get("id"),
                        "espn_event_id": espn_ev.get("id") if espn_ev else None,
                        "home_team": home_name,
                        "away_team": away_name,
                        "matchup": f"{away_name} @ {home_name}",
                        "market": mk,
                        "market_label": f"Prop · {stat}",
                        "is_prop": True,
                        "prop_player": player,
                        "prop_stat": stat,
                        "side_label": f"{player} {'Over' if side == 'over' else 'Under'} {pt:g}",
                        "point": pt,
                        "picked_side_key": "home" if side == "over" else "away",
                        "price": best["price"],
                        "price_cents": int(round(best["implied"] * 100)),
                        "book": best["book"],
                        "book_implied": best["implied"],
                        "fair_prob": fair_prob,
                        "fair_source": fair_source,
                        "edge": edge,
                        "edge_pct": edge * 100.0,
                        "kelly_frac": kf,
                        "stake_dollars": stake,
                        "kalshi_price_cents": None,
                        "kalshi_implied": None,
                        "kalshi_ticker": None,
                        "kalshi_url": None,
                        "candidates": candidates,
                        "start_utc": espn_ev.get("start_utc") if espn_ev else None,
                        "commence_time": g.get("commence_time"),
                        "home_abbr": ((espn_ev.get("home") or {}).get("abbr") or "").upper() if espn_ev else "",
                        "away_abbr": ((espn_ev.get("away") or {}).get("abbr") or "").upper() if espn_ev else "",
                        "status_state": espn_ev.get("status_state") if espn_ev else None,
                        "status_detail": espn_ev.get("status_detail") if espn_ev else None,
                        "home_score": (espn_ev.get("home") or {}).get("score") if espn_ev else None,
                        "away_score": (espn_ev.get("away") or {}).get("score") if espn_ev else None,
                        "grade": None,
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
