#!/usr/bin/env python3
"""Regression for src/archive-stale-results.py's empty-file exclusion.

The archiver moves stale results/*.txt into a dated archive dir on mtime. An
EMPTY .txt must be excluded: a producer can create a proactive-*.txt and pause
before its first flush, and an empty file keeps its creation mtime while the
descriptor is open. Moving it on age would strand the producer's later flush in
the archived inode — the exact data-loss the proactive drain removes
(sonichi/sutando#2324). The archiver is the other mtime-keyed mover of these
files, so it must make the same exclusion for the guarantee to hold end-to-end.

Runs the archiver IN-PROCESS (import + main()) rather than as a subprocess, so
the diff-coverage gate instruments the new exclusion branch — a subprocess call
executes the lines but coverage on the parent process never sees them.

Guards:
  1. a CONTENTFUL stale .txt is archived (flood prevention still works)
  2. an EMPTY stale .txt is NOT archived — left in place for the drain
  3. a fresh (non-stale) .txt is untouched regardless of size
  4. a PARTIAL-but-nonempty .txt held open by a writer is NOT archived, and the
     producer's completed body is still readable from the LIVE queue afterwards
     — content is not a completion signal, so the guard consults the
     open-descriptor table rather than inspecting bytes
  5. when that check cannot be run, NOTHING is archived and the reason is
     printed — fail closed, loudly
  6. producer-to-reader: a result this archiver moves is still findable by the
     SHARED locator afterwards. The archiver writes `archive-<YYYY-MM-DD>/`, a
     sibling of `archive/`; a locator that searches only `archive/` reports an
     archived answer as never delivered.

Several guards assert absence ("not archived"), which would also hold if the
archiver had simply stopped working. Each is therefore paired with a positive
calibration that the SAME file archives once its blocking condition is removed,
so a preserved file is attributable to the guard and not to a no-op.

Run: python3 tests/archive-stale-results.test.py   (exit 0/1)
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
fails: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ok  " if cond else "  FAIL ") + name + ((" — " + detail) if detail and not cond else ""))
    if not cond:
        fails.append(name)


def _load_archiver(workspace: Path):
    # The module resolves its workspace + reads RETENTION_HOURS/DRY_RUN at IMPORT
    # time, so the env must be set before exec_module. SUTANDO_TEST_MODE lets
    # resolve_workspace honor SUTANDO_WORKSPACE (post-#1440 it is otherwise
    # ignored). Import (not subprocess) so coverage instruments main().
    os.environ["SUTANDO_TEST_MODE"] = "1"
    os.environ["SUTANDO_WORKSPACE"] = str(workspace)
    os.environ["RETENTION_HOURS"] = "24"
    os.environ.pop("DRY_RUN", None)
    spec = importlib.util.spec_from_file_location(
        "archiver_ut", REPO / "src" / "archive-stale-results.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _guard_producer_to_reader() -> None:
    """The archiver's own output must remain readable by the shared locator."""
    sys.path.insert(0, str(REPO / "src"))
    from local_task_protocol import find_result  # noqa: E402
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        # Same pre-satisfaction main() does: keep resolve_workspace() from
        # relocating this repo's notes/build_log into the throwaway workspace.
        (ws / ".notes-migrated").touch()
        (ws / ".build_log-migrated").touch()
        results = ws / "results"
        results.mkdir(parents=True)
        f = results / "task-retention-lifecycle.txt"
        f.write_text("DURABLE ANSWER")
        stale = time.time() - 48 * 3600
        os.utime(f, (stale, stale))

        live = find_result(results, "task-retention-lifecycle")
        check("calibration: findable BEFORE archiving", live is not None)

        mod = _load_archiver(ws)
        with contextlib.redirect_stdout(io.StringIO()):
            mod.main()
        check("archiver moved it out of the live dir", not f.exists())
        moved = list(results.glob("archive-*/task-retention-lifecycle.txt"))
        check("archiver used the dated retention layout", len(moved) == 1,
              f"found {moved}")

        found = find_result(results, "task-retention-lifecycle")
        check("locator still finds it AFTER archiving", found is not None,
              "archived answer reads as never delivered")
        if found is not None:
            check("and it is the same answer",
                  found.read_text() == "DURABLE ANSWER")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="archiver-empty-"))
    # Pre-satisfy the in-repo migrators so resolve_workspace() at import doesn't
    # relocate this repo's notes/build_log into the throwaway workspace.
    (tmp / ".notes-migrated").touch()
    (tmp / ".build_log-migrated").touch()

    arch = _load_archiver(tmp)
    results = arch.RESULTS  # = <tmp>/results, captured at import
    results.mkdir(parents=True, exist_ok=True)

    old = time.time() - 48 * 3600  # well past the default 24h retention
    contentful = results / "proactive-contentful.txt"
    contentful.write_text("a real stale nudge body\n")
    os.utime(contentful, (old, old))

    empty = results / "proactive-empty.txt"
    empty.write_text("")  # 0 bytes — a producer that has not flushed yet
    os.utime(empty, (old, old))

    # air's #2360 follow-up: a whitespace-only file is NON-zero size but empty
    # after strip() — a producer that wrote a newline/header then paused. A
    # size-only check would archive it; the drain would not. The two movers must
    # agree, so this must ALSO be left in place.
    whitespace = results / "proactive-whitespace.txt"
    whitespace.write_text("   \n\t\n")  # size > 0, strip() == ""
    os.utime(whitespace, (old, old))

    # John's #2360 ask: an invalid-UTF-8 stale file exercises the fail-safe
    # OSError/UnicodeDecodeError branch (src/archive-stale-results.py:102-103). A
    # file we cannot decode is exactly one that may be mid-write, so read_text()
    # raising must leave it in place, never archive it on age.
    undecodable = results / "proactive-binary.txt"
    undecodable.write_bytes(b"\xff\xfe\x00\x80 partial write")  # invalid UTF-8
    os.utime(undecodable, (old, old))

    fresh_empty = results / "proactive-fresh.txt"
    fresh_empty.write_text("")  # 0 bytes but recent — also must stay

    # John's #2360 P1 blocker: nonempty readable content is NOT a completion
    # signal. A producer that wrote a header and paused leaves a file that is
    # non-zero, decodable and strip()-nonempty — every content check above says
    # "archive it" — while the descriptor is still open. Renaming it strands the
    # producer's later flush in the archived inode. Reproduced on 780f7b6:
    # before='header\n', archived-after-flush='header\nlater body\n',
    # original_exists=False. The fd stays open across main() ON PURPOSE.
    partial = results / "proactive-partial.txt"
    partial_fh = open(partial, "w")
    partial_fh.write("header\n")
    partial_fh.flush()
    os.fsync(partial_fh.fileno())
    os.utime(partial, (old, old))

    rc = arch.main()  # in-process → coverage sees the strip-empty exclusion branch
    check("archiver main() returns 0", rc == 0, f"rc={rc}")

    archived_names = {p.name for p in results.glob("archive-*/*.txt")}

    check("contentful stale .txt is archived (flood prevention intact)",
          "proactive-contentful.txt" in archived_names and not contentful.exists(),
          f"archived={sorted(archived_names)}")
    check("empty stale .txt is NOT archived — left in place for the drain",
          empty.exists() and "proactive-empty.txt" not in archived_names,
          f"empty exists={empty.exists()} archived={sorted(archived_names)}")
    check("whitespace-only stale .txt is NOT archived (strip-empty, matches the drain)",
          whitespace.exists() and "proactive-whitespace.txt" not in archived_names,
          f"whitespace exists={whitespace.exists()} archived={sorted(archived_names)}")
    check("invalid-UTF-8 stale .txt is NOT archived (fail-safe branch: may be mid-write)",
          undecodable.exists() and "proactive-binary.txt" not in archived_names,
          f"undecodable exists={undecodable.exists()} archived={sorted(archived_names)}")
    check("fresh empty .txt is untouched",
          fresh_empty.exists() and "proactive-fresh.txt" not in archived_names)

    # The blocker guard. Finish the producer's write AFTER the sweep and assert
    # the completed body is readable from the LIVE queue — not merely that the
    # file was skipped. Asserting only "not archived" would pass even if the
    # bytes had gone somewhere useless; the consumer-visible fact is what the
    # owner actually depends on.
    partial_fh.write("later body\n")
    partial_fh.flush()
    os.fsync(partial_fh.fileno())
    partial_fh.close()
    check("partial-but-nonempty .txt held open by a writer is NOT archived",
          partial.exists() and "proactive-partial.txt" not in archived_names,
          f"partial exists={partial.exists()} archived={sorted(archived_names)}")
    check("the producer's completed body is intact in the LIVE queue",
          partial.exists() and partial.read_text() == "header\nlater body\n",
          f"live content={partial.read_text()!r}" if partial.exists() else "file gone")

    # Calibration: the guard must still be able to say YES. A suite where every
    # assertion is "not archived" would also pass if the archiver were a no-op,
    # so pin the positive case explicitly rather than inferring it.
    check("guard is not a blanket no-op — the closed contentful file DID move",
          "proactive-contentful.txt" in archived_names)

    # ---- fail-closed: lsof cannot be consulted -----------------------------
    # Runs IN-PROCESS (same reason as the rest of this file: a subprocess would
    # execute the branch but coverage on the parent would never see it). A real
    # stub on PATH rather than a monkeypatched shutil.which, so the actual
    # resolve-and-run path executes; a stub that exits 4 is neither lsof's "some
    # matched" (0) nor its "none matched" (1), i.e. a genuine non-answer.
    stub_dir = tmp / "fakebin"
    stub_dir.mkdir(exist_ok=True)
    stub = stub_dir / "lsof"
    stub.write_text("#!/bin/sh\necho 'simulated lsof failure' >&2\nexit 4\n")
    stub.chmod(0o755)

    unverifiable = results / "proactive-unverifiable.txt"
    unverifiable.write_text("a complete body nothing holds open\n")
    os.utime(unverifiable, (old, old))

    prev_path = os.environ["PATH"]
    os.environ["PATH"] = f"{stub_dir}:{prev_path}"
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            rc_unverif = arch.main()
    finally:
        os.environ["PATH"] = prev_path

    check("fail-closed run still exits 0 (a non-answer is not an error)",
          rc_unverif == 0, f"rc={rc_unverif}")
    check("cannot-verify ⇒ the stale file is NOT archived",
          unverifiable.exists()
          and "proactive-unverifiable.txt" not in {p.name for p in results.glob("archive-*/*.txt")},
          f"exists={unverifiable.exists()}")
    check("cannot-verify is reported loudly on stderr, not degraded silently",
          "cannot verify open writers" in err.getvalue(), err.getvalue().strip()[:120])

    # CALIBRATION. Without this, the two assertions above would also pass if the
    # archiver had simply stopped working — they only observe absence. Remove the
    # stub and the SAME file must now move, which attributes its preservation to
    # the unavailable check and nothing else. (My first draft of this control
    # passed because the script crashed before reaching the guard.)
    rc_after = arch.main()
    check("calibration — with lsof restored the SAME file archives",
          rc_after == 0
          and not unverifiable.exists()
          and "proactive-unverifiable.txt" in {p.name for p in results.glob("archive-*/*.txt")},
          f"rc={rc_after} exists={unverifiable.exists()}")

    # ---- paths_held_open() edge cases, exercised directly -------------------
    # These are branches main() cannot reach on its own: it only calls the helper
    # when there is at least one candidate and when lsof is resolvable. Testing
    # them through main() would need contortions that prove less than calling the
    # function does.

    # Empty input must short-circuit. This is not defensive noise: `lsof -F pn --`
    # with NO file arguments lists EVERY open file on the machine, so a missing
    # guard here would turn "nothing to check" into "everything looks held" —
    # the archiver would silently stop working. Assert the short-circuit rather
    # than trusting the caller's `if candidates:` to stay there forever.
    check("paths_held_open([]) short-circuits to empty (never a bare lsof)",
          arch.paths_held_open([]) == set())

    # An lsof that cannot be EXECUTED raises OSError out of subprocess.run — a
    # different branch from the non-zero-exit case above, which runs a real stub.
    # Emptying PATH is not enough: shutil.which() then returns None and the code
    # falls back to the absolute /usr/sbin/lsof, which exists on macOS and not on
    # Linux, so that approach would pass or fail depending on the runner. Patch
    # the resolution seam instead, so the branch fires identically everywhere.
    missing_bin = str(tmp / "definitely-not-lsof")
    real_which = arch.shutil.which
    arch.shutil.which = lambda *a, **k: missing_bin
    try:
        raised = False
        try:
            arch.paths_held_open([results / "anything.txt"])
        except arch.OpenWriterCheckUnavailable:
            raised = True
        except Exception as e:  # any other exception type is a bug, not a pass
            raised = f"wrong exception: {type(e).__name__}: {e}"
    finally:
        arch.shutil.which = real_which
    check("unrunnable lsof raises OpenWriterCheckUnavailable, not a raw OSError",
          raised is True, str(raised))
    check("...and shutil.which was restored (no leak into later assertions)",
          arch.shutil.which is real_which)

    # A sweep with NO stale candidates must not consult lsof at all and must
    # still report cleanly. Separate workspace so the files above don't leak in.
    tmp2 = Path(tempfile.mkdtemp(prefix="archiver-nocand-"))
    (tmp2 / ".notes-migrated").touch()
    (tmp2 / ".build_log-migrated").touch()
    arch2 = _load_archiver(tmp2)
    arch2.RESULTS.mkdir(parents=True, exist_ok=True)
    (arch2.RESULTS / "fresh.txt").write_text("recent, not stale\n")
    check("a sweep with zero stale candidates returns 0", arch2.main() == 0)
    check("...and leaves the fresh file alone",
          (arch2.RESULTS / "fresh.txt").exists())

    print()
    _guard_producer_to_reader()

    if fails:
        print(f"FAIL — {len(fails)}: {fails}")
        return 1
    print("PASS — archiver excludes empty results files (open-fd invariant)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
