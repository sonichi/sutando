#!/usr/bin/env python3
"""Tests for idle-held.py. Run: python3 skills/proactive-loop/scripts/idle-held.test.py"""
import contextlib
import importlib.util
import io
import json
import pathlib
import sys
import tempfile

SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "skills" / "proactive-loop" / "scripts"
spec = importlib.util.spec_from_file_location("ih", SCRIPTS / "idle-held.py")
ih = importlib.util.module_from_spec(spec); spec.loader.exec_module(ih)

fails, ran = [], 0
def check(name, cond, detail=""):
    global ran; ran += 1
    print(f"  {'ok  ' if cond else 'FAIL'}  {name}" + (f"   {detail}" if detail and not cond else ""))
    if not cond: fails.append(name)

def state(items, **extra):
    d = {"streak": 0, "last_surfaced_hash": "abc", "held_item_ids": items,
         "held_item_notes": {"keep": "must survive a write"}, **extra}
    f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump(d, f); f.close()
    return pathlib.Path(f.name)

BASE = [["sutando-3198", "owner"], ["cinny-717", "owner"], ["ds-pr-12", "owner"]]
def run(args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = ih.main(args)
    return rc, out.getvalue(), err.getvalue()

print("idle-held")

p = state(BASE)
rc, out, _ = run(["--state", str(p)])
check("no ops emits the RECORDED list verbatim", rc == 0 and json.loads(out) == BASE)

rc, out, _ = run(["--state", str(p), "--remove", "cinny-717", "--reason", "merged"])
check("remove drops exactly one, order preserved",
      rc == 0 and json.loads(out) == [["sutando-3198","owner"],["ds-pr-12","owner"]])

rc, _, err = run(["--state", str(p), "--remove", "cinny-717"])
check("a removal with NO reason is refused", rc == 1 and "silent shrink" in err, err[:70])

rc, _, err = run(["--state", str(p), "--remove", "3198", "--reason", "x"])
check("an id built from RECALL is refused, not silently applied",
      rc == 1 and "not in the record" in err, err[:80])
check("...and it names the near-miss so the real id is one read away",
      "sutando-3198" in err, err[:90])

rc, out, _ = run(["--state", str(p), "--add", "new-thing:ci"])
check("add appends the pair", rc == 0 and ["new-thing","ci"] in json.loads(out))

rc, _, err = run(["--state", str(p), "--add", "cinny-717:owner"])
check("adding an existing id is refused", rc == 1 and "already held" in err, err[:70])

rc, _, err = run(["--state", str(p), "--add", "nogate"])
check("--add without a gate is refused", rc == 1 and "ID:GATE" in err, err[:70])

# there is NO interface that takes a whole list — the defect, made unreachable
check("no --items/--set/stdin path exists",
      not any(f in (SCRIPTS / "idle-held.py").read_text() for f in ('"--items"', '"--set"', "stdin.read()")))

bad = pathlib.Path(tempfile.mkdtemp()) / "bad.json"; bad.write_text("{not json")
rc, _, err = run(["--state", str(bad)])
check("a state file that is not JSON is cannot-answer (exit 2)", rc == 2 and "not JSON" in err, err[:70])
_o = io.StringIO()
with contextlib.redirect_stdout(_o):
    ih.audit_notes({"held_item_notes": {"k": "note"}}, ".")
check("audit-notes: with no held_item_ids the orphan check is SKIPPED, not judged",
      "orphan check SKIPPED" in _o.getvalue(), _o.getvalue()[:80])

missing = pathlib.Path(tempfile.mkdtemp()) / "nope.json"
rc, _, err = run(["--state", str(missing)])
check("absent state -> cannot answer (2), never an empty list", rc == 2 and "CANNOT ANSWER" in err)

bad = state(BASE); bad.write_text(json.dumps({"streak": 0}))
rc, _, err = run(["--state", str(bad)])
check("state with no held_item_ids -> 2, refuses to invent one", rc == 2 and "refusing to invent" in err)

shape = state(BASE); shape.write_text(json.dumps({"held_item_ids": ["sutando-3198"]}))
rc, _, err = run(["--state", str(shape)])
check("a bare-string list is rejected on SHAPE, not iterated as characters",
      rc == 2 and "list of [id, gate] pairs" in err, err[:80])

w = state(BASE)
rc, _, _ = run(["--state", str(w), "--remove", "ds-pr-12", "--reason", "landed", "--write"])
d = json.loads(w.read_text())
check("--write persists the new list", rc == 0 and [x[0] for x in d["held_item_ids"]] == ["sutando-3198","cinny-717"])
check("--write preserves the OTHER keys", d.get("held_item_notes", {}).get("keep") == "must survive a write")
check("--write records the removal reason", d.get("held_item_removals") == [{"id":"ds-pr-12","reason":"landed"}])

nw = state(BASE)
run(["--state", str(nw), "--remove", "ds-pr-12", "--reason", "landed"])
check("without --write the file is UNCHANGED", json.loads(nw.read_text())["held_item_ids"] == BASE)



# --- --audit-notes: a sha in a note is a COPY of a fact git owns ---------------
import subprocess as _sp
import tempfile as _tf

def _git_repo():
    d = pathlib.Path(_tf.mkdtemp())
    _sp.run(["git","init","-q","-b","main"], cwd=d, check=True)
    (d/"f").write_text("x")
    _sp.run(["git","add","-A"], cwd=d, check=True)
    _sp.run(["git","-c","user.email=t@t","-c","user.name=t","commit","-qm","c"], cwd=d, check=True)
    _sp.run(["git","branch","fix/thing"], cwd=d, check=True)
    return d, _sp.run(["git","rev-parse","--short=8","fix/thing"], cwd=d,
                      capture_output=True, text=True).stdout.strip()

_d, _sha = _git_repo()

def _audit(note):
    # The note key must be an id the doc actually HOLDS, or the orphan check fires
    # and this test stops measuring what it names.
    doc = {"held_item_ids": BASE, "held_item_notes": {BASE[0][0]: note}}
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = ih.audit_notes(doc, str(_d))
    return rc, buf.getvalue()

_rc, _out = _audit("fix/thing @ " + _sha + " — the branch")
check("audit: a note matching git exits 0", _rc == 0 and "1 of 1 match" in _out, _out[:70])

_rc, _out = _audit("fix/thing @ deadbeef — the branch")
check("audit: a DRIFTED sha exits 1 and is named", _rc == 1 and "DRIFT" in _out, _out[:70])

_rc, _out = _audit("no branch reference here at all")
check("audit: no branch@sha -> 0, and says it discriminated nothing",
      _rc == 0 and "nothing this check can discriminate" in _out, _out[:70])

_rc, _out = _audit("fix/absent @ " + _sha + " — a branch git cannot resolve")
check("audit: an unresolvable branch is reported, not silently counted as a match",
      "cannot resolve" in _out, _out[:70])

# --- --audit-notes must ALSO catch ORPHAN notes (id no longer held) -----------
# A sha-only audit cannot see one: an orphan carrying no branch@sha is invisible.
def _audit_full(notes, items):
    d = {"held_item_ids": items, "held_item_notes": notes}
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = ih.audit_notes(d, str(_d))
    return rc, out.getvalue()

_rc, _out = _audit_full({"ghost-42": "a note for an id nobody holds"},
                        [["sutando-3198", "owner"]])
check("audit: an ORPHAN note is reported", "ghost-42" in _out and "ORPHAN" in _out.upper(), _out[:110])
check("audit: an orphan note makes it exit non-zero", _rc == 1, f"rc={_rc} {_out[:80]}")

# Control: a note whose id IS held must NOT be reported as an orphan.
_rc2, _out2 = _audit_full({"sutando-3198": "a note for an id that IS held"},
                          [["sutando-3198", "owner"]])
check("audit control: a held id's note is not an orphan",
      "ORPHAN" not in _out2.upper() and _rc2 == 0, f"rc={_rc2} {_out2[:90]}")

# A held id with NO note is NOT an error — reported, never exit 1. Failing on
# those would fire every run and get the whole check demoted.
_rc3, _out3 = _audit_full({}, [["sutando-3198", "owner"], ["cinny-700", "owner"]])
check("audit: note-less held ids are reported but do NOT fail", _rc3 == 0, f"rc={_rc3}")
check("audit: note-less held ids are named", "2" in _out3 and "no note" in _out3.lower(), _out3[:110])

# ---- --audit-prs: sonichi/sutando#3487 MERGED 2026-09-01 is a stable terminal
# fixture, so a held item pointing at it MUST be reported stale -----------------
import os as _os
import sys as _sys
_TOOL = str(SCRIPTS / "idle-held.py")
_PY = [_sys.executable]
if _os.environ.get("SUTANDO_TEST_SUBPROCESS_COVERAGE") == "1":
    _PY += ["-m", "coverage", "run", f"--rcfile={SCRIPTS.parents[2] / '.coveragerc'}"]


def _gh_shim(td):
    """A fake `gh` on PATH: #3487 is MERGED, #3198 is OPEN, anything else fails.
    Hermetic — the real tool shells out to gh, which CI cannot reach."""
    d = pathlib.Path(td) / "bin"; d.mkdir()
    gh = d / "gh"
    gh.write_text("""#!/bin/sh
# argv: pr view <num> --repo <repo> --json ...
[ -n "$IH_TEST_GH_FAIL" ] && { echo "gh: could not resolve" >&2; exit 1; }
case "$3" in
  3487) echo '{"state": "MERGED", "mergeStateStatus": "CLEAN"}' ;;
  3198) echo '{"state": "OPEN", "mergeStateStatus": "CLEAN"}' ;;
  *) echo "gh: could not resolve" >&2; exit 1 ;;
esac
""")
    gh.chmod(0o755)
    return {**_os.environ, "PATH": f"{d}{_os.pathsep}{_os.environ.get('PATH', '')}"}

def _audit(doc, gh_fail=False):
    with _tf.TemporaryDirectory() as td:
        f = _os.path.join(td, "s.json")
        open(f, "w").write(json.dumps(doc))
        env = _gh_shim(td)
        if gh_fail:
            env["IH_TEST_GH_FAIL"] = "1"
        r = _sp.run([*_PY, _TOOL, "--state", f, "--audit-prs"],
                    capture_output=True, text=True, env=env)
        return r.returncode, r.stdout + r.stderr

rc, out = _audit({"held_item_ids": [["merged-one", "owner"]],
                  "held_item_notes": {"merged-one": "PR sonichi/sutando#3487 landed by auto-merge"}})
check("audit-prs: a MERGED PR makes the held item stale (exit 1)", rc == 1, f"rc={rc}")
check("audit-prs: names the stale id", "merged-one" in out)
check("audit-prs: prints the retire command", "--remove merged-one" in out)

rc, out = _audit({"held_item_ids": [["open-one", "owner"]],
                  "held_item_notes": {"open-one": "PR sonichi/sutando#3198 waiting on the owner"}})
check("audit-prs: an OPEN PR passes (exit 0)", rc == 0, f"rc={rc}")

# An id that LOOKS like it holds a PR number must never be guessed at: by hand,
# `stroke-fix-36177568` parsed as PRs 36177 AND 568, neither of which exists.
rc, out = _audit({"held_item_ids": [["stroke-fix-36177568", "owner"]], "held_item_notes": {}})
check("audit-prs: does NOT infer a PR from the id (exit 0)", rc == 0, f"rc={rc}")
check("audit-prs: reports it unmapped instead", "unmapped" in out)
check("audit-prs: never emits the phantom number", "36177" not in out.replace("stroke-fix-36177568", ""))

# A PR that cannot be read is not a PR known to be fine.
rc, out = _audit({"held_item_ids": [["open-one", "owner"]],
                  "held_item_notes": {"open-one": "PR sonichi/sutando#3198 waiting"}}, gh_fail=True)
check("audit-prs: an unreadable PR is cannot-answer (exit 2), not a clean bill", rc == 2, f"rc={rc}")
check("audit-prs: names the unresolvable row", "ERROR  open-one" in out, out[:120])
rc, out = _audit({"held_item_ids": "not-a-list"})
check("audit-prs: no held_item_ids list -> exit 2", rc == 2, f"rc={rc}")


# --- archive-orphan-notes: an orphan note outlives the id it described, and
# the audit stays red until something can clear it -------------------------

def _arch(doc, write=False):
    with _tf.TemporaryDirectory() as td:
        f = _os.path.join(td, "s.json")
        open(f, "w").write(json.dumps(doc))
        cmd = [*_PY, _TOOL, "--state", f, "--archive-orphan-notes"]
        if write:
            cmd.append("--write")
        r = _sp.run(cmd, capture_output=True, text=True)
        return r.returncode, r.stdout + r.stderr, json.load(open(f))

ORPH = {"held_item_ids": [["kept", "owner"]],
        "held_item_notes": {"kept": "still held", "gone": "PR merged last week"}}

rc, out, doc = _arch(ORPH)
check("archive: finds the orphan (exit 0)", rc == 0, f"rc={rc}")
check("archive: names the orphan", "gone" in out)
check("archive: dry run does NOT move it", doc["held_item_notes"].get("gone") is not None)
check("archive: dry run says so", "not written" in out)

rc, out, doc = _arch(ORPH, write=True)
check("archive --write: orphan LEAVES held_item_notes", "gone" not in doc["held_item_notes"])
check("archive --write: content PRESERVED, not deleted",
      doc["held_item_notes_archived"]["gone"]["note"] == "PR merged last week")
check("archive --write: records when", "archived_at" in doc["held_item_notes_archived"]["gone"])

# THE DISCRIMINATOR: a note whose id is still held must survive. Without this the
# op passes by archiving everything, which is deletion wearing an archive's name.
check("archive --write: a HELD id's note is untouched",
      doc["held_item_notes"].get("kept") == "still held")
check("archive --write: a HELD id is NOT archived",
      "kept" not in doc.get("held_item_notes_archived", {}))

# Absent ids: orphanhood is unanswerable, so refuse (2), never archive. Through
# the CLI it is load() that refuses, so the function's own guard is hit directly below.
rc, out, doc = _arch({"held_item_notes": {"a": "x"}}, write=True)
check("archive: no held_item_ids -> CANNOT ANSWER (exit 2)", rc == 2, f"rc={rc}")
check("archive: it is load() that answers, by its own wording",
      "refusing to invent one" in out)
check("archive: refusing leaves the notes alone", doc["held_item_notes"] == {"a": "x"})

import importlib.util as _ilu
_spec = _ilu.spec_from_file_location("_ih", _TOOL)
_ih = _ilu.module_from_spec(_spec); _spec.loader.exec_module(_ih)
_moved, _err = _ih.archive_orphan_notes({"held_item_notes": {"a": "x"}}, "now")
check("archive fn: refuses directly when ids are absent", _moved is None and bool(_err),
      f"moved={_moved!r} err={_err!r}")
_moved, _err = _ih.archive_orphan_notes({"held_item_ids": [["k", "owner"]]}, "now")
check("archive fn: no notes object -> refuses rather than inventing", _moved is None and bool(_err),
      f"moved={_moved!r} err={_err!r}")

rc, out, doc = _arch({"held_item_ids": [["kept", "owner"]],
                      "held_item_notes": {"kept": "still held"}}, write=True)
check("archive: no orphans -> exit 0 and no archive key created", rc == 0 and
      "held_item_notes_archived" not in doc, f"rc={rc}")

# End-to-end: archiving is what lets --audit-notes reach green.
with _tf.TemporaryDirectory() as td:
    f = _os.path.join(td, "s.json")
    open(f, "w").write(json.dumps(ORPH))
    _sp.run([*_PY, _TOOL, "--state", f, "--archive-orphan-notes", "--write"],
            capture_output=True, text=True)
    r = _sp.run([*_PY, _TOOL, "--state", f, "--audit-notes", str(SCRIPTS)],
                capture_output=True, text=True)
    check("archive: --audit-notes goes GREEN afterwards", r.returncode == 0,
          f"rc={r.returncode} {r.stdout}{r.stderr}")


# --init-empty: the ONE path that may create the key, and only when absent (#3773).
# `load` cannot tell a never-seeded host from a drifted one, so it refuses both.
fresh = pathlib.Path(tempfile.mkdtemp()) / "s.json"
fresh.write_text(json.dumps({"streak": 0, "noop_total": 48,
                             "last_surfaced_ids": ["3753:peer-review"]}))

rc, _, err = run(["--state", str(fresh), "--add", "3857:owner", "--write"])
check("PRE-CONTROL: --add on a keyless state still refuses",
      rc == 2 and "refusing to invent" in err, err[:80])

rc, out, _ = run(["--state", str(fresh), "--init-empty"])
_d = json.loads(fresh.read_text())
check("--init-empty creates the key as [] and exits 0",
      rc == 0 and _d.get("held_item_ids") == [], f"rc={rc} {_d.get('held_item_ids')!r}")
check("--init-empty records provenance, so a seeded key is never anonymous",
      _d.get("held_item_seed", {}).get("by") == "idle-held.py --init-empty",
      str(_d.get("held_item_seed"))[:70])
check("--init-empty leaves every other key untouched",
      _d.get("streak") == 0 and _d.get("noop_total") == 48
      and _d.get("last_surfaced_ids") == ["3753:peer-review"], str(_d)[:90])

rc, _, err = run(["--state", str(fresh), "--init-empty"])
check("--init-empty REFUSES a second time — it bootstraps, never clears",
      rc == 2 and "REFUSED" in err, err[:80])

rc, _, _ = run(["--state", str(fresh), "--add", "3857:owner", "--write"])
check("THE POINT: --add works after the bootstrap",
      rc == 0 and json.loads(fresh.read_text())["held_item_ids"] == [["3857", "owner"]],
      str(json.loads(fresh.read_text()).get("held_item_ids"))[:60])

pop = state(BASE)
rc, _, err = run(["--state", str(pop), "--init-empty"])
check("--init-empty on a POPULATED record refuses and does not wipe it",
      rc == 2 and "REFUSED" in err
      and json.loads(pop.read_text())["held_item_ids"] == BASE, err[:80])

nofile = pathlib.Path(tempfile.mkdtemp()) / "nope.json"
rc, _, err = run(["--state", str(nofile), "--init-empty"])
check("--init-empty on a MISSING state file cannot answer, never creates one",
      rc == 2 and not nofile.exists(), f"rc={rc} exists={nofile.exists()}")


print(f"\n{'FAILED: ' + ', '.join(fails) if fails else 'all passed'} ({ran - len(fails)}/{ran} assertions)")
sys.exit(1 if fails else 0)
