"""Run every test file with one command.

    python run_tests.py            # everything, including the live ESPN check
    python run_tests.py --offline  # no network (what CI and the pre-push check use)

Each tests/test_*.py is a standalone script that exits non-zero on failure.
"""
import glob
import os
import subprocess
import sys
import time


def main() -> int:
    offline = "--offline" in sys.argv[1:]
    here = os.path.dirname(os.path.abspath(__file__))
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    if offline:
        env["EDGE_OFFLINE"] = "1"
    files = sorted(glob.glob(os.path.join(here, "tests", "test_*.py")))
    if not files:
        print("no test files found")
        return 1
    failed = []
    total_pass = total_fail = 0
    for f in files:
        t0 = time.time()
        r = subprocess.run([sys.executable, f], cwd=here, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
        out = r.stdout or ""
        p, x = out.count("[PASS]"), out.count("[FAIL]")
        total_pass += p
        total_fail += x
        ok = r.returncode == 0
        print(f"{'ok  ' if ok else 'FAIL'}  {os.path.basename(f):28s} {p:3d} passed {x:2d} failed  {time.time() - t0:4.1f}s")
        if not ok:
            failed.append(f)
            for line in out.splitlines():
                if "[FAIL]" in line:
                    print("      " + line.strip())
            if r.stderr:
                print("      " + r.stderr.strip().splitlines()[-1])
    mode = "offline" if offline else "with live checks"
    print(f"\n{total_pass} checks passed, {total_fail} failed across {len(files)} files ({mode})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
