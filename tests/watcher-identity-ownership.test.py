#!/usr/bin/env python3
"""Which checkout does a watcher belong to?

The watcher derives its repository from the EXECUTED script (`$0`), and callers
launch it by absolute path from an unrelated cwd — the codex notifier does
exactly that. So cwd is not an ownership signal, and a filter built on it fails
in BOTH directions: it drops this core's watcher (the boot gate then starts a
duplicate and every task is processed twice) and keeps a sibling clone's (the
needed start is suppressed and owner tasks strand).

The crossed cases below are the ones a cwd-based filter gets backwards, and they
are the reason this is derived from the script path instead.

Run: python3 tests/watcher-identity-ownership.test.py
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import watcher_identity as wi  # noqa: E402

A = "/checkouts/sutando-core"       # this checkout
B = "/checkouts/sutando-e2e-pool"   # a sibling clone that also runs a watcher
FAILURES: list[str] = []


def vec(script):
    """An authoritative argv vector for a watcher started as `bash <script>`."""
    return lambda pid: ["/bin/bash", script]


def check(name, got, want):
    ok = got is want if want is None or isinstance(want, bool) else got == want
    print(("  ok   " if ok else "  FAIL ") + name + ("" if ok else f" — got {got!r}, want {want!r}"))
    if not ok:
        FAILURES.append(name)


# The two crossed cases. A cwd filter answers both the wrong way round.
check("own script launched from the sibling's cwd is still OURS",
      wi.owns_watcher("", A, pid=1, argv_vector=vec(f"{A}/src/watch-tasks-stream.sh"), cwd=B), True)
check("the sibling's script launched from OUR cwd is not ours",
      wi.owns_watcher("", A, pid=2, argv_vector=vec(f"{B}/src/watch-tasks-stream.sh"), cwd=A), False)

# The aligned case a cwd filter also passes — kept so the two above are not the
# only evidence, and so a rule that answered "True" always would be visible.
check("aligned: our script from our cwd",
      wi.owns_watcher("", A, pid=3, argv_vector=vec(f"{A}/src/watch-tasks-stream.sh"), cwd=A), True)
check("aligned: the sibling's script from the sibling's cwd",
      wi.owns_watcher("", A, pid=4, argv_vector=vec(f"{B}/src/watch-tasks-stream.sh"), cwd=B), False)

# UNKNOWN must stay UNKNOWN. An unprovable owner is not a foreign one: treating
# None as False is what makes a probe drop a watcher it simply could not read.
check("argv that proves nothing is None, never False",
      wi.owns_watcher("bash", A, pid=5, argv_vector=lambda p: None), None)
check("a non-watcher script is None, not a false owner",
      wi.owns_watcher("", A, pid=6, argv_vector=vec("/somewhere/other-script.sh")), None)
check("an empty repo argument cannot decide ownership",
      wi.owns_watcher("", "", pid=7, argv_vector=vec(f"{A}/src/watch-tasks-stream.sh")), None)

# cwd has exactly one job: resolving a RELATIVE script operand.
check("a relative operand resolves against cwd (ours)",
      wi.owns_watcher("", A, pid=8, argv_vector=vec("src/watch-tasks-stream.sh"), cwd=A), True)
check("a relative operand resolves against cwd (the sibling's)",
      wi.owns_watcher("", A, pid=9, argv_vector=vec("src/watch-tasks-stream.sh"), cwd=B), False)
check("a relative operand with no cwd is unprovable, not foreign",
      wi.owns_watcher("", A, pid=10, argv_vector=vec("src/watch-tasks-stream.sh")), None)

# The flattened-argv path, where only the two-token form can be trusted.
check("flattened two-token argv is authoritative",
      wi.owns_watcher(f"/bin/bash {A}/src/watch-tasks-stream.sh", A), True)
check("flattened argv with operands is not authoritative",
      wi.owns_watcher(f"/bin/bash {A}/src/watch-tasks-stream.sh /some/tasks", A), None)

# The repo extraction itself, so a failure above points at the right half.
check("the repo is the script's grandparent",
      wi.watcher_repo("", pid=11, argv_vector=vec(f"{A}/src/watch-tasks-stream.sh")), A)
check("the executed script path is reported verbatim",
      wi.watcher_script_path("", pid=12, argv_vector=vec(f"{B}/src/watch-tasks-stream.sh")),
      f"{B}/src/watch-tasks-stream.sh")

# The whole point, end to end: a `ps` snapshot carrying BOTH checkouts' watchers,
# which is the live condition on a host that also runs a worker pool from a clone.
PS = f"""  100     1 /bin/bash {A}/src/watch-tasks-stream.sh
  101   100 /bin/bash {A}/src/watch-tasks-stream.sh
  200     1 /bin/bash {B}/src/watch-tasks-stream.sh
  201   200 /bin/bash {B}/src/watch-tasks-stream.sh
  300     1 /usr/bin/python3 something-else.py
"""

unfiltered = wi.watcher_trees(PS)
check("without a repo, both checkouts' trees are returned", sorted(unfiltered), ["100", "200"])

ours = wi.watcher_trees(PS, repo=A)
check("filtered to this checkout, only our tree survives", sorted(ours), ["100"])
check("and it still carries its whole tree", sorted(ours.get("100", [])), ["100", "101"])

theirs = wi.watcher_trees(PS, repo=B)
check("the same snapshot filtered to the sibling gives the other tree", sorted(theirs), ["200"])

# The fail-safe direction, which is the half that matters when a probe is wrong.
PS_UNKNOWN = """  400     1 /bin/bash /somewhere/watch-tasks-stream.sh --tasks /x/y
"""
check("a root whose argv cannot prove ownership is KEPT, not dropped",
      sorted(wi.watcher_trees(PS_UNKNOWN, repo=A)), ["400"])

print(("FAILED — " + ", ".join(FAILURES)) if FAILURES else "PASS — ownership comes from the executed script")
sys.exit(1 if FAILURES else 0)
