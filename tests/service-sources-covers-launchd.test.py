#!/usr/bin/env python3
"""_SERVICE_SOURCES must cover every persistent launchd job's program.

The table is hand-maintained, so it drifts silently by construction: a job added
to src/launchd/ with no row is invisible to check_live_checkout_branch, and the
symptom is a service running stale source that no probe reports. This test turns
that drift into a failing check instead of a gap nobody sees.

Scope is deliberately KeepAlive jobs only. A StartInterval job is short-lived, so
`pgrep` for it succeeds or fails depending on when the probe happens to run, and a
row for one would report staleness nondeterministically.
"""

import importlib.util
import json
import pathlib
import re
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
failures = []
checked = 0


def check(label, ok, detail=""):
    global checked
    checked += 1
    if ok:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}: {detail}")
        failures.append(label)


def load_table():
    spec = importlib.util.spec_from_file_location("hc", REPO / "src" / "health-check.py")
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except SystemExit:
        pass
    return mod._SERVICE_SOURCES


def parse_plist(path):
    """plistlib where it works, plutil where it does not, None when neither does.

    Homebrew pythons here raise on the pyexpat import and some shipped templates
    carry `--` inside an XML comment, which is valid to launchd but not to expat.
    """
    raw = re.sub(rb"<!--.*?-->", b"", path.read_bytes(), flags=re.S)
    try:
        import plistlib

        return plistlib.loads(raw)
    except Exception:
        pass
    try:
        out = subprocess.run(
            ["/usr/bin/plutil", "-convert", "json", "-o", "-", str(path)],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode == 0:
            return json.loads(out.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return None


def program_path(data):
    """Repo-relative path of the program this job runs, or None."""
    args = data.get("ProgramArguments") or [data.get("Program", "")]
    for arg in (str(a) for a in args):
        if "__REPO__/" in arg:
            return arg.split("__REPO__/", 1)[1]
    return None


def covered(rel, table):
    for src, _pattern in table:
        if rel == src or (src.endswith("/") and rel.startswith(src)):
            return src
    return None


table = load_table()
plists = sorted((REPO / "src" / "launchd").glob("*.plist"))

# A glob that matches nothing would make every assertion below vacuously true,
# which is the failure this file exists to prevent, turned on itself.
check("found launchd plists to check", len(plists) >= 4, f"only {len(plists)}")

def execs_away(rel):
    """True when the program replaces itself, leaving no process to pgrep for."""
    path = REPO / rel
    if path.suffix != ".sh" or not path.is_file():
        return False
    return any(
        re.match(r"\s*exec\s", line)
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
    )


unparseable = []
for plist in plists:
    data = parse_plist(plist)
    if data is None:
        unparseable.append(plist.name)
        continue
    if not data.get("KeepAlive"):
        continue
    rel = program_path(data)
    if rel is None:
        continue
    if execs_away(rel):
        # The row would never match, and an unmatchable row reads as coverage.
        check(
            f"{plist.stem} execs away, so {rel} correctly has no row",
            covered(rel, table) is None,
            "this wrapper `exec`s into its payload, so its own path leaves the "
            "process table — a pgrep row for it can never match, and an "
            "unmatchable row looks identical to a covered service",
        )
        continue
    check(
        f"{plist.stem} -> {rel} has a _SERVICE_SOURCES row",
        covered(rel, table) is not None,
        "no row covers it, so staleness in this program is invisible to "
        "check_live_checkout_branch",
    )

# Skipping silently would let a parser regression read as full coverage.
check(
    "every plist was parseable",
    not unparseable,
    f"could not parse: {', '.join(unparseable)} (no plistlib and no plutil?)",
)

# Guards the exclusion rule itself: if someone adds a row for a periodic job the
# reason above stops holding, and this says so rather than letting it rot.
for plist in plists:
    data = parse_plist(plist)
    if data is None or data.get("KeepAlive") or not data.get("StartInterval"):
        continue
    rel = program_path(data)
    if rel is None:
        continue
    check(
        f"{plist.stem} is periodic and correctly has no row",
        covered(rel, table) is None,
        f"{rel} is StartInterval={data['StartInterval']} — a pgrep for it is a "
        "coin flip, so the row would report staleness nondeterministically",
    )

print(f"\n{checked - len(failures)}/{checked} passed")
sys.exit(1 if failures else 0)
