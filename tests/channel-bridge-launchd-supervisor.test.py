#!/usr/bin/env python3
"""Regression coverage for crash recovery of Slack/Discord/Telegram bridges."""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import tempfile
import time
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
        # Crash path alerts too: shim the sink here as well, not only in
        # _stage_clean_exit — this staging builds its own env.
        shims = root / "shims"; shims.mkdir(exist_ok=True)
        (shims / "osascript").write_text("#!/bin/bash\nexit 0\n")
        (shims / "osascript").chmod(0o755)
        env = os.environ.copy()
        env.update({
            "PATH": f"{shims}:{env.get('PATH','')}",
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


def _stage_clean_exit(root, rc=75):
    """Wrapper + config staged with a child exiting `rc` (75 = declared stand-down)."""
    repo, workspace, config = root / "repo", root / "workspace", root / "config"
    (repo / "src" / "launchd").mkdir(parents=True)
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
    fake_python.write_text(
        "#!/bin/bash\nprintf '%s\\n' \"$*\" >> \"$TEST_EXEC_LOG\"\n"
        f"exit {rc}\n")
    fake_python.chmod(0o755)
    exec_log = root / "exec.log"
    # Same reason as the shell harness: keep the real notification sink out.
    shims = root / "shims"; shims.mkdir(exist_ok=True)
    (shims / "osascript").write_text("#!/bin/bash\nexit 0\n")
    (shims / "osascript").chmod(0o755)
    env = os.environ.copy()
    env.update({
        "PATH": f"{shims}:{env.get('PATH','')}",
        "SUTANDO_CHANNEL_BRIDGE_PYTHON": str(fake_python),
        "TEST_EXEC_LOG": str(exec_log),
        "HOME": str(root),
        "SUTANDO_CHANNEL_BRIDGE_RESTART_DELAY": "0.05",
    })
    return repo / "src" / "launchd" / WRAPPER.name, workspace, exec_log, env


SETTLE = 3.0  # seconds a respawn or alert gets to appear before absence is claimed


def _wait_for(pred, timeout, interval=0.05):
    """Poll to a deadline, returning as soon as pred() is true.

    Absence is only meaningful after a window: emit_restart_alert shells out, so
    a check taken immediately can precede the alert it claims is not there.
    """
    deadline = time.monotonic() + timeout
    val = pred()
    while not val and time.monotonic() < deadline:
        time.sleep(interval)
        val = pred()
    return val


def _launches(exec_log):
    return len(exec_log.read_text().splitlines()) if exec_log.exists() else 0


def _alerts(workspace):
    return sorted(p.name for p in
                  (workspace / "results").glob("proactive-slack-bridge-restarted-*.txt"))


def test_clean_exit_is_a_standdown():
    """exit 75 must NOT respawn and must NOT alert - the inverse of the crash case.

    The crash test above proves a non-zero child comes back. Only this one pins
    the change itself: before it, the wrapper could not tell a deliberate
    stand-down from a crash, so a child exiting 0 was respawned forever. Live on
    2026-08-24 that produced 95 laps against a held lock, one owner alert each.
    """
    import time
    with tempfile.TemporaryDirectory() as raw:
        wrapper, workspace, exec_log, env = _stage_clean_exit(Path(raw))
        proc = subprocess.Popen(["bash", str(wrapper), "slack"], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            # A fixed window is wrong BOTH ways here: too early and the first
            # launch has not happened, too late and a respawn is missed.
            started = _wait_for(lambda: _launches(exec_log) >= 1, 10.0)
            respawned = _wait_for(lambda: _launches(exec_log) >= 2, SETTLE)
            alerted = _wait_for(lambda: bool(_alerts(workspace)), SETTLE)
        finally:
            proc.terminate()
            proc.wait(timeout=5)
        check(started, "the child is launched at all (else the rest is vacuous)")
        check(not respawned,
              f"a clean exit is NOT respawned (launches={_launches(exec_log)})")
        check(not alerted,
              f"a clean exit writes NO owner alert (found {_alerts(workspace)})")


def test_a_launchd_relaunch_after_a_standdown_does_not_alert():
    """Exiting hands the respawn to launchd, whose KeepAlive is unconditional.

    So the wrapper is re-entered from the top, where the start marker survives
    from the previous run and reads as a crash. Measured under a real launchd job
    on 2026-08-24: exiting alone moved the 10s alert cadence from the wrapper to
    launchd unchanged (5 launches / 4 alerts vs 5 / 5 before the change).
    """
    with tempfile.TemporaryDirectory() as raw:
        wrapper, workspace, exec_log, env = _stage_clean_exit(Path(raw))
        for lap in range(3):        # one wrapper run per launchd relaunch
            r = subprocess.run(["bash", str(wrapper), "slack"], env=env,
                               capture_output=True, text=True, timeout=30)
            check(r.returncode == 0,
                  f"lap {lap}: a stand-down exits 0, letting launchd decide (rc={r.returncode})")
        launches = _launches(exec_log)
        check(launches == 3, f"each relaunch starts the child exactly once ({launches})")
        # The laps have exited, but emit_restart_alert shells out — an alert can
        # still land after the last one returns, so absence needs a window.
        check(not _wait_for(lambda: bool(_alerts(workspace)), SETTLE),
              f"a relaunch after a stand-down writes NO owner alert (found {_alerts(workspace)})")


def test_the_alert_detector_actually_fires():
    """Positive control for every `no alert` assertion in this suite.

    Three arms claim an alert is ABSENT. That claim is vacuous unless the same
    helper, on the same staging, can be shown to observe one - a detector that
    never fires reports silence identically to a system that is behaving.
    """
    with tempfile.TemporaryDirectory() as raw:
        # rc=1 is a crash, which is exactly what SHOULD alert.
        wrapper, workspace, _exec_log, env = _stage_clean_exit(Path(raw), rc=1)
        proc = subprocess.Popen(["bash", str(wrapper), "slack"], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            seen = _wait_for(lambda: bool(_alerts(workspace)), SETTLE)
        finally:
            proc.terminate()
            proc.wait(timeout=5)
        check(seen,
              f"a CRASH is alerted within the same {SETTLE}s window the negatives use")


def test_a_bare_exit_zero_is_NOT_a_standdown():
    """The guard must key on the DECLARED code, never on "clean".

    single_instance is the only deliberate stand-down in the tree, but it is not
    the only way to exit 0: a bridge whose main loop returns falls off __main__
    and exits 0 too. Standing down on that inverts the bug being fixed - instead
    of spamming the owner it leaves the bridge off and suppresses the very alert
    that would have told him. Silence is the worse direction.
    """
    import time
    with tempfile.TemporaryDirectory() as raw:
        wrapper, workspace, exec_log, env = _stage_clean_exit(Path(raw), rc=0)
        proc = subprocess.Popen(["bash", str(wrapper), "slack"], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            _wait_for(lambda: bool(_alerts(workspace)) or proc.poll() is not None, 10.0)
            alerted = bool(_alerts(workspace))
            still_running = proc.poll() is None
        finally:
            proc.terminate()
            proc.wait(timeout=5)
        check(still_running,
              "a bare exit 0 keeps the wrapper supervising (it did not stand down)")
        check(alerted,
              "a bare exit 0 still alerts the owner - it is not a declared stand-down")


def test_a_deliberate_restart_window_suppresses_the_alert():
    """restart.sh stamps state/channel-bridge-supervisor/deliberate-restart; a crash
    inside that window is ours and must not reach the owner (four alerts per two
    restarts, owner 2026-09-02)."""
    with tempfile.TemporaryDirectory() as raw:
        wrapper, workspace, exec_log, env = _stage_clean_exit(Path(raw), rc=1)
        sup = workspace / "state" / "channel-bridge-supervisor"
        sup.mkdir(parents=True)
        (sup / "deliberate-restart").write_text(str(int(time.time())))
        proc = subprocess.Popen(["bash", str(wrapper), "slack"], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            _wait_for(lambda: _launches(exec_log) >= 3, SETTLE)
            seen = _wait_for(lambda: bool(_alerts(workspace)), 1.0)
        finally:
            proc.terminate()
            proc.wait(timeout=5)
        check(_launches(exec_log) >= 3, "the wrapper still respawns inside the window")
        check(not seen, f"no alert inside the deliberate window (found {_alerts(workspace)})")


def test_repeated_crashes_alert_once_per_cooldown():
    """A crash loop is one line, not a stream: the second alert within the cooldown is dropped."""
    with tempfile.TemporaryDirectory() as raw:
        wrapper, workspace, exec_log, env = _stage_clean_exit(Path(raw), rc=1)
        proc = subprocess.Popen(["bash", str(wrapper), "slack"], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            _wait_for(lambda: _launches(exec_log) >= 4, SETTLE)
            _wait_for(lambda: bool(_alerts(workspace)), SETTLE)
            time.sleep(0.3)
        finally:
            proc.terminate()
            proc.wait(timeout=5)
        check(_launches(exec_log) >= 4, "at least four launches happened")
        check(len(_alerts(workspace)) == 1,
              f"exactly one alert across the crash loop (found {_alerts(workspace)})")


if __name__ == "__main__":
    test_contract()
    test_wrapper_restart_signal()
    test_clean_exit_is_a_standdown()
    test_a_launchd_relaunch_after_a_standdown_does_not_alert()
    test_the_alert_detector_actually_fires()
    test_a_bare_exit_zero_is_NOT_a_standdown()
    test_a_deliberate_restart_window_suppresses_the_alert()
    test_repeated_crashes_alert_once_per_cooldown()
    print("all passed")
