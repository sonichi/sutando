#!/usr/bin/env python3
"""A blank-but-set `$SUTANDO_HOST_LABEL` must not become the host label.

Both host-label implementations tested "is the override set?" with a check that
a whitespace-only string passes:

    src/util_paths.py   `if env:`          -> "   " is truthy
    scripts/sync-workspace.sh  `[ -n "$env" ]`  -> "   " is non-empty

so `SUTANDO_HOST_LABEL="  "` — trivially produced by an unquoted expansion or a
launcher that exports an empty-ish value — returned the whitespace itself. The
result is a `hosts/   /` directory, a `state/cores/   .alive`, and a vault
branch `host/   /<id>`: exactly the per-host path split that `_host_label()`'s
own docstring documents from the 2026-06-22 DHCP-drift incident, except
self-inflicted and much harder to see in a directory listing.

SIX resolvers read that env var, and four were wrong: `util_paths.py`,
`sync-workspace.sh`, `util_paths.ts`, `main.swift`'s `perHostLabel()`, plus the
fallback branches in `sync-memory.sh` and `codex/cli/start-cli.sh`. Their doc
comments claim lockstep with each other and were not in lockstep.

A correction worth keeping, since the first version of this file asserted the
opposite: `SutandoConfig.hostLabel()` — cited in a #2416 review as the one
implementation that already trimmed — **does not exist**. `SutandoConfig.swift`
is 299 lines, the cited 266-284 is `detectEnvWorkspaceInDotenv`, and `hostLabel`
appears nowhere in it. That citation was taken as fact and a "two of three are
wrong" story built on it. Count the sites yourself before writing the number.

Blank means "not set" -> fall through to scutil/hostname.

The bash half is exercised BEHAVIOURALLY, not by grep. `sync-workspace.sh`
dispatches on `$1` at end-of-file so it cannot be sourced, and the existing
`sync-workspace-uninit-guard.test.sh` therefore asserts structure. Extracting
just `_host()` with awk and eval'ing it in a clean shell runs the real code
without running the script — a grep would pass against any trim, including one
that compacts interior spaces.

Run:  python3 tests/host-label-blank-env.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SYNC = REPO / "scripts" / "sync-workspace.sh"

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        failures.append(label)
        if detail:
            print(f"        {detail}")


# --- Python: src/util_paths.py:_host_label() ---------------------------------
sys.path.insert(0, str(REPO / "src"))
import util_paths  # noqa: E402


def py_label(env_value: "str | None") -> str:
    saved = {k: os.environ.get(k) for k in
             ("SUTANDO_HOST_LABEL", "SUTANDO_HOST_OVERRIDE")}
    try:
        os.environ.pop("SUTANDO_HOST_OVERRIDE", None)
        if env_value is None:
            os.environ.pop("SUTANDO_HOST_LABEL", None)
        else:
            os.environ["SUTANDO_HOST_LABEL"] = env_value
        return util_paths._host_label()
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# --- Bash: scripts/sync-workspace.sh:_host() ---------------------------------
# Extract the function and eval it. The script dispatches at EOF, so sourcing
# it would run a sync; awk gives us the real body without the side effect.
_HOST_SRC = subprocess.run(
    ["awk", "/^_host\\(\\) \\{/,/^}$/", str(SYNC)],
    capture_output=True, text=True, check=True).stdout


def sh_label(env_value: "str | None") -> str:
    env = dict(os.environ)
    env.pop("SUTANDO_HOST_OVERRIDE", None)
    if env_value is None:
        env.pop("SUTANDO_HOST_LABEL", None)
    else:
        env["SUTANDO_HOST_LABEL"] = env_value
    r = subprocess.run(["bash", "-c", _HOST_SRC + "\n_host\n"],
                       capture_output=True, text=True, env=env, timeout=20)
    if r.returncode != 0:
        raise AssertionError(f"_host() exited {r.returncode}: {r.stderr[:200]}")
    return r.stdout.rstrip("\n")


print("host-label blank-env contract")

# Guard every assertion below: if extraction failed, the bash cases would fail
# for the wrong reason and prove nothing.
check("bash _host() was extracted from the real script",
      "_host()" in _HOST_SRC and "SUTANDO_HOST_LABEL" in _HOST_SRC,
      f"awk returned {len(_HOST_SRC)} bytes")

# The control both halves are measured against — what an UNSET override yields.
py_unset, sh_unset = py_label(None), sh_label(None)
check("positive control: unset override resolves to a real host name",
      bool(py_unset.strip()) and py_unset == sh_unset,
      f"py={py_unset!r} sh={sh_unset!r}")

# --- 1. The defect: blank must fall through, not become the label ------------
for blank in ("   ", "\t", " \n ", ""):
    shown = repr(blank)
    check(f"python: blank {shown} falls through to the real host",
          py_label(blank) == py_unset,
          f"got {py_label(blank)!r}, expected {py_unset!r}")
    check(f"bash:   blank {shown} falls through to the real host",
          sh_label(blank) == sh_unset,
          f"got {sh_label(blank)!r}, expected {sh_unset!r}")

# --- 2. A set override still wins, and is trimmed ----------------------------
check("python: a real override still wins", py_label("Chis-Mac-mini") == "Chis-Mac-mini")
check("bash:   a real override still wins", sh_label("Chis-Mac-mini") == "Chis-Mac-mini")
check("python: surrounding whitespace is trimmed",
      py_label("  Chis-Mac-mini  ") == "Chis-Mac-mini",
      repr(py_label("  Chis-Mac-mini  ")))
check("bash:   surrounding whitespace is trimmed",
      sh_label("  Chis-Mac-mini  ") == "Chis-Mac-mini",
      repr(sh_label("  Chis-Mac-mini  ")))

# --- 3. The trim is ENDS-ONLY -------------------------------------------------
# `tr -d '[:space:]'` and `.replace(" ","")` both make every case above pass
# while silently rewriting a legal label. This is the assertion that tells a
# correct trim from a lazy one, in both languages.
check("python: an interior space is preserved, not compacted",
      py_label("  My Host  ") == "My Host", repr(py_label("  My Host  ")))
check("bash:   an interior space is preserved, not compacted",
      sh_label("  My Host  ") == "My Host", repr(sh_label("  My Host  ")))

# --- 4. The two implementations agree on every case --------------------------
for case in (None, "   ", "  Chis-Mac-mini  ", "My Host", "Chis-MacBook-Pro"):
    check(f"python and bash agree for {case!r}",
          py_label(case) == sh_label(case),
          f"py={py_label(case)!r} sh={sh_label(case)!r}")

# --- 5. TypeScript: the third RUNTIME, not just the third file ---------------
# `if (label)` is truthy for "   " exactly as `if env:` was in Python. Missed in
# the first cut of this fix and caught in review with a live repro; driving the
# real module through tsx is the only thing that would have caught it here.
TSX = REPO / "node_modules" / ".bin" / "tsx"
if not TSX.exists():
    print("  SKIP  typescript (node_modules/.bin/tsx absent — CI installs it)")
else:
    def ts_label(env_value):
        env_lit = "{}" if env_value is None else "{ SUTANDO_HOST_LABEL: %r }" % env_value
        prog = (
            "import { resolveHostLabel } from './src/util_paths.ts';\n"
            f"process.stdout.write(JSON.stringify(resolveHostLabel({env_lit}, () => 'RealHost', 'fallback.local')));"
        ).replace("'", '"') if False else (
            "import { resolveHostLabel } from './src/util_paths.ts';\n"
            f"process.stdout.write(JSON.stringify(resolveHostLabel({env_lit}, () => 'RealHost', 'fallback.local')));"
        )
        r = subprocess.run([str(TSX), "-e", prog], capture_output=True, text=True,
                           cwd=str(REPO), timeout=120)
        if r.returncode != 0:
            raise AssertionError(f"tsx failed: {r.stderr[-300:]}")
        return json.loads(r.stdout.strip())

    ts_unset = ts_label(None)
    check("positive control: TS resolves a real host when the override is unset",
          ts_unset == "RealHost", repr(ts_unset))
    for blank in ("   ", "\t"):
        check(f"typescript: blank {blank!r} falls through to the real host",
              ts_label(blank) == ts_unset, repr(ts_label(blank)))
    check("typescript: surrounding whitespace is trimmed",
          ts_label("  Chis-Mac-mini  ") == "Chis-Mac-mini", repr(ts_label("  Chis-Mac-mini  ")))
    check("typescript: an interior space is preserved, not compacted",
          ts_label("  My Host  ") == "My Host", repr(ts_label("  My Host  ")))

# --- 6. Swift: SOURCE-level, and labelled as such ----------------------------
# `main.swift`'s perHostLabel() is inside the app entry point, so compiling it in
# isolation would drag in AppKit. This asserts the SHAPE of the fix, not its
# behaviour, and says so rather than implying a behavioural test ran.
swift = (REPO / "src" / "Sutando" / "main.swift").read_text()
m = re.search(r"func perHostLabel\(\) -> String \{(.*?)\n    \}", swift, re.S)
check("swift: perHostLabel() was found in main.swift", m is not None)
if m:
    body = m.group(1)
    # Split on the scutil CALL, not the word: my own explanatory comment above the
    # env branch mentions "scutil", so splitting on the bare token truncated the
    # branch before the line under test and failed for the wrong reason. Anchoring
    # on a marker that also appears in prose is the same defect this suite is about.
    CALL = 'runShell("/usr/sbin/scutil"'
    env_branch = body[:body.index(CALL)] if CALL in body else body
    check("swift (SOURCE-level): the env branch trims before the isEmpty test",
          "trimmingCharacters" in env_branch,
          "no trim between reading the env and returning it")
    check("swift (SOURCE-level): the raw `!v.isEmpty` form is gone",
          'env["SUTANDO_HOST_OVERRIDE"], !v.isEmpty' not in body, env_branch[:160])

print()
if failures:
    print(f"{len(failures)} check(s) FAILED: {failures}")
    sys.exit(1)
print("all checks passed — a blank override falls through in both implementations")
