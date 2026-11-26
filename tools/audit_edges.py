"""Audit edge detection over every cached Odds API game (no API credits used).

    python tools/audit_edges.py


For each sport x market: run the app's scanners, recompute fair/edge
independently from the raw outcomes (exact name match, no helpers shared with
the scanner), and report mismatches, name-resolution problems, stale quotes
and what the board's side-collapse does to +EV rows.
"""
import sys, json, glob, collections, datetime as dt
import os
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from calc import agent
from apis import kalshi as K
from apis.team_mapping import same_team

SHOP = ["Bovada", "LowVig.ag", "BetOnline.ag", "BetUS", "MyBookie.ag"]
SHOP_L = {b.lower() for b in SHOP}
now = dt.datetime.now(dt.timezone.utc)

def imp(a):
    a = float(a)
    return 100.0 / (a + 100.0) if a > 0 else abs(a) / (abs(a) + 100.0)

games = {}
for f in glob.glob(os.path.join(ROOT, ".odds_cache", "*.json")):
    try:
        d = json.load(open(f, encoding="utf-8"))
    except Exception:
        continue
    items = d if isinstance(d, list) else ([d] if isinstance(d, dict) and "bookmakers" in d else [])
    for g in items:
        if not isinstance(g, dict) or not g.get("id"):
            continue
        cur = games.setdefault(g["id"], dict(g, bookmakers=[]))
        have = {(b.get("key"), m.get("key")) for b in cur["bookmakers"] for m in b.get("markets") or []}
        for b in g.get("bookmakers") or []:
            tgt = next((x for x in cur["bookmakers"] if x.get("key") == b.get("key")), None)
            if tgt is None:
                tgt = dict(b, markets=[]); cur["bookmakers"].append(tgt)
            for m in b.get("markets") or []:
                if (b.get("key"), m.get("key")) not in have:
                    tgt["markets"].append(m)

def app_sport(sk):
    sk = sk or ""
    if sk.startswith("soccer"): return "epl"
    if sk.startswith("tennis_atp"): return "atp"
    if sk.startswith("tennis_wta"): return "wta"
    if sk.startswith("mma"): return "ufc"
    return {"basketball_nba": "nba", "icehockey_nhl": "nhl", "americanfootball_nfl": "nfl",
            "americanfootball_ncaaf": "ncaaf", "basketball_ncaab": "ncaab", "baseball_mlb": "mlb"}.get(sk, sk)

by_sport = collections.defaultdict(list)
for g in games.values():
    by_sport[g.get("sport_key")].append(g)

print(f"games in cache: {len(games)}")
issues = collections.Counter()
detail = collections.defaultdict(list)
rows_out = []
for sk, gl in sorted(by_sport.items()):
    sp = app_sport(sk)
    mkts = collections.Counter(m.get("key") for g in gl for b in g["bookmakers"] for m in b.get("markets") or [])
    game_mk = [m for m in mkts if not (m.startswith("player_") or m.startswith("batter_") or m.startswith("pitcher_"))]
    prop_mk = [m for m in mkts if m not in game_mk]
    pinn = sum(1 for g in gl if any((b.get("title") or "").lower() == "pinnacle" for b in g["bookmakers"]))
    # --- name resolution: does same_team agree with exact equality?
    for g in gl:
        h, a = g.get("home_team"), g.get("away_team")
        if same_team(h, a):
            issues["home/away indistinguishable to same_team"] += 1
            detail["collide"].append(f"{sk}: {a} @ {h}")
    # --- staleness
    for g in gl:
        for b in g["bookmakers"]:
            lu = b.get("last_update")
            for m in b.get("markets") or []:
                lu2 = m.get("last_update") or lu
                try:
                    age = (now - dt.datetime.fromisoformat(lu2.replace("Z", "+00:00"))).total_seconds() / 3600
                except Exception:
                    continue
                detail["age:" + (b.get("title") or "")].append(age)
    # --- run scanner
    try:
        edges = agent.scan_offshore_markets_vs_pinnacle(gl, 500.0, sport_key=sp, markets=game_mk, shop_books=SHOP,
                                                         min_edge_pct=-1.0, max_bet_pct=0.025)
    except Exception as ex:
        issues[f"offshore scanner crashed on {sk}"] += 1; detail["crash"].append(f"{sk}: {ex!r}"); edges = []
    try:
        pedges = agent.scan_player_props_vs_pinnacle(gl, 500.0, sport_key=sp, prop_markets=prop_mk, shop_books=SHOP,
                                                     min_edge_pct=0.01, max_bet_pct=0.025) if prop_mk else []
    except Exception as ex:
        issues[f"prop scanner crashed on {sk}"] += 1; detail["crash"].append(f"{sk}: {ex!r}"); pedges = []
    # --- independent recompute for game markets
    gid = {g["id"]: g for g in gl}
    mism = 0
    for e in edges:
        g = gid[e["event_id"]]; mk = e["market"]
        pin = next((m for b in g["bookmakers"] if (b.get("title") or "").lower() == "pinnacle"
                    for m in b.get("markets") or [] if m.get("key") == mk), None)
        outs = pin.get("outcomes") or []
        tot = sum(imp(o["price"]) for o in outs)
        want = e["side_label"]
        def is_side(o):
            n = o.get("name")
            sk_ = e["picked_side_key"]
            if n in ("Over", "Yes"): return sk_ == "home"
            if n in ("Under", "No"): return sk_ == "away"
            if (n or "").lower() == "draw": return sk_ == "draw"
            return (n == g["home_team"] and sk_ == "home") or (n == g["away_team"] and sk_ == "away")
        mine = [o for o in outs if is_side(o)]
        if len(mine) != 1:
            issues["side not uniquely resolvable"] += 1; continue
        fair = imp(mine[0]["price"]) / tot
        if abs(fair - e["fair_prob"]) > 1e-6:
            mism += 1; detail["mismatch"].append(f"{sk} {mk} {e['matchup']} {want}: scanner fair {e['fair_prob']:.4f} vs {fair:.4f}")
        # the shop price must be the same bet
        bo = next((o for b in g["bookmakers"] if (b.get("title") or "") == e["book"]
                   for m in b.get("markets") or [] if m.get("key") == mk
                   for o in m.get("outcomes") or [] if is_side(o)), None)
        if bo is None or abs(float(bo["price"]) - float(e["price"])) > 1e-9:
            issues["shop price not found for same side"] += 1
        elif mine[0].get("point") is not None and bo.get("point") != mine[0].get("point"):
            issues["shop point differs from Pinnacle point"] += 1
        if len(outs) not in (2, 3):
            issues[f"{len(outs)}-outcome Pinnacle market ({mk})"] += 1
    issues["fair mismatches"] += mism
    # --- what the board collapse does (keep higher fair prob per event/market/abs point)
    grp = collections.defaultdict(list)
    for e in edges:
        pt = e.get("point")
        if e["market"].startswith("spreads") and pt is not None: pt = abs(float(pt))
        grp[(e["event_id"], e["market"], pt)].append(e)
    dropped = 0
    for k, es in grp.items():
        keep = max(es, key=lambda x: (x["edge"], x["fair_prob"]))  # the Board's rule: bigger edge wins
        best = max(es, key=lambda x: x["edge"])
        if best["edge"] > 0 and keep is not best:
            dropped += 1
            detail["dropped"].append(f"{sk} {best['market']} {best['matchup']}: hidden {best['side_label']} {best['edge']*100:+.1f}% "
                                     f"(shown {keep['side_label']} {keep['edge']*100:+.1f}%)")
        if sum(1 for x in es if x["edge"] > 0.02) > 1:
            issues["both sides >2% edge (arb / stale)"] += 1
    issues["+EV side hidden by board collapse"] += dropped
    pos = [e for e in edges if e["edge"] > 0]; big = [e for e in edges if e["edge"] > 0.08]
    for e in big:
        detail["big"].append(f"{sk} {e['market']} {e['matchup']} {e['side_label']} @{e['book']} {e['price']:+.0f}: fair {e['fair_prob']*100:.1f} edge {e['edge']*100:+.1f}")
    pbig = [e for e in pedges if e["edge"] > 0.08]
    for e in pbig:
        detail["big"].append(f"{sk} {e['market']} {e['side_label']} @{e['book']} {e['price']:+.0f}: fair {e['fair_prob']*100:.1f} ({e['fair_source']}) edge {e['edge']*100:+.1f}")
    rows_out.append((sk, len(gl), pinn, ",".join(sorted(game_mk)), len(edges), len(pos), sum(1 for e in edges if e["edge"] >= 0.02),
                     len(prop_mk), len(pedges), collections.Counter(e["fair_source"].split("-book")[-1].strip() or e["fair_source"] for e in pedges)))

print("\nsport | games | w/Pinnacle | game markets | rows | +EV | tierA | prop mkts | prop +EV | prop fair sources")
for r in rows_out:
    print(" | ".join(str(x) for x in r))
print("\nISSUES"); [print(f"  {v:4d}  {k}") for k, v in issues.most_common()]
for k in ("crash", "collide", "mismatch"):
    if detail[k]: print(f"\n{k}:"); [print("  ", x) for x in detail[k][:15]]
print(f"\ndropped +EV sides ({len(detail['dropped'])}):"); [print("  ", x) for x in detail["dropped"][:25]]
print(f"\nedges over 8% ({len(detail['big'])}):"); [print("  ", x) for x in detail["big"][:30]]
print("\nquote age by book (hours: median / max / n):")
for k, v in sorted(detail.items()):
    if k.startswith("age:"):
        v = sorted(v); print(f"   {k[4:]:16s} {v[len(v)//2]:6.2f} {v[-1]:7.2f} {len(v)}")
