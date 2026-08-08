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
    return fails


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
            # Core FIRST, 'other' LAST: tmux's implicit target is the most
            # recent session, so an untargeted `setenv -u` clears 'other' and
            # leaves the core pinned. Reversing this order makes the case pass
            # even with the bug present.
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
            }
            # The script may exec `tmux attach` and block; that is fine. Kill the
            # whole group after the clear has had its chance to run.
            proc = subprocess.Popen(
                ["/bin/bash", str(SCRIPT)], env=env, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, start_new_session=True)
            try:
                out = proc.communicate(timeout=45)[0] or ""
            except subprocess.TimeoutExpired:
                out = ""
            finally:
                # start_new_session makes the child its own group leader, so the
                # pgid IS proc.pid -- no getpgid, which could race a fast exit.
                # Unconditional: the script spawns background monitors that
                # outlive a clean exit and must not leak onto a dev machine.
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                try:
                    out = (out or "") + (proc.communicate(timeout=10)[0] or "")
                except (subprocess.TimeoutExpired, ValueError):
                    pass
                proc.wait(timeout=10)
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


def main() -> int:
    cases = [
        ("default", case_default_no_model_flag),
        ("env-ignored", case_env_set_is_ignored),
        ("tmux-clear", case_tmux_defaults_clear_both_scopes),
        ("tmux-launch", case_tmux_launch_clears_a_pinned_socket),
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
