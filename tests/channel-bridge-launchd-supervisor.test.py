#!/usr/bin/env python3
"""Regression coverage for crash recovery of Slack/Discord/Telegram bridges."""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import tempfile
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
WRAPPER = REPO / "src" / "launchd" / "channel-bridge-wrapper.sh"
PLIST = REPO / "src" / "launchd" / "com.sutando.channel-bridge.plist"
# Gateway supervision lives in start_gateway_lanes() (startup-runtime.sh) since
# #3147; channel-bridge supervision is still in startup.sh. Read both.
STARTUP = REPO / "src" / "startup.sh"
RUNTIME = REPO / "src" / "startup-runtime.sh"


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"ok  {message}")


def test_contract() -> None:
    with PLIST.open("rb") as fh:
        plist = plistlib.load(fh)
    check(plist["KeepAlive"] is True, "launchd unconditionally restarts dead bridges")
    check(plist["ThrottleInterval"] == 10, "crash loops are throttled")

    startup = STARTUP.read_text() + RUNTIME.read_text()
    installer = (REPO / "src" / "install-channel-bridge-launchd.sh").read_text()
    check('launchctl kickstart "$SERVICE"' in installer,
          "installer explicitly starts newly bootstrapped jobs")
    check("launchctl kickstart -k \"$service\"" in startup,
          "loaded-but-idle jobs are kickstarted after credentials return")
    check('launchctl kickstart -k "gui/$(id -u)/$_GW_LABEL"' in startup,
          "loaded-but-idle gateway job is also kickstarted")
    for channel in ("slack", "discord", "telegram"):
        check(f"channel_bridge_supervised {channel}" in startup,
              f"startup delegates {channel} to launchd")


def test_wrapper_restart_signal() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        repo = root / "repo"
        workspace = root / "workspace"
        config = root / "config"
        (repo / "src" / "launchd").mkdir(parents=True)
        (repo / "src").mkdir(exist_ok=True)
        (repo / "scripts").mkdir()
        (config / "channels" / "slack").mkdir(parents=True)
        workspace.mkdir()
        shutil.copy2(WRAPPER, repo / "src" / "launchd" / WRAPPER.name)
        (repo / "src" / "slack-bridge.py").write_text("# dummy\n")
        (config / "channels" / "slack" / ".env").write_text("SLACK_BOT_TOKEN=x-test\n")

        helper = repo / "scripts" / "sutando-config.sh"
        helper.write_text(
            "#!/bin/bash\n"
            f"if [ \"$1\" = workspace ]; then echo '{workspace}'; "
            f"else echo \"{config}/$2\"; fi\n"
        )
        helper.chmod(0o755)
        fake_python = root / "python"
        # Exit 1: this file covers CRASH recovery, and the wrapper treats a
        # clean exit as a deliberate stand-down rather than something to respawn.
        fake_python.write_text(
            "#!/bin/bash\nprintf '%s\\n' \"$*\" >> \"$TEST_EXEC_LOG\"\nexit 1\n")
        fake_python.chmod(0o755)
        exec_log = root / "exec.log"
        env = os.environ.copy()
        env.update({
            "SUTANDO_CHANNEL_BRIDGE_PYTHON": str(fake_python),
            "TEST_EXEC_LOG": str(exec_log),
            "HOME": str(root),
        })

        wrapper = repo / "src" / "launchd" / WRAPPER.name
        env["SUTANDO_CHANNEL_BRIDGE_RESTART_DELAY"] = "0.05"
        proc = subprocess.Popen(["bash", str(wrapper), "slack"], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for _ in range(100):
            if exec_log.exists() and len(exec_log.read_text().splitlines()) >= 2:
                break
            import time
            time.sleep(0.02)
        proc.terminate()
        proc.wait(timeout=5)
        check((workspace / "state" / "channel-bridge-supervisor" / "slack.started").exists(),
              "first launch records its supervisor marker")
        alerts = list((workspace / "results").glob("proactive-slack-bridge-restarted-*.txt"))
        check(bool(alerts) and "automatically restarted" in alerts[-1].read_text(),
              f"restart writes a durable owner alert (found {[p.name for p in alerts]})")
        check(len(exec_log.read_text().splitlines()) >= 2, "wrapper restarts an exited bridge child")


if __name__ == "__main__":
    test_contract()
    test_wrapper_restart_signal()
    print("all passed")
