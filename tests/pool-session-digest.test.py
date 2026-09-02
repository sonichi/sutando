#!/usr/bin/env python3
"""Contract for scripts/pool-session-digest.py.

The age column is the load-bearing part: an operator reads it to decide whether
a session is wedged. Transcripts stamp UTC, so reading them with a local-time
parser reports an age that is wrong by the UTC offset -- and correcting with
time.timezone rather than the DST-aware offset lands exactly one hour out, which
is precisely the shape of "stale" an operator is looking for. The first version
of this script shipped that bug and reported 3-second-old activity as 60m.
"""
import calendar
import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import unittest.mock
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "pool-session-digest.py"

_spec = importlib.util.spec_from_file_location("digest_mod", SCRIPT)
digest_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(digest_mod)


# A fixed instant inside US daylight saving. The bug this suite pins is a
# local-time parse, which is invisible whenever the runner's TZ offset is zero.
FROZEN = calendar.timegm((2026, 7, 1, 12, 0, 0, 0, 0, 0))
DST_TZ = "America/Los_Angeles"


def utc_iso(offset_secs: int = 0) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z",
                         time.gmtime(FROZEN - offset_secs))


class AgeTest(unittest.TestCase):
    def setUp(self):
        # Ambient TZ decided whether the regression was detectable at all: the
        # mutant passed the whole suite under TZ=UTC. Pin the zone and the clock.
        prior = os.environ.get("TZ")
        os.environ["TZ"] = DST_TZ
        time.tzset()

        def restore():
            if prior is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = prior
            time.tzset()

        self.addCleanup(restore)
        patcher = unittest.mock.patch.object(time, "time", lambda: FROZEN)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_pinned_zone_actually_has_a_nonzero_dst_offset(self):
        # Positive control: if the zone resolved to UTC the cases below would
        # pass against the very bug they exist to catch.
        self.assertNotEqual(time.localtime(FROZEN).tm_gmtoff, 0)
        self.assertTrue(time.localtime(FROZEN).tm_isdst)

    def test_recent_utc_stamp_reads_as_seconds_not_hours(self):
        """A local-time parse of a UTC stamp is off by the whole UTC offset."""
        self.assertTrue(digest_mod.age(utc_iso(3)).endswith("s ago"),
                        "a 3s-old event must not read as minutes or hours")

    def test_age_scales(self):
        self.assertTrue(digest_mod.age(utc_iso(600)).endswith("m ago"))
        self.assertTrue(digest_mod.age(utc_iso(3 * 3600)).endswith("h ago"))

    def test_future_stamp_is_skew_not_freshness(self):
        """A future stamp must not be indistinguishable from a live core.

        This previously asserted "0s ago" — pinning the clamp. The age column
        IS the wedge signal, so rendering skew as fresh hides the thing it
        exists to show.
        """
        self.assertEqual(digest_mod.age(utc_iso(-120)), "clock skew")
        self.assertNotEqual(digest_mod.age(utc_iso(-120)),
                            digest_mod.age(utc_iso(0)))
        # Sub-second jitter is not skew; the tolerance must survive.
        self.assertEqual(digest_mod.age(utc_iso(-1)), "0s ago")

    def test_unparsable_stamp_is_reported_not_raised(self):
        self.assertEqual(digest_mod.age("not-a-timestamp"), "?")
        self.assertEqual(digest_mod.age(""), "?")


class DigestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "s.jsonl"

    def _write(self, records):
        self.path.write_text("\n".join(json.dumps(r) for r in records) + "\n")

    def _rec(self, blocks):
        return {"type": "assistant", "timestamp": utc_iso(1),
                "message": {"content": blocks}}

    # `rendered` is what the block degrades to; None means dropped is fine.
    # Isolation alone would pass a before/after check with every guard reverted.
    MALFORMED_LEAVES = {
        "tool_input_is_a_string": ({"type": "tool_use", "name": "Bash",
                                    "input": "not-a-dict"}, "not-a-dict"),
        "text_is_an_object": ({"type": "text", "text": {"a": 1}}, None),
        "thinking_is_an_array": ({"type": "thinking",
                                  "thinking": ["a", "b"]}, None),
        "tool_name_is_an_int": ({"type": "tool_use", "name": 42,
                                 "input": {"command": "ls"}}, "ls"),
        "tool_input_value_nested": ({"type": "tool_use", "name": "Bash",
                                     "input": {"command": {"deep": 1}}},
                                    "{'deep': 1}"),
    }

    def test_a_malformed_leaf_does_not_hide_later_events(self):
        for name, (bad, rendered) in self.MALFORMED_LEAVES.items():
            with self.subTest(name):
                self._write([self._rec([{"type": "text", "text": "before"}]),
                             self._rec([bad]),
                             self._rec([{"type": "text", "text": "after"}])])
                out = digest_mod.digest(self.path, keep=10, width=80,
                                        want_thinking=True)
                bodies = [e[2] for e in out["tail"]]
                self.assertIn("after", bodies,
                              f"{name}: the malformed record hid every later event")
                self.assertIn("before", bodies, name)
                if rendered is not None:
                    self.assertIn(rendered, bodies,
                                  f"{name}: degraded to dropped, not rendered")

    def test_a_non_string_timestamp_does_not_hide_later_events(self):
        """`(rec.get("timestamp") or "")[11:19]` subscripts an int and raises;
        `or ""` does not catch it, because a non-zero int is truthy."""
        self._write([self._rec([{"type": "text", "text": "before"}]),
                     {"type": "assistant", "timestamp": 1234567890,
                      "message": {"content": [{"type": "text", "text": "mid"}]}},
                     self._rec([{"type": "text", "text": "after"}])])
        out = digest_mod.digest(self.path, keep=10, width=80, want_thinking=True)
        bodies = [e[2] for e in out["tail"]]
        self.assertEqual(bodies, ["before", "mid", "after"])

    def test_counts_blocks_and_tolerates_junk_lines(self):
        self.path.write_text(
            json.dumps(self._rec([{"type": "text", "text": "hello"}])) + "\n"
            + "{ not json\n\n"
            + json.dumps(self._rec([{"type": "thinking", "thinking": "hmm"}])) + "\n")
        d = digest_mod.digest(self.path, keep=10, width=80, want_thinking=True)
        self.assertEqual(d["records"], 2, "a malformed line must not abort the scan")
        self.assertEqual(d["blocks"]["text"], 1)
        self.assertEqual(d["blocks"]["thinking"], 1)

    def test_thinking_is_counted_but_hidden_unless_requested(self):
        self._write([self._rec([{"type": "thinking", "thinking": "private"}])])
        d = digest_mod.digest(self.path, keep=10, width=80, want_thinking=False)
        self.assertEqual(d["blocks"]["thinking"], 1, "still counted")
        self.assertEqual(d["tail"], [], "reasoning must not leak without --thinking")

    def test_tail_is_bounded_to_keep(self):
        self._write([self._rec([{"type": "text", "text": f"m{i}"}]) for i in range(50)])
        d = digest_mod.digest(self.path, keep=5, width=80, want_thinking=False)
        self.assertEqual(len(d["tail"]), 5, "tail must stay bounded on huge transcripts")
        self.assertEqual(d["tail"][-1][2], "m49", "must keep the NEWEST events")

    def test_tool_use_summarises_the_command(self):
        self._write([self._rec([{"type": "tool_use", "name": "Bash",
                                 "input": {"command": "git status"}}])])
        d = digest_mod.digest(self.path, keep=5, width=80, want_thinking=False)
        _, kind, body = d["tail"][0]
        self.assertEqual(kind, "BASH")
        self.assertEqual(body, "git status")

    def test_long_summary_is_truncated_to_width(self):
        self._write([self._rec([{"type": "text", "text": "x" * 500}])])
        d = digest_mod.digest(self.path, keep=5, width=40, want_thinking=False)
        self.assertLessEqual(len(d["tail"][0][2]), 41)

    def test_tool_use_without_a_known_field_falls_back_to_its_input(self):
        """Not every tool names its argument command/file_path/pattern; those
        must still show something rather than an empty line."""
        self._write([self._rec([{"type": "tool_use", "name": "Odd",
                                 "input": {"unexpected": "value"}}])])
        d = digest_mod.digest(self.path, keep=5, width=80, want_thinking=False)
        self.assertIn("unexpected", d["tail"][0][2])

    def test_unknown_block_types_are_counted_but_not_shown(self):
        self._write([self._rec([{"type": "image", "source": "x"}])])
        d = digest_mod.digest(self.path, keep=5, width=80, want_thinking=False)
        self.assertEqual(d["blocks"]["image"], 1)
        self.assertEqual(d["tail"], [], "an unrenderable block must not emit a blank row")

    def test_malformed_records_are_skipped_without_raising(self):
        """Content that is not a list, blocks that are not dicts, and records
        with no timestamp all appear in real transcripts."""
        self._write([
            {"type": "user", "message": {"content": "a bare string"}},
            {"type": "assistant", "message": {"content": ["not-a-dict"]}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "kept"}]}},
        ])
        d = digest_mod.digest(self.path, keep=5, width=80, want_thinking=False)
        self.assertEqual(d["records"], 3)
        self.assertEqual([t[2] for t in d["tail"]], ["kept"])
        self.assertEqual(d["last_ts"], "", "no record carried a timestamp")


class DiscoveryTest(unittest.TestCase):
    """The I/O layer. Every failure path here returns empty rather than raising,
    which is right for an ops script but means a broken CLI looks identical to
    an idle machine -- so each path is pinned deliberately."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = Path(self.tmp.name)

    def test_config_dir_delegates_to_the_shared_claude_home_resolver(self):
        """No private copy of the resolution rule — including $CLAUDE_HOME.

        The script used to hand-roll $CLAUDE_CONFIG_DIR or ~/.claude, which
        silently ignored the $CLAUDE_HOME override the shared contract honours.
        """
        with unittest.mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.cfg)}):
            self.assertEqual(digest_mod.config_dir(), self.cfg)
        # The override the private copy could not see.
        alt = self.cfg / "alt-home"
        alt.mkdir()
        with unittest.mock.patch.dict(os.environ, {"CLAUDE_HOME": str(alt)}, clear=True):
            self.assertEqual(digest_mod.config_dir(), alt)
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(digest_mod.config_dir(), Path.home() / ".claude")

    def _run_result(self, rc=0, stdout="[]"):
        return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr="")

    def test_live_sessions_parses_success(self):
        payload = '[{"name": "core-1", "sessionId": "abc", "status": "busy", "pid": 1}]'
        with unittest.mock.patch.object(subprocess, "run",
                                        return_value=self._run_result(stdout=payload)):
            sessions, reason = digest_mod.live_sessions()
            self.assertIsNone(reason)
            self.assertEqual(sessions[0]["name"], "core-1")

    def test_every_discovery_failure_is_distinguishable(self):
        """These all used to return [] — so "none running" and "the CLI is
        gone/hung/unauthenticated" rendered identically on a liveness tool."""
        run_cases = {
            "empty":     self._run_result(stdout="[]"),
            "auth":      self._run_result(rc=2, stdout=""),
            "not json":  self._run_result(stdout="<html>502</html>"),
            "dict":      self._run_result(stdout='{"sessions": []}'),
            "non-dicts": self._run_result(stdout='["core-1"]'),
        }
        raise_cases = {
            "missing":   FileNotFoundError(2, "No such file"),
            "denied":    PermissionError(13, "Permission denied"),
            "timeout":   subprocess.TimeoutExpired("claude", 30),
        }
        reasons = {}
        for label, result in run_cases.items():
            with unittest.mock.patch.object(subprocess, "run", return_value=result):
                sessions, reasons[label] = digest_mod.live_sessions()
                self.assertEqual(sessions, [], label)
        for label, exc in raise_cases.items():
            with unittest.mock.patch.object(subprocess, "run", side_effect=exc):
                sessions, reasons[label] = digest_mod.live_sessions()
                self.assertEqual(sessions, [], f"{label} must not propagate")

        self.assertIsNone(reasons["empty"],
                          "a CLEAN answer of zero sessions is not a failure")
        failures = {k: v for k, v in reasons.items() if k != "empty"}
        self.assertTrue(all(failures.values()), f"a failure carried no reason: {failures}")
        self.assertEqual(len(set(failures.values())), len(failures),
                         f"two failure modes share a message: {failures}")

    def test_wrong_shape_does_not_reach_the_caller(self):
        """{"sessions": []} parses, then raises AttributeError on the first .get()."""
        with unittest.mock.patch.object(subprocess, "run",
                                        return_value=self._run_result(stdout='{"sessions": []}')):
            sessions, reason = digest_mod.live_sessions()
        self.assertEqual(sessions, [])
        self.assertIn("list", reason)

    def test_find_transcript_locates_by_session_id_else_none(self):
        proj = self.cfg / "projects" / "-some-slug"
        proj.mkdir(parents=True)
        (proj / "sid-1.jsonl").write_text("")
        with unittest.mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.cfg)}):
            self.assertEqual(digest_mod.find_transcript("sid-1").name, "sid-1.jsonl")
            self.assertIsNone(digest_mod.find_transcript("absent"))


class MainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = Path(self.tmp.name)
        self.proj = self.cfg / "projects" / "-slug"
        self.proj.mkdir(parents=True)

    def _session(self, name="core-1", sid="sid-1"):
        return {"name": name, "sessionId": sid, "status": "busy", "pid": 42}

    def _transcript(self, sid="sid-1"):
        rec = {"type": "assistant", "timestamp": utc_iso(1),
               "message": {"content": [{"type": "text", "text": "did a thing"}]}}
        (self.proj / f"{sid}.jsonl").write_text(json.dumps(rec) + "\n")

    def _main(self, argv, sessions, reason=None):
        out = io.StringIO()
        err = io.StringIO()
        with unittest.mock.patch.object(sys, "argv", ["prog", *argv]), \
             unittest.mock.patch.object(digest_mod, "live_sessions",
                                        return_value=(sessions, reason)), \
             unittest.mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.cfg)}), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = digest_mod.main()
        return rc, out.getvalue(), err.getvalue()

    def test_lone_surrogate_in_a_session_does_not_hide_the_next_one(self):
        """A JSON-decoded lone surrogate reaches print() through _safe; on a real
        UTF-8 stdout that raised and ended the sweep before the clean session."""
        self._transcript("sid-clean")
        bad = json.loads('{"name": "core-x", "sessionId": "\\ud800", "status": "busy", "pid": 1}')
        raw = io.BytesIO()
        with unittest.mock.patch.object(sys, "argv", ["prog"]), \
             unittest.mock.patch.object(digest_mod, "live_sessions",
                                        return_value=([bad, self._session(sid="sid-clean")], None)), \
             unittest.mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.cfg)}), \
             contextlib.redirect_stderr(io.StringIO()):
            wrapped = io.TextIOWrapper(raw, encoding="utf-8", write_through=True)
            with contextlib.redirect_stdout(wrapped):
                rc = digest_mod.main()
                wrapped.flush()
                out = raw.getvalue().decode("utf-8")
        self.assertEqual(rc, 0, out)
        self.assertIn("core-x", out)
        self.assertIn("\ufffd", out)
        self.assertIn("core-1", out)
        self.assertIn("did a thing", out)

    def test_reports_a_session_and_its_last_event(self):
        self._transcript()
        rc, out, _ = self._main([], [self._session()])
        self.assertEqual(rc, 0)
        self.assertIn("core-1", out)
        self.assertIn("did a thing", out)

    def test_no_live_sessions_is_a_nonzero_exit(self):
        rc, _, err = self._main([], [])
        self.assertEqual(rc, 1)
        self.assertIn("no live sessions", err)

    def test_broken_discovery_exits_differently_from_an_empty_pool(self):
        rc_broken, _, err_broken = self._main([], [], reason="`claude` is not on PATH")
        rc_empty, _, err_empty = self._main([], [])
        self.assertEqual(rc_broken, 2, "a broken CLI must not look like an idle pool")
        self.assertNotEqual(rc_broken, rc_empty)
        self.assertIn("not on PATH", err_broken)
        self.assertNotEqual(err_broken, err_empty)

    def test_session_filter_matches_and_misses(self):
        self._transcript()
        rc, out, _ = self._main(["-s", "CORE-1"], [self._session()])
        self.assertEqual(rc, 0, "filter must be case-insensitive")
        self.assertIn("core-1", out)
        rc, _, err = self._main(["-s", "nope"], [self._session()])
        self.assertEqual(rc, 1)
        self.assertIn("no session matching", err)

    def test_missing_transcript_is_reported_not_fatal(self):
        rc, out, _ = self._main([], [self._session(sid="absent")])
        self.assertEqual(rc, 0, "one session without a transcript must not abort the rest")
        self.assertIn("no transcript", out)


class TerminalSafety(unittest.TestCase):
    """This digest prints to a TTY, so transcript content must not carry controls."""

    def test_osc52_clipboard_escape_is_neutralized(self):
        out = digest_mod._one_line("safe\x1b]52;c;YXR0YWNr\x07text", 200)
        self.assertNotIn("\x1b", out)
        self.assertNotIn("\x07", out)
        self.assertIn("safe", out)

    def test_csi_colour_sequence_is_neutralized(self):
        self.assertNotIn("\x1b", digest_mod._one_line("a\x1b[31mred\x1b[0m", 50))

    def test_c1_and_del_are_neutralized(self):
        for ch in ("\x7f", "\x9b", "\x00"):
            with self.subTest(ch=ch):
                self.assertNotIn(ch, digest_mod._one_line(f"x{ch}y", 50))

    def test_ordinary_text_is_untouched(self):
        """Positive control: the guard must not mangle normal content."""
        self.assertEqual(digest_mod._one_line("hello  world", 50), "hello world")

    def test_tool_name_is_sanitized_too(self):
        ev = digest_mod._event({"timestamp": "2026-08-23T00:00:00Z"},
                        {"type": "tool_use", "name": "a\x1bb",
                         "input": {"command": "ls"}}, 50)
        self.assertNotIn("\x1b", ev[1])


ATTACK_SID = "abc\x1b]52;c;YXR0YWNr\x07def"


class UntrustedSessionIdTest(unittest.TestCase):
    """sessionId is metadata, not a name we control: it reaches a TTY and a path."""

    def _main_with(self, session_id):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            cfg = td / "cfg"
            (cfg / "projects" / "p1").mkdir(parents=True)
            (cfg / "projects" / "p1" / "real-session.jsonl").write_text(
                '{"timestamp":"2026-08-24T10:00:00.000Z"}\n')
            payload = td / "payload.json"
            payload.write_text(json.dumps(
                [{"name": "n1", "status": "ok", "pid": 1,
                  "sessionId": session_id}]))
            stub = td / "bin"
            stub.mkdir()
            (stub / "claude").write_text(f"#!/bin/sh\ncat {payload}\n")
            (stub / "claude").chmod(0o755)
            env = dict(os.environ, CLAUDE_CONFIG_DIR=str(cfg),
                       PATH=f"{stub}:{os.environ['PATH']}")
            r = subprocess.run([sys.executable, str(SCRIPT)],
                               capture_output=True, text=True, env=env)
            return r.stdout + r.stderr

    def test_no_transcript_path_neutralizes_terminal_controls(self):
        out = self._main_with(ATTACK_SID)
        self.assertNotIn("\x1b", out, "ESC reached the terminal")
        self.assertNotIn("\x07", out, "BEL reached the terminal (OSC 52 sets the clipboard)")
        self.assertIn("\ufffd", out,
                      "control: the escape was present and REPLACED, not merely absent")

    def test_session_id_is_matched_literally_never_as_a_pattern(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "cfg"
            (cfg / "projects" / "p1").mkdir(parents=True)
            real = cfg / "projects" / "p1" / "real-session.jsonl"
            real.write_text("{}\n")
            with unittest.mock.patch.dict(os.environ,
                                          {"CLAUDE_CONFIG_DIR": str(cfg)}):
                for bad in ("*", "real-*", "../../etc/passwd", ""):
                    self.assertIsNone(digest_mod.find_transcript(bad),
                                      f"{bad!r} selected a transcript")
                self.assertEqual(digest_mod.find_transcript("real-session"), real,
                                 "control: a legitimate id must still resolve")



class UntrustedInputTest(unittest.TestCase):
    """A transcript and the CLI's JSON are external artifacts. Every decoded
    level is untrusted, and one bad one must not hide the rest."""

    def _fresh(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "dg_fresh", Path(__file__).resolve().parent.parent
            / "scripts" / "pool-session-digest.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m

    def _discovery(self, payload):
        """`m.subprocess` is the SHARED module object — assigning to its .run
        mutates it for every test. Patch inside a scope instead."""
        m = self._fresh()

        class R:
            returncode, stdout, stderr = 0, payload, ""

        return m, unittest.mock.patch.object(
            m.subprocess, "run", lambda *a, **k: R())

    def test_a_malformed_record_does_not_suppress_a_later_session(self):
        m = self._fresh()
        with tempfile.TemporaryDirectory() as td:
            good = json.dumps({"timestamp": "2026-08-24T19:00:00Z",
                               "message": {"content": [{"type": "text",
                                                        "text": "hi"}]}})
            (Path(td) / "s1.jsonl").write_text("[]\n" + good + "\n")
            (Path(td) / "s2.jsonl").write_text(good + "\n")
            m.live_sessions = lambda: (
                [{"name": "one", "sessionId": "s1", "status": "r", "pid": 1},
                 {"name": "two", "sessionId": "s2", "status": "r", "pid": 2}], None)
            m.find_transcript = lambda sid: Path(td) / f"{sid}.jsonl"
            buf = io.StringIO()
            with unittest.mock.patch.object(sys, "argv", ["d"]), \
                 contextlib.redirect_stdout(buf):
                rc = m.main()
        self.assertEqual(rc, 0)
        self.assertIn("two", buf.getvalue(),
                      "a bad record in session one hid session two entirely")

    def test_an_unreadable_session_does_not_end_the_sweep(self):
        """Record validation cannot cover a session that fails at the FILE
        level (vanished, permissions, IO). That is what the wrapper is for."""
        m = self._fresh()
        with tempfile.TemporaryDirectory() as td:
            good = json.dumps({"timestamp": "2026-08-24T19:00:00Z",
                               "message": {"content": [{"type": "text",
                                                        "text": "hi"}]}})
            (Path(td) / "s2.jsonl").write_text(good + "\n")
            (Path(td) / "s1.jsonl").write_text(good + "\n")
            real = m.digest

            def boom(path, *a, **k):
                if path.name == "s1.jsonl":
                    raise OSError("Input/output error")
                return real(path, *a, **k)

            m.digest = boom
            m.live_sessions = lambda: (
                [{"name": "one", "sessionId": "s1", "status": "r", "pid": 1},
                 {"name": "two", "sessionId": "s2", "status": "r", "pid": 2}], None)
            m.find_transcript = lambda sid: Path(td) / f"{sid}.jsonl"
            buf = io.StringIO()
            with unittest.mock.patch.object(sys, "argv", ["d"]), \
                 contextlib.redirect_stdout(buf):
                rc = m.main()
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("unreadable transcript", out, "the failure was not reported")
        self.assertIn("two", out, "one unreadable session ended the sweep")

    def test_a_string_message_or_unhashable_type_is_survivable(self):
        m = self._fresh()
        with tempfile.TemporaryDirectory() as td:
            t = Path(td) / "t.jsonl"
            t.write_text('"just a string"\n'
                         '{"message": "not-an-object"}\n'
                         '{"message": {"content": [{"type": ["un", "hashable"]}]}}\n')
            d = m.digest(t, 5, 80, False)
        self.assertEqual(d["blocks"]["?"], 1, "unhashable type was not coerced")

    def test_null_member_is_rejected_rather_than_passed_through(self):
        # `next(..., None)` cannot express this: None IS the invalid member.
        m, patched = self._discovery("[null]")
        with patched:
            sessions, reason = m.live_sessions()
        self.assertEqual(sessions, [])
        self.assertIn("NoneType", reason)

    def test_non_string_identity_fields_are_rejected(self):
        for payload, kind in (('[{"name": "a", "sessionId": 5}]', "int"),
                              ('[{"name": null, "sessionId": "x"}]', "NoneType")):
            m, patched = self._discovery(payload)
            with patched:
                sessions, reason = m.live_sessions()
            self.assertEqual(sessions, [], payload)
            self.assertIn(kind, reason, payload)

    def test_a_rejected_discovery_exits_two_not_a_traceback(self):
        m, patched = self._discovery("[null]")
        buf = io.StringIO()
        with patched, unittest.mock.patch.object(sys, "argv", ["d"]), \
             contextlib.redirect_stderr(buf):
            rc = m.main()
        self.assertEqual(rc, 2, "malformed discovery must take the diagnostic path")
        self.assertIn("could not list sessions", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
