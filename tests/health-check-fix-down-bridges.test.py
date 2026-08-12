#!/usr/bin/env python3
"""
Regression tests for fix_down_bridges(): `--fix` restarting bridges that are
"configured but not running".

Incident (2026-07-02): discord-bridge died at boot with nothing logged. Its
check status is "warn" (optional channels don't page), which excludes it from
`issues` — so main()'s fix loop never reached the bridge-restart branch and
`--fix` left it down while owner DMs queued channel-side. fix_down_bridges()
dispatches off the full `checks` list instead, mirroring the screen-capture
warn-fix pattern.

Guards:

  a) "configured but not running" warn → bridge restarted (all 3 bridges)
  b) other bridge warns (multiple PIDs, token invalid, stale log) → untouched
  c) non-bridge checks with the same detail → untouched
  d) ok/fail bridge statuses → untouched (fail belongs to the main fix loop)

Run: python3 tests/health-check-fix-down-bridges.test.py
Exit code: 0 on pass, 1 on fail.
"""

from __future__ import annotations
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent

# Load src/health-check.py as `health_check` (filename has a hyphen, can't
# import directly).
spec = importlib.util.spec_from_file_location("health_check", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hc)


def check(name: str, status: str, detail: str) -> dict:
    return {"name": name, "status": status, "detail": detail}


def run_with_popen_stub(checks: list, *, action="restart",
                        guard=lambda repo: (True, "test-clean"),
                        sender=None, notifier=None) -> tuple[list, list]:
    """Call fix_down_bridges with Popen stubbed; return (restarted, spawn argvs)."""
    spawned = []
    notified = []

    def fake_popen(argv, **kwargs):
        spawned.append(argv)
        return mock.MagicMock()

    with tempfile.TemporaryDirectory() as td:
        with mock.patch.object(hc, "WORKSPACE_DIR", Path(td)), \
             mock.patch.object(hc, "_bridge_interpreter", return_value="python3"), \
             mock.patch.object(hc, "_load_channel_env", return_value={"SLACK_BOT_TOKEN": "xoxb-test"}), \
             mock.patch.object(hc.subprocess, "Popen", side_effect=fake_popen):
            restarted = hc.fix_down_bridges(
                checks, action=action, guard=guard,
                sender=(sender if sender is not None else (lambda _m: True)),
                # Injected so the local-notification fallback never reaches the
                # patched Popen — `spawned` must stay a record of RESTARTS only.
                notifier=(notifier if notifier is not None
                          else (lambda _m: notified.append(_m) or False)),
            )
    run_with_popen_stub.last_notified = notified
    return restarted, spawned


def case_a_down_bridges_restarted() -> list[str]:
    fails = []
    checks = [
        check("discord-bridge", "warn", "configured but not running"),
        check("telegram-bridge", "warn", "configured but not running"),
        check("slack-bridge", "warn", "configured but not running"),
    ]
    restarted, spawned = run_with_popen_stub(checks)
    if restarted != ["discord-bridge", "telegram-bridge", "slack-bridge"]:
        fails.append(f"a) expected all 3 bridges restarted, got {restarted}")
    if len(spawned) != 3:
        fails.append(f"a) expected 3 spawns, got {len(spawned)}")
    for argv in spawned:
        if not str(argv[1]).endswith("-bridge.py"):
            fails.append(f"a) spawn argv doesn't target a bridge script: {argv}")
    return fails


def case_b_other_bridge_warns_untouched() -> list[str]:
    fails = []
    checks = [
        check("discord-bridge", "warn", "multiple processes (2 PIDs: 1,2)"),
        check("discord-bridge", "warn", "token invalid (LoginFailure) — regenerate at discord.com/developers/applications"),
        check("telegram-bridge", "warn", "log stale (36.0h) — process may be wedged"),
    ]
    restarted, spawned = run_with_popen_stub(checks)
    if restarted or spawned:
        fails.append(f"b) non-down bridge warns triggered restart: {restarted}")
    return fails


def case_c_non_bridge_checks_untouched() -> list[str]:
    fails = []
    checks = [
        check("conversation-server", "warn", "configured but not running"),
        check("credential-proxy", "warn", "configured but not running"),
    ]
    restarted, spawned = run_with_popen_stub(checks)
    if restarted or spawned:
        fails.append(f"c) non-covered checks triggered restart: {restarted}")
    return fails


def case_d_other_statuses_untouched() -> list[str]:
    fails = []
    checks = [
        check("discord-bridge", "ok", "running"),
        check("telegram-bridge", "down", "configured but not running"),
    ]
    restarted, spawned = run_with_popen_stub(checks)
    if restarted or spawned:
        fails.append(f"d) ok/down statuses triggered restart: {restarted}")
    return fails


def case_e_main_fix_prints_bridge_names() -> list[str]:
    """main() --fix exercises lines 2349-2354: prints '{name}: restart attempted'."""
    fails = []
    fake_checks = [
        check("discord-bridge", "warn", "configured but not running"),
        check("slack-bridge",   "warn", "configured but not running"),
    ]
    captured = io.StringIO()
    with mock.patch.object(sys, "argv", ["health-check.py", "--fix"]), \
         mock.patch.object(hc, "run_all_checks", return_value=fake_checks), \
         mock.patch.object(hc, "fix_down_bridges", return_value=["discord-bridge", "slack-bridge"]):
        try:
            with redirect_stdout(captured):
                hc.main()
        except SystemExit:
            pass
    out = captured.getvalue()
    for name in ("discord-bridge", "slack-bridge"):
        expected = f"  {name}: restart attempted (was not running)"
        if expected not in out:
            fails.append(f"e) missing expected line '{expected}' in main() --fix output")
    return fails


def case_f_run_all_checks_emits_slack_configured_not_running() -> list[str]:
    """Reachability guard (PR #1898): run_all_checks() must emit the
    'configured but not running' warn for slack-bridge — otherwise
    fix_down_bridges()'s slack branch is dead code and case_a is a false
    positive. Slack is made to look configured (access.json present) and not
    running (pgrep for slack-bridge.py returns nothing); every OTHER pgrep/
    subprocess call is delegated to the real implementation so the rest of the
    health check runs normally.
    """
    fails = []
    real_run = hc.subprocess.run

    def fake_run(argv, *args, **kwargs):
        # Intercept ONLY the slack-bridge pgrep so it reports "not running".
        if (isinstance(argv, list) and len(argv) >= 3
                and argv[0] == "/usr/bin/pgrep" and "slack-bridge" in argv[2]):
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")
        return real_run(argv, *args, **kwargs)

    with tempfile.TemporaryDirectory() as home_td:
        home = Path(home_td)
        # Make slack look configured: create channels/slack/access.json under a
        # fake claude-home. Point claude_home_path() at it for ALL lookups
        # (real one just joins subpaths onto the home root, which is what we
        # emulate here).
        (home / "channels" / "slack").mkdir(parents=True, exist_ok=True)
        (home / "channels" / "slack" / "access.json").write_text('{"allowFrom": []}')

        def fake_home(*subpath):
            return home.joinpath(*subpath)

        with mock.patch.object(hc.subprocess, "run", side_effect=fake_run), \
             mock.patch.object(hc, "claude_home_path", side_effect=fake_home):
            try:
                checks = hc.run_all_checks()
            except Exception as e:  # pragma: no cover - defensive
                return [f"f) run_all_checks raised: {e!r}"]

    slack = [c for c in checks if c.get("name") == "slack-bridge"]
    if not slack:
        fails.append("f) run_all_checks emitted NO slack-bridge check (branch unreachable)")
    elif not any(c.get("detail") == "configured but not running" for c in slack):
        fails.append(f"f) slack-bridge check(s) present but none 'configured but not running': {slack}")
    return fails


def case_g_launch_parity_interpreter_and_env() -> list[str]:
    """Launch parity (PR #1898): fix_down_bridges must (1) launch discord/slack
    with an interpreter probed for the bridge's import — NOT bare
    sys.executable — and (2) inject the slack channel .env into the child.
    """
    fails = []
    spawned = []  # (argv, env)

    def fake_popen(argv, **kwargs):
        spawned.append((argv, kwargs.get("env")))
        return mock.MagicMock()

    checks = [
        check("discord-bridge", "warn", "configured but not running"),
        check("slack-bridge", "warn", "configured but not running"),
    ]
    with tempfile.TemporaryDirectory() as td:
        with mock.patch.object(hc, "WORKSPACE_DIR", Path(td)), \
             mock.patch.object(hc, "_bridge_interpreter", return_value="/opt/homebrew/bin/python3"), \
             mock.patch.object(hc, "_load_channel_env", return_value={"SLACK_BOT_TOKEN": "xoxb-abc", "SLACK_APP_TOKEN": "xapp-xyz"}), \
             mock.patch.object(hc.subprocess, "Popen", side_effect=fake_popen):
            restarted = hc.fix_down_bridges(
                checks, action="restart",
                guard=lambda repo: (True, "test-clean"), sender=lambda _m: True)

    if restarted != ["discord-bridge", "slack-bridge"]:
        fails.append(f"g) expected both restarted, got {restarted}")
    for argv, env in spawned:
        if argv[0] != "/opt/homebrew/bin/python3":
            fails.append(f"g) bridge not launched with probed interpreter: {argv[0]}")
        if str(argv[1]).endswith("slack-bridge.py"):
            if not env or env.get("SLACK_BOT_TOKEN") != "xoxb-abc":
                fails.append("g) slack child env missing SLACK_BOT_TOKEN from channel .env")
    return fails


def case_h_launch_parity_failsafe_skips() -> list[str]:
    """Fail-safe (PR #1898): if no interpreter can import the bridge dep, OR the
    slack tokens are unavailable, fix_down_bridges must SKIP that bridge (no
    crash-loop spawn) rather than launch it.
    """
    fails = []
    spawned = []

    def fake_popen(argv, **kwargs):
        spawned.append(argv)
        return mock.MagicMock()

    checks = [
        check("discord-bridge", "warn", "configured but not running"),
        check("slack-bridge", "warn", "configured but not running"),
    ]
    # discord: no capable interpreter (None). slack: interpreter fine but env
    # has no token — and ensure the ambient env doesn't carry one either.
    clean_env = {k: v for k, v in os.environ.items() if k != "SLACK_BOT_TOKEN"}
    with tempfile.TemporaryDirectory() as td:
        with mock.patch.object(hc, "WORKSPACE_DIR", Path(td)), \
             mock.patch.object(hc, "_bridge_interpreter", side_effect=lambda n: None if n == "discord-bridge" else "python3"), \
             mock.patch.object(hc, "_load_channel_env", return_value={}), \
             mock.patch.dict(hc.os.environ, clean_env, clear=True), \
             mock.patch.object(hc.subprocess, "Popen", side_effect=fake_popen):
            restarted = hc.fix_down_bridges(
                checks, action="restart",
                guard=lambda repo: (True, "test-clean"), sender=lambda _m: True)

    if restarted:
        fails.append(f"h) expected no restarts (fail-safe), got {restarted}")
    if spawned:
        fails.append(f"h) fail-safe still spawned a process: {spawned}")
    return fails


def case_i_bridge_interpreter_no_import_gate() -> list[str]:
    """_bridge_interpreter (PR #1898): a bridge with no required import
    (telegram — absent from _BRIDGE_REQUIRED_IMPORT) short-circuits to
    sys.executable without probing any candidate. Covers the `required is None`
    branch.
    """
    fails = []
    got = hc._bridge_interpreter("telegram-bridge")
    if got != sys.executable:
        fails.append(f"i) no-import-gate bridge should return sys.executable, got {got!r}")
    return fails


def case_j_bridge_interpreter_probes_and_picks() -> list[str]:
    """_bridge_interpreter (PR #1898): for a bridge WITH a required import,
    walk the candidate list, skip candidates that don't exist on disk, and
    return the first whose probe imports the module cleanly (returncode 0).
    Covers the which/exists skip + the successful-probe return.
    """
    fails = []
    # Two candidates: the first is not installed anywhere (skipped by the
    # which/exists guard), the second exists and probes clean.
    good = "/opt/homebrew/bin/python3-good"
    cands = ["/nonexistent/python3-missing", good]

    def fake_which(cand):
        return good if cand == good else None

    def fake_run(argv, **kwargs):
        # Only the "good" candidate is ever probed (the missing one is skipped
        # before any subprocess runs).
        rc = 0 if argv[0] == good else 1
        return subprocess.CompletedProcess(argv, rc, stdout=b"", stderr=b"")

    with mock.patch.object(hc, "_BRIDGE_INTERP_CANDIDATES", cands), \
         mock.patch.object(hc.shutil, "which", side_effect=fake_which), \
         mock.patch.object(hc.subprocess, "run", side_effect=fake_run):
        got = hc._bridge_interpreter("slack-bridge")
    if got != good:
        fails.append(f"j) expected first importing candidate {good!r}, got {got!r}")
    return fails


def case_k_bridge_interpreter_none_when_no_capable() -> list[str]:
    """_bridge_interpreter (PR #1898): when no candidate can import the required
    module — a failed probe (rc != 0) plus a probe that raises (OSError /
    TimeoutExpired) — the function returns None so the caller skips the restart.
    Covers the timeout/OSError `continue` and the terminal `return None`.
    """
    fails = []
    cands = ["/opt/homebrew/bin/python3-a", "/opt/homebrew/bin/python3-b"]

    def fake_run(argv, **kwargs):
        if argv[0].endswith("python3-a"):
            # Import fails cleanly (module absent) → rc != 0.
            return subprocess.CompletedProcess(argv, 1, stdout=b"", stderr=b"ImportError")
        # The other candidate blows up mid-probe → caught, continue.
        raise subprocess.TimeoutExpired(argv, 10)

    with mock.patch.object(hc, "_BRIDGE_INTERP_CANDIDATES", cands), \
         mock.patch.object(hc.shutil, "which", side_effect=lambda c: c), \
         mock.patch.object(hc.subprocess, "run", side_effect=fake_run):
        got = hc._bridge_interpreter("discord-bridge")
    if got is not None:
        fails.append(f"k) expected None when no capable interpreter, got {got!r}")
    return fails


def case_l_load_channel_env_parses_file() -> list[str]:
    """_load_channel_env (PR #1898): parse channels/<channel>/.env into a dict,
    honoring `export ` prefixes, quote-stripping, and skipping blank/comment/
    non-KEY=VALUE lines. Covers the file-present parse branch.
    """
    fails = []
    with tempfile.TemporaryDirectory() as home_td:
        home = Path(home_td)
        chan = home / "channels" / "slack"
        chan.mkdir(parents=True, exist_ok=True)
        (chan / ".env").write_text(
            "# a comment\n"
            "\n"
            "SLACK_BOT_TOKEN=\"xoxb-quoted\"\n"
            "export SLACK_APP_TOKEN='xapp-exported'\n"
            "MALFORMED_NO_EQUALS_SIGN\n"
        )

        def fake_home(*subpath):
            return home.joinpath(*subpath)

        with mock.patch.object(hc, "claude_home_path", side_effect=fake_home):
            env = hc._load_channel_env("slack")

    if env.get("SLACK_BOT_TOKEN") != "xoxb-quoted":
        fails.append(f"l) SLACK_BOT_TOKEN quote-strip failed: {env!r}")
    if env.get("SLACK_APP_TOKEN") != "xapp-exported":
        fails.append(f"l) `export ` prefix strip failed: {env!r}")
    if "MALFORMED_NO_EQUALS_SIGN" in env or "# a comment" in env:
        fails.append(f"l) skipped-line leaked into env: {env!r}")
    return fails


def case_m_load_channel_env_absent_file() -> list[str]:
    """_load_channel_env (PR #1898): an absent .env returns {} (the caller
    treats missing tokens as a reason to skip the restart). Covers the
    not-exists early return.
    """
    fails = []
    with tempfile.TemporaryDirectory() as home_td:
        home = Path(home_td)  # no channels/<channel>/.env created

        def fake_home(*subpath):
            return home.joinpath(*subpath)

        with mock.patch.object(hc, "claude_home_path", side_effect=fake_home):
            env = hc._load_channel_env("slack")
    if env != {}:
        fails.append(f"m) absent .env should yield {{}}, got {env!r}")
    return fails


def case_n_load_channel_env_unreadable_file() -> list[str]:
    """_load_channel_env (PR #1898): a present-but-unreadable .env (read raises
    OSError) is swallowed and yields {} — the watchdog never crashes on a
    permission-denied env file. Covers the `except OSError` branch.
    """
    fails = []
    with tempfile.TemporaryDirectory() as home_td:
        home = Path(home_td)
        chan = home / "channels" / "slack"
        chan.mkdir(parents=True, exist_ok=True)
        env_file = chan / ".env"
        env_file.write_text("SLACK_BOT_TOKEN=xoxb-present\n")

        def fake_home(*subpath):
            return home.joinpath(*subpath)

        real_read_text = Path.read_text

        def boom(self, *args, **kwargs):
            if self == env_file:
                raise OSError("permission denied")
            return real_read_text(self, *args, **kwargs)

        with mock.patch.object(hc, "claude_home_path", side_effect=fake_home), \
             mock.patch.object(Path, "read_text", boom):
            env = hc._load_channel_env("slack")
    if env != {}:
        fails.append(f"n) unreadable .env should yield {{}}, got {env!r}")
    return fails


def case_u_defaults_from_config_and_module() -> list[str]:
    """Cover the default-resolution branches when action/sender/guard are omitted:"""
    fails = []
    checks = [check("discord-bridge", "warn", "configured but not running")]
    with tempfile.TemporaryDirectory() as td:
        with mock.patch.object(hc, "WORKSPACE_DIR", Path(td)), \
             mock.patch.object(hc, "resolve_down_bridge_action", return_value="restart"), \
             mock.patch.object(hc, "_checkout_is_canonical", return_value=(True, "clean")), \
             mock.patch.object(hc, "_default_slack_sender", return_value=True), \
             mock.patch.object(hc, "_bridge_interpreter", return_value="python3"), \
             mock.patch.object(hc.subprocess, "Popen", side_effect=lambda a, **k: mock.MagicMock()):
            restarted = hc.fix_down_bridges(checks)  # no kwargs → module defaults
    if restarted != ["discord-bridge"]:
        fails.append(f"u) default-resolution path should restart, got {restarted}")
    return fails


def case_v_alert_send_failure_is_swallowed() -> list[str]:
    """_alert must swallow a sender that raises — alerting never breaks the check."""
    fails = []

    def boom(_m):
        raise RuntimeError("slack unreachable")

    checks = [check("discord-bridge", "warn", "configured but not running")]
    try:
        restarted, spawned = run_with_popen_stub(checks, action="alert", sender=boom)
    except Exception as e:  # pragma: no cover — a leak here IS the failure
        return [f"v) a raising sender propagated out of fix_down_bridges: {e!r}"]
    if restarted or spawned:
        fails.append(f"v) alert mode must not restart: {restarted}/{spawned}")
    return fails


def case_x_failed_sender_falls_back_to_a_local_owner_surface() -> list[str]:
    """A failed primary sender must reach an owner surface, not just a log line."""
    import contextlib
    import io

    fails = []
    checks = [check("discord-bridge", "warn", "configured but not running")]

    # 1. primary False + fallback True -> delivered, and NO undelivered marker
    got = []
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        run_with_popen_stub(checks, action="alert", sender=lambda _m: False,
                            notifier=lambda m: got.append(m) or True)
    out = buf.getvalue()
    if not got:
        fails.append("x) primary sender returned False but the fallback was never called")
    if hc.ALERT_UNDELIVERED_MARKER in out:
        fails.append(f"x) fallback delivered yet {hc.ALERT_UNDELIVERED_MARKER} was printed: {out!r}")
    if "local notification" not in out:
        fails.append(f"x) fallback delivery was not stated in the output: {out!r}")
    if got and "discord-bridge" not in got[0]:
        fails.append(f"x) the fallback got a message naming no bridge: {got[0]!r}")

    # 2. BOTH fail -> marker still present (the fallback must not mask it)
    buf2 = io.StringIO()
    with contextlib.redirect_stdout(buf2):
        run_with_popen_stub(checks, action="alert", sender=lambda _m: False,
                            notifier=lambda _m: False)
    out2 = buf2.getvalue()
    if hc.ALERT_UNDELIVERED_MARKER not in out2:
        fails.append(f"x) both surfaces failed but no {hc.ALERT_UNDELIVERED_MARKER}: {out2!r}")

    # 3. a fallback that RAISES is undelivered, never an exception out of the check
    buf3 = io.StringIO()
    def boom(_m):
        raise RuntimeError("notifier exploded")
    try:
        with contextlib.redirect_stdout(buf3):
            run_with_popen_stub(checks, action="alert", sender=lambda _m: False,
                                notifier=boom)
    except Exception as e:  # noqa: BLE001
        fails.append(f"x) a raising fallback escaped the check: {e!r}")
    if hc.ALERT_UNDELIVERED_MARKER not in buf3.getvalue():
        fails.append("x) a raising fallback did not report undelivered")

    # 4. CONTROL: a working primary must never reach the fallback
    reached = []
    with contextlib.redirect_stdout(io.StringIO()):
        run_with_popen_stub(checks, action="alert", sender=lambda _m: True,
                            notifier=lambda m: reached.append(m) or True)
    if reached:
        fails.append(f"x) fallback fired despite a DELIVERED primary: {reached!r}")

    # 5. the shipped default reports False on a non-zero exit rather than assuming success
    if hc._default_local_notifier.__doc__ is None:
        fails.append("x) _default_local_notifier lost its contract docstring")
    with mock.patch.object(hc.subprocess, "run",
                           return_value=mock.MagicMock(returncode=1)):
        if hc._default_local_notifier("m") is not False:
            fails.append("x) default notifier reported success on a non-zero exit")
    with mock.patch.object(hc.subprocess, "run",
                           return_value=mock.MagicMock(returncode=0)):
        if hc._default_local_notifier("m") is not True:
            fails.append("x) default notifier reported failure on a zero exit")
    return fails


def case_w_alert_undelivered_is_observable() -> list[str]:
    """A sender that RETURNS False (not raises) must be observable."""
    import io
    import contextlib

    fails = []
    checks = [check("discord-bridge", "warn", "configured but not running")]

    # 1. sender returns False -> marker present
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        run_with_popen_stub(checks, action="alert", sender=lambda _m: False)
    out_false = buf.getvalue()
    if hc.ALERT_UNDELIVERED_MARKER not in out_false:
        fails.append(
            "w) a sender returning False produced no "
            f"{hc.ALERT_UNDELIVERED_MARKER} signal: {out_false!r}"
        )

    # 2. CONTROL: sender returns True -> marker absent
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        run_with_popen_stub(checks, action="alert", sender=lambda _m: True)
    out_true = buf.getvalue()
    if hc.ALERT_UNDELIVERED_MARKER in out_true:
        fails.append(
            f"w) a DELIVERED alert wrongly printed {hc.ALERT_UNDELIVERED_MARKER}: {out_true!r}"
        )

    # 3. a RAISING sender is undelivered too — same observable signal, and it
    #    still must not propagate (case_v pins the non-propagation).
    def boom(_m):
        raise RuntimeError("slack unreachable")

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        run_with_popen_stub(checks, action="alert", sender=boom)
    if hc.ALERT_UNDELIVERED_MARKER not in buf.getvalue():
        fails.append("w) a RAISING sender produced no undelivered signal")

    return fails


def case_o_action_off_is_noop() -> list[str]:
    """down_bridge_action="off": never restart, never alert (return [])."""
    fails = []
    sent = []
    checks = [check("discord-bridge", "warn", "configured but not running")]
    restarted, spawned = run_with_popen_stub(checks, action="off", sender=sent.append)
    if restarted or spawned:
        fails.append(f"o) action=off restarted/spawned: {restarted}/{spawned}")
    if sent:
        fails.append(f"o) action=off should be silent, sent: {sent}")
    return fails


def case_p_action_alert_alerts_not_restarts() -> list[str]:
    """down_bridge_action="alert": alert the owner per down bridge, no restart."""
    fails = []
    sent = []
    checks = [
        check("discord-bridge", "warn", "configured but not running"),
        check("slack-bridge", "warn", "configured but not running"),
    ]
    restarted, spawned = run_with_popen_stub(checks, action="alert", sender=sent.append)
    if restarted or spawned:
        fails.append(f"p) action=alert must not restart: {restarted}/{spawned}")
    if len(sent) != 2 or not all("alert-only" in m for m in sent):
        fails.append(f"p) expected 2 alert-only messages, got {sent}")
    return fails


def case_q_restart_guard_fail_downgrades_to_alert() -> list[str]:
    """action="restart" but the checkout guard fails → alert, do NOT restart
    (the 2026-07-25 fix: never auto-restart onto a non-canonical checkout)."""
    fails = []
    sent = []
    checks = [check("discord-bridge", "warn", "configured but not running")]
    restarted, spawned = run_with_popen_stub(
        checks, action="restart",
        guard=lambda repo: (False, "checkout on 'feature-x', not main"),
        sender=sent.append)
    if restarted or spawned:
        fails.append(f"q) guard-fail must not restart: {restarted}/{spawned}")
    if not (sent and "NOT auto-restarted" in sent[0] and "feature-x" in sent[0]):
        fails.append(f"q) expected a downgrade alert naming the reason, got {sent}")
    return fails


def case_r_restart_guard_ok_restarts_and_alerts() -> list[str]:
    """action="restart" + canonical checkout → restart AND alert (visibility)."""
    fails = []
    sent = []
    checks = [check("discord-bridge", "warn", "configured but not running")]
    restarted, spawned = run_with_popen_stub(
        checks, action="restart",
        guard=lambda repo: (True, "clean + on main"), sender=sent.append)
    if restarted != ["discord-bridge"] or len(spawned) != 1:
        fails.append(f"r) expected 1 restart, got {restarted}/{spawned}")
    if not (sent and "auto-restarted" in sent[0]):
        fails.append(f"r) expected an auto-restarted alert, got {sent}")
    return fails


def case_s_checkout_is_canonical() -> list[str]:
    """_checkout_is_canonical: True only for clean + on main; False for a
    branch, a dirty tree, or unreadable git state (fail-closed)."""
    fails = []

    def fake_run(argv, **kwargs):
        if "rev-parse" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout=fake_run.branch + "\n", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout=fake_run.dirty, stderr="")

    with mock.patch.object(hc.subprocess, "run", side_effect=fake_run):
        fake_run.branch, fake_run.dirty = "main", ""
        ok, why = hc._checkout_is_canonical("/repo")
        if not ok:
            fails.append(f"s) clean+main should be canonical, got {why}")
        fake_run.branch, fake_run.dirty = "feature-x", ""
        ok, why = hc._checkout_is_canonical("/repo")
        if ok or "not main" not in why:
            fails.append(f"s) non-main should fail, got ({ok},{why})")
        fake_run.branch, fake_run.dirty = "main", " M src/x.py"
        ok, why = hc._checkout_is_canonical("/repo")
        if ok or "uncommitted" not in why:
            fails.append(f"s) dirty tree should fail, got ({ok},{why})")

    # A NONZERO git exit with empty stdout must fail closed — an errored `git status
    # --porcelain` (empty output) must not read as "clean" and green-light an auto-restart
    def fail_status(argv, **kwargs):
        if "rev-parse" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout="main\n", stderr="")
        return subprocess.CompletedProcess(argv, 128, stdout="", stderr="fatal")
    with mock.patch.object(hc.subprocess, "run", side_effect=fail_status):
        ok, why = hc._checkout_is_canonical("/repo")
        if ok or "unreadable" not in why:
            fails.append(f"s) nonzero git exit should fail-closed, got ({ok},{why})")

    def boom(*a, **k):
        raise OSError("git missing")
    with mock.patch.object(hc.subprocess, "run", side_effect=boom):
        ok, why = hc._checkout_is_canonical("/repo")
        if ok or "unreadable" not in why:
            fails.append(f"s) unreadable git state should fail-closed, got ({ok},{why})")
    return fails


def case_t_resolve_down_bridge_action() -> list[str]:
    """resolve_down_bridge_action: config default, env override, unknown→restart."""
    fails = []
    env = {k: v for k, v in os.environ.items() if k != "SUTANDO_DOWN_BRIDGE_ACTION"}
    with mock.patch.dict(os.environ, env, clear=True):
        if hc.resolve_down_bridge_action() != "restart":
            fails.append(f"t) default should be 'restart', got {hc.resolve_down_bridge_action()!r}")
    with mock.patch.dict(os.environ, {"SUTANDO_DOWN_BRIDGE_ACTION": "alert"}):
        if hc.resolve_down_bridge_action() != "alert":
            fails.append("t) env override to 'alert' ignored")
    with mock.patch.dict(os.environ, {"SUTANDO_DOWN_BRIDGE_ACTION": "bogus"}):
        if hc.resolve_down_bridge_action() != "restart":
            fails.append("t) unknown value should fall back to 'restart'")
    return fails


def main() -> int:
    all_fails = []
    for case in (case_a_down_bridges_restarted, case_b_other_bridge_warns_untouched,
                 case_c_non_bridge_checks_untouched, case_d_other_statuses_untouched,
                 case_e_main_fix_prints_bridge_names,
                 case_f_run_all_checks_emits_slack_configured_not_running,
                 case_g_launch_parity_interpreter_and_env,
                 case_h_launch_parity_failsafe_skips,
                 case_i_bridge_interpreter_no_import_gate,
                 case_j_bridge_interpreter_probes_and_picks,
                 case_k_bridge_interpreter_none_when_no_capable,
                 case_l_load_channel_env_parses_file,
                 case_m_load_channel_env_absent_file,
                 case_n_load_channel_env_unreadable_file,
                 case_o_action_off_is_noop,
                 case_p_action_alert_alerts_not_restarts,
                 case_q_restart_guard_fail_downgrades_to_alert,
                 case_r_restart_guard_ok_restarts_and_alerts,
                 case_s_checkout_is_canonical,
                 case_t_resolve_down_bridge_action,
                 case_u_defaults_from_config_and_module,
                 case_v_alert_send_failure_is_swallowed,
                 case_w_alert_undelivered_is_observable,
                 case_x_failed_sender_falls_back_to_a_local_owner_surface):
        fails = case()
        status = "PASS" if not fails else "FAIL"
        print(f"  {status} {case.__name__}")
        all_fails.extend(fails)
    if all_fails:
        print()
        for f in all_fails:
            print(f"  ✗ {f}")
        return 1
    print("All fix_down_bridges tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
