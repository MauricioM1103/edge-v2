"""Ledger copy in a private GitHub repo: restore on a fresh disk, push on save.

Runs against an in-memory stand-in for the GitHub contents API; no network.
"""
import base64
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from storage import picks as picks_store  # noqa: E402
from storage import remote  # noqa: E402

FAILS = 0
TOKEN = "tok_SECRET_do_not_print"


def check(name, cond, detail=""):
    global FAILS
    if not cond:
        FAILS += 1
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))


class FakeGitHub:
    """Just enough of GET/PUT /repos/{repo}/contents/{path}."""

    def __init__(self, rows=None):
        self.rows = rows
        self.sha = "sha0" if rows is not None else None
        self.calls = []
        self.fail_with = None        # (status, message) for every call
        self.conflict_once = False   # first PUT answers 409

    def __call__(self, method, url, token, body=None, timeout=12.0):
        self.calls.append((method, token == TOKEN))
        if self.fail_with:
            return self.fail_with[0], {"message": self.fail_with[1]}
        if method == "GET":
            if self.rows is None:
                return 404, {"message": "Not Found"}
            content = base64.b64encode(json.dumps(self.rows).encode()).decode()
            return 200, {"sha": self.sha, "content": content}
        if method == "PUT":
            if self.conflict_once:
                self.conflict_once = False
                self.sha = "sha_other"
                return 409, {"message": "is at sha_other but expected ..."}
            if self.rows is not None and body.get("sha") != self.sha:
                return 409, {"message": "sha mismatch"}
            self.rows = json.loads(base64.b64decode(body["content"]).decode())
            self.sha = f"sha{len(self.calls)}"
            return 200 if body.get("sha") else 201, {"content": {"sha": self.sha}}
        return 405, None

    def puts(self):
        return sum(1 for m, _ in self.calls if m == "PUT")


def setup(fake, configured=True):
    remote._reset_for_tests()
    remote._http = fake
    secrets = {"ledger_github_repo": "me/edge-data", "ledger_github_token": TOKEN} if configured else {}
    remote._secret = lambda name: secrets.get(name)


def row(i, **kw):
    return dict({"id": f"r{i}", "selection": f"Pick {i}", "status": "open", "price_american": 110,
                 "risk_units": 1.0, "last_seen_on_board": "2026-11-25T00:00:00+00:00"}, **kw)


def main():
    real_default, real_http, real_secret = picks_store._default_path, remote._http, remote._secret
    tmpdir = tempfile.mkdtemp(prefix="edge_remote_")
    path = os.path.join(tmpdir, "picks.json")
    picks_store._default_path = lambda: path
    try:
        print("== not configured")
        fake = FakeGitHub([row(1)])
        setup(fake, configured=False)
        picks_store.add_pick(row(9), path=path)
        check("no secrets, no network calls", fake.calls == [], str(fake.calls))
        check("and no status line", remote.describe_status() == "")
        os.remove(path)

        print("== fresh disk restores the stored ledger")
        fake = FakeGitHub([row(1), row(2), row(3)])
        setup(fake)
        got = picks_store.list_picks(path)
        check("three stored picks come back on first read", [p["id"] for p in got] == ["r1", "r2", "r3"], str(len(got)))
        check("and are written to the local file", len(json.load(open(path))) == 3)
        check("status says so", "restored 3 picks" in remote.describe_status(), remote.describe_status())
        n = len(fake.calls)
        picks_store.list_picks(path)
        check("restore runs once, not on every read", len(fake.calls) == n)

        print("== saves are pushed")
        picks_store.add_pick(row(4), path=path)
        check("a new pick is pushed", fake.rows is not None and len(fake.rows) == 4 and fake.puts() == 1, str(fake.puts()))
        check("the token is sent as the credential", all(ok for _, ok in fake.calls))
        remote._state["last_push"] = 0.0
        picks_store.set_status("r4", "win", path=path)
        check("settling a pick is pushed", fake.puts() == 2 and fake.rows[-1]["status"] == "win")
        remote._state["last_push"] = 0.0
        picks_store.delete_pick("r1", path=path)
        check("a delete is pushed", fake.puts() == 3 and len(fake.rows) == 3)

        print("== board-timestamp refreshes are batched")
        before = fake.puts()
        rows = picks_store.list_picks(path)
        for k in range(5):
            for p in rows:
                p["last_seen_on_board"] = f"2026-11-25T00:0{k}:00+00:00"
            picks_store._save(path, rows)
        check("five timestamp-only saves make no commits", fake.puts() == before, str(fake.puts() - before))
        remote._state["last_quiet_push"] = 0.0
        picks_store._save(path, rows)
        check("but one goes through once the quiet interval has passed", fake.puts() == before + 1)

        print("== conflict and failure")
        fake.conflict_once = True
        remote._state["last_push"] = 0.0
        picks_store.add_pick(row(5), path=path)
        check("a conflict is retried with the current sha", len(fake.rows) == 4 and remote.STATUS["last_error"] is None,
              str(remote.STATUS["last_error"]))
        fake.fail_with = (401, "Bad credentials")
        remote._state["last_push"] = 0.0
        picks_store.add_pick(row(6), path=path)
        check("a rejected token does not lose the local save", len(picks_store.list_picks(path)) == 5)
        msg = remote.describe_status()
        check("the problem is reported", "PROBLEM" in msg and "token rejected" in msg, msg)
        check("the token never appears in the message", TOKEN not in msg and TOKEN not in json.dumps(remote.STATUS))
        fake.fail_with = None
        remote._state["last_push"] = 0.0
        picks_store.add_pick(row(7), path=path)
        check("the next save catches the copy up", len(fake.rows) == 6 and "PROBLEM" not in remote.describe_status())

        print("== restore never replaces a ledger that has picks")
        fake = FakeGitHub([row(1)])
        setup(fake)
        got = picks_store.list_picks(path)
        check("local rows are kept when the disk was not wiped", len(got) == 6, str(len(got)))

        print("== a failed fetch at start")
        os.remove(path)
        fake = FakeGitHub([row(1), row(2)])
        fake.fail_with = (0, "URLError")
        setup(fake)
        check("reads as empty this time", picks_store.list_picks(path) == [])
        check("and reports the problem", "PROBLEM" in remote.describe_status(), remote.describe_status())
        fake.fail_with = None
        check("then restores on the next read", len(picks_store.list_picks(path)) == 2)

        print("== first run with nothing stored yet")
        os.remove(path)
        for f in os.listdir(tmpdir):  # a fresh disk has no dated backups either
            os.remove(os.path.join(tmpdir, f))
        fake = FakeGitHub(None)
        setup(fake)
        check("empty repo, empty ledger", picks_store.list_picks(path) == [])
        picks_store.add_pick(row(1), path=path)
        check("first save creates the file in the repo", fake.rows is not None and len(fake.rows) == 1)
    finally:
        picks_store._default_path, remote._http, remote._secret = real_default, real_http, real_secret
        remote._reset_for_tests()
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)
    print(f"\n{'ALL PASS' if FAILS == 0 else f'{FAILS} FAILED'}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
