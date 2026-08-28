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
  y) gateway-bridge down → restarted, spawning remote-gateway-bridge.py
  z) gateway-bridge's non-down warns describe a LIVE process → never respawned
 aa) a tokenless gateway yields no launch plan (alert, not a crash-loop)
 ab) AG2_REMOTE_TOKEN alone is enough, matching _gateway_configured()
 ac) a non-bridge name with absent/None detail is NEVER restarted

Second incident (2026-08-20): the gateway bridge — the ag2.space carrier — was
the one channel bridge this path excluded, and its check name differs from its
script name, so it was down 67 minutes with three tasks held gateway-side.

Run: python3 tests/health-check-fix-down-bridges.test.py
Exit code: 0 on pass, 1 on fail.
"""

from __future__ import annotations
import importlib.util
import io
import json
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


# Channel-aware: a flat dict gives the gateway slack's tokens, so every gateway
# case would fail for the wrong reason (no token) and hide a real regression.
_CHANNEL_ENV = {
    "slack": {"SLACK_BOT_TOKEN": "xoxb-test", "SLACK_APP_TOKEN": "xapp-test"},
    "ag2space": {"REMOTE_TASK_TOKEN": "gw-test-token"},
}


def _channel_env(channel: str) -> dict:
    return dict(_CHANNEL_ENV.get(channel, {}))


def run_with_popen_stub(checks: list, *, action="restart",
                        guard=lambda repo: (True, "test-clean"),
                        sender=None, notifier=None) -> tuple[list, list]:
    """Call fix_down_bridges with Popen stubbed; return (restarted, spawn argvs).

    Also stubs the interpreter probe and slack-env load so the test is
    hermetic: without these, fix_down_bridges would probe the host for
    discord.py / slack_bolt (flaky across machines) and skip the restart when
    absent. Here every bridge gets a known-good interpreter and slack gets a
    token, so the restart path is exercised deterministically; the vault tier
    is stubbed empty (never the real Keychain).
    """
    spawned = []
    notified = []

    def fake_popen(argv, **kwargs):
        spawned.append(argv)
        return mock.MagicMock()

    with tempfile.TemporaryDirectory() as td:
        with mock.patch.object(hc, "WORKSPACE_DIR", Path(td)), \
             mock.patch.object(hc, "_bridge_interpreter", return_value="python3"), \
             mock.patch.object(hc, "_load_channel_env", side_effect=_channel_env), \
             mock.patch.object(hc, "token_from_vault", return_value=""), \
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


def case_y_gateway_bridge_down_is_restarted() -> list[str]:
    """The gateway bridge carries ag2.space; it was the one channel bridge this
    path excluded, so a death went unattended until someone ran health-check by
    hand. Observed 2026-08-20: 67 minutes down, three tasks held gateway-side."""
    fails = []
    checks = [check("gateway-bridge", "warn", hc.GATEWAY_DOWN_DETAIL)]
    restarted, spawned = run_with_popen_stub(checks)
    if restarted != ["gateway-bridge"]:
        fails.append(f"y) expected gateway-bridge restarted, got {restarted}")
    if len(spawned) != 1:
        fails.append(f"y) expected 1 spawn, got {len(spawned)}")
    elif not str(spawned[0][1]).endswith("remote-gateway-bridge.py"):
        # src/gateway-bridge.py does not exist, so a bare f"{name}.py" Popens a
        # missing path and the "restart" is a no-op that still reports True.
        fails.append(f"y) spawn must target remote-gateway-bridge.py: {spawned[0]}")
    return fails


def case_z_gateway_other_warns_never_respawn() -> list[str]:
    """Only the DOWN detail may respawn. gateway-bridge's other warns describe a
    bridge that IS running (not serving / duplicate pileup); spawning another
    against a live one is the dual-poll state its singleton lock exists to avoid."""
    fails = []
    checks = [
        check("gateway-bridge", "warn",
              "process running but NOT serving — 12m; last ok 2026-08-20T11:54:15Z"),
        check("gateway-bridge", "warn",
              "2 gateway process(es) claimed by no instance lock (PIDs: 1,2); locks held: gateway-bridge=3"),
        check("gateway-bridge", "warn",
              "running; last poll did not succeed, last one that did was 9m ago"),
        check("gateway-bridge", "ok", "running + connected"),
    ]
    restarted, spawned = run_with_popen_stub(checks)
    if restarted or spawned:
        fails.append(f"z) non-down gateway warns must not respawn: {restarted} {spawned}")
    return fails


def case_ac_unknown_name_without_detail_is_untouched() -> list[str]:
    """Reported by Sutando (rui) on #3203 and reproduced against the production
    function before fixing. Matching only on the per-bridge detail lets an
    unknown name through: the lookup is None, a check with no `detail` is also
    None, and `None == None` restarts it — `_launch_bridge` then puts the check
    NAME into a path, so `voice-agent` spawned `src/voice-agent.py`.

    Existing case (c) cannot catch this: it pairs a non-bridge name with a real
    bridge detail, which fails the match either way. The discriminator is a
    non-bridge name whose detail is absent or None."""
    fails = []
    for label, chk in (
        ("no detail key", {"name": "voice-agent", "status": "warn"}),
        ("detail None", {"name": "voice-agent", "status": "warn", "detail": None}),
        ("unknown + gateway detail", {"name": "voice-agent", "status": "warn",
                                      "detail": hc.GATEWAY_DOWN_DETAIL}),
    ):
        restarted, spawned = run_with_popen_stub([chk])
        if restarted or spawned:
            fails.append(f"ac) {label}: non-bridge check must never restart — "
                         f"got {restarted} {spawned}")
    return fails


def case_aa_gateway_plan_requires_a_token() -> list[str]:
    """No token → no plan → alert instead of a crash-looping spawn, matching the
    slack branch and startup.sh's labeled skip."""
    fails = []
    # Shut the vault off at channel_token, NOT hc.token_from_vault: the plan
    # calls gateway_token() with no vault_get, so the real Keychain answers.
    with mock.patch("channel_token.token_from_vault", return_value=""), \
         mock.patch.object(hc, "_bridge_interpreter", return_value="python3"), \
         mock.patch.object(hc, "_load_channel_env", return_value={}), \
         mock.patch.dict(os.environ, {}, clear=True):
        if hc._bridge_launch_plan("gateway-bridge") is not None:
            fails.append("aa) tokenless gateway must yield no launch plan")
    with mock.patch("channel_token.token_from_vault", return_value=""), \
         mock.patch.object(hc, "_bridge_interpreter", return_value="python3"), \
         mock.patch.object(hc, "_load_channel_env", side_effect=_channel_env), \
         mock.patch.dict(os.environ, {}, clear=True):
        plan = hc._bridge_launch_plan("gateway-bridge")
        if plan is None:
            fails.append("aa) gateway with a channel-env token must yield a plan")
        else:
            _, env = plan
            # Never interpolate the value: on a leaky run this message is the leak.
            if env.get("REMOTE_TASK_TOKEN") != "gw-test-token":
                fails.append("aa) plan must carry the resolved fixture token")
            if env.get("SUTANDO_SUPERVISED") != "1":
                fails.append("aa) plan must mark the launch supervised")
    return fails


def case_ab_ag2_remote_token_alias_is_accepted() -> list[str]:
    """_gateway_configured() treats AG2_REMOTE_TOKEN as configuring the gateway,
    so a host with only that alias must be launchable — otherwise the check warns
    forever about a bridge --fix structurally refuses to start."""
    fails = []
    with mock.patch("channel_token.token_from_vault", return_value=""), \
         mock.patch.object(hc, "_bridge_interpreter", return_value="python3"), \
         mock.patch.object(hc, "_load_channel_env", return_value={"AG2_REMOTE_TOKEN": "alias-tok"}), \
         mock.patch.dict(os.environ, {}, clear=True):
        plan = hc._bridge_launch_plan("gateway-bridge")
        if plan is None:
            fails.append("ab) AG2_REMOTE_TOKEN alone must yield a plan")
        elif plan[1].get("REMOTE_TASK_TOKEN") != "alias-tok":
            fails.append("ab) alias must be normalised into REMOTE_TASK_TOKEN")
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
    clean_env = {k: v for k, v in os.environ.items() if k not in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN")}
    with tempfile.TemporaryDirectory() as td:
        with mock.patch.object(hc, "WORKSPACE_DIR", Path(td)), \
             mock.patch.object(hc, "_bridge_interpreter", side_effect=lambda n: None if n == "discord-bridge" else "python3"), \
             mock.patch.object(hc, "_load_channel_env", return_value={}), \
             mock.patch.object(hc, "token_from_vault", return_value=""), \
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
    """action="restart" but the checkout guard fails → alert, do NOT restart:
    a non-canonical checkout must never be the code an auto-restart relaunches."""
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
    """resolve_down_bridge_action: default ALERT, env override, unknown -> alert.

    The default is alert because a restart that looks successful but leaves the
    bridge unable to deliver is worse than one that is visibly down. Stubs the
    config loader: reading the host's own sutando.config.json made the default
    assertion pass for the wrong reason on a machine that sets it explicitly."""
    fails = []
    import sutando_config
    env = {k: v for k, v in os.environ.items() if k != "SUTANDO_DOWN_BRIDGE_ACTION"}
    with mock.patch.object(sutando_config, "load_config", lambda *a, **k: {}):
        with mock.patch.dict(os.environ, env, clear=True):
            if sutando_config.resolve_down_bridge_action() != "alert":
                fails.append(f"t) default should be 'alert', got "
                             f"{sutando_config.resolve_down_bridge_action()!r}")
        with mock.patch.dict(os.environ, {"SUTANDO_DOWN_BRIDGE_ACTION": "restart"}):
            if sutando_config.resolve_down_bridge_action() != "restart":
                fails.append("t) env override to 'restart' ignored")
        with mock.patch.dict(os.environ, {"SUTANDO_DOWN_BRIDGE_ACTION": "bogus"}):
            if sutando_config.resolve_down_bridge_action() != "alert":
                fails.append("t) unknown value should fall back to 'alert'")

    # The stubs above cover the missing-key fallback, a state no install is in:
    # sutando.config.json sets the key, so only an unstubbed read sees what ships.
    env = {k: v for k, v in os.environ.items() if k != "SUTANDO_DOWN_BRIDGE_ACTION"}
    with mock.patch.dict(os.environ, env, clear=True):
        shipped = sutando_config.resolve_down_bridge_action(REPO)
        if shipped != "alert":
            fails.append(f"t) the TRACKED sutando.config.json must ship 'alert', "
                         f"got {shipped!r} — a code-side default cannot override it")
    tracked = json.loads((REPO / "sutando.config.json").read_text())
    if (tracked.get("health_check") or {}).get("down_bridge_action") != "alert":
        fails.append("t) sutando.config.json health_check.down_bridge_action must be 'alert'")

    # Opt-in still reaches the restart arm, from the config rather than the env.
    with mock.patch.object(sutando_config, "load_config",
                           lambda *a, **k: {"health_check": {"down_bridge_action": "restart"}}):
        with mock.patch.dict(os.environ, env, clear=True):
            if sutando_config.resolve_down_bridge_action() != "restart":
                fails.append("t) explicit config opt-in to 'restart' ignored")
    return fails

def _run_main_fix_with_stale(checks: list, plan="REAL", channel_env=None, ambient_env=None):
    """Drive main() --fix with `checks` in issues; return (stdout, spawns, kills).
    plan="REAL" resolves the real plan (interpreter stubbed, channel env/ambient injectable)."""
    spawned, killed = [], []
    real_run = hc.subprocess.run

    def fake_popen(argv, **kwargs):
        spawned.append(argv)
        return mock.MagicMock()

    def fake_run(argv, *args, **kwargs):
        if isinstance(argv, list) and argv and argv[0] == "/usr/bin/pgrep":
            return subprocess.CompletedProcess(argv, 0, stdout="4242\n", stderr="")
        if isinstance(argv, list) and argv and argv[0] == "/bin/kill":
            killed.append(argv[1])
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        return real_run(argv, *args, **kwargs)

    if plan == "REAL":
        plan_patch = mock.patch.object(hc, "_bridge_interpreter", return_value="/usr/local/bin/python3-probed")
        env_patch = mock.patch.object(hc, "_load_channel_env", return_value=channel_env or {})
        ambient = ambient_env if ambient_env is not None else {
            k: v for k, v in os.environ.items() if k not in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN")}
        os_patch = mock.patch.dict(hc.os.environ, ambient, clear=True)
    else:
        plan_patch = mock.patch.object(hc, "_bridge_launch_plan", return_value=plan)
        env_patch = mock.patch.object(hc, "_load_channel_env", return_value={})
        os_patch = mock.patch.dict(hc.os.environ, dict(os.environ), clear=True)

    captured = io.StringIO()
    with tempfile.TemporaryDirectory() as td:
        with mock.patch.object(sys, "argv", ["health-check.py", "--fix"]), \
             mock.patch.object(hc, "WORKSPACE_DIR", Path(td)), \
             mock.patch.object(hc, "run_all_checks", return_value=checks), \
             mock.patch.object(hc, "fix_down_bridges", return_value=[]), \
             mock.patch.object(hc, "token_from_vault", return_value=""), \
             plan_patch, env_patch, os_patch, \
             mock.patch.object(hc.subprocess, "run", side_effect=fake_run), \
             mock.patch.object(hc.subprocess, "Popen", side_effect=fake_popen), \
             mock.patch("time.sleep", lambda *_: None):
            try:
                with redirect_stdout(captured):
                    hc.main()
            except SystemExit:
                pass
    return captured.getvalue(), spawned, killed


def case_o_stale_restart_uses_launch_plan() -> list[str]:
    """Stale relaunch goes through the shared plan (probed interpreter +
    channel env), never bare sys.executable."""
    fails = []
    checks = [check("slack-bridge", "stale", "running but code is 99 min newer than process — restart needed")]
    plan = ("/usr/local/bin/python3-probed", dict(os.environ))
    out, spawned, killed = _run_main_fix_with_stale(checks, plan)
    if "slack-bridge: restarted (stale code)" not in out:
        fails.append(f"o) missing 'restarted (stale code)' line; got: {out!r}")
    if killed != ["4242"]:
        fails.append(f"o) expected old pid 4242 killed, got {killed}")
    if len(spawned) != 1 or spawned[0][0] != "/usr/local/bin/python3-probed":
        fails.append(f"o) spawn must use the plan's interpreter, got {spawned}")
    if any(argv[0] == sys.executable for argv in spawned):
        fails.append("o) stale restart still used sys.executable")
    return fails


def case_p_stale_no_plan_skips_without_kill() -> list[str]:
    """No viable plan leaves the stale bridge RUNNING — no kill, no spawn
    (a kill with a failed relaunch turns a warning into an outage)."""
    fails = []
    checks = [check("slack-bridge", "stale", "running but code is 99 min newer than process — restart needed")]
    out, spawned, killed = _run_main_fix_with_stale(checks, plan=None)
    if killed:
        fails.append(f"p) killed a stale bridge with no relaunch plan: {killed}")
    if spawned:
        fails.append(f"p) spawned despite missing plan: {spawned}")
    if "restart skipped" not in out:
        fails.append(f"p) missing skip message; got: {out!r}")
    return fails


def case_q_down_path_requires_both_slack_tokens() -> list[str]:
    """Down path: a missing OR present-but-empty app token must skip the
    slack restart (the bridge treats empty strings as missing)."""
    fails = []
    checks = [check("slack-bridge", "warn", "configured but not running")]
    clean_env = {k: v for k, v in os.environ.items() if k not in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN")}
    for label, env in (("absent", {"SLACK_BOT_TOKEN": "xoxb-only"}),
                       ("empty", {"SLACK_BOT_TOKEN": "xoxb-ok", "SLACK_APP_TOKEN": ""})):
        spawned = []
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(hc, "WORKSPACE_DIR", Path(td)), \
                 mock.patch.object(hc, "_bridge_interpreter", return_value="python3"), \
                 mock.patch.object(hc, "_load_channel_env", return_value=env), \
                 mock.patch.object(hc, "token_from_vault", return_value=""), \
                 mock.patch.dict(hc.os.environ, clean_env, clear=True), \
                 mock.patch.object(hc.subprocess, "Popen", side_effect=lambda *a, **k: spawned.append(a) or mock.MagicMock()):
                # The guard and alert path also shell out, so this stub records
                # them too: an unstubbed layer would read as "slack was launched".
                restarted = hc.fix_down_bridges(
                    checks, guard=lambda repo: (True, "test"),
                    sender=lambda msg: True, notifier=lambda msg: True)
        if restarted or spawned:
            fails.append(f"q) {label}-app-token slack was launched anyway: {restarted or spawned}")
    return fails


def case_r_stale_missing_app_token_no_kill_no_spawn() -> list[str]:
    """Stale path with the REAL plan: a missing OR present-but-empty app token
    must leave the running stale bridge alone — no kill, no spawn."""
    fails = []
    checks = [check("slack-bridge", "stale", "running but code is 99 min newer than process — restart needed")]
    for label, env in (("absent", {"SLACK_BOT_TOKEN": "xoxb-only"}),
                       ("empty", {"SLACK_BOT_TOKEN": "xoxb-ok", "SLACK_APP_TOKEN": ""})):
        out, spawned, killed = _run_main_fix_with_stale(checks, plan="REAL", channel_env=env)
        if killed:
            fails.append(f"r) {label}: killed the stale bridge: {killed}")
        if spawned:
            fails.append(f"r) {label}: spawned anyway: {spawned}")
        if "restart skipped" not in out:
            fails.append(f"r) {label}: missing skip message; got: {out!r}")
    return fails


def case_s_vault_only_slack_tokens_launchable() -> list[str]:
    """Vault-only slack install (tokens only in the Keychain vault, injected
    here) must yield a launchable plan with no secrets embedded in the env."""
    fails = []
    clean_env = {k: v for k, v in os.environ.items() if k not in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN")}
    fake_vault = {"SLACK_BOT_TOKEN": "xoxb-vault-secret", "SLACK_APP_TOKEN": "xapp-vault-secret"}
    with mock.patch.object(hc, "_bridge_interpreter", return_value="/usr/fake/python3-probed"), \
         mock.patch.object(hc, "_load_channel_env", return_value={}), \
         mock.patch.object(hc, "token_from_vault", side_effect=lambda v: fake_vault.get(v, "")), \
         mock.patch.dict(hc.os.environ, clean_env, clear=True):
        plan = hc._bridge_launch_plan("slack-bridge")
    if plan is None:
        fails.append("s) vault-only slack install yields launch_plan=None (bridge left down)")
    else:
        interp, child_env = plan
        if interp != "/usr/fake/python3-probed":
            fails.append(f"s) plan interpreter wrong: {interp!r}")
        leaked = [k for k, v in child_env.items() if v in fake_vault.values()]
        if leaked:
            fails.append(f"s) vault secret embedded in child env under {leaked}")

    # One vault token alone is not enough — the gate still requires BOTH.
    with mock.patch.object(hc, "_bridge_interpreter", return_value="/usr/fake/python3-probed"), \
         mock.patch.object(hc, "_load_channel_env", return_value={}), \
         mock.patch.object(hc, "token_from_vault", side_effect=lambda v: fake_vault.get(v, "") if v == "SLACK_BOT_TOKEN" else ""), \
         mock.patch.dict(hc.os.environ, clean_env, clear=True):
        plan = hc._bridge_launch_plan("slack-bridge")
    if plan is not None:
        fails.append("s) bot-token-only vault still produced a plan")
    return fails


def case_t_interpreter_probe_never_execs_bare_python3() -> list[str]:
    """The bare `python3` candidate never reaches subprocess execution: it is
    substituted via _resolved_bare_python3, and dropped when unresolvable."""
    fails = []
    executed = []

    def fake_run(argv, **kwargs):
        executed.append(argv[0])
        return subprocess.CompletedProcess(argv, 1, stdout=b"", stderr=b"")

    # (1) substitute resolves -> the RESOLVED path is probed, never "python3".
    with mock.patch.object(hc, "_BRIDGE_INTERP_CANDIDATES", ["/nonexistent/python3-missing", "python3"]), \
         mock.patch.object(hc, "_resolved_bare_python3", return_value="/usr/fake/python3-resolved"), \
         mock.patch.object(hc.shutil, "which", side_effect=lambda c: c if "resolved" in c else None), \
         mock.patch.object(hc.subprocess, "run", side_effect=fake_run):
        hc._bridge_interpreter("slack-bridge")
    if "python3" in executed:
        fails.append(f"t) bare python3 was executed: {executed}")
    if "/usr/fake/python3-resolved" not in executed:
        fails.append(f"t) resolved substitute not probed: {executed}")

    # (2) substitute unresolvable -> candidate dropped, nothing executed.
    executed.clear()
    with mock.patch.object(hc, "_BRIDGE_INTERP_CANDIDATES", ["python3"]), \
         mock.patch.object(hc, "_resolved_bare_python3", return_value=None), \
         mock.patch.object(hc.shutil, "which", side_effect=lambda c: c), \
         mock.patch.object(hc.subprocess, "run", side_effect=fake_run):
        got = hc._bridge_interpreter("discord-bridge")
    if executed:
        fails.append(f"t) dropped bare candidate still executed: {executed}")
    if got is not None:
        fails.append(f"t) expected None with only an unresolvable bare candidate, got {got!r}")

    # (3) the shipped candidate list carries no bare token other than the one
    # the loop substitutes — a new bare entry would dodge the substitution.
    bare = [c for c in hc._BRIDGE_INTERP_CANDIDATES if not c.startswith("/")]
    if bare != ["python3"]:
        fails.append(f"t) unexpected bare candidates in shipped list: {bare}")
    return fails


def case_u_resolved_bare_python3_policy() -> list[str]:
    """_resolved_bare_python3 walks startup.sh's order ($SUTANDO_PY, bundled,
    PATH) and refuses a system-bin python3 unless the developer tools exist."""
    fails = []
    clean_env = {k: v for k, v in os.environ.items() if k != "SUTANDO_PY"}
    system_stub = os.path.join("/usr", "bin", "python3")  # assembled: literal is review-flagged
    with tempfile.TemporaryDirectory() as td:
        fake_repo = Path(td) / "repo"
        fake_repo.mkdir()

        # (1) $SUTANDO_PY override wins.
        py = Path(td) / "custom-python3"
        py.write_text("#!/bin/sh\n")
        py.chmod(0o755)
        with mock.patch.dict(hc.os.environ, {**clean_env, "SUTANDO_PY": str(py)}, clear=True), \
             mock.patch.object(hc, "REPO_DIR", fake_repo):
            got = hc._resolved_bare_python3()
        if got != str(py):
            fails.append(f"u) SUTANDO_PY override ignored: {got!r}")

        # (2) system-bin python3 without developer tools -> refused (the stub).
        with mock.patch.dict(hc.os.environ, clean_env, clear=True), \
             mock.patch.object(hc, "REPO_DIR", fake_repo), \
             mock.patch.object(hc.sys, "platform", "darwin"), \
             mock.patch.object(hc.shutil, "which", return_value=system_stub), \
             mock.patch.object(hc, "developer_tools_installed", return_value=False):
            got = hc._resolved_bare_python3()
        if got is not None:
            fails.append(f"u) system stub returned without developer tools: {got!r}")

        # (3) same location WITH the tools -> real interpreter, accepted.
        with mock.patch.dict(hc.os.environ, clean_env, clear=True), \
             mock.patch.object(hc, "REPO_DIR", fake_repo), \
             mock.patch.object(hc.sys, "platform", "darwin"), \
             mock.patch.object(hc.shutil, "which", return_value=system_stub), \
             mock.patch.object(hc, "developer_tools_installed", return_value=True):
            got = hc._resolved_bare_python3()
        if got != system_stub:
            fails.append(f"u) system python3 refused despite developer tools: {got!r}")

        # (4) non-system PATH python3 (Homebrew/pyenv/...) -> accepted as-is.
        with mock.patch.dict(hc.os.environ, clean_env, clear=True), \
             mock.patch.object(hc, "REPO_DIR", fake_repo), \
             mock.patch.object(hc.sys, "platform", "darwin"), \
             mock.patch.object(hc.shutil, "which", return_value="/usr/fake/bin/python3"):
            got = hc._resolved_bare_python3()
        if got != "/usr/fake/bin/python3":
            fails.append(f"u) non-system PATH python3 not accepted: {got!r}")

        # (5) bundled runtime interpreter beside the repo wins over PATH.
        bundled = Path(td) / "runtime" / "python" / "bin" / "python3"
        bundled.parent.mkdir(parents=True, exist_ok=True)
        bundled.write_text("#!/bin/sh\n")
        bundled.chmod(0o755)
        with mock.patch.dict(hc.os.environ, clean_env, clear=True), \
             mock.patch.object(hc, "REPO_DIR", fake_repo), \
             mock.patch.object(hc.shutil, "which", return_value="/usr/fake/bin/python3"):
            got = hc._resolved_bare_python3()
        if got != str(bundled):
            fails.append(f"u) bundled runtime interpreter not preferred: {got!r}")
        bundled.unlink()

        # (6) nothing on PATH and no bundled runtime -> None (candidate dropped).
        with mock.patch.dict(hc.os.environ, clean_env, clear=True), \
             mock.patch.object(hc, "REPO_DIR", fake_repo), \
             mock.patch.object(hc.shutil, "which", return_value=None):
            got = hc._resolved_bare_python3()
        if got is not None:
            fails.append(f"u) expected None with no runnable python3, got {got!r}")

        # (7) non-darwin: no CLT stub exists, PATH python3 accepted directly.
        with mock.patch.dict(hc.os.environ, clean_env, clear=True), \
             mock.patch.object(hc, "REPO_DIR", fake_repo), \
             mock.patch.object(hc.sys, "platform", "linux"), \
             mock.patch.object(hc.shutil, "which", return_value="/usr/fake/bin/python3"):
            got = hc._resolved_bare_python3()
        if got != "/usr/fake/bin/python3":
            fails.append(f"u) non-darwin PATH python3 not accepted: {got!r}")
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
                 case_x_failed_sender_falls_back_to_a_local_owner_surface,
                 case_o_stale_restart_uses_launch_plan,
                 case_p_stale_no_plan_skips_without_kill,
                 case_q_down_path_requires_both_slack_tokens,
                 case_r_stale_missing_app_token_no_kill_no_spawn,
                 case_s_vault_only_slack_tokens_launchable,
                 case_t_interpreter_probe_never_execs_bare_python3,
                 case_u_resolved_bare_python3_policy,
                 case_y_gateway_bridge_down_is_restarted,
                 case_z_gateway_other_warns_never_respawn,
                 case_aa_gateway_plan_requires_a_token,
                 case_ab_ag2_remote_token_alias_is_accepted,
                 case_ac_unknown_name_without_detail_is_untouched):
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
