#!/usr/bin/env python3
"""`user-timeline` must fail loudly where it cannot run, and accept what X accepts.

Two defects this pins, both found in review of #3428:

1. The command was handled ONLY in the bearer fast path. With OAuth1 credentials
   present and no X_BEARER_TOKEN, the lower dispatch had no matching arm and no
   `else`, so the process fell off the chain and exited 0 with no output — a
   silent no-op that is indistinguishable from success.

2. The limit guard enforced 10..100. That is the `search` endpoint's bound,
   measured against `search` and applied here. `users/{id}/tweets` accepts 5,
   and said so itself: "The `max_results` query parameter value [4] is not
   between 5 and 100". So the guard refused valid requests.
"""

import os
import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = REPO / "skills" / "x-twitter" / "x-post.py"
failures = []
checked = 0


def check(label, ok, detail=""):
    global checked
    checked += 1
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f": {detail}" if not ok else ""))
    if not ok:
        failures.append(label)


def run(args, env_extra):
    """Run the CLI with a controlled env. Never touches the network: every case
    here is refused during dispatch, before any endpoint call."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("X_BEARER_TOKEN", "X_API_KEY", "X_API_SECRET",
                        "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET")}
    env.update(env_extra)
    return subprocess.run([sys.executable, str(SCRIPT)] + args,
                          capture_output=True, text=True, env=env, timeout=60)


OAUTH_ONLY = {"X_API_KEY": "k", "X_API_SECRET": "s",
              "X_ACCESS_TOKEN": "t", "X_ACCESS_TOKEN_SECRET": "ts"}

# 1. OAuth1-only host: must NOT exit 0 silently.
r = run(["user-timeline", "Chi_Wang_"], OAUTH_ONLY)
check("oauth1-only: nonzero exit", r.returncode != 0, f"rc={r.returncode}")
check("oauth1-only: says what is missing",
      "X_BEARER_TOKEN" in (r.stdout + r.stderr),
      f"stdout={r.stdout[:80]!r} stderr={r.stderr[:80]!r}")
check("oauth1-only: not a silent no-op",
      bool((r.stdout + r.stderr).strip()), "produced no output at all")

# 2. Limit bound is THIS endpoint's, 5..100. Refusals happen before any call,
#    so a bearer value is supplied but never used.
BEARER = {"X_BEARER_TOKEN": "unused-refused-before-any-request"}
for limit, want_refused in ((4, True), (101, True), (5, False), (100, False)):
    r = run(["user-timeline", "Chi_Wang_", "--limit", str(limit)], BEARER)
    refused = (r.returncode == 2 and "--limit must be between" in (r.stdout + r.stderr))
    check(f"--limit {limit} {'refused' if want_refused else 'accepted by the guard'}",
          refused == want_refused,
          f"rc={r.returncode} out={(r.stdout + r.stderr)[:70]!r}")

# 3. The message must name the real bound; 10 was the search endpoint's.
r = run(["user-timeline", "Chi_Wang_", "--limit", "4"], BEARER)
msg = r.stdout + r.stderr
check("guard message names 5..100", "between 5 and 100" in msg, msg[:90])
check("guard message does not repeat the search bound",
      "between 10 and 100" not in msg, msg[:90])

print(f"\n{checked - len(failures)}/{checked} passed")
sys.exit(1 if failures else 0)
