#!/usr/bin/env python3
"""Idempotently pre-seed agy's (Antigravity CLI) onboarding-complete cache.

agy has no onboarding-skip CLI flag, and its internal SkipOnboarding RPC is
dead ("deprecated and no longer supported" per the binary's own strings — see
sutando#4272). The wizard is gated on three booleans in
~/.gemini/antigravity-cli/cache/onboarding.json; setting all three `true`
(verified by hand: a fresh launch lands on the same one-keypress
workspace-trust prompt Claude Code shows, instead of the wizard) skips it.

Factored out of start-cli.sh so the merge/idempotency logic is unit-testable
without a real agy session.
"""
import json
import os
import stat
import sys
import tempfile

FIELDS = (
    "consumerOnboardingComplete",
    "enterpriseOnboardingComplete",
    "onboardingComplete",
)


def default_path():
    return os.path.expanduser("~/.gemini/antigravity-cli/cache/onboarding.json")


def seed(path):
    """Ensure every field in FIELDS is `true` at `path`. Merge — never drop
    unrelated keys a future agy version might add to this file. Atomic write
    via a unique same-directory staging file + os.replace, preserving the
    existing file's mode (owner-only for a newly created one), so concurrent
    callers and restrictive umasks are both safe.

    Returns True if the file was written (something changed), False if it
    was already fully seeded (no-op).
    """
    try:
        with open(path) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}

    changed = False
    for field in FIELDS:
        if data.get(field) is not True:
            data[field] = True
            changed = True

    if not changed:
        return False

    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    # Preserve the existing mode; a not-yet-existing file is created owner-only.
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
    except FileNotFoundError:
        mode = 0o600

    # A fixed `<path>.tmp` is itself shared state that races between callers.
    fd, tmp = tempfile.mkstemp(dir=parent or ".", prefix=os.path.basename(path) + ".", suffix=".tmp")
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return True


def main(argv):
    path = argv[1] if len(argv) > 1 else default_path()
    changed = seed(path)
    print(("seeded: " if changed else "already seeded: ") + path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
