#!/usr/bin/env python3
"""skills/agent-room-ops/post_command.py: prints one shell command that posts a reply file as
op:message, with only strictly valid ids inside its quotes; an invalid argument exits 2 and
prints nothing; the token is referenced by variable name and never read. The command is run
once against a fake curl to show the body is posted verbatim as JSON."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SKILL = Path(__file__).resolve().parents[3] / "skills" / "agent-room-ops"
sys.path.insert(0, str(SKILL))
import post_command as pc  # noqa: E402

GATEWAY = "https://gateway.example"
BASE = ["--gateway-url", GATEWAY, "--room", "!room:ag2.space", "--dedupe-key", "codex-cloud:task-1"]
FAKE_CURL = """#!%(python)s
import json, sys
json.dump({"argv": sys.argv[1:], "stdin": sys.stdin.read()}, open(%(out)r, "w"))
"""


def run_main(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = pc.main(argv)
    return rc, out.getvalue(), err.getvalue()


class PostCommandTest(unittest.TestCase):
    def test_prints_one_line_with_all_ids(self):
        with mock.patch.dict(os.environ, {"AG2_RELAY_TOKEN": "sekrit-relay-value"}):
            rc, out, err = run_main(BASE + ["--thread-root", "$thread1", "--reply-to", "$ask1",
                                            "--gateway-url", GATEWAY + "/"])
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(out.count("\n"), 1)
        self.assertEqual(out.strip(), (
            "python3 -c 'import json, sys; print(json.dumps({\"op\": \"message\", \"room_id\": \"!room:ag2.space\", "
            "\"thread_root\": \"$thread1\", \"reply_to\": \"$ask1\", \"dedupe_key\": \"codex-cloud:task-1\", "
            "\"body\": sys.stdin.read()}))' < /tmp/ag2-reply.md | curl -sS --fail-with-body -X POST "
            "\"https://gateway.example/v1/room\" -H \"Authorization: Bearer $AG2_RELAY_TOKEN\" "
            "-H \"Content-Type: application/json\" --data-binary @-"))
        self.assertNotIn("sekrit", out)

    def test_optional_ids_are_left_out_and_names_are_configurable(self):
        rc, out, _ = run_main(BASE + ["--token-env", "MY_TOKEN", "--reply-file", "/work/answer.txt"])
        self.assertEqual(rc, 0)
        self.assertNotIn("thread_root", out)
        self.assertNotIn("reply_to", out)
        self.assertIn("$MY_TOKEN", out)
        self.assertIn("< /work/answer.txt |", out)

    def test_any_invalid_argument_exits_2_and_prints_nothing(self):
        bad = [["--room", ""], ["--room", "!r:x'"], ["--room", "#alias:x"], ["--room", "!r x:y"],
               ["--thread-root", "$a' ; rm -rf ~"], ["--thread-root", "nope"], ["--reply-to", "$a b"],
               ["--gateway-url", "http://gateway.example"], ["--gateway-url", "https://g.example/a b"],
               ["--token-env", "bad-name"], ["--token-env", "X;Y"], ["--dedupe-key", "a b"],
               ["--reply-file", "relative.md"], ["--reply-file", "/tmp/a b"]]
        for extra in bad:
            rc, out, err = run_main(BASE + extra)
            self.assertEqual((rc, out), (2, ""), extra)
            self.assertIn("is not a valid value", err)

    def test_the_command_posts_the_reply_file_verbatim_as_json(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "curl.json"
            bindir = Path(d) / "bin"
            bindir.mkdir()
            (bindir / "curl").write_text(FAKE_CURL % {"python": sys.executable, "out": str(out)})
            (bindir / "curl").chmod(0o755)
            reply = Path(d) / "reply.md"
            answer = 'It\'s "done" -- $HOME stays literal\n\\ and a second line\n'
            reply.write_text(answer)
            cmd = pc.build(GATEWAY, "!r1:ag2.space", "AG2_RELAY_TOKEN", "codex-cloud:task9", str(reply))
            subprocess.run(["/bin/sh", "-c", cmd], check=True,
                           env={"PATH": "%s:/usr/bin:/bin" % bindir, "AG2_RELAY_TOKEN": "tok-123"})
            got = json.loads(out.read_text())
            self.assertEqual(json.loads(got["stdin"]), {"op": "message", "room_id": "!r1:ag2.space",
                                                        "dedupe_key": "codex-cloud:task9", "body": answer})
            self.assertIn("Authorization: Bearer tok-123", got["argv"])
            self.assertIn(GATEWAY + "/v1/room", got["argv"])

    def test_runs_as_a_script(self):
        r = subprocess.run([sys.executable, str(SKILL / "post_command.py")] + BASE,
                           capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.stdout.startswith("python3 -c "))


if __name__ == "__main__":
    unittest.main()
