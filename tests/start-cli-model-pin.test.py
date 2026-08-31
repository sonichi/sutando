#!/usr/bin/env python3
"""start-cli.sh must ignore $SUTANDO_CORE_MODEL and clear it from every
tmux session scope, not just tmux's default target."""
from __future__ import annotations
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "src" / "agent" / "claude" / "cli" / "start-cli.sh"


def _isolated_workspace(td: Path) -> str:
    """A redirected workspace needs its state/ dir: start-cli.sh writes
    <ws>/state/core-supervisor-relay-loop.pid and aborts under set -e without it."""
    ws = td / "workspace"
    (ws / "state").mkdir(parents=True, exist_ok=True)
    return str(ws)


def _launch_argv(model_env: "str | None") -> list[str]:
    """Run start-cli.sh through its no-tmux fallback and return claude's argv."""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        bind = td / "bin"
        bind.mkdir()
        args_file = td / "argv.txt"

        # Stub claude: record argv (one per line), exit 0.
        claude = bind / "claude"
        claude.write_text('#!/bin/bash\nprintf "%s\\n" "$@" > "$ARGS_FILE"\n')
        claude.chmod(0o755)
        # Stub pgrep: always "no match" so the already-running guard passes.
        pgrep = bind / "pgrep"
        pgrep.write_text("#!/bin/bash\nexit 1\n")
        pgrep.chmod(0o755)

        env = {
            # /usr/bin excluded intentionally: tmux lives there on Ubuntu CI
            # and the test targets the no-tmux fallback exec path.
            "PATH": f"{bind}:/bin",
            "HOME": str(td),
            "ARGS_FILE": str(args_file),
            # start-cli.sh stamps <workspace>/state/session-starts.log; without
            # this redirect the run appends a fake launch to the LIVE workspace.
            "SUTANDO_TEST_MODE": "1",
            "SUTANDO_WORKSPACE": _isolated_workspace(td),
        }
        if model_env is not None:
            env["SUTANDO_CORE_MODEL"] = model_env

        subprocess.run(
            ["/bin/bash", str(SCRIPT)],
            env=env, capture_output=True, text=True, timeout=30,
        )
        if not args_file.exists():
            return []
        return [ln for ln in args_file.read_text().splitlines() if ln != ""]


def case_default_no_model_flag() -> list[str]:
    fails = []
    argv = _launch_argv(None)
    if not argv:
        return ["default) claude was never exec'd (fallback path didn't run)"]
    if "--model" in argv:
        fails.append(f"default) unexpected --model in argv (1M should stay default): {argv}")
    if "--name" not in argv or "sutando-core" not in argv:
        fails.append(f"default) sanity: expected --name sutando-core, got {argv}")
    return fails


def case_env_set_is_ignored() -> list[str]:
    """A pin in the environment must NOT reach claude's argv."""
    fails = []
    argv = _launch_argv("opus")
    if not argv:
        return ["set) claude was never exec'd (fallback path didn't run)"]
    if "--model" in argv:
        fails.append(f"set) a stale SUTANDO_CORE_MODEL pin still reached claude: {argv}")
    if "opus" in argv:
        fails.append(f"set) the pinned value leaked into argv: {argv}")
    return fails


def case_tmux_defaults_clear_both_scopes() -> list[str]:
    """Both tmux scopes must be cleared; -g is invisible to a per-session query.
    Static so it still runs where tmux is absent, rather than skipping."""
    src = SCRIPT.read_text()
    # Comments discuss the removed flag by name, so scan CODE only — otherwise the
    # explanation of the removal trips the guard against the thing it removed.
    code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
    fails = []
    if "setenv -gu SUTANDO_CORE_MODEL" not in code:
        fails.append("clear) apply_tmux_defaults must clear the GLOBAL scope (setenv -gu)")
    # TARGETED per session: an untargeted `setenv -u` clears tmux's default
    # session, which on a multi-session socket is not the core's.
    if 'setenv -t "=$_pin_sess" -u SUTANDO_CORE_MODEL' not in code:
        fails.append("clear) the session clear must target each session with -t, not tmux's default")
    if "list-sessions -F '#{session_name}'" not in code:
        fails.append("clear) must enumerate sessions to clear each one")
    # The pass-through must be gone, not merely bypassed.
    if "MODEL_ARGS" in code:
        fails.append("clear) MODEL_ARGS is back — an empty array is one edit from a pin")
    if "--model" in code:
        fails.append("clear) start-cli.sh reintroduced a --model flag")
    # A server takes its global env from whoever starts it, so the launcher's own
    # unset must precede any tmux call. Behaviourally covered by fresh-socket.
    lines = code.splitlines()
    unset_at = next((i for i, l in enumerate(lines) if "unset SUTANDO_CORE_MODEL" in l), None)
    tmux_at = next((i for i, l in enumerate(lines) if "tmux -S" in l), None)
    if unset_at is None:
        fails.append("clear) the launcher must unset SUTANDO_CORE_MODEL from its own env")
    elif tmux_at is not None and unset_at > tmux_at:
        fails.append(f"clear) unset is at code line {unset_at} but a tmux call is at "
                     f"{tmux_at} — a server started first inherits the pin")
    return fails


def _run_launcher(env: dict, log: Path, timeout: int = 45) -> "tuple[str, bool]":
    """Run start-cli.sh capturing to a FILE, not a pipe. communicate() waits for EOF,
    so any backgrounded descendant inheriting stdout blocks it until timeout."""
    import signal
    with open(log, "w") as fh:
        proc = subprocess.Popen(
            ["/bin/bash", str(SCRIPT)], env=env, stdin=subprocess.DEVNULL,
            stdout=fh, stderr=subprocess.STDOUT, text=True, start_new_session=True)
        timed_out = False
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
        finally:
            # pgid IS proc.pid (start_new_session). Unconditional: spawned
            # monitors outlive a clean exit.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
    return (log.read_text() if log.exists() else ""), timed_out


def case_tmux_launch_clears_a_pinned_socket() -> list[str]:
    """Drives the real script on its tmux path (scratch socket, never the live
    core's): proves the clear RUNS, where the static case only proves it exists."""
    import shutil
    import signal
    tmux = shutil.which("tmux", path="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin")
    if not tmux:
        return ["tmux-launch) tmux not found — cannot exercise the tmux path"]
    fails = []
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        bind = td / "bin"
        bind.mkdir()
        (td / "home").mkdir()
        sock = td / "scratch-tmux.sock"
        # pgrep exits 0 = "already running": skips ensure_core_monitor's
        # core-input-watch.py spawn, which outlived the temp dir and held the pipe.
        for stub, body in (("claude", "exit 0\n"), ("pgrep", "exit 0\n")):
            f = bind / stub
            f.write_text("#!/bin/bash\n" + body)
            f.chmod(0o755)

        def tm(*args, **kw):
            return subprocess.run([tmux, "-S", str(sock), *args],
                                  capture_output=True, text=True, **kw)

        try:
            # Core FIRST so tmux's implicit target is 'other': reversing this
            # makes the case pass even with an untargeted clear.
            tm("new-session", "-d", "-s", "sutando-core",
               "-e", "SUTANDO_CORE_MODEL=opus", "sleep 300")
            tm("new-session", "-d", "-s", "other", "sleep 300")
            tm("setenv", "-g", "SUTANDO_CORE_MODEL", "opus")
            if "SUTANDO_CORE_MODEL=opus" not in tm("show-environment", "-g",
                                                   "SUTANDO_CORE_MODEL").stdout:
                return ["tmux-launch) could not stage the pin — fixture is unrepresentative"]

            env = {
                "PATH": f"{bind}:{Path(tmux).parent}:/usr/bin:/bin:/usr/sbin",
                "HOME": str(td / "home"),
                "SUTANDO_TMUX_SOCKET": str(sock),
                "SUTANDO_TEST_MODE": "1",
                "SUTANDO_WORKSPACE": _isolated_workspace(td),
            }
            # The script may exec `tmux attach` and block; that is fine. Kill the
            # whole group after the clear has had its chance to run.
            out, timed_out = _run_launcher(env, td / "launcher.log")
            if timed_out:
                return ["tmux-launch) launcher did not exit within the timeout"]
            if "tmux not found" in out:
                return ["tmux-launch) script skipped its tmux path — fixture never reached the clear"]

            for scope_args, label in ((["-g"], "global"),
                                      (["-t", "=sutando-core"], "session")):
                got = tm("show-environment", *scope_args, "SUTANDO_CORE_MODEL").stdout
                if "SUTANDO_CORE_MODEL=opus" in got:
                    fails.append(f"tmux-launch) {label} scope still pinned after launch: {got.strip()!r}")
            leaked = subprocess.run(
                ["/bin/sh", "-c", f"ps -Ao args= | grep -c '[c]ore-input-watch.py .*{sock}'"],
                capture_output=True, text=True).stdout.strip()
            if leaked not in ("0", ""):
                fails.append(f"tmux-launch) leaked {leaked} core-input-watch monitor(s) on the scratch socket")
        finally:
            tm("kill-server")
    return fails


def case_fresh_socket_server_is_born_unpinned() -> list[str]:
    """The serverless case, entered rather than described: with no server on the
    socket the setenv clears reach nothing, so only the launcher's own unset works."""
    import shutil
    import signal
    tmux = shutil.which("tmux", path="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin")
    if not tmux:
        return ["fresh-socket) tmux not found — cannot exercise the tmux path"]
    fails = []
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        bind = td / "bin"
        bind.mkdir()
        (td / "home").mkdir()
        sock = td / "fresh-tmux.sock"
        # claude must PERSIST: an exiting stub ends the session and the server with
        # it, leaving nothing to inspect. pgrep 0 = "running", skips the monitor.
        for stub, body in (("claude", "sleep 300\n"), ("pgrep", "exit 0\n")):
            f = bind / stub
            f.write_text("#!/bin/bash\n" + body)
            f.chmod(0o755)

        def tm(*args):
            return subprocess.run([tmux, "-S", str(sock), *args],
                                  capture_output=True, text=True)

        try:
            if tm("list-sessions").returncode == 0:
                return ["fresh-socket) a server already exists — this is not the fresh case"]
            env = {
                "PATH": f"{bind}:{Path(tmux).parent}:/usr/bin:/bin:/usr/sbin",
                "HOME": str(td / "home"),
                "SUTANDO_TMUX_SOCKET": str(sock),
                "SUTANDO_TEST_MODE": "1",
                "SUTANDO_WORKSPACE": _isolated_workspace(td),
                "SUTANDO_CORE_MODEL": "opus",   # the pin is in the LAUNCHER's env
            }
            out, timed_out = _run_launcher(env, td / "launcher.log")
            if timed_out:
                return ["fresh-socket) launcher did not exit within the timeout"]
            if "tmux not found" in out:
                return ["fresh-socket) script skipped its tmux path — never reached the create"]
            if tm("list-sessions").returncode != 0:
                return ["fresh-socket) no server after launch — the checks below would "
                        "pass vacuously"]
            for scope_args, label in ((["-g"], "global"),
                                      (["-t", "=sutando-core"], "session")):
                got = tm("show-environment", *scope_args, "SUTANDO_CORE_MODEL").stdout
                if "SUTANDO_CORE_MODEL=opus" in got:
                    fails.append(f"fresh-socket) the new server was born pinned in "
                                 f"{label} scope: {got.strip()!r}")
            for pid in tm("list-panes", "-s", "-a", "-F", "#{pane_pid}").stdout.split():
                argv = subprocess.run(["ps", "-o", "args=", "-p", pid],
                                      capture_output=True, text=True).stdout
                if "--model" in argv:
                    fails.append(f"fresh-socket) a stale pin reached the launched "
                                 f"argv: {argv.strip()!r}")
        finally:
            tm("kill-server")
    return fails


def case_bare_invocation_still_warns_on_a_pinned_live_core() -> list[str]:
    """The upgrade trap end to end: new launcher, OLD pinned process already running.
    The attach path clears tmux env without replacing the process, so health must keep
    warning from the immutable argv rather than going green on a clean tmux."""
    import importlib.util
    import shutil
    import signal
    tmux = shutil.which("tmux", path="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin")
    if not tmux:
        return ["pinned-attach) tmux not found — cannot exercise the attach path"]
    fails = []
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        bind = td / "bin"
        bind.mkdir()
        (td / "home").mkdir()
        sock = td / "attach-tmux.sock"
        stub = bind / "claude"
        stub.write_text("#!/bin/bash\nsleep 300\n")
        stub.chmod(0o755)

        def tm(*args):
            return subprocess.run([tmux, "-S", str(sock), *args],
                                  capture_output=True, text=True)
        try:
            tm("new-session", "-d", "-s", "sutando-core",
               f"{stub} --name sutando-core --model opus")
            pids = tm("list-panes", "-s", "-t", "=sutando-core",
                      "-F", "#{pane_pid}").stdout.split()
            staged = [subprocess.run(["ps", "-o", "args=", "-p", q],
                                     capture_output=True, text=True).stdout for q in pids]
            if not any("--model opus" in a and "--name sutando-core" in a for a in staged):
                return [f"pinned-attach) fixture staged no pinned core: {staged!r}"]
            # core_claude_pids() reads `pgrep -ax claude`; report the staged pid so the
            # script takes its already-running attach path instead of launching.
            pg = bind / "pgrep"
            pg.write_text("#!/bin/bash\necho '%s claude'\n" % pids[0])
            pg.chmod(0o755)
            tm("setenv", "-g", "SUTANDO_CORE_MODEL", "opus")
            tm("setenv", "-t", "=sutando-core", "SUTANDO_CORE_MODEL", "opus")

            env = {
                "PATH": f"{bind}:{Path(tmux).parent}:/usr/bin:/bin:/usr/sbin",
                "HOME": str(td / "home"),
                "SUTANDO_TMUX_SOCKET": str(sock),
                "SUTANDO_TEST_MODE": "1",
                "SUTANDO_WORKSPACE": _isolated_workspace(td),
            }
            out, timed_out = _run_launcher(env, td / "launcher.log")
            if timed_out:
                # A hung launcher is not the same finding as a silent one; the old
                # message printed '' for both and sent the diagnosis the wrong way.
                return [f"pinned-attach) launcher did not exit within the timeout; "
                        f"captured so far: {out.strip()[:160]!r}"]
            if "already running" not in out:
                return [f"pinned-attach) script did not take the attach path: {out.strip()[:200]!r}"]
            for scope, label in ((["-g"], "global"), (["-t", "=sutando-core"], "session")):
                if "SUTANDO_CORE_MODEL=opus" in tm("show-environment", *scope,
                                                   "SUTANDO_CORE_MODEL").stdout:
                    fails.append(f"pinned-attach) {label} scope not cleared by the attach path")
            still = [subprocess.run(["ps", "-o", "args=", "-p", q],
                                    capture_output=True, text=True).stdout for q in pids]
            if not any("--model opus" in a for a in still):
                return ["pinned-attach) the pinned process died — nothing left to detect"]

            spec = importlib.util.spec_from_file_location(
                "hc_attach", REPO / "src" / "health-check.py")
            hc = importlib.util.module_from_spec(spec)
            sys.modules["hc_attach"] = hc
            try:
                spec.loader.exec_module(hc)
            except SystemExit:
                pass
            prev = os.environ.get("SUTANDO_TMUX_SOCKET")
            os.environ["SUTANDO_TMUX_SOCKET"] = str(sock)
            try:
                r = hc.check_core_model_pin()
            finally:
                if prev is None:
                    os.environ.pop("SUTANDO_TMUX_SOCKET", None)
                else:
                    os.environ["SUTANDO_TMUX_SOCKET"] = prev
            if r.get("status") != "warn":
                fails.append(f"pinned-attach) health went GREEN on a pinned live core: {r}")
            elif "opus" not in r.get("detail", ""):
                fails.append(f"pinned-attach) warn does not name the pinned model: {r}")
        finally:
            tm("kill-server")
    return fails


def main() -> int:
    cases = [
        ("default", case_default_no_model_flag),
        ("env-ignored", case_env_set_is_ignored),
        ("tmux-clear", case_tmux_defaults_clear_both_scopes),
        ("tmux-launch", case_tmux_launch_clears_a_pinned_socket),
        ("fresh-socket", case_fresh_socket_server_is_born_unpinned),
        ("pinned-attach", case_bare_invocation_still_warns_on_a_pinned_live_core),
    ]
    all_failures = []
    for label, fn in cases:
        try:
            fails = fn()
        except Exception as e:
            fails = [f"{label}) raised {type(e).__name__}: {e}"]
        if fails:
            all_failures.extend(fails)
            print(f"  ✗ case {label}")
            for f in fails:
                print(f"      {f}")
        else:
            print(f"  ✓ case {label}")
    if all_failures:
        print(f"\n{len(all_failures)} failure(s)")
        return 1
    print("\nstart-cli.sh ignores the model pin and clears it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
