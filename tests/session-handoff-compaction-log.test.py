#!/usr/bin/env python3
"""A compaction must leave a durable trace. NOT run end to end: a temp
sutando.config.local.json still resolved to the LIVE workspace when measured."""
from pathlib import Path
import json
import re
import shlex
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parent.parent / "src" / "session-handoff.sh"
FN = "record_compaction_event"
HELPERS = ["_ch_json_escape"]


def _fn_source() -> str:
    """The real bodies, extracted — never a re-typed copy. HELPERS must ride
    along: an extracted function whose callee is missing silently writes ""."""
    text = SCRIPT.read_text()
    out = []
    for name in HELPERS + [FN]:
        m = re.search(rf"^{name}\(\) \{{.*?^\}}", text, re.S | re.M)
        if not m:
            raise AssertionError(f"{name} not found in {SCRIPT}")
        out.append(m.group(0))
    return "\n".join(out)


def _run(workspace: Path, *calls: tuple) -> subprocess.CompletedProcess:
    """Exec the extracted function against a temp WORKSPACE_DIR."""
    body = [f'WORKSPACE_DIR={workspace!s}', _fn_source()]
    for transcript, trigger in calls:
        # shlex.quote, not an f-string: a " in the VALUE would otherwise close
        # the shell string and the harness would test a different input.
        body.append(f"{FN} {shlex.quote(transcript)} {shlex.quote(trigger)}")
    body.append("exit 0")
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as fh:
        fh.write("\n".join(body))
        path = fh.name
    return subprocess.run(["bash", path], capture_output=True, text=True, timeout=30)


class CallSiteIsReachable(unittest.TestCase):
    """A recorder nothing calls is the defect this change is about."""

    def setUp(self):
        self.text = SCRIPT.read_text()
        self.lines = self.text.splitlines()

    def test_the_function_is_defined(self):
        # (?m) is load-bearing: assertRegex uses re.search, so a bare ^ anchors
        # to the start of the whole file, not to a line.
        self.assertRegex(self.text, rf"(?m)^{FN}\(\) \{{", "recorder is not defined")

    def _line_of(self, pattern):
        for i, l in enumerate(self.lines, 1):
            if re.match(pattern, l):
                return i
        return None

    def test_there_is_a_column_zero_call_site(self):
        """A bare grep also matches a call nested in a function or an if."""
        self.assertIsNotNone(self._line_of(rf'^{FN} "'),
                             "defined but never called at top level")

    def test_the_call_is_outside_the_definition(self):
        start = self._line_of(rf"^{FN}\(\) \{{")
        end = next(i for i, l in enumerate(self.lines[start:], start + 1)
                   if l == "}")
        call = self._line_of(rf'^{FN} "')
        self.assertGreater(call, end,
                           f"call at {call} is inside the body (ends {end})")

    def test_no_unclosed_conditional_precedes_the_call(self):
        """Column 0 rules out an indented call, not a top-level `if` wrapping it."""
        start = self._line_of(rf"^{FN}\(\) \{{")
        end = next(i for i, l in enumerate(self.lines[start:], start + 1)
                   if l == "}")
        call = self._line_of(rf'^{FN} "')
        between = self.lines[end:call - 1]
        opens = sum(1 for l in between if re.match(r"^(if|case|while|until|for) ", l))
        closes = sum(1 for l in between if re.match(r"^(fi|esac|done)$", l))
        self.assertLessEqual(opens, closes,
                             f"unclosed conditional before the call "
                             f"(opens={opens} closes={closes}) — may not run")

    def test_the_log_lives_under_state(self):
        self.assertIn("state/compactions.jsonl", self.text,
                      "workspace contract puts state under state/")


class TheRecordItWrites(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.ws = Path(self._td.name) / "ws"
        (self.ws / "state").mkdir(parents=True)
        self.log = self.ws / "state" / "compactions.jsonl"

    def tearDown(self):
        self._td.cleanup()

    def test_one_valid_json_line_with_every_field(self):
        _run(self.ws, ("/some/path/transcript-abc.jsonl", "precompact"))
        self.assertTrue(self.log.is_file(), "no line written")
        rec = json.loads(self.log.read_text().strip().splitlines()[-1])
        for k in ("ts", "epoch", "host", "transcript", "trigger"):
            self.assertIn(k, rec)
        self.assertEqual(rec["transcript"], "transcript-abc.jsonl",
                         "transcript must be the basename, not the full path")
        self.assertEqual(rec["trigger"], "precompact")
        self.assertIsInstance(rec["epoch"], int)

    def test_it_appends_rather_than_overwrites(self):
        _run(self.ws, ("/a.jsonl", "precompact"), ("/b.jsonl", "precompact"))
        self.assertEqual(len(self.log.read_text().strip().splitlines()), 2)

    def test_bounded_so_a_long_lived_core_cannot_grow_it_forever(self):
        self.log.write_text('{"ts":"x","epoch":0}\n' * 600)
        _run(self.ws, ("/third.jsonl", "precompact"))
        lines = self.log.read_text().strip().splitlines()
        self.assertLessEqual(len(lines), 500, "unbounded")
        self.assertIn("third.jsonl", lines[-1],
                      "the trim must keep the NEWEST event, not the oldest")

    def test_a_quote_in_any_field_still_yields_parseable_json(self):
        """host/transcript/trigger are external input; unescaped they break the
        whole line for every later reader."""
        _run(self.ws, ('/tmp/tra"nscript.jsonl', 'pre"compact'))
        rec = json.loads(self.log.read_text().strip().splitlines()[-1])
        self.assertEqual(rec["transcript"], 'tra"nscript.jsonl')
        self.assertEqual(rec["trigger"], 'pre"compact')

    def test_a_backslash_survives_as_a_backslash(self):
        _run(self.ws, (r'/tmp/back\slash.jsonl', "precompact"))
        rec = json.loads(self.log.read_text().strip().splitlines()[-1])
        self.assertIn("\\", rec["transcript"])

    def test_a_newline_cannot_split_one_event_into_two_lines(self):
        """jsonl is line-delimited, so an embedded newline is not just ugly."""
        _run(self.ws, ("/tmp/a.jsonl", "pre\ncompact"))
        lines = self.log.read_text().strip().splitlines()
        self.assertEqual(len(lines), 1, f"one event became {len(lines)} lines")
        json.loads(lines[0])

    def test_an_unwritable_log_is_never_fatal(self):
        """This runs inside PreCompact; a nonzero exit is worse than no line."""
        ws = Path(self._td.name) / "ro"
        ws.mkdir()
        (ws / "state").write_text("a file where the dir must go")
        self.assertEqual(_run(ws, ("/x.jsonl", "precompact")).returncode, 0)


class ControlCharactersAreEscaped(unittest.TestCase):
    """JSON forbids every raw U+0000-U+001F, not only the three whitespace ones."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.ws = Path(self._td.name) / "ws"
        (self.ws / "state").mkdir(parents=True)
        self.log = self.ws / "state" / "compactions.jsonl"

    def tearDown(self):
        self._td.cleanup()

    def test_a_control_char_outside_tab_newline_cr_still_parses(self):
        _run(self.ws, ("/t.jsonl", "pre\x01compact"))
        line = self.log.read_text().strip().splitlines()[-1]
        rec = json.loads(line)          # raised "Invalid control character" pre-fix
        self.assertNotIn("\x01", rec["trigger"])

    def test_every_c0_control_character_is_removed(self):
        """One positive control per character, so no single survivor hides."""
        for code in list(range(1, 32)) + [127]:
            with self.subTest(code=code):
                self.log.unlink(missing_ok=True)
                _run(self.ws, ("/t.jsonl", f"pre{chr(code)}compact"))
                rec = json.loads(self.log.read_text().strip().splitlines()[-1])
                self.assertNotIn(chr(code), rec["trigger"])

    def test_the_escaper_does_not_smuggle_the_class_name(self):
        """A literal [[:cntrl:]] mismatch would replace nothing and still pass a
        weaker assertion; prove an ordinary character survives untouched."""
        _run(self.ws, ("/t.jsonl", "precompact-ok"))
        rec = json.loads(self.log.read_text().strip().splitlines()[-1])
        self.assertEqual(rec["trigger"], "precompact-ok")


class ConcurrentWritersDoNotLoseEvents(unittest.TestCase):
    """Trim-then-append is read-modify-write on one shared file. Drives the
    PRODUCTION function, not a re-typed recipe."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.ws = Path(self._td.name) / "ws"
        (self.ws / "state").mkdir(parents=True)
        self.log = self.ws / "state" / "compactions.jsonl"

    def tearDown(self):
        self._td.cleanup()

    def test_32_concurrent_writers_at_the_bound_lose_nothing(self):
        # Pre-seed at the cap so every writer takes the trim path.
        self.log.write_text(''.join(
            '{"ts":"x","epoch":0,"trigger":"seed"}\n' for _ in range(500)))
        scripts = []
        for i in range(32):
            body = [f'WORKSPACE_DIR={self.ws!s}', _fn_source(),
                    f"{FN} /t.jsonl {shlex.quote(f'concurrent-{i}')}", "exit 0"]
            fh = tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False)
            fh.write("\n".join(body))
            fh.close()
            scripts.append(fh.name)
        procs = [subprocess.Popen(["bash", s], stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL) for s in scripts]
        for pr in procs:
            pr.wait(timeout=60)
        text = self.log.read_text()
        missing = [i for i in range(32) if f'"concurrent-{i}"' not in text]
        self.assertEqual(missing, [], f"lost events: {missing}")

    def test_every_line_is_still_parseable_after_the_race(self):
        """A lost event and a torn line are different failures; check both."""
        self.log.write_text(''.join(
            '{"ts":"x","epoch":0,"trigger":"seed"}\n' for _ in range(500)))
        procs = []
        for i in range(16):
            body = [f'WORKSPACE_DIR={self.ws!s}', _fn_source(),
                    f"{FN} /t.jsonl {shlex.quote(f'torn-{i}')}", "exit 0"]
            fh = tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False)
            fh.write("\n".join(body))
            fh.close()
            procs.append(subprocess.Popen(["bash", fh.name],
                                          stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL))
        for pr in procs:
            pr.wait(timeout=60)
        for n, line in enumerate(self.log.read_text().strip().splitlines(), 1):
            json.loads(line)

    def test_no_lock_or_temp_is_left_behind(self):
        _run(self.ws, ("/t.jsonl", "precompact"))
        leftovers = sorted(p.name for p in (self.ws / "state").iterdir()
                           if p.name != "compactions.jsonl")
        self.assertEqual(leftovers, [], f"stale artifacts: {leftovers}")



class CallSitePassesTheResolvedTranscript(unittest.TestCase):
    """The stock hook supplies transcript_path on STDIN, not argv. Passing $1
    to the recorder logs an empty transcript on exactly that path."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.ws = Path(self._td.name) / "ws"
        (self.ws / "state").mkdir(parents=True)
        self.log = self.ws / "state" / "compactions.jsonl"

    def tearDown(self):
        self._td.cleanup()

    def _stdin_path_script(self) -> str:
        """Real top-level lines, extracted — the assignment, the stdin parse and
        the call. A re-typed copy could not observe the argument bug."""
        text = SCRIPT.read_text()
        assign = re.search(r'^TRANSCRIPT="\$1".*$', text, re.M)
        parse = re.search(r'^if \[ -z "\$TRANSCRIPT" \] && \[ ! -t 0 \]; then.*?^fi$',
                          text, re.S | re.M)
        call = re.search(r'^record_compaction_event .*$', text, re.M)
        for name, m in (("assignment", assign), ("stdin parse", parse), ("call site", call)):
            if not m:
                raise AssertionError(f"{name} not found in {SCRIPT}")
        return "\n".join([f"WORKSPACE_DIR={self.ws!s}", _fn_source(),
                           assign.group(0), parse.group(0), call.group(0), "exit 0"])

    def _run_with_stdin(self, payload: str) -> subprocess.CompletedProcess:
        with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as fh:
            fh.write(self._stdin_path_script())
            path = fh.name
        return subprocess.run(["bash", path], input=payload, capture_output=True,
                              text=True, timeout=30)

    def test_transcript_from_stdin_reaches_the_log(self):
        self._run_with_stdin('{"transcript_path": "/tmp/x/transcript-stdin.jsonl"}')
        self.assertTrue(self.log.is_file(), "no line written on the stdin hook path")
        rec = json.loads(self.log.read_text().strip().splitlines()[-1])
        self.assertEqual(
            rec["transcript"], "transcript-stdin.jsonl",
            "the call site passed $1 instead of the stdin-resolved $TRANSCRIPT, so the "
            "record names no transcript on the ONLY path the stock hook uses")

    def test_an_explicit_argv_path_still_wins(self):
        """Manual invocation passes $1; stdin parsing must not displace it."""
        with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as fh:
            fh.write(self._stdin_path_script().replace(
                "exit 0", "").replace(f"WORKSPACE_DIR={self.ws!s}",
                                      f"WORKSPACE_DIR={self.ws!s}\nset -- /tmp/y/argv-one.jsonl"))
            path = fh.name
        subprocess.run(["bash", path], input="", capture_output=True, text=True, timeout=30)
        rec = json.loads(self.log.read_text().strip().splitlines()[-1])
        self.assertEqual(rec["transcript"], "argv-one.jsonl")

    def test_neither_source_present_is_empty_not_a_crash(self):
        r = self._run_with_stdin("")
        self.assertEqual(r.returncode, 0)
        rec = json.loads(self.log.read_text().strip().splitlines()[-1])
        self.assertEqual(rec["transcript"], "")



class ALockThisCallDidNotTakeIsNotReleased(unittest.TestCase):
    """The bounded wait must not become a licence to write unlocked AND free the
    holder's lock — that lets a third writer in under the very condition it tolerates."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.ws = Path(self._td.name) / "ws"
        (self.ws / "state").mkdir(parents=True)
        self.log = self.ws / "state" / "compactions.jsonl"
        self.lock = self.ws / "state" / "compactions.jsonl.lock"

    def tearDown(self):
        self._td.cleanup()

    def test_a_foreign_lock_survives_the_call(self):
        self.lock.mkdir()
        _run(self.ws, ("/tmp/t.jsonl", "precompact"))
        self.assertTrue(self.lock.is_dir(),
                        "removed a lock it never acquired — the holder is now unprotected")

    def test_a_give_up_parks_the_event_and_never_touches_the_log(self):
        """The trim REPLACES the pathname, so an unlocked append is discarded by
        the holder's mv. Park it instead — durable, and out of the race."""
        self.lock.mkdir()
        self.log.write_text('{"ts":"x","epoch":0,"trigger":"pre-existing"}\n')
        _run(self.ws, ("/tmp/t.jsonl", "held-lock"))
        # The sidecar PATHNAME is per-writer now (a shared one loses records: an fd
        # already open on it follows the inode through the holder's mv). Assert the
        # behaviour — parked somewhere, exactly one record — not the old filename.
        parked = sorted((self.ws / "state").glob("compactions.jsonl.pending.*"))
        self.assertTrue(parked, "the event was not parked anywhere")
        self.assertEqual(len(parked), 1, f"expected one sidecar, got {parked}")
        self.assertEqual(json.loads(parked[0].read_text().strip())["trigger"], "held-lock")
        self.assertFalse((self.ws / "state" / "compactions.jsonl.wip").exists(),
                         "left an unpublished .wip file behind")
        self.assertEqual(self.log.read_text().count("held-lock"), 0,
                         "wrote the main log without the lock — the mv would drop it")

    def test_each_give_up_writer_gets_its_own_sidecar(self):
        """The invariant the shared pathname violated. Two give-up writers must
        park in TWO files: a shared name is unsafe because an fd already opened on
        it follows the inode through the holder's mv, so the record lands in a file
        the holder has already drained and unlinked. Pre-fix this yields 1."""
        self.lock.mkdir()
        _run(self.ws, ("/tmp/a.jsonl", "give-up-A"))
        _run(self.ws, ("/tmp/b.jsonl", "give-up-B"))
        parked = sorted((self.ws / "state").glob("compactions.jsonl.pending.*"))
        self.assertEqual(len(parked), 2,
                         f"two give-up writers must not share a pathname; got {parked}")
        triggers = {json.loads(f.read_text().strip())["trigger"] for f in parked}
        self.assertEqual(triggers, {"give-up-A", "give-up-B"}, "a parked record was lost")

    def test_a_locked_writer_absorbs_every_sidecar(self):
        """Per-writer sidecars are only safe if the drain collects ALL of them."""
        (self.ws / "state" / "compactions.jsonl.pending.aaa").write_text(
            '{"ts":"x","epoch":0,"trigger":"parked-A"}\n')
        (self.ws / "state" / "compactions.jsonl.pending.bbb").write_text(
            '{"ts":"x","epoch":0,"trigger":"parked-B"}\n')
        _run(self.ws, ("/tmp/t.jsonl", "now-locked"))
        body = self.log.read_text()
        for want in ("parked-A", "parked-B", "now-locked"):
            self.assertIn(want, body, f"{want} was not absorbed")
        self.assertFalse(sorted((self.ws / "state").glob("compactions.jsonl.pending.*")),
                         "sidecars survived the drain and will be re-absorbed")

    def test_the_next_locked_writer_folds_the_sidecar_in(self):
        """Parking is only safe if something absorbs it."""
        pending = self.ws / "state" / "compactions.jsonl.pending"
        pending.write_text('{"ts":"x","epoch":0,"trigger":"parked-earlier"}\n')
        _run(self.ws, ("/tmp/t.jsonl", "now-locked"))
        body = self.log.read_text()
        self.assertIn("parked-earlier", body, "the parked event was never absorbed")
        self.assertIn("now-locked", body)
        self.assertFalse(pending.exists(), "sidecar left behind; it would be folded twice")

    def test_the_trim_is_SKIPPED_while_another_writer_holds_the_lock(self):
        """The trim is the read-modify-write. Running it unlocked is the data loss
        the lock exists to prevent, so it must not run on the give-up path."""
        self.lock.mkdir()
        self.log.write_text('{"ts":"x","epoch":0,"trigger":"seed"}\n' * 600)
        _run(self.ws, ("/tmp/t.jsonl", "no-trim"))
        lines = self.log.read_text().strip().splitlines()
        self.assertEqual(len(lines), 600,
                         "the give-up path must neither trim nor append to the main log")

    def test_an_acquired_lock_is_still_released(self):
        _run(self.ws, ("/tmp/t.jsonl", "precompact"))
        self.assertFalse(self.lock.exists(), "leaked a lock it did acquire")


if __name__ == "__main__":
    unittest.main(verbosity=2)
