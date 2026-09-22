#!/usr/bin/env python3
"""`watch` must publish presence under SOME name, or it joins invisibly.

`set_presence` was called only when `--name` was given, and a peer with no
name is not rendered anywhere: the web client's `peersOf` skips a state
without a non-empty name, and the service's presence summary counts such a
peer without naming it. So omitting `--name` produced a connection that looked
joined to the agent and absent to everyone else — the same silent-no-op shape
as the `read`-instead-of-`watch` bug. An mxid already carries a usable name.
"""
import importlib.util
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
CLI = REPO / "skills" / "room-collab" / "scripts" / "room_collab.py"
# The client module exits at import without its deps; load the CLI as a module
# without executing main, and skip if the deps are genuinely absent.
sys.path.insert(0, str(CLI.parent))
spec = importlib.util.spec_from_file_location("room_collab_cli", CLI)
mod = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(mod)
except SystemExit as exc:  # deps missing on this host
    print(f"SKIP - room-collab deps unavailable ({exc})")
    raise SystemExit(0)

presence_name = mod.presence_name
FAILS = []


def check(label, got, want):
    if got != want:
        FAILS.append(f"{label}: got {got!r}, want {want!r}")


# An explicit name always wins, trimmed.
check("explicit name", presence_name("Mars", "@m:x"), "Mars")
check("explicit name trimmed", presence_name("  Mars  ", "@m:x"), "Mars")
# No name: the mxid's localpart, which is what the summon hint fills in.
check("localpart", presence_name(None, "@sutando-qingyun-001:ag2.space"), "sutando-qingyun-001")
check("localpart over host colons", presence_name(None, "@weird:host:port"), "weird")
check("blank name falls through", presence_name("   ", "@m:ag2.space"), "m")
# Nothing usable: None, so the caller skips set_presence rather than
# publishing an empty name the readers would drop anyway.
check("no name, no id", presence_name(None, None), None)
check("malformed id", presence_name(None, "not-an-mxid"), None)
check("id with no localpart", presence_name(None, "@:server"), None)
check("empty id", presence_name(None, ""), None)

if FAILS:
    print("FAIL")
    for f in FAILS:
        print(f" - {f}")
    raise SystemExit(1)
print("ok - watch publishes presence under a name, defaulting to the mxid's localpart")
