"""Pre-push check: tests pass and nothing private is about to be published.

    python tools/prepush.py

Checks the commits not yet on origin plus anything staged:
  * the offline test suite passes
  * no ledger, settings, cache, backup or secrets file is tracked
  * no webhook URL or private key appears in the outgoing changes
"""
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PRIVATE_FILES = re.compile(
    r"(^|/)(picks(\.backup-.*|\.before-restore-.*)?\.json|settings\.json|\.odds_credits\.json|secrets\.toml)$|(^|/)\.odds_cache/"
)
SECRETS = [
    (re.compile(r"discord(app)?\.com/api/webhooks/\d+/[\w-]{20,}"), "Discord webhook URL"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "private key"),
]


def git(*args) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace").stdout


def main() -> int:
    problems = []
    tracked = [f for f in git("ls-files").splitlines() if PRIVATE_FILES.search(f.replace("\\", "/"))]
    if tracked:
        problems.append("private files are tracked by git: " + ", ".join(tracked))
    upstream = git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}").strip()
    outgoing = git("diff", f"{upstream}..HEAD") if upstream else ""
    added = [l[1:] for l in (outgoing + git("diff", "--cached")).splitlines() if l.startswith("+") and not l.startswith("+++")]
    for rx, label in SECRETS:
        if any(rx.search(l) for l in added):
            problems.append(f"a {label} appears in the outgoing changes")
    r = subprocess.run([sys.executable, os.path.join(ROOT, "run_tests.py"), "--offline"], cwd=ROOT)
    if r.returncode != 0:
        problems.append("tests failed")
    if problems:
        print("\nDO NOT PUSH:")
        for p in problems:
            print("  - " + p)
        return 1
    print("\nOK to push.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
