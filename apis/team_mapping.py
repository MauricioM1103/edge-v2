"""Normalize team names so ESPN schedules can be joined with Odds API lines.

Both APIs are inconsistent:
  * ESPN uses "Los Angeles Lakers", abbreviation "LAL".
  * Odds API uses "Los Angeles Lakers" or sometimes "LA Lakers".
  * NFL "Washington Commanders" / older "Washington Football Team".
  * College teams vary enormously.

The strategy: canonicalize to a lowercased, punctuation-stripped key that
tolerates minor differences. For known-ambiguous cases we add explicit aliases.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Dict, Optional

_PUNCT_RE = re.compile(r"[^a-z0-9 ]+")
_WS_RE = re.compile(r"\s+")

# Known aliases: force these to resolve to the same canonical form.
_ALIASES: Dict[str, str] = {
    "la lakers": "los angeles lakers",
    "la clippers": "los angeles clippers",
    "la rams": "los angeles rams",
    "la chargers": "los angeles chargers",
    "la angels": "los angeles angels",
    "la kings": "los angeles kings",
    "la dodgers": "los angeles dodgers",
    "ny giants": "new york giants",
    "ny jets": "new york jets",
    "ny yankees": "new york yankees",
    "ny mets": "new york mets",
    "ny knicks": "new york knicks",
    "ny rangers": "new york rangers",
    "ny islanders": "new york islanders",
    "sf giants": "san francisco giants",
    "sf 49ers": "san francisco 49ers",
    "sd padres": "san diego padres",
    "tb buccaneers": "tampa bay buccaneers",
    "tb rays": "tampa bay rays",
    "tb lightning": "tampa bay lightning",
    "washington football team": "washington commanders",
    "washington redskins": "washington commanders",
    "oakland raiders": "las vegas raiders",
    "st louis rams": "los angeles rams",
    "san diego chargers": "los angeles chargers",
    "cleveland guardians": "cleveland guardians",
    "cleveland indians": "cleveland guardians",
}


def canonical(name) -> str:
    """Canonical lowercase, punct-stripped, whitespace-collapsed team key."""
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", str(name))
    s = s.encode("ascii", "ignore").decode("ascii")
    s = s.lower().strip()
    s = _PUNCT_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    return _ALIASES.get(s, s)


def same_team(a, b) -> bool:
    """True when a and b map to the same canonical team key."""
    if not a or not b:
        return False
    ca, cb = canonical(a), canonical(b)
    if not ca or not cb:
        return False
    if ca == cb:
        return True
    # last-word fallback: "lakers" matches "los angeles lakers"
    return ca.split()[-1] == cb.split()[-1] and ca.split()[-1] not in {
        "giants", "cardinals", "rangers", "kings"  # ambiguous across sports/cities
    }


def find_match(target_name, candidates) -> Optional[int]:
    """Return index of candidate whose canonical name matches target, or None."""
    if not target_name or not candidates:
        return None
    tc = canonical(target_name)
    if not tc:
        return None
    for i, c in enumerate(candidates):
        if canonical(c) == tc:
            return i
    # last-word fallback
    for i, c in enumerate(candidates):
        cc = canonical(c)
        if cc and cc.split()[-1] == tc.split()[-1]:
            return i
    return None
