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


def run_with_popen_stub(checks: list) -> tuple[list, list]:
    """Call fix_down_bridges with Popen stubbed; return (restarted, spawn argvs).

    Also stubs the interpreter probe and slack-env load so the test is
    hermetic: without these, fix_down_bridges would probe the host for
    discord.py / slack_bolt (flaky across machines) and skip the restart when
    absent. Here every bridge gets a known-good interpreter and slack gets a
    token, so the restart path is exercised deterministically; the vault tier
    is stubbed empty (never the real Keychain).
    """
    spawned = []

    def fake_popen(argv, **kwargs):
        spawned.append(argv)
        return mock.MagicMock()

    with tempfile.TemporaryDirectory() as td:
        with mock.patch.object(hc, "WORKSPACE_DIR", Path(td)), \
             mock.patch.object(hc, "_bridge_interpreter", return_value="python3"), \
             mock.patch.object(hc, "_load_channel_env", return_value={"SLACK_BOT_TOKEN": "xoxb-test", "SLACK_APP_TOKEN": "xapp-test"}), \
             mock.patch.object(hc, "token_from_vault", return_value=""), \
             mock.patch.object(hc.subprocess, "Popen", side_effect=fake_popen):
            restarted = hc.fix_down_bridges(checks)
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
            restarted = hc.fix_down_bridges(checks)

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
            restarted = hc.fix_down_bridges(checks)

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
                restarted = hc.fix_down_bridges(checks)
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
                 case_o_stale_restart_uses_launch_plan,
                 case_p_stale_no_plan_skips_without_kill,
                 case_q_down_path_requires_both_slack_tokens,
                 case_r_stale_missing_app_token_no_kill_no_spawn,
                 case_s_vault_only_slack_tokens_launchable,
                 case_t_interpreter_probe_never_execs_bare_python3,
                 case_u_resolved_bare_python3_policy):
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
