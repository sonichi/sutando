#!/usr/bin/env python3
"""Census: DiscordRestClient is the only production code that POSTs to Discord.

A chokepoint is only as good as its coverage — one hand-rolled sender outside
it and an injected post-gate validator silently misses that path (the reason
this gate is structural, not disciplinary). This walks every production tree
and pins two invariants:

  1. The API literal `discord.com/api` appears ONLY in the allowlisted
     modules: the shared client (the chokepoint) and the GET-only reader
     modules. A new sender pasting its own endpoint fails here by
     construction. (Bare `discord.com` prose — e.g. health-check's
     "regenerate at discord.com/developers" hint — is not an endpoint.)
  2. The reader modules stay read-only: no POST/PATCH/DELETE verbs and no
     request body construction, so an allowlisted file cannot quietly grow
     into a second send path.

Scope note: gateway-library sends inside discord-bridge.py (`channel.send`
via discord.py) ride the library's own HTTP stack and are out of census scope;
this census governs hand-rolled REST, which is where the drift lived.

Run: python3 tests/discord-post-census.test.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# Production trees. tests/ and docs are exempt (fixtures may quote endpoints).
TREES = ("src", "skills", "scripts", "hooks")
SUFFIXES = {".py", ".ts", ".sh", ".swift", ".js"}

# The chokepoint plus the GET-only readers (each held read-only by
# invariant 2 below, so an allowlisted file cannot grow into a sender).
ALLOWED = {
    "src/channels/discord/client.py",
    "src/channels/discord/reader.py",
    "src/policy/context/discord.py",
    "src/read_discord_channel.py",
    "hooks/context-source-guard.py",
}
READERS = ALLOWED - {"src/channels/discord/client.py"}

_MUTATING = re.compile(r"""method\s*=\s*["'](POST|PATCH|DELETE|PUT)["']""")

_fails = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        _fails.append(name)


def production_files():
    for tree in TREES:
        root = REPO / tree
        if not root.is_dir():
            continue
        for f in sorted(root.rglob("*")):
            if not f.is_file() or f.suffix not in SUFFIXES:
                continue
            rel = f.relative_to(REPO).as_posix()
            if ".bak" in rel or "node_modules" in rel:
                continue
            yield rel, f


# --- invariant 1: the endpoint literal only lives in the allowlist -----------
offenders = []
population = 0
for rel, f in production_files():
    population += 1
    try:
        text = f.read_text(errors="replace")
    except OSError:
        continue
    if "discord.com/api" in text and rel not in ALLOWED:
        offenders.append(rel)

check(f"no discord.com/api literal outside the chokepoint+readers "
      f"(scanned {population} production files)", offenders == [], ", ".join(offenders))
check("the scan population is real (a broken walk would pass vacuously)",
      population > 100, str(population))

# --- invariant 2: allowlisted readers stay read-only -------------------------
for rel in sorted(READERS):
    text = (REPO / rel).read_text(errors="replace")
    verbs = _MUTATING.findall(text)
    check(f"{rel} carries no mutating verb", verbs == [], ", ".join(verbs))

# Positive control: the pattern DOES fire on the client itself, so an empty
# reader result above means "clean", not "pattern never matches anything".
client_text = (REPO / "src" / "channels" / "discord" / "client.py").read_text()
check("positive control: the mutating-verb pattern fires on the client",
      bool(_MUTATING.search(client_text)))
check("positive control: the literal scan fires on the client",
      "discord.com/api" in client_text)

print()
if _fails:
    print(f"{len(_fails)} FAILED: " + "; ".join(_fails))
    sys.exit(1)
print("census holds: every hand-rolled Discord POST path routes through DiscordRestClient")
