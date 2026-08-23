"""Every test suite in this repository, and how to run it.

One definition, used by both run_tests.ps1 and CI, so the two can never
disagree about what "all the tests" means.

The zero check is the point of this file existing. `unittest discover`
exits 0 when it finds nothing, and "Ran 0 tests ... OK" reads almost
exactly like success -- which is how notifier's 313 tests sat unrun for
weeks behind a missing __init__.py, in a repository whose docs claimed
they passed. A suite that finds nothing is a broken suite, and this says
so with a non-zero exit.

    python tools/run_suites.py            # all of them
    python tools/run_suites.py notifier   # just one, by name
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# (name, arguments after "-m unittest", working directory relative to REPO)
SUITES = [
    ("gateway", ["discover", "-s", "firmware/gateway", "-t", "."], "."),
    ("orderagent", ["discover", "-s", "orderagent/tests", "-t", "."], "."),
    ("notifier", ["discover", "-s", ".", "-t", "."], "notifier"),
    # Repository-wide discipline (model-name drift). Named rather than
    # discovered: discovering from the root would collect orderagent a
    # second time and count it twice.
    ("repo", ["tests_models", "tests_commands"], "."),
]


def run(name: str, args: list[str], cwd: str) -> tuple[int, bool]:
    """Runs one suite. Returns (tests run, passed)."""
    print(f"--- {name} ---", flush=True)
    completed = subprocess.run(
        [sys.executable, "-m", "unittest", *args],
        cwd=REPO / cwd, capture_output=True, text=True,
        encoding="utf-8", errors="replace")
    output = (completed.stdout or "") + (completed.stderr or "")
    ran = 0
    for line in output.splitlines():
        if line.startswith("Ran ") and " test" in line:
            ran = int(line.split()[1])
    ok = completed.returncode == 0
    if not ok:
        print(output.strip(), flush=True)
        print(f"  NG  {name}: {ran} 件中に失敗", flush=True)
        return ran, False
    if ran == 0:
        # Not a pass. Something stopped the loader from importing the
        # tests -- most often a missing __init__.py.
        print(output.strip(), flush=True)
        print(f"  !!  {name}: 1件も実行されていない。"
              f"tests/ に __init__.py があるか確認", flush=True)
        return 0, False
    print(f"  OK  {name}: {ran} 件", flush=True)
    return ran, True


def main() -> int:
    wanted = sys.argv[1:]
    suites = [s for s in SUITES if not wanted or s[0] in wanted]
    if wanted and not suites:
        print(f"知らないスイート: {wanted}. "
              f"あるのは {[s[0] for s in SUITES]}")
        return 2
    total = 0
    failed = []
    for name, args, cwd in suites:
        ran, ok = run(name, args, cwd)
        total += ran
        if not ok:
            failed.append(name)
    print()
    if failed:
        print(f"失敗したスイート: {', '.join(failed)}  (実行 {total} 件)")
        return 1
    print(f"全部通った: {total} 件")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
