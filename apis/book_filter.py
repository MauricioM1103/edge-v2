"""Isolate a specific bookmaker's prices from an Odds API game dict."""
from __future__ import annotations

from typing import Optional


def book_market(game: dict, book_key_or_title: str, market_key: str) -> Optional[dict]:
    """Return {home_price, away_price, home_point, away_point} for one book+market."""
    if not game or not book_key_or_title:
        return None
    target = book_key_or_title.lower()
    home_team = game.get("home_team")
    away_team = game.get("away_team")
    for b in (game.get("bookmakers") or []):
        bk = (b.get("key") or "").lower()
        bt = (b.get("title") or "").lower()
        if target not in (bk, bt) and target not in bk and target not in bt:
            continue
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
                if o.get("name") == home_team or o.get("name") == "Over":
                    hp = price
                    hpt = point
                elif o.get("name") == away_team or o.get("name") == "Under":
                    ap = price
                    apt = point
            return {
                "home_price": hp,
                "away_price": ap,
                "home_point": hpt,
                "away_point": apt,
                "book_title": b.get("title"),
                "book_key": b.get("key"),
            }
    return None
