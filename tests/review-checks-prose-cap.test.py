#!/usr/bin/env python3
"""Contract for the prose-cap gate. Classification is tokenize-based, so the two
false positives a line scanner produces are pinned here as controls."""
import importlib.util
import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "review-checks-prose-cap.py"

spec = importlib.util.spec_from_file_location("pc", SCRIPT)
pc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pc)

failures = []


def check(name, filename, body, want_rc):
    """Write body as a new file, synthesize its add-diff, run the gate."""
    with tempfile.TemporaryDirectory() as td:
        p = pathlib.Path(td) / filename
        p.write_text(body)
        n = len(body.splitlines())
        diff = (f"diff --git a/{filename} b/{filename}\n--- /dev/null\n"
                f"+++ b/{filename}\n@@ -0,0 +1,{n} @@\n"
                + "".join("+" + l + "\n" for l in body.splitlines()))
        found, unscannable = pc.violations(diff, 2, (".py",), root=td)
        rc = 1 if found else 0
        if rc != want_rc:
            failures.append(f"{name}: rc={rc} want={want_rc} found={found} unscannable={unscannable}")
        else:
            print(f"  ok: {name}")


# --- positive control: the gate must fire on a real over-cap comment run -----
check("3-line comment run fires", "a.py",
      "# one\n# two\n# three\nVALUE = 1\n", 1)
check("2-line comment run is clean", "b.py",
      "# one\n# two\nVALUE = 1\n", 0)

# --- false-positive controls the line scanner reproduced ---------------------
check("hash lines INSIDE a string are not a comment run", "c.py",
      'Q = """\n# not a comment\n# nor this\n# nor this\nSELECT 1\n"""\n', 0)
check("bare triple-quoted fixture after an assignment is out of scope", "d.py",
      'T = "x"\n"""\nline one\nline two\nline three\n"""\n', 0)
check("module docstring over the cap is out of scope", "e.py",
      '"""one\ntwo\nthree\n"""\nVALUE = 1\n', 0)

# --- scope: a run is only a violation when every line of it is ADDED ---------
with tempfile.TemporaryDirectory() as td:
    p = pathlib.Path(td) / "f.py"
    p.write_text("# pre-existing one\n# pre-existing two\n# newly added third\nV = 1\n")
    diff = ("diff --git a/f.py b/f.py\n--- a/f.py\n+++ b/f.py\n@@ -2,1 +3,1 @@\n"
            "+# newly added third\n")
    found, _ = pc.violations(diff, 2, (".py",), root=td)
    if found:
        failures.append(f"partially-added run must not fire: {found}")
    else:
        print("  ok: a run whose earlier lines were NOT added does not fire")

# --- an unscannable file is reported, never silently passed ------------------
with tempfile.TemporaryDirectory() as td:
    p = pathlib.Path(td) / "g.py"
    p.write_text("def broken(:\n")
    diff = ("diff --git a/g.py b/g.py\n--- /dev/null\n+++ b/g.py\n@@ -0,0 +1,1 @@\n"
            "+def broken(:\n")
    found, unscannable = pc.violations(diff, 2, (".py",), root=td)
    if "g.py" not in unscannable:
        failures.append("an untokenizable file must be REPORTED as unscannable")
    else:
        print("  ok: an untokenizable file is reported, not silently passed")

# --- empty diff is not a pass ------------------------------------------------
r = subprocess.run([sys.executable, str(SCRIPT)], input="", capture_output=True, text=True)
if r.returncode == 0:
    failures.append("empty diff must NOT exit 0")
else:
    print("  ok: empty diff refuses to report a pass")

if failures:
    print("\nFAILURES:")
    for f in failures:
        print("  ✖", f)
    sys.exit(1)
print("ALL PASS")
