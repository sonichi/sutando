"""The attach happens and is AWAITED — asserted from behaviour, not source text.

The source arms in x-post-browser-media.test.py pass with the upload commented
out and with the `await` dropped, because both leave the tokens in place. Here
the real script runs against a stub `playwright` that records an ordered event
log; the stub resolves the attachments wait on a LATER tick, so code that does
not await it screenshots while the attachment is still absent.
"""
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "x-twitter" / "x-post-browser.mjs"
FIX = ROOT / "tests" / "fixtures"


def run(*args):
    """Run the real script with `playwright` resolved to the stub."""
    log = pathlib.Path(tempfile.mkdtemp()) / "events.log"
    env = dict(os.environ,
               XSTUB_LOG=str(log),
               XSTUB_URL=(FIX / "x-post-browser-stub.mjs").as_uri(),
               X_BROWSER_PROFILE=tempfile.mkdtemp())
    p = subprocess.run(["node", "--import", str(FIX / "x-post-browser-loader.mjs"),
                        str(SCRIPT), *args],
                       capture_output=True, text=True, timeout=60, env=env, check=False)
    events = log.read_text().splitlines() if log.exists() else []
    return p, events


class AttachIsPerformedAndAwaited(unittest.TestCase):
    def setUp(self):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(b"\x89PNG\r\n\x1a\n")
        self.img = f.name

    def tearDown(self):
        pathlib.Path(self.img).unlink(missing_ok=True)

    def test_the_upload_is_actually_performed(self):
        """Fails when `setInputFiles` is commented out — the source arms do not."""
        p, ev = run("post", "text", "--media", self.img, "--dry-run")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("setInputFiles", ev)

    def test_the_upload_precedes_the_wait(self):
        p, ev = run("post", "text", "--media", self.img, "--dry-run")
        self.assertLess(ev.index("setInputFiles"), ev.index("wait:attachments:start"))

    def test_the_wait_is_AWAITED_before_the_composer_is_accepted(self):
        """The discriminator for a dropped `await`: the stub flips its flag on a
        later tick, so an unawaited wait screenshots with the attach absent."""
        p, ev = run("post", "text", "--media", self.img, "--dry-run")
        shot = [e for e in ev if e.startswith("screenshot(")]
        self.assertEqual(shot, ["screenshot(attachmentsReady=true)"])

    def test_text_only_does_not_attach(self):
        """Control: without --media none of the above can pass for free."""
        p, ev = run("post", "text", "--dry-run")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn("setInputFiles", ev)
        self.assertNotIn("wait:attachments:start", ev)


if __name__ == "__main__":
    unittest.main()
