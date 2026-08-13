#!/usr/bin/env python3
"""Unit tests for scripts/review-checks-root-artifacts.py. Pins the diff-parsing
branches; the coverage gate runs tests/**/*.test.py only, so a .sh test scores 0."""
import contextlib
import importlib.util
import io
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MODPATH = REPO / "scripts" / "review-checks-root-artifacts.py"

spec = importlib.util.spec_from_file_location("rc_root_artifacts", MODPATH)
ra = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ra)

GLOBS = ["prbody*", "reply*.md", "*.patch", "nohup.out"]

passed = 0
failed = 0


def check(label, got, want):
    global passed, failed
    if got == want:
        passed += 1
        print("  ok   %s" % label)
    else:
        failed += 1
        print("  FAIL %s\n         got  %r\n         want %r" % (label, got, want))


def added(path):
    """A minimal `git diff` for one added file."""
    return (
        "diff --git a/{p} b/{p}\n"
        "new file mode 100644\n"
        "index 0000000..1111111\n"
        "--- /dev/null\n"
        "+++ b/{p}\n"
        "@@ -0,0 +1 @@\n"
        "+body\n"
    ).format(p=path)


def paths(diff, globs=GLOBS):
    return [p for p, _ in ra.violations(diff, globs)]


print("review-checks-root-artifacts (python):")

# --- the reported failure ----------------------------------------------------
check("prbody.md at the root is a violation", paths(added("prbody.md")), ["prbody.md"])
check("reply1.md at the root is a violation", paths(added("reply1.md")), ["reply1.md"])
check("the matched glob is reported alongside the path",
      ra.violations(added("prbody.md"), GLOBS), [("prbody.md", "prbody*")])

# --- root-only scoping: a rule reaching tests/ fixtures gets switched off ----
check("the same name one level down is out of scope",
      paths(added("tests/reply1.md")), [])
check("...and at any depth", paths(added("a/b/c/prbody.md")), [])
check("an unmatched new root file is clean", paths(added("CHANGELOG.md")), [])

# --- only ADDITIONS can strand an artifact -----------------------------------
deleted = (
    "diff --git a/prbody.md b/prbody.md\n"
    "deleted file mode 100644\n"
    "--- a/prbody.md\n"
    "+++ /dev/null\n"
    "@@ -1 +0,0 @@\n"
    "-body\n"
)
check("deleting the artifact is not a violation", paths(deleted), [])

modified = (
    "diff --git a/prbody.md b/prbody.md\n"
    "index 1111111..2222222 100644\n"
    "--- a/prbody.md\n"
    "+++ b/prbody.md\n"
    "@@ -1 +1,2 @@\n"
    " body\n"
    "+more\n"
)
check("modifying an existing root file is not a violation", paths(modified), [])

# --- a rename is an ARRIVAL: `git mv` output has no `+++ b/`, no `new file` ---
pure_rename = (
    "diff --git a/notes/old-notes.md b/prbody.md\n"
    "similarity index 100%\n"
    "rename from notes/old-notes.md\n"
    "rename to prbody.md\n"
)
check("a pure rename INTO the root is a violation",
      paths(pure_rename), ["prbody.md"])

rename_modify = (
    "diff --git a/notes/old-notes.md b/prbody.md\n"
    "similarity index 87%\n"
    "rename from notes/old-notes.md\n"
    "rename to prbody.md\n"
    "--- a/notes/old-notes.md\n"
    "+++ b/prbody.md\n"
    "@@ -1 +1,2 @@\n"
    " body\n"
    "+more\n"
)
check("rename+modify into the root is a violation (counted once)",
      paths(rename_modify), ["prbody.md"])

# The mirror: moving an artifact OUT of the root is the fix, not the offence.
rename_out = (
    "diff --git a/prbody.md b/notes/prbody.md\n"
    "similarity index 100%\n"
    "rename from prbody.md\n"
    "rename to notes/prbody.md\n"
)
check("renaming an artifact OUT of the root is clean", paths(rename_out), [])
check("a rename to an unmatched root name is clean",
      paths(pure_rename.replace("prbody.md", "CHANGELOG.md")), [])

# --- git-quoted paths: C-escaped, so left encoded they match no glob ---------
# The escape must fall AFTER the glob-matching prefix, or this passes vacuously.
quoted = (
    'diff --git "a/prbody\\303\\251.md" "b/prbody\\303\\251.md"\n'
    "new file mode 100644\n"
    "--- /dev/null\n"
    '+++ "b/prbody\\303\\251.md"\n'
)
check("a quoted non-ASCII root path is decoded and flagged",
      paths(quoted), ["prbodyé.md"])
check("_unquote leaves an ordinary path untouched",
      ra._unquote("prbody.md"), "prbody.md")
check("_unquote falls back to the raw inner text on undecodable bytes",
      ra._unquote('"pr\\377body.md"').endswith("body.md"), True)
check("the quoted diff header still resets state for the next file",
      paths(quoted + modified), ["prbodyé.md"])
check("a quoted rename destination is decoded and flagged",
      paths('diff --git "a/notes/x.md" "b/prbody\\303\\251.md"\n'
            'rename to "prbody\\303\\251.md"\n'), ["prbodyé.md"])
# Defensive: a `+++` line that is neither /dev/null nor a b/ path is malformed
# input, not an arrival — it must not be read as a root-level filename.
check("a +++ line without the b/ prefix is ignored",
      paths(added("prbody.md").replace("+++ b/prbody.md", "+++ prbody.md")), [])

# --- is_new must not leak across files ---------------------------------------
# Without the per-header reset, a modification after an addition reads as one.
leak = added("CHANGELOG.md") + modified
check("is_new resets at each diff header", paths(leak), [])

two = added("prbody.md") + added("nohup.out")
check("every violation in a multi-file diff is reported",
      paths(two), ["prbody.md", "nohup.out"])

# --- glob semantics ----------------------------------------------------------
check("matching is case-sensitive (fnmatchcase, not the platform default)",
      paths(added("PRBODY.md")), [])
check("a suffix glob matches", paths(added("fix.patch")), ["fix.patch"])
check("an exact-name glob matches", paths(added("nohup.out")), ["nohup.out"])
check("no globs configured means nothing is flagged", paths(added("prbody.md"), []), [])

# --- main(): env-driven, stdin-driven ----------------------------------------
def run_main(diff, env_globs):
    """main() reads globs from the env and the diff from stdin."""
    prev_env = os.environ.get("RC_ROOT_ARTIFACT_GLOBS")
    prev_stdin = sys.stdin
    if env_globs is None:
        os.environ.pop("RC_ROOT_ARTIFACT_GLOBS", None)
    else:
        os.environ["RC_ROOT_ARTIFACT_GLOBS"] = env_globs
    sys.stdin = io.StringIO(diff)
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = ra.main()
    finally:
        sys.stdin = prev_stdin
        if prev_env is None:
            os.environ.pop("RC_ROOT_ARTIFACT_GLOBS", None)
        else:
            os.environ["RC_ROOT_ARTIFACT_GLOBS"] = prev_env
    return rc, buf.getvalue()

rc, out = run_main(added("prbody.md"), "prbody*\nreply*.md")
check("main() prints the offending path", "prbody.md" in out, True)
check("main() names the glob so the rule is findable", "prbody*" in out, True)
check("main() exits 0 even WITH hits — the caller decides pass/fail", rc, 0)

rc, out = run_main(added("CHANGELOG.md"), "prbody*")
check("main() prints nothing on a clean diff", out, "")
check("main() exits 0 on a clean diff", rc, 0)

# An unconfigured scan has checked nothing; 0 here let the runner print "clean"
# with the gate off. Non-zero is its fail-closed signal.
rc, out = run_main(added("prbody.md"), None)
check("an unset glob env is a config error, not a pass", rc, 2)
check("...and prints nothing on stdout that could read as a result", out, "")
rc, out = run_main(added("prbody.md"), "")
check("an empty glob env is a config error too", rc, 2)
rc, out = run_main(added("prbody.md"), "\n \n")
check("a whitespace-only glob env is a config error too", rc, 2)

# A blank line in the env list must not become a glob: fnmatch("x", "") is
# False today, but an empty pattern in the list is a caller bug either way.
rc, out = run_main(added("prbody.md"), "\n\nprbody*\n\n")
check("blank lines in the glob env are dropped", "prbody.md" in out, True)

print("PASS" if not failed else "FAILED (%d)" % failed)
sys.exit(0 if not failed else 1)
