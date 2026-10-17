#!/usr/bin/env python3
"""The channel-bridge wrapper's restart alert reaches results/ whole (#3956).

A drain claims `results/proactive-*.txt` on sight. A shell `>` opens the name and
fills it afterwards, so the drain can read the empty or partial file it creates.
The wrapper publishes through `src/result_publish.py`, which renames a finished
file onto the name instead.

The probe: the alert's name is made to exist already as a hard link to a canary.
Filling the name in place writes the canary's inode; a rename only replaces the
directory entry, so the canary stays empty. `date` is pinned so the name is known.

Run: python3 tests/channel-bridge-restart-alert-publishes-whole.test.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
NOW = "1700000000"


def _exe(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(0o755)


class RestartAlertPublishesWhole(unittest.TestCase):
    def test_alert_replaces_the_name_instead_of_filling_it(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, ws, shims = root / "repo", root / "ws", root / "shims"
            for d in (repo / "src" / "launchd", repo / "scripts", ws / "state" / "channel-bridge-supervisor",
                      ws / "results", shims):
                d.mkdir(parents=True)
            shutil.copy2(REPO / "src" / "launchd" / "channel-bridge-wrapper.sh", repo / "src" / "launchd")
            shutil.copy2(REPO / "src" / "result_publish.py", repo / "src")
            (repo / "src" / "slack-bridge.py").write_text("# dummy\n")
            _exe(repo / "scripts" / "sutando-config.sh",
                 f"#!/bin/bash\nif [ \"$1\" = workspace ]; then echo '{ws}'; else echo '{root}/none.env'; fi\n")
            # The bridge interpreter: the stub child exits at once; the publisher runs for real.
            _exe(root / "python", "#!/bin/bash\ncase \"$1\" in *result_publish.py) exec "
                 f"'{sys.executable}' \"$@\";; esac\nsleep 0.2; exit 1\n")
            _exe(shims / "osascript", "#!/bin/bash\nexit 0\n")
            _exe(shims / "date", f"#!/bin/bash\n[ \"$1\" = +%s ] && {{ echo {NOW}; exit 0; }}\nexec /bin/date \"$@\"\n")
            (ws / "state" / "channel-bridge-supervisor" / "slack.started").write_text("1\n")

            alert = ws / "results" / f"proactive-slack-bridge-restarted-{NOW}.txt"
            canary = root / "canary"
            canary.write_text("")
            os.link(canary, alert)

            env = {k: v for k, v in os.environ.items() if not k.startswith(("SUTANDO_", "AG2_", "REMOTE_"))}
            env.update(PATH=f"{shims}:{env.get('PATH', '')}", HOME=str(root), SLACK_BOT_TOKEN="x-test",
                       SUTANDO_CHANNEL_BRIDGE_PYTHON=str(root / "python"),
                       SUTANDO_CHANNEL_BRIDGE_RESTART_DELAY="5")
            proc = subprocess.Popen(["bash", str(repo / "src" / "launchd" / "channel-bridge-wrapper.sh"), "slack"],
                                    env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                deadline = time.time() + 10
                while time.time() < deadline and alert.stat().st_ino == canary.stat().st_ino \
                        and not canary.read_text():
                    time.sleep(0.05)
            finally:
                proc.terminate()
                _, err = proc.communicate(timeout=10)

            self.assertIn("automatically restarted", alert.read_text(), err)
            self.assertEqual(canary.read_text(), "", "the alert was written into the existing name in place")
            self.assertNotEqual(alert.stat().st_ino, canary.stat().st_ino)
            self.assertEqual(sorted(p.name for p in (ws / "results").iterdir()), [alert.name],
                             "a staged file was left behind in results/")


if __name__ == "__main__":
    unittest.main(verbosity=2)
