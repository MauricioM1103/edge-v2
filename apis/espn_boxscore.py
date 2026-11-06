"""ESPN box-score client for grading player props.

Uses the public (unofficial) ESPN summary endpoint:
    https://site.api.espn.com/apis/site/v2/sports/{path}/summary?event={event_id}

The response carries a `boxscore.players` structure — one block per team,
each with one or more stat groups (NBA: a single unnamed group; NFL:
passing / rushing / receiving / kicking ...; MLB: batting / pitching; NHL:
forwards / defenses / goalies) holding a `keys` array that names the columns
and an `athletes` list whose `stats` arrays line up with those keys. Soccer
summaries put the per-player stats under top-level `rosters` instead.

This module maps each Odds API prop key (see calc.agent.PROP_CATALOG) to the
ESPN column(s) that settle it, finds the player by a tolerant name match, and
returns the final number. No API key, same `_fetch` helper as apis.espn.

Public surface:
    lookup_player_stat(sport_key, event_id, player_name, prop_market_key) -> dict
        {status, value, player, label, detail}
        status is one of:
          "ok"          value is the player's final number for that prop
          "dnp"         player is on the box score but did not play -> void
          "not_found"   player is not on the box score at all      -> void
          "unsupported" ESPN's box score has no column for that prop
          "unavailable" fetch failed / box score not published yet -> retry later
    get_player_stat(...) -> Optional[float]
        Thin wrapper: the value when status == "ok", else None.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Dict, List, Optional, Tuple

from apis.espn import SPORT_PATHS, _fetch

ESPN_SUMMARY = "https://site.api.espn.com/apis/site/v2/sports/{path}/summary?event={event_id}"

# Small in-process cache so grading several props from the same game costs
# one HTTP call. Bounded so a long-running Streamlit process doesn't grow.
_SUMMARY_CACHE: Dict[Tuple[str, str], dict] = {}
_SUMMARY_CACHE_MAX = 64


# ---------------------------------------------------------------------------
# Sport families — the box-score shape is per sport, not per league.
# ---------------------------------------------------------------------------

def _family(sport_key: str) -> Optional[str]:
    path = SPORT_PATHS.get((sport_key or "").lower()) or ""
    head = path.split("/", 1)[0]
    return {
        "basketball": "basketball",
        "football": "football",
        "baseball": "baseball",
        "hockey": "hockey",
        "soccer": "soccer",
    }.get(head)


# A term is one box-score column that contributes to a prop's number.
#   groups : tuple of stat-group names the column lives in (None = any group)
#   key    : the ESPN column key
#   part   : for compound cells like "8-12" / "10/21", which half to read
#            (0 = made/completions, 1 = attempts); None = whole cell
# A prop's value is the sum of its terms (so PRA = points + rebounds + assists).
_SKATERS = ("forwards", "defenses", "skaters")

_BASKETBALL = {
    "player_points":                 [(None, "points", None)],
    "player_rebounds":               [(None, "rebounds", None)],
    "player_assists":                [(None, "assists", None)],
    "player_threes":                 [(None, "threePointFieldGoalsMade-threePointFieldGoalsAttempted", 0)],
    "player_blocks":                 [(None, "blocks", None)],
    "player_steals":                 [(None, "steals", None)],
    "player_turnovers":              [(None, "turnovers", None)],
    "player_points_rebounds_assists": [(None, "points", None), (None, "rebounds", None), (None, "assists", None)],
    "player_points_rebounds":        [(None, "points", None), (None, "rebounds", None)],
    "player_points_assists":         [(None, "points", None), (None, "assists", None)],
    "player_rebounds_assists":       [(None, "rebounds", None), (None, "assists", None)],
    "player_blocks_steals":          [(None, "blocks", None), (None, "steals", None)],
}

_FOOTBALL = {
    "player_pass_yds":          [(("passing",), "passingYards", None)],
    "player_pass_tds":          [(("passing",), "passingTouchdowns", None)],
    "player_pass_attempts":     [(("passing",), "completions/passingAttempts", 1)],
    "player_pass_completions":  [(("passing",), "completions/passingAttempts", 0)],
    "player_pass_interceptions": [(("passing",), "interceptions", None)],
    "player_rush_yds":          [(("rushing",), "rushingYards", None)],
    "player_rush_attempts":     [(("rushing",), "rushingAttempts", None)],
    "player_reception_yds":     [(("receiving",), "receivingYards", None)],
    "player_receptions":        [(("receiving",), "receptions", None)],
    "player_rush_reception_yds": [(("rushing",), "rushingYards", None), (("receiving",), "receivingYards", None)],
    "player_kicking_points":    [(("kicking",), "totalKickingPoints", None)],
}

_BASEBALL = {
    "batter_hits":          [(("batting",), "hits", None)],
    "batter_home_runs":     [(("batting",), "homeRuns", None)],
    "batter_rbis":          [(("batting",), "RBIs", None)],
    "batter_runs_scored":   [(("batting",), "runs", None)],
    "batter_hits_runs_rbis": [(("batting",), "hits", None), (("batting",), "runs", None), (("batting",), "RBIs", None)],
    "batter_strikeouts":    [(("batting",), "strikeouts", None)],
    "batter_walks":         [(("batting",), "walks", None)],
    "batter_total_bases":   "total_bases",          # special-cased: needs the play log
    "pitcher_strikeouts":   [(("pitching",), "strikeouts", None)],
    "pitcher_outs":         [(("pitching",), "fullInnings.partInnings", "outs")],
    "pitcher_earned_runs":  [(("pitching",), "earnedRuns", None)],
    "pitcher_hits_allowed": [(("pitching",), "hits", None)],
    "pitcher_walks":        [(("pitching",), "walks", None)],
}

_HOCKEY = {
    "player_points":          [(_SKATERS, "goals", None), (_SKATERS, "assists", None)],
    "player_assists":         [(_SKATERS, "assists", None)],
    "player_goals":           [(_SKATERS, "goals", None)],
    # ESPN's "S" column (key shotsTotal) is shots on goal; "SM" is misses.
    "player_shots_on_goal":   [(_SKATERS, "shotsTotal", None)],
    "player_blocked_shots":   [(_SKATERS, "blockedShots", None)],
    "player_total_saves":     [(("goalies",), "saves", None)],
    # The summary box score carries no power-play split -> unsupported.
}

_SOCCER = {
    "player_shots_on_target": [(None, "shotsOnTarget", None)],
    "player_shots":           [(None, "totalShots", None)],
    "player_assists":         [(None, "goalAssists", None)],
}

STAT_MAP: Dict[str, dict] = {
    "basketball": _BASKETBALL,
    "football": _FOOTBALL,
    "baseball": _BASEBALL,
    "hockey": _HOCKEY,
    "soccer": _SOCCER,
}

# When a found player has no row in the stat group a term needs:
#   "zero" -> he played but never recorded that stat (NFL: a RB with no
#             targets is absent from the receiving table) -> count 0
#   "void" -> he never took part in that role (MLB: a batter prop on a
#             pitcher who didn't bat; NHL saves on a backup) -> void
_MISSING_GROUP_POLICY = {
    "basketball": "zero",
    "football": "zero",
    "baseball": "void",
    "hockey": "void",
    "soccer": "zero",
}

# Short unit labels for the Ledger ("27 pts vs 26.5").
STAT_LABELS: Dict[str, str] = {
    "player_points": "pts", "player_rebounds": "reb", "player_assists": "ast",
    "player_threes": "3PM", "player_blocks": "blk", "player_steals": "stl",
    "player_turnovers": "TO", "player_points_rebounds_assists": "PRA",
    "player_points_rebounds": "P+R", "player_points_assists": "P+A",
    "player_rebounds_assists": "R+A", "player_blocks_steals": "B+S",
    "player_pass_yds": "pass yds", "player_pass_tds": "pass TD",
    "player_pass_attempts": "att", "player_pass_completions": "comp",
    "player_pass_interceptions": "INT", "player_rush_yds": "rush yds",
    "player_rush_attempts": "car", "player_reception_yds": "rec yds",
    "player_receptions": "rec", "player_rush_reception_yds": "rush+rec yds",
    "player_kicking_points": "kick pts",
    "batter_hits": "H", "batter_total_bases": "TB", "batter_home_runs": "HR",
    "batter_rbis": "RBI", "batter_runs_scored": "R", "batter_hits_runs_rbis": "H+R+RBI",
    "batter_strikeouts": "K", "batter_walks": "BB",
    "pitcher_strikeouts": "K", "pitcher_outs": "outs", "pitcher_earned_runs": "ER",
    "pitcher_hits_allowed": "H", "pitcher_walks": "BB",
    "player_goals": "G", "player_shots_on_goal": "SOG", "player_blocked_shots": "BS",
    "player_power_play_points": "PPP", "player_total_saves": "SV",
    "player_shots_on_target": "SOT", "player_shots": "shots",
}

# Hockey shares player_points / player_assists keys with basketball.
_HOCKEY_LABELS = {"player_points": "pts", "player_assists": "A"}


def stat_label(sport_key: str, prop_market_key: str) -> str:
    if _family(sport_key) == "hockey" and prop_market_key in _HOCKEY_LABELS:
        return _HOCKEY_LABELS[prop_market_key]
    return STAT_LABELS.get(prop_market_key) or prop_market_key.replace("player_", "").replace("_", " ")


# ---------------------------------------------------------------------------
# Name matching
# ---------------------------------------------------------------------------

_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


def normalize_name(name: str) -> str:
    """Lowercase ASCII, no accents / punctuation / generational suffixes.

    "Luka Dončić" -> "luka doncic"; "T.J. McConnell" -> "tj mcconnell";
    "Kentavious Caldwell-Pope" -> "kentavious caldwell pope";
    "Labaron Philon Jr." -> "labaron philon".
    """
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", str(name))
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.lower()
    s = s.replace("-", " ").replace("’", "").replace("'", "").replace(".", "")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    toks = [t for t in s.split() if t]
    while len(toks) > 1 and toks[-1] in _SUFFIXES:
        toks.pop()
    return " ".join(toks)


def _name_forms(name: str) -> dict:
    norm = normalize_name(name)
    toks = norm.split()
    return {
        "norm": norm,
        "compact": norm.replace(" ", ""),
        "first": toks[0] if toks else "",
        "initial": toks[0][0] if toks and toks[0] else "",
        "last": toks[-1] if toks else "",
        "rest": " ".join(toks[1:]) if len(toks) > 1 else "",
    }


def match_player(wanted: str, candidates: List[dict]) -> Tuple[Optional[dict], str]:
    """Pick the candidate whose name matches `wanted`.

    candidates: [{"id", "name", "short"}, ...] (one per distinct athlete).
    Returns (candidate, how) where how is "exact" / "initial" / "" (no match)
    or "ambiguous" when the fallback hits several athletes.

    Order: exact normalized full name -> compact (no spaces) -> last name +
    first initial (also against ESPN's shortName "L. James").
    """
    w = _name_forms(wanted)
    if not w["norm"]:
        return None, ""
    forms = [(_name_forms(c.get("name") or ""), c) for c in candidates]
    for f, c in forms:
        if f["norm"] and f["norm"] == w["norm"]:
            return c, "exact"
    for f, c in forms:
        if f["compact"] and f["compact"] == w["compact"]:
            return c, "exact"
    # Fallback: same last name (or same everything-after-first) + same first initial.
    hits = []
    for f, c in forms:
        if not f["last"] or not w["last"]:
            continue
        same_last = (f["last"] == w["last"]) or (f["rest"] and f["rest"] == w["rest"])
        if same_last and f["initial"] == w["initial"]:
            hits.append(c)
            continue
        short = _name_forms(c.get("short") or "")
        if short["norm"] and short["last"] == w["last"] and short["initial"] == w["initial"]:
            hits.append(c)
    if len(hits) == 1:
        return hits[0], "initial"
    if len(hits) > 1:
        return None, "ambiguous"
    return None, ""


# ---------------------------------------------------------------------------
# Fetch + flatten
# ---------------------------------------------------------------------------

def fetch_summary(sport_key: str, event_id: str) -> Optional[dict]:
    """Return the raw ESPN summary for an event (cached per process)."""
    path = SPORT_PATHS.get((sport_key or "").lower())
    if not path or not event_id:
        return None
    ck = ((sport_key or "").lower(), str(event_id))
    if ck in _SUMMARY_CACHE:
        return _SUMMARY_CACHE[ck]
    data = _fetch(ESPN_SUMMARY.format(path=path, event_id=event_id), timeout=12)
    if not isinstance(data, dict):
        return None
    if len(_SUMMARY_CACHE) >= _SUMMARY_CACHE_MAX:
        _SUMMARY_CACHE.clear()
    _SUMMARY_CACHE[ck] = data
    return data


def clear_cache() -> None:
    _SUMMARY_CACHE.clear()


def _num(s) -> Optional[float]:
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    s = str(s).strip()
    if not s or s in ("-", "--"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _part(s, idx: int) -> Optional[float]:
    """Read half of a compound cell: "8-12" -> made/att, "10/21" -> comp/att."""
    if s is None:
        return None
    bits = re.split(r"[-/]", str(s).strip())
    if len(bits) < 2:
        return _num(s)
    try:
        return _num(bits[idx])
    except IndexError:
        return None


def _outs_from_ip(s) -> Optional[float]:
    """Innings pitched "5.2" -> 17 outs (the .1/.2 convention)."""
    if s is None:
        return None
    txt = str(s).strip()
    if not txt:
        return None
    if "." in txt:
        full, _, part = txt.partition(".")
    else:
        full, part = txt, "0"
    try:
        return float(int(full or 0) * 3 + int(part or 0))
    except ValueError:
        return None


def _rows_from_boxscore(summary: dict) -> List[dict]:
    """Flatten boxscore.players into rows:
    {athlete_id, name, short, group, stats: {key: raw}, dnp: bool, raw}.
    """
    rows: List[dict] = []
    box = summary.get("boxscore") or {}
    for team_block in box.get("players") or []:
        for grp in team_block.get("statistics") or []:
            gname = (grp.get("name") or grp.get("type") or "").lower()
            keys = grp.get("keys") or grp.get("names") or grp.get("labels") or []
            for a in grp.get("athletes") or []:
                ath = a.get("athlete") or {}
                vals = a.get("stats") or []
                stats = {k: vals[i] for i, k in enumerate(keys) if i < len(vals)}
                dnp = bool(a.get("didNotPlay")) or not vals
                # Hockey lists dressed-but-unused goalies with 0:00 TOI.
                toi = stats.get("timeOnIce")
                if toi is not None and str(toi).strip() in ("0:00", "00:00", "0"):
                    dnp = True
                rows.append({
                    "athlete_id": str(ath.get("id") or ""),
                    "name": ath.get("displayName") or ath.get("fullName") or "",
                    "short": ath.get("shortName") or "",
                    "group": gname,
                    "stats": stats,
                    "dnp": dnp,
                    "raw": a,
                })
    return rows


def _rows_from_rosters(summary: dict) -> List[dict]:
    """Soccer: per-player stats live under rosters[].roster[].stats[{name,value}]."""
    rows: List[dict] = []
    for side in summary.get("rosters") or []:
        for a in side.get("roster") or []:
            ath = a.get("athlete") or {}
            stats = {}
            for s in a.get("stats") or []:
                if isinstance(s, dict) and s.get("name"):
                    stats[s["name"]] = s.get("value", s.get("displayValue"))
            apps = _num(stats.get("appearances"))
            played = bool(a.get("starter")) or bool(a.get("subbedIn"))
            if apps is not None:
                played = played or apps > 0
            rows.append({
                "athlete_id": str(ath.get("id") or ""),
                "name": ath.get("displayName") or ath.get("fullName") or "",
                "short": ath.get("shortName") or "",
                "group": "",
                "stats": stats,
                "dnp": not played,
                "raw": a,
            })
    return rows


def _player_rows(summary: dict, family: str) -> List[dict]:
    rows = _rows_from_boxscore(summary)
    if not rows and family == "soccer":
        rows = _rows_from_rosters(summary)
    elif family == "soccer" and not any(r["stats"] for r in rows):
        rows = _rows_from_rosters(summary) or rows
    return rows


def _candidates(rows: List[dict]) -> List[dict]:
    seen = {}
    for r in rows:
        key = r["athlete_id"] or normalize_name(r["name"])
        if key and key not in seen:
            seen[key] = {"id": key, "name": r["name"], "short": r["short"]}
    return list(seen.values())


# ---------------------------------------------------------------------------
# Baseball total bases — ESPN's batting table has no TB column, so walk the
# player's at-bats through the play log (atBats[].playId -> plays[].text).
# ---------------------------------------------------------------------------

_TB_PATTERNS = [
    (re.compile(r"\b(home run|homer(?:ed|s)?|grand slam)\b", re.I), 4),
    (re.compile(r"\btripl(?:ed|e|es)\b", re.I), 3),
    (re.compile(r"\bdoubl(?:ed|e|es)\b", re.I), 2),
    (re.compile(r"\bsingl(?:ed|e|es)\b", re.I), 1),
]


def _bases_from_text(text: str) -> int:
    t = text or ""
    # "...hit into a double play" / "fielder's choice" are outs, not doubles.
    if re.search(r"double play|triple play", t, re.I):
        return 0
    for pat, bases in _TB_PATTERNS:
        if pat.search(t):
            return bases
    return 0


def _total_bases(summary: dict, batting_row: dict) -> Tuple[Optional[float], str]:
    stats = batting_row["stats"]
    hits = _num(stats.get("hits"))
    if hits is not None and hits == 0:
        return 0.0, "no hits"
    plays = summary.get("plays") or []
    by_id = {str(p.get("id")): p for p in plays if isinstance(p, dict) and p.get("id") is not None}
    ab_refs = batting_row["raw"].get("atBats") or []
    if by_id and ab_refs:
        total = 0
        counted_hits = 0
        for ab in ab_refs:
            pl = by_id.get(str(ab.get("playId") or ab.get("id") or ""))
            if not pl:
                continue
            b = _bases_from_text(pl.get("text") or "")
            if b:
                total += b
                counted_hits += 1
        # Sanity: the play log should account for every hit the box score shows.
        if hits is None or counted_hits == hits:
            return float(total), "from play log"
    # Fallback: team batting notes list doubles/triples by last name, e.g.
    # "Contreras (2, Cleavinger); Turang (4, Rasmussen)". HR comes from the row.
    hr = _num(stats.get("homeRuns")) or 0.0
    last = _name_forms(batting_row["name"])["last"]
    extra = {"doubles": 0, "triples": 0}
    found_notes = False
    for team_block in (summary.get("boxscore") or {}).get("teams") or []:
        for det in team_block.get("details") or []:
            if (det.get("name") or "").lower() != "battingdetails":
                continue
            for s in det.get("stats") or []:
                nm = (s.get("name") or "").lower()
                if nm not in extra:
                    continue
                found_notes = True
                for entry in (s.get("displayValue") or "").split(";"):
                    e = normalize_name(entry.split("(")[0])
                    if not e:
                        continue
                    toks = e.split()
                    mult = 1
                    if toks and toks[-1].isdigit():
                        mult = int(toks.pop())
                    if toks and toks[-1] == last:
                        extra[nm] += mult
    if hits is not None and (found_notes or (extra["doubles"] == 0 and extra["triples"] == 0 and hr == hits)):
        singles = hits - extra["doubles"] - extra["triples"] - hr
        if singles >= 0:
            return float(singles + 2 * extra["doubles"] + 3 * extra["triples"] + 4 * hr), "from batting notes"
    return None, "play log unavailable"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _result(status: str, value=None, player: str = "", label: str = "", detail: str = "") -> dict:
    return {"status": status, "value": value, "player": player, "label": label, "detail": detail}


def lookup_player_stat(sport_key: str, event_id: str, player_name: str,
                       prop_market_key: str) -> dict:
    """Resolve a player's final number for an Odds API prop market.

    See the module docstring for the status vocabulary. Never raises on bad
    data — anything unexpected becomes "unavailable" so the caller can retry.
    """
    sport_key = (sport_key or "").lower()
    label = stat_label(sport_key, prop_market_key)
    family = _family(sport_key)
    if not family:
        return _result("unsupported", label=label, detail=f"no ESPN path for sport {sport_key!r}")
    terms = STAT_MAP.get(family, {}).get(prop_market_key)
    if terms is None:
        return _result("unsupported", label=label,
                       detail=f"ESPN {family} box score has no column for {prop_market_key}")
    summary = fetch_summary(sport_key, event_id)
    if not summary:
        return _result("unavailable", label=label, detail="ESPN summary fetch failed")
    try:
        rows = _player_rows(summary, family)
    except (AttributeError, TypeError, KeyError):
        rows = []
    if not rows:
        return _result("unavailable", label=label, detail="box score not published yet")

    cand, how = match_player(player_name, _candidates(rows))
    if cand is None:
        if how == "ambiguous":
            return _result("not_found", label=label,
                           detail=f"{player_name!r} matches several players on the box score")
        return _result("not_found", label=label, detail=f"{player_name!r} not on the ESPN box score")
    mine = [r for r in rows if (r["athlete_id"] or normalize_name(r["name"])) == cand["id"]]
    matched = cand["name"]
    if mine and all(r["dnp"] for r in mine):
        reason = ""
        for r in mine:
            reason = r["raw"].get("reason") or reason
        return _result("dnp", player=matched, label=label,
                       detail=f"did not play{(' · ' + reason.lower()) if reason else ''}")

    # Baseball total bases needs the play log, not a column.
    if terms == "total_bases":
        bat = next((r for r in mine if r["group"] == "batting"), None)
        if bat is None:
            return _result("dnp", player=matched, label=label, detail="did not bat")
        tb, how_tb = _total_bases(summary, bat)
        if tb is None:
            return _result("unavailable", player=matched, label=label, detail=how_tb)
        return _result("ok", value=tb, player=matched, label=label, detail=how_tb)

    policy = _MISSING_GROUP_POLICY.get(family, "zero")
    total = 0.0
    any_value = False
    for groups, key, part in terms:
        pool = [r for r in mine if (groups is None or r["group"] in groups) and not r["dnp"]]
        row = next((r for r in pool if key in r["stats"]), None)
        if row is None:
            if policy == "void":
                return _result("dnp", player=matched, label=label,
                               detail=f"no {'/'.join(groups or ('',)) or 'box-score'} line for this player")
            continue  # played, never recorded this stat -> 0
        raw = row["stats"].get(key)
        if part == "outs":
            v = _outs_from_ip(raw)
        elif isinstance(part, int):
            v = _part(raw, part)
        else:
            v = _num(raw)
        if v is None:
            return _result("unavailable", player=matched, label=label,
                           detail=f"could not parse {key}={raw!r}")
        total += v
        any_value = True
    if not any_value and policy == "void":
        return _result("dnp", player=matched, label=label, detail="no stat line")
    return _result("ok", value=float(total), player=matched, label=label,
                   detail=f"matched {how}")


def get_player_stat(sport_key: str, event_id: str, player_name: str,
                    prop_market_key: str) -> Optional[float]:
    """The player's final number for the prop, or None when it can't be read
    (did not play, not on the box score, unsupported stat, fetch failed).
    Use lookup_player_stat() when you need to tell those cases apart.
    """
    res = lookup_player_stat(sport_key, event_id, player_name, prop_market_key)
    return res["value"] if res["status"] == "ok" else None
