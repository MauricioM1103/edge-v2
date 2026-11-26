"""Keep a copy of the ledger in a private GitHub repo so it survives restarts.

Streamlit Community Cloud gives the app a fresh disk on every redeploy or
reboot, so `picks.json` on the hosted copy was lost each time. With two
secrets set, the ledger is also stored as a file in a private repo:

    ledger_github_repo  = "owner/private-repo"
    ledger_github_token = "<fine-grained token, Contents: read and write on that repo>"
    ledger_github_path  = "picks.json"        # optional
    ledger_github_branch = "main"             # optional

* On start, if the local ledger is missing or empty, the stored copy is
  pulled down first.
* After each save the copy is pushed. Changes that matter (a pick added,
  removed, settled, re-priced) go at once; "still on the board" timestamp
  refreshes are batched so a Board that reruns every few seconds does not
  write a commit every few seconds.

Without the secrets nothing here runs, which is the case on a local machine.
The token is never logged, stored on disk, or put in a message.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from typing import List, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

API = "https://api.github.com"
#: Fields that change on every scan and say nothing about the bets themselves.
_VOLATILE_FIELDS = ("last_seen_on_board",)
#: Seconds between pushes that carry only volatile changes.
QUIET_PUSH_INTERVAL_S = 15 * 60
#: Seconds between pushes of any kind (several picks logged in one scan
#: become one commit).
MIN_PUSH_INTERVAL_S = 5

#: What the UI shows. Updated on every pull and push attempt.
#: app.py reloads every local module on each rerun; reading the previous
#: objects back out of globals() keeps this state for the life of the process.
STATUS = globals().get("STATUS") or {
    "configured": False, "restored_rows": None, "last_push_at": None, "last_error": None,
    "last_error_at": None, "pending": False}

_state = globals().get("_state") or {
    "restored_for": None, "sha": None, "material": None, "last_push": 0.0, "last_quiet_push": 0.0}


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------
def _secret(name: str) -> Optional[str]:
    v = os.environ.get(name.upper())
    if v:
        return v
    try:
        import streamlit as st
        if hasattr(st, "secrets"):
            try:
                v = st.secrets.get(name)  # type: ignore[attr-defined]
                if v:
                    return str(v)
            except Exception:
                return None
    except ImportError:
        return None
    return None


def config() -> Optional[dict]:
    repo, token = _secret("ledger_github_repo"), _secret("ledger_github_token")
    if not repo or not token or "/" not in repo:
        STATUS["configured"] = False
        return None
    STATUS["configured"] = True
    return {
        "repo": repo.strip(), "token": token.strip(),
        "path": (_secret("ledger_github_path") or "picks.json").strip().lstrip("/"),
        "branch": (_secret("ledger_github_branch") or "main").strip(),
    }


# --------------------------------------------------------------------------
# transport (replaced in tests)
# --------------------------------------------------------------------------
def _http(method: str, url: str, token: str, body: Optional[dict] = None, timeout: float = 12.0):
    """Return (status_code, parsed_json_or_None). Never raises for HTTP errors."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "edge-ledger-sync",
        **({"Content-Type": "application/json"} if data else {}),
    })
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except HTTPError as ex:
        try:
            payload = json.loads(ex.read() or b"null")
        except ValueError:
            payload = None
        return ex.code, payload
    except (URLError, TimeoutError, ConnectionError, OSError) as ex:
        return 0, {"message": type(ex).__name__}


def _fail(what: str, status: int, payload) -> None:
    msg = (payload or {}).get("message") if isinstance(payload, dict) else None
    hint = {401: "token rejected", 403: "token lacks access", 404: "repo, branch or file not found",
            409: "conflict", 422: "conflict", 0: "network error"}.get(status, "")
    STATUS["last_error"] = f"{what} failed (HTTP {status}{', ' + hint if hint else ''}{': ' + str(msg)[:80] if msg else ''})"
    STATUS["last_error_at"] = time.time()


def _contents_url(cfg: dict) -> str:
    return f"{API}/repos/{cfg['repo']}/contents/{cfg['path']}"


def _fetch(cfg: dict):
    """Return (rows_or_None, sha_or_None, found). rows is None on any failure."""
    status, payload = _http("GET", f"{_contents_url(cfg)}?ref={cfg['branch']}", cfg["token"])
    if status == 404:
        return [], None, False
    if status != 200 or not isinstance(payload, dict):
        _fail("Reading the stored ledger", status, payload)
        return None, None, False
    sha = payload.get("sha")
    content = payload.get("content") or ""
    if not content and payload.get("git_url"):
        # Files over 1 MB come back without content; the blob endpoint has it.
        status2, blob = _http("GET", payload["git_url"], cfg["token"])
        if status2 != 200 or not isinstance(blob, dict):
            _fail("Reading the stored ledger", status2, blob)
            return None, sha, True
        content = blob.get("content") or ""
    try:
        rows = json.loads(base64.b64decode(content).decode("utf-8")) if content.strip() else []
    except (ValueError, UnicodeDecodeError):
        STATUS["last_error"] = "The stored ledger is not valid JSON; it was left untouched"
        STATUS["last_error_at"] = time.time()
        return None, sha, True
    return (rows if isinstance(rows, list) else []), sha, True


# --------------------------------------------------------------------------
# public hooks
# --------------------------------------------------------------------------
def material_fingerprint(picks: List[dict]) -> str:
    """Hash of everything except the fields that tick on every scan."""
    slim = [{k: v for k, v in p.items() if k not in _VOLATILE_FIELDS} for p in picks if isinstance(p, dict)]
    return hashlib.sha256(json.dumps(slim, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def restore_if_needed(path: str, local_rows: Optional[int]) -> Optional[List[dict]]:
    """Once per process: if the local ledger is missing or empty, return the
    stored rows so the caller can write them. Returns None when there is
    nothing to restore (not configured, local already has rows, fetch failed,
    or nothing stored yet)."""
    if _state["restored_for"] == path:
        return None
    cfg = config()
    if cfg is None:
        _state["restored_for"] = path
        return None
    rows, sha, found = _fetch(cfg)
    if rows is None:
        return None  # failed: leave restored_for unset so the next load retries
    _state["restored_for"] = path
    _state["sha"] = sha
    if local_rows:
        # The local file already has picks (same process kept running, or a
        # local machine was pointed at the repo). Never replace it.
        _state["material"] = None
        return None
    if not rows:
        return None
    _state["material"] = material_fingerprint(rows)
    # Only the quiet timer starts here; a real change right after a restore
    # (a pick logged by the first scan) must not wait.
    _state["last_quiet_push"] = time.time()
    STATUS["restored_rows"] = len(rows)
    STATUS["last_error"] = None
    return rows


def after_save(picks: List[dict], force: bool = False) -> bool:
    """Push the ledger if it changed enough to be worth a commit.

    Returns True when a push happened. Never raises: the local save has
    already succeeded and must not be undone by a network problem.
    """
    cfg = config()
    if cfg is None:
        return False
    now = time.time()
    fp = material_fingerprint(picks)
    material_changed = fp != _state["material"]
    if not force:
        if material_changed:
            if now - _state["last_push"] < MIN_PUSH_INTERVAL_S:
                STATUS["pending"] = True
                return False
        elif now - _state["last_quiet_push"] < QUIET_PUSH_INTERVAL_S:
            return False
    body_text = json.dumps(picks, indent=2, default=str)
    for attempt in range(2):
        body = {"message": f"ledger: {len(picks)} picks", "branch": cfg["branch"],
                "content": base64.b64encode(body_text.encode("utf-8")).decode("ascii")}
        if _state["sha"]:
            body["sha"] = _state["sha"]
        status, payload = _http("PUT", _contents_url(cfg), cfg["token"], body)
        if status in (200, 201) and isinstance(payload, dict):
            _state["sha"] = ((payload.get("content") or {}).get("sha")) or _state["sha"]
            _state["material"] = fp
            _state["last_push"] = _state["last_quiet_push"] = now
            STATUS.update(last_push_at=now, last_error=None, pending=False)
            return True
        if status in (409, 422) and attempt == 0:
            # Someone else wrote the file (or we had no sha yet). Take the
            # current sha and try once more with our rows.
            _, sha, _ = _fetch(cfg)
            if sha:
                _state["sha"] = sha
                continue
        _fail("Saving the ledger copy", status, payload)
        STATUS["pending"] = True
        return False
    return False


def describe_status() -> str:
    """One line for the Ledger tab, or "" when the feature is off."""
    if not STATUS.get("configured") and config() is None:
        return ""
    parts = []
    if STATUS.get("restored_rows"):
        parts.append(f"restored {STATUS['restored_rows']} picks at start")
    if STATUS.get("last_push_at"):
        mins = max(0, int((time.time() - STATUS["last_push_at"]) // 60))
        parts.append("saved just now" if mins == 0 else f"last saved {mins} min ago")
    elif not STATUS.get("last_error"):
        parts.append("nothing saved yet this session")
    if STATUS.get("last_error"):
        parts.append("PROBLEM: " + STATUS["last_error"])
    return "Ledger copy in GitHub: " + " · ".join(parts)


def _reset_for_tests() -> None:
    _state.update(restored_for=None, sha=None, material=None, last_push=0.0, last_quiet_push=0.0)
    STATUS.update(configured=False, restored_rows=None, last_push_at=None, last_error=None,
                  last_error_at=None, pending=False)
