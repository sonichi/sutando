#!/usr/bin/env python3
"""src/correlation_log.py: the one writer for task-correlation records.

Schema and bounds per kind, the locked append under concurrent production writers, rotation,
never raising, and the git trailers. In process, so coverage sees it.

Run: python3 tests/correlation-log.test.py   (stdlib only)
"""
import contextlib
import io
import json
import multiprocessing
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import correlation_log as cl  # noqa: E402

GOOD = {"interaction_id": "ix_0123456789ab", "object": "ag2-space/fluffy#426", "counterpart": "@echo.agent:s.space",
        "counterpart_kind": "agent", "counterpart_scope": "internal", "room_id": "!r:s.space", "room_kind": "room",
        "task_id": "task-s-space~task-0123456789", "run_id": "task-0123456789~1", "channel": "room", "event_id": "$e1"}


def _append_many(ws, n):
    for _ in range(n):
        assert cl.record("interaction", dict(GOOD, interaction_id=cl.mint_interaction_id()), workspace=ws)


class Writer(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp()
        cl._warned = False

    def test_a_record_is_written_once_with_its_kind_and_time(self):
        rec = cl.record("interaction", GOOD, workspace=self.ws)
        self.assertEqual(rec["kind"], "interaction")
        self.assertRegex(rec["at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(cl.read("interaction", workspace=self.ws), [rec])
        self.assertEqual(cl.read("run", workspace=self.ws), [])
        self.assertEqual(oct(cl.log_path(self.ws).stat().st_mode & 0o777), "0o600")

    def test_minted_ids_are_well_formed_and_distinct(self):
        ids = {cl.mint_interaction_id() for _ in range(200)}
        self.assertEqual(len(ids), 200)
        self.assertTrue(all(cl.INTERACTION_RE.match(i) for i in ids))

    def test_the_schema_refuses_what_it_does_not_know(self):
        bad = [
            ("nope", GOOD, "unknown kind"),
            ("interaction", dict(GOOD, name="Chi"), "unknown fields"),
            ("interaction", {k: v for k, v in GOOD.items() if k != "object"}, "needs object"),
            ("interaction", dict(GOOD, interaction_id="ix_short"), "interaction_id is not a valid id"),
            ("interaction", dict(GOOD, task_id="has space"), "task_id is not a valid id"),
            ("interaction", dict(GOOD, task_id="@chi:s.space"), "task_id is not a valid id"),
            ("interaction", dict(GOOD, run_id="task-0123456789"), "run_id is not a valid id"),
            ("interaction", dict(GOOD, counterpart="Chi Wang"), "counterpart is not a valid id"),
            ("interaction", dict(GOOD, object="some prose here"), "object is not a valid id"),
            ("interaction", dict(GOOD, room_id="the dev room"), "room_id is not a valid id"),
            ("interaction", dict(GOOD, event_id="e1"), "event_id is not a valid id"),
            ("interaction", dict(GOOD, channel="email"), "channel must be"),
            ("interaction", dict(GOOD, counterpart=42), "counterpart must be a string"),
            ("interaction", None, "fields must be a dict"),
            (None, GOOD, "unknown kind"),
            ("interaction", dict(GOOD, counterpart="x" * 301), "too long"),
            ("interaction", dict(GOOD, object="two\nlines"), "spans lines"),
            ("interaction", dict(GOOD, counterpart_kind="robot"), "counterpart_kind must be"),
            ("interaction", dict(GOOD, room_kind="space"), "room_kind must be"),
        ]
        for kind, fields, why in bad:
            with self.assertRaises(ValueError, msg=why) as cm:
                cl.validate(kind, fields)
            self.assertIn(why, str(cm.exception))
        for task in ("task-s-space~task-182c2b5ce99cc9e8ae", "task-9f06cb8851db56858f",
                     "worker-pin-1792961761921-60fb041f", "task-local-hs~worker-pin-1790398604983-fada1e1c"):
            self.assertEqual(cl.validate("interaction", dict(GOOD, task_id=task))["task_id"], task)
        rec = cl.validate("interaction", dict(GOOD, run_id=None, event_id=""))
        self.assertNotIn("run_id", rec)
        self.assertNotIn("event_id", rec)

    def test_a_refused_or_failed_write_returns_none_and_says_so_once(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertIsNone(cl.record("interaction", dict(GOOD, interaction_id="bad"), workspace=self.ws))
            Path(self.ws, "state").mkdir()
            cl.log_path(self.ws).mkdir()          # a directory where the log should be: OSError
            self.assertIsNone(cl.record("interaction", GOOD, workspace=self.ws))
        self.assertEqual(err.getvalue().count("correlation_log: not recorded"), 1)

    def test_malformed_input_and_a_missing_resolver_never_raise(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            for kind, fields in ((None, GOOD), ("interaction", None), ("interaction", ["x"]),
                                 ("interaction", dict(GOOD, counterpart=object()))):
                self.assertIsNone(cl.record(kind, fields, workspace=self.ws))
            orig = cl.log_path
            cl.log_path = lambda workspace=None: (_ for _ in ()).throw(ImportError("no workspace_default"))
            try:
                self.assertIsNone(cl.record("interaction", GOOD))
            finally:
                cl.log_path = orig
        self.assertFalse(Path(cl.log_path(self.ws)).exists())

    def test_a_short_write_is_completed_not_reported_as_done(self):
        real, calls = os.write, []

        def eight_bytes_once(fd, data):
            calls.append(len(data))
            return real(fd, data[:8]) if len(calls) == 1 else real(fd, data)
        cl.os.write = eight_bytes_once
        try:
            self.assertIsNotNone(cl.record("interaction", GOOD, workspace=self.ws))
        finally:
            cl.os.write = real
        self.assertGreater(len(calls), 1)
        self.assertIsNotNone(cl.record("interaction", GOOD, workspace=self.ws))
        self.assertEqual(len(cl.read("interaction", workspace=self.ws)), 2)

    def test_a_failed_write_is_truncated_back_and_reported(self):
        self.assertIsNotNone(cl.record("interaction", GOOD, workspace=self.ws))
        size = cl.log_path(self.ws).stat().st_size
        real = os.write

        def partial_then_fail(fd, data, state={"n": 0}):
            state["n"] += 1
            if state["n"] == 1:
                return real(fd, data[:8])
            raise OSError("disk full")
        cl.os.write = partial_then_fail
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err):
                self.assertIsNone(cl.record("interaction", GOOD, workspace=self.ws))
        finally:
            cl.os.write = real
        self.assertEqual(cl.log_path(self.ws).stat().st_size, size)
        self.assertIsNotNone(cl.record("interaction", GOOD, workspace=self.ws))
        self.assertEqual(len(cl.read("interaction", workspace=self.ws)), 2)

    def test_a_partial_line_left_by_a_crash_does_not_swallow_the_next_record(self):
        self.assertIsNotNone(cl.record("interaction", GOOD, workspace=self.ws))
        with open(cl.log_path(self.ws), "ab") as f:
            f.write(b'{"kind":"interaction","at":"2026')
        self.assertIsNotNone(cl.record("interaction", GOOD, workspace=self.ws))
        self.assertEqual(len(cl.read("interaction", workspace=self.ws)), 2)

    def test_rotation_keeps_one_old_file_and_read_spans_both(self):
        old = cl.MAX_BYTES
        try:
            cl.MAX_BYTES = 1200
            for _ in range(8):
                cl.record("interaction", dict(GOOD, interaction_id=cl.mint_interaction_id()), workspace=self.ws)
        finally:
            cl.MAX_BYTES = old
        path = cl.log_path(self.ws)
        self.assertTrue(path.with_name(cl.LOG_NAME + ".1").exists())
        self.assertLessEqual(path.stat().st_size, 1200)
        recs = cl.read(workspace=self.ws)
        self.assertGreater(len(recs), 1)
        self.assertEqual(len({r["interaction_id"] for r in recs}), len(recs))

    def test_read_skips_lines_it_cannot_parse(self):
        path = cl.log_path(self.ws)
        path.parent.mkdir(parents=True)
        path.write_text('not json\n["a list"]\n' + json.dumps({"kind": "interaction", "object": "x"}) + "\n")
        self.assertEqual(cl.read(workspace=self.ws), [{"kind": "interaction", "object": "x"}])

    def test_concurrent_production_writers_lose_and_tear_nothing(self):
        procs = [multiprocessing.Process(target=_append_many, args=(self.ws, 25)) for _ in range(4)]
        for p in procs:
            p.start()
        for p in procs:
            p.join(30)
            self.assertEqual(p.exitcode, 0)
        lines = cl.log_path(self.ws).read_text().splitlines()
        self.assertEqual(len(lines), 100)
        self.assertEqual(len({json.loads(line)["interaction_id"] for line in lines}), 100)


class Trailers(unittest.TestCase):
    def test_trailers_render_only_the_ids_given_and_refuse_non_ids(self):
        self.assertEqual(cl.trailers(), "")
        self.assertEqual(cl.trailers("task-s-space~task-1a2b3c", "task-1a2b3c~2", "ix_0123456789ab"),
                         "Sutando-Task: task-s-space~task-1a2b3c\nSutando-Run: task-1a2b3c~2\n"
                         "Interaction-Id: ix_0123456789ab")
        self.assertEqual(cl.trailers(task_id=" task-1 "), "Sutando-Task: task-1")
        for bad in ({"task_id": "two words"}, {"interaction_id": "ix_x"}, {"task_id": "@chi:s.space"},
                    {"task_id": "!room:s.space"}, {"run_id": "task-1a2b3c"}, {"task_id": "Chi Wang"}):
            with self.assertRaises(ValueError):
                cl.trailers(**bad)

    def test_the_cli_mints_and_renders(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cl.main(["mint"]), 0)
            self.assertEqual(cl.main(["trailers", "--task", "task-1", "--interaction", "ix_0123456789ab"]), 0)
        lines = out.getvalue().splitlines()
        self.assertRegex(lines[0], r"^ix_[0-9a-f]{12}$")
        self.assertEqual(lines[1:], ["Sutando-Task: task-1", "Interaction-Id: ix_0123456789ab"])
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(cl.main(["trailers", "--task", "bad id"]), 2)
        self.assertIn("not an id", err.getvalue())

    def test_the_default_log_is_under_the_resolved_workspace(self):
        from unittest.mock import patch
        import workspace_default
        with patch.object(workspace_default, "resolve_workspace", return_value="/ws"):
            self.assertEqual(cl.log_path(), Path("/ws/state/correlation.jsonl"))


if __name__ == "__main__":
    unittest.main()
