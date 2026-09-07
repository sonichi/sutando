"""`--media` is validated before a browser exists, and the attach waits for X.

Two properties, both learned the same evening:

  * A bad `--media` must be refused BEFORE `launchPersistentContext`. Every
    browser command here opens the ONE persistent profile, and opening it evicts
    whoever holds it — a `check` run twice closed an owner's live login window.
    So an argument error must never cost a browser launch.
  * The upload must be awaited. `setInputFiles` returns once the file is handed
    over, not once X has it; posting between those two points publishes the text
    with no image and reports success.

No Playwright, no network: argv behaviour by subprocess, the wait by source.
"""
import pathlib
import subprocess
import sys
import tempfile
import unittest

SCRIPT = (pathlib.Path(__file__).resolve().parents[1]
          / "skills" / "x-twitter" / "x-post-browser.mjs")
SRC = SCRIPT.read_text()

# The argv guards sit behind `import playwright`, so a checkout without
# node_modules skips them loudly instead of failing on ERR_MODULE_NOT_FOUND.
_PROBE = subprocess.run(["node", "-e", "import('playwright').then(()=>0,()=>process.exit(9))"],
                        capture_output=True, text=True, check=False)
NO_PLAYWRIGHT = _PROBE.returncode != 0


def run(*args, timeout=20):
    return subprocess.run(["node", str(SCRIPT), *args],
                          capture_output=True, text=True, timeout=timeout,
                          check=False)


@unittest.skipIf(NO_PLAYWRIGHT,
    "playwright unresolvable: the argv guards sit behind its import, so these "
    "arms cannot run here. NOT a pass — the source arms below still apply.")
class ArgvGuards(unittest.TestCase):
    def test_a_missing_media_file_exits_2_without_a_browser(self):
        r = run("post", "text", "--media", "/no/such/file.png")
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("no such file", r.stderr)

    def test_bare_media_flag_exits_2(self):
        r = run("post", "text", "--media")
        self.assertEqual(r.returncode, 2, r.stderr)

    def test_media_flag_swallowing_the_next_flag_is_refused(self):
        """`--media --dry-run` must not read `--dry-run` as a path. The MESSAGE
        is the discriminator: without the `startsWith('--')` guard this still
        exits 2, but as "no such file: --dry-run" — a path error for something
        that was never a path."""
        r = run("post", "text", "--media", "--dry-run")
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("needs a file path", r.stderr)
        self.assertNotIn("no such file", r.stderr)

    def test_the_guard_runs_before_any_launch(self):
        """The control for all three: the refusal must be FAST, because a
        browser launch is what it exists to avoid. A launch takes seconds."""
        import time
        t0 = time.monotonic()
        run("post", "text", "--media", "/no/such/file.png")
        self.assertLess(time.monotonic() - t0, 5.0)

    def test_usage_names_the_flag(self):
        r = run()
        self.assertIn("--media", r.stderr)


class AttachWaitsForX(unittest.TestCase):
    def test_the_media_guard_precedes_the_first_browser_launch(self):
        """Source-level, so it runs with no node_modules: whatever the import
        order costs, the guard must still come before any persistent-context
        launch — that launch is what evicts a live login window."""
        self.assertLess(SRC.index("--media: no such file"),
                        SRC.index("launchPersistentContext"))

    def test_it_targets_the_HIDDEN_input_not_the_button(self):
        self.assertIn('input[type="file"]', SRC)
        self.assertIn("setInputFiles", SRC)

    def test_it_waits_for_the_upload_to_land(self):
        """removeMedia appears per file once X has it; without this wait the
        post can publish before the image attaches. Match the SELECTOR CALL,
        not the bare word — the word also occurs in the comment above it, so a
        substring check passed even with the wait deleted."""
        import re
        call = re.search(r'waitForSelector\(\s*\'\[data-testid="removeMedia"\]\'', SRC)
        self.assertIsNotNone(call, "no waitForSelector on removeMedia")
        self.assertLess(SRC.index("setInputFiles"), call.start(),
                        "the wait must follow setInputFiles")


if __name__ == "__main__":
    unittest.main()
