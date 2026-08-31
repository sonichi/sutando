#!/usr/bin/env python3
"""Contract for the prose-cap gate. Classification is tokenize-based, so the two
false positives a line scanner produces are pinned here as controls."""
import contextlib
import hashlib
import importlib.util
import io
import os
import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "review-checks-prose-cap.py"

spec = importlib.util.spec_from_file_location("pc", SCRIPT)
pc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pc)

def blob_sha(text):
    """git's blob hash for `text` — what a real diff's `index a..b` post-image is."""
    b = text.encode()
    return hashlib.sha1(b"blob %d\0" % len(b) + b).hexdigest()


failures = []


def check(name, filename, body, want_rc):
    """Write body as a new file, synthesize its add-diff, run the gate."""
    with tempfile.TemporaryDirectory() as td:
        p = pathlib.Path(td) / filename
        p.write_text(body)
        n = len(body.splitlines())
        diff = (f"diff --git a/{filename} b/{filename}\n"
                f"index 0000000..{blob_sha(body)} 100644\n--- /dev/null\n"
                f"+++ b/{filename}\n@@ -0,0 +1,{n} @@\n"
                + "".join("+" + l + "\n" for l in body.splitlines()))
        found, unscannable, _absent, _inscope = pc.violations(diff, 2, (".py",), root=td)
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
    diff = (f"diff --git a/f.py b/f.py\nindex 0000000..{blob_sha(p.read_text())} 100644\n"
            "--- a/f.py\n+++ b/f.py\n@@ -2,1 +3,1 @@\n+# newly added third\n")
    found, _, _absent, _inscope = pc.violations(diff, 2, (".py",), root=td)
    if found:
        failures.append(f"partially-added run must not fire: {found}")
    else:
        print("  ok: a run whose earlier lines were NOT added does not fire")

# --- an unreadable file is reported, never silently passed ------------------
with tempfile.TemporaryDirectory() as td:
    p = pathlib.Path(td) / "g.py"
    p.write_text("def broken(:\n")
    diff = (f"diff --git a/g.py b/g.py\nindex 0000000..{blob_sha(p.read_text())} 100644\n"
            "--- /dev/null\n+++ b/g.py\n@@ -0,0 +1,1 @@\n+def broken(:\n")
    found, unscannable, _absent, _inscope = pc.violations(diff, 2, (".py",), root=td)
    if "g.py" not in unscannable:
        failures.append("an untokenizable file must be REPORTED as unscannable")
    else:
        print("  ok: an untokenizable file is reported, not silently passed")

# --- a tree at a DIFFERENT revision must not yield findings about its content --
# Control runs first: a SKIPPED that merely switched the gate off fails the pair.
with tempfile.TemporaryDirectory() as td:
    disk = "# disk one\n# disk two\n# disk three\nV = 1\n"
    (pathlib.Path(td) / "rev.py").write_text(disk)

    same = (f"diff --git a/rev.py b/rev.py\nindex 0000000..{blob_sha(disk)} 100644\n"
            "--- /dev/null\n+++ b/rev.py\n@@ -0,0 +1,4 @@\n"
            + "".join("+" + l + "\n" for l in disk.splitlines()))
    found, _u, det, _inscope = pc.violations(same, 2, (".py",), root=td)
    if found and not det:
        print("  ok: control — when the tree IS the diff's revision, the run still fires")
    else:
        failures.append(f"matching-revision control must fire: found={found} detached={det}")

    submitted = "# submitted one\n# submitted two\n# submitted three\nV = 1\n"
    other = (f"diff --git a/rev.py b/rev.py\nindex 0000000..{blob_sha(submitted)} 100644\n"
             "--- /dev/null\n+++ b/rev.py\n@@ -0,0 +1,4 @@\n"
             + "".join("+" + l + "\n" for l in submitted.splitlines()))
    found, _u, det, _inscope = pc.violations(other, 2, (".py",), root=td)
    if not found and det == ["rev.py"]:
        print("  ok: a foreign revision is SKIPPED, not reported as the diff's own finding")
    else:
        failures.append(f"foreign revision must not produce findings: found={found} detached={det}")

# --- the reviewer's control: SAME added lines, DIFFERENT surrounding context ---
# Only the triple-quote OUTSIDE the hunk decides they tokenize as STRING.
with tempfile.TemporaryDirectory() as td:
    submitted = 'x = 1\n# one\n# two\n# three\n'
    foreign = 'x = 1\n"""\n# one\n# two\n# three\n"""\n'
    (pathlib.Path(td) / "ctx.py").write_text(foreign)
    d = (f"diff --git a/ctx.py b/ctx.py\nindex 0000000..{blob_sha(submitted)} 100644\n"
         "--- a/ctx.py\n+++ b/ctx.py\n@@ -1,4 +1,4 @@\n x = 1\n+# one\n+# two\n+# three\n")
    found, _u, det, _inscope = pc.violations(d, 2, (".py",), root=td)
    if not found and det == ["ctx.py"]:
        print("  ok: identical added lines in a foreign context are SKIPPED, not judged")
    else:
        failures.append(f"foreign surrounding context must SKIP: found={found} detached={det}")

# --- branch coverage: paths violations() takes that the cases above miss -----
def diff_for(filename, body, *, disk=None):
    # body=None models a diff naming a file with no post-image on disk.
    if body is None:
        return (f"diff --git a/{filename} b/{filename}\n--- /dev/null\n"
                f"+++ b/{filename}\n@@ -0,0 +1,3 @@\n+# one\n+# two\n+# three\n")
    n = len(body.splitlines())
    head = (f"diff --git a/{filename} b/{filename}\n"
            f"index 0000000..{blob_sha(body if disk is None else disk)} 100644\n"
            f"--- a/{filename}\n+++ b/{filename}\n@@ -1,{n} +1,{n} @@\n")
    return head + "".join("+" + l + "\n" for l in body.splitlines())


with tempfile.TemporaryDirectory() as td:
    # A non-.py file in the diff must be skipped by the extension filter.
    (pathlib.Path(td) / "notes.md").write_text("# one\n# two\n# three\n")
    found, _, _absent, _inscope = pc.violations(diff_for("notes.md", "# one\n# two\n# three\n"), 2, (".py",), root=td)
    print("  ok: non-.py file is skipped by the extension filter" if not found
          else f"  FAIL ext filter: {found}")
    if found:
        failures.append("extension filter did not skip a non-.py path")

with tempfile.TemporaryDirectory() as td:
    # A run split by a NON-added comment line must flush at the boundary.
    p2 = pathlib.Path(td) / "split.py"
    p2.write_text("# a1\n# a2\n# a3\n# preexisting\n# b1\n# b2\n# b3\nV = 1\n")
    d = (f"diff --git a/split.py b/split.py\nindex 0000000..{blob_sha(p2.read_text())} 100644\n"
         "--- a/split.py\n+++ b/split.py\n@@ -1,8 +1,8 @@\n"
         "+# a1\n+# a2\n+# a3\n # preexisting\n+# b1\n+# b2\n+# b3\n V = 1\n")
    found, _, _absent, _inscope = pc.violations(d, 2, (".py",), root=td)
    if len(found) == 2:
        print("  ok: a run split by a non-added comment flushes as two runs")
    else:
        failures.append(f"split-run flush: expected 2 runs, got {found}")

with tempfile.TemporaryDirectory() as td:
    # Two added runs separated by CODE: the comment line numbers are non-consecutive,
    # so the first run must flush at the gap rather than merging across it.
    p3 = pathlib.Path(td) / "gap.py"
    p3.write_text("# a1\n# a2\n# a3\nV = 1\n# b1\n# b2\n# b3\nW = 2\n")
    found, _, _absent, _inscope = pc.violations(diff_for("gap.py", p3.read_text()), 2, (".py",), root=td)
    if len(found) == 2 and found[0][1] == 1 and found[1][1] == 5:
        print("  ok: runs separated by code flush at the gap, not merged")
    else:
        failures.append(f"gap flush: expected runs at lines 1 and 5, got {found}")

# --- main(): drive it in-process so the paths are attributed ------------------


def run_main(diff, env=None):
    """Drive main() in-process. `env` sets RC_* for this call only, so a leaked
    value cannot make a later case pass for the wrong reason."""
    out, err = io.StringIO(), io.StringIO()
    stdin = sys.stdin
    saved = {k: os.environ.get(k) for k in ("RC_PROSE_CAP", "RC_PROSE_EXTS")}
    for k in saved:
        os.environ.pop(k, None)
    os.environ.update(env or {})
    sys.stdin = io.StringIO(diff)
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = pc.main()
    finally:
        sys.stdin = stdin
        for k, v in saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v
    return rc, out.getvalue(), err.getvalue()


_cwd = os.getcwd()
with tempfile.TemporaryDirectory() as td:
    os.chdir(td)
    try:
        pathlib.Path("v.py").write_text("# one\n# two\n# three\nV = 1\n")
        rc, out, err = run_main(diff_for("v.py", "# one\n# two\n# three\nV = 1\n"))
        if rc == 0 and "v.py:1" in out and "over the 2-line cap" in err:
            print("  ok: main() reports a violation on stdout and still exits 0")
        else:
            failures.append(f"main violation path: rc={rc} out={out!r} err={err!r}")

        pathlib.Path("c.py").write_text("# one\n# two\nV = 1\n")
        rc, out, err = run_main(diff_for("c.py", "# one\n# two\nV = 1\n"))
        if rc == 0 and out.strip() == "" and "PASS" in err:
            print("  ok: main() prints PASS and empty stdout when clean")
        else:
            failures.append(f"main clean path: rc={rc} out={out!r} err={err!r}")

        pathlib.Path("bad.py").write_text("def broken(:\n")
        rc, out, err = run_main(diff_for("bad.py", "def broken(:\n"))
        if rc != 0 and "FAIL-CLOSED" in err:
            print("  ok: an unreadable post-image FAILS CLOSED, never PASS")
        else:
            failures.append(f"main unscannable path: rc={rc} err={err!r}")

        # No post-image must say SKIPPED, never PASS (a verdict about an unread file)
        # and never non-zero (that failed 3 sibling suites on every detached diff).
        rc, out, err = run_main(diff_for("missing.py", None))
        if rc == 0 and "PASS" not in err and "SKIPPED" in err:
            print("  ok: an absent post-image reports SKIPPED — neither PASS nor fail-closed")
        else:
            failures.append(f"absent post-image: rc={rc} err={err!r}")

        # The regression itself: a detached diff must leave the OTHER scanners' verdict
        # intact. review-checks.sh exits 2 on any non-zero here, so this is the gate.
        rc, out, err = run_main(diff_for("gone.py", None).replace("+# one\n+# two\n+# three\n",
                                                                 "+harmless = 1\n"))
        if rc == 0 and out.strip() == "":
            print("  ok: a clean detached diff exits 0, so the runner still reports its peers")
        else:
            failures.append(f"detached clean diff must not fail closed: rc={rc} out={out!r}")

        # Present-but-unparseable stays fail-closed: it was reachable and still unread.
        pathlib.Path("held.py").write_text("def broken(:\n")
        rc, out, err = run_main(diff_for("held.py", "def broken(:\n"))
        if rc == 2 and "FAIL-CLOSED" in err:
            print("  ok: absent and unreadable are different answers (unreadable still gates)")
        else:
            failures.append(f"unreadable must stay fail-closed: rc={rc} err={err!r}")

        # --- the configured cap/exts must be honored, not decorative ---------
        pathlib.Path("cfg.py").write_text("# one\n# two\n# three\nx = 1\n")
        d = diff_for("cfg.py", "# one\n# two\n# three\nx = 1\n")
        rc, out, err = run_main(d, env={"RC_PROSE_CAP": "3", "RC_PROSE_EXTS": ".pyi"})
        if rc == 0 and out.strip() == "":
            print("  ok: RC_PROSE_EXTS=.pyi puts a .py file out of scope")
        else:
            failures.append(f"RC_PROSE_EXTS ignored: rc={rc} out={out!r}")

        # A non-Python extension must REFUSE, not scan. Before this gate it was
        # selected, tokenized as Python, found commentless and reported PASS.
        pathlib.Path("cfg.ts").write_text("// one\n// two\n// three\n// four\nexport const y = 1;\n")
        dts = diff_for("cfg.ts", "// one\n// two\n// three\n// four\nexport const y = 1;\n")
        rc, out, err = run_main(dts, env={"RC_PROSE_CAP": "2", "RC_PROSE_EXTS": ".ts"})
        if rc == 2 and "FAIL-CLOSED" in err and ".ts" in err:
            print("  ok: a configured .ts is REFUSED, not silently passed")
        else:
            failures.append(f"unsupported ext must fail closed: rc={rc} out={out!r} err={err!r}")
        # The same body under a supported ext must still be FOUND — otherwise the
        # refusal above could be passing for the wrong reason.
        pathlib.Path("cfg2.py").write_text("# one\n# two\n# three\n# four\ny = 1\n")
        dpy = diff_for("cfg2.py", "# one\n# two\n# three\n# four\ny = 1\n")
        rc, out, err = run_main(dpy, env={"RC_PROSE_CAP": "2", "RC_PROSE_EXTS": ".py"})
        if rc == 0 and "cfg2.py:1" in out:
            print("  ok: control — the same 4-line run IS caught under .py")
        else:
            failures.append(f"control failed, refusal proves nothing: rc={rc} out={out!r}")
        rc, out, err = run_main(d, env={"RC_PROSE_CAP": "3"})
        if rc == 0 and out.strip() == "":
            print("  ok: RC_PROSE_CAP=3 does not flag a 3-line block")
        else:
            failures.append(f"RC_PROSE_CAP ignored: rc={rc} out={out!r}")
        rc, out, err = run_main(d)
        if rc == 0 and "cap 2" in out:
            print("  ok: with no env, the default cap 2 still flags it")
        else:
            failures.append(f"default cap regressed: rc={rc} out={out!r}")
    finally:
        os.chdir(_cwd)

# --- empty diff is not a pass ------------------------------------------------
rc, out, err = run_main("")
if rc == 0:
    failures.append("empty diff must NOT exit 0 (in-process)")
else:
    print("  ok: empty diff refuses to report a pass (in-process)")
r = subprocess.run([sys.executable, str(SCRIPT)], input="", capture_output=True, text=True)
if r.returncode == 0:
    failures.append("empty diff must NOT exit 0 (subprocess)")
else:
    print("  ok: empty diff refuses to report a pass (as a real subprocess too)")

# --- END-TO-END: the guide's prose_exts must reach the runner, not just the module.
# The shell hardcoded ".py", so a guide naming another extension was decorative.
with tempfile.TemporaryDirectory() as td:
    ts_body = "// one\n// two\n// three\n"
    py_body = "# one\n# two\n# three\nV = 1\n"
    (pathlib.Path(td) / "a.py").write_text(py_body)
    guide = pathlib.Path(td) / "GUIDE.md"
    guide.write_text("```yaml\nchecks:\n  prose-cap:\n    prose_cap: 2\n"
                     "    prose_exts: ['.pyi']\n```\n")
    diff = (f"diff --git a/a.py b/a.py\nindex 0000000..{blob_sha(py_body)} 100644\n"
            f"--- a/a.py\n+++ b/a.py\n@@ -1,4 +1,4 @@\n"
            + "".join("+" + l + "\n" for l in py_body.splitlines()))
    r = subprocess.run(["bash", str(ROOT / "scripts" / "review-checks.sh"),
                        "--guide", str(guide)], input=diff, capture_output=True, text=True, cwd=td)
    if "exts .pyi" in r.stderr and "a.py:1" not in (r.stdout + r.stderr):
        print("  ok: a guide declaring prose_exts ['.pyi'] puts .py out of scope END-TO-END")
    else:
        failures.append(f"guide prose_exts ignored by the runner: out={r.stdout!r} err={r.stderr[-300:]!r}")

    # END-TO-END through the SHELL: a guide naming a non-Python extension must
    # take the runner down, not hand it a PASS assembled from an unread file.
    (pathlib.Path(td) / "a.ts").write_text(ts_body)
    guide.write_text("```yaml\nchecks:\n  prose-cap:\n    prose_cap: 2\n"
                     "    prose_exts: ['.ts']\n```\n")
    tsdiff = (f"diff --git a/a.ts b/a.ts\nindex 0000000..{blob_sha(ts_body)} 100644\n"
              f"--- a/a.ts\n+++ b/a.ts\n@@ -1,3 +1,3 @@\n"
              + "".join("+" + l + "\n" for l in ts_body.splitlines()))
    r = subprocess.run(["bash", str(ROOT / "scripts" / "review-checks.sh"),
                        "--guide", str(guide)], input=tsdiff, capture_output=True, text=True, cwd=td)
    if r.returncode != 0 and "FAIL-CLOSED" in r.stderr and "PASS" not in r.stdout:
        print("  ok: a guide declaring prose_exts ['.ts'] takes the RUNNER down END-TO-END")
    else:
        failures.append(f"runner passed an unsupported ext: rc={r.returncode} "
                        f"out={r.stdout!r} err={r.stderr[-300:]!r}")

    guide.write_text("```yaml\nchecks:\n  prose-cap:\n    prose_cap: 2\n"
                     "    prose_exts: ['.py']\n```\n")
    r = subprocess.run(["bash", str(ROOT / "scripts" / "review-checks.sh"),
                        "--guide", str(guide)], input=diff, capture_output=True, text=True, cwd=td)
    if "a.py:1" in (r.stdout + r.stderr) and r.returncode == 1:
        print("  ok: control — the same runner DOES flag it when the guide says '.py'")
    else:
        failures.append(f"guide control failed to flag: out={r.stdout!r} err={r.stderr[-300:]!r}")


# --- The verdict TOKEN must carry the scope, not just the parenthetical:
# a reader keys on the first word and never reaches the caveat.
with tempfile.TemporaryDirectory() as td:
    body = "# one\n# two\nV = 1\n"
    (pathlib.Path(td) / "a.py").write_text(body)
    guide = pathlib.Path(td) / "GUIDE.md"
    guide.write_text("```yaml\nchecks:\n  prose-cap:\n    prose_cap: 2\n"
                     "    prose_exts: ['.py']\n```\n")

    def run(index_sha):
        d = (f"diff --git a/a.py b/a.py\nindex 0000000..{index_sha} 100644\n"
             f"--- a/a.py\n+++ b/a.py\n@@ -1,3 +1,3 @@\n"
             + "".join("+" + l + "\n" for l in body.splitlines()))
        return subprocess.run(["bash", str(ROOT / "scripts" / "review-checks.sh"),
                               "--guide", str(guide)], input=d,
                              capture_output=True, text=True, cwd=td)

    # Post-image present and matching: the gate ran, so the token is PASS.
    r = run(blob_sha(body))
    if r.returncode == 0 and r.stdout.startswith("review-checks: PASS ("):
        print("  ok: a fully-scanned run still leads with PASS")
    else:
        failures.append(f"scanned run lost its PASS token: rc={r.returncode} out={r.stdout!r}")

    # Same diff, index sha of a different blob: prose-cap cannot scan.
    r = run(blob_sha("# other\nV = 2\n"))
    if not r.stdout.startswith("review-checks: PARTIAL ("):
        failures.append("a skipped gate still led with a clean-looking token: "
                        f"out={r.stdout!r} err={r.stderr[-200:]!r}")
    elif "prose-cap SKIPPED" not in r.stdout:
        failures.append(f"PARTIAL verdict dropped the scope wording: out={r.stdout!r}")
    elif r.returncode != 0:
        failures.append(f"PARTIAL must keep exit 0 — a skip is not a failure: rc={r.returncode}")
    else:
        print("  ok: a skipped gate reports PARTIAL, keeps the scope, and exits 0")

    # A FAIL must still outrank a skip: a real finding is not a partial scan.
    over = "# one\n# two\n# three\nV = 1\n"
    (pathlib.Path(td) / "b.py").write_text(over)
    d = (f"diff --git a/b.py b/b.py\nindex 0000000..{blob_sha(over)} 100644\n"
         f"--- a/b.py\n+++ b/b.py\n@@ -1,4 +1,4 @@\n"
         + "".join("+" + l + "\n" for l in over.splitlines()))
    r = subprocess.run(["bash", str(ROOT / "scripts" / "review-checks.sh"),
                        "--guide", str(guide)], input=d, capture_output=True, text=True, cwd=td)
    if r.returncode == 1 and "PARTIAL" not in r.stdout and "PASS" not in r.stdout:
        print("  ok: a real finding still fails — PARTIAL did not soften it")
    else:
        failures.append(f"a finding was reported as a verdict token: rc={r.returncode} out={r.stdout!r}")


# --- Zero in-scope files is the same lie one step out (#2989): a shell-only
# diff cannot fail a .py-scoped gate, so its token must be PARTIAL, not PASS.
with tempfile.TemporaryDirectory() as td:
    sh_body = "# one\n# two\n# three\n# four\necho hi\n"
    (pathlib.Path(td) / "only.sh").write_text(sh_body)
    guide = pathlib.Path(td) / "GUIDE.md"
    guide.write_text("```yaml\nchecks:\n  prose-cap:\n    prose_cap: 2\n"
                     "    prose_exts: ['.py']\n```\n")
    d = (f"diff --git a/only.sh b/only.sh\nindex 0000000..{blob_sha(sh_body)} 100644\n"
         f"--- a/only.sh\n+++ b/only.sh\n@@ -1,5 +1,5 @@\n"
         + "".join("+" + l + "\n" for l in sh_body.splitlines()))
    r = subprocess.run(["bash", str(ROOT / "scripts" / "review-checks.sh"),
                        "--guide", str(guide)], input=d, capture_output=True, text=True, cwd=td)
    if "review-checks: PARTIAL" in r.stdout and "no in-scope files" in (r.stdout + r.stderr):
        print("  ok: shell-only diff yields PARTIAL naming no-in-scope, never a bare PASS")
    else:
        failures.append(f"shell-only diff verdict: rc={r.returncode} out={r.stdout!r} err={r.stderr[-200:]!r}")

if failures:
    print("\nFAILURES:")
    for f in failures:
        print("  ✖", f)
    sys.exit(1)
print("ALL PASS")
