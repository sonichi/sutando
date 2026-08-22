#!/usr/bin/env python3
"""start-cli.sh orphan-adopt must only adopt a core that belongs to THIS
install's tmux socket (sonichi/sutando#2884).

The adopt branch fires when no tmux session exists but a `claude --name
sutando-core` process does. Today the process match is machine-global, so a
DIFFERENT install's core (OSS checkout vs desktop bundle, distinct sockets)
gets falsely adopted: the launcher exits 0 without creating its own session
and every downstream tmux consumer fails with "no server running".

Ownership is judged from the candidate's exec-time environment: tmux stamps
TMUX=<socket>,<pid>,<idx> into every pane child, and the snapshot survives the
tmux server's death — which is exactly the legitimate-orphan case adoption
must keep serving. A core with no socket marker at all is treated as the
default-socket (/tmp) install's, because the no-tmux launch path is the only
way to produce one.
"""
from __future__ import annotations
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "src" / "agent" / "claude" / "cli" / "start-cli.sh"

FAKE_PID = "99999999"  # above pid_max on macOS and default Linux: /proc/<pid> never exists


def _isolated_workspace(td: Path) -> str:
    ws = td / "workspace"
    (ws / "state").mkdir(parents=True, exist_ok=True)
    return str(ws)


def _run(td: Path, *, args_line: str, env_blob: str, socket: "str | None",
         ppid: str = "", parent_args: str = "") -> "tuple[str, bool]":
    """Run start-cli.sh with a staged phantom core and no tmux on PATH.

    Returns (combined output, claude_was_execd). pgrep reports FAKE_PID for the
    core scan and "running" (exit 0, no output) for every -f guard so no
    monitor/relay loops are spawned. ps serves the argv line for plain queries
    and the argv+env blob when the caller asks for the environment (-E / -e).
    """
    bind = td / "bin"
    bind.mkdir(exist_ok=True)
    argv_file = td / "claude-argv.txt"
    argv_file.unlink(missing_ok=True)  # cases share a tempdir across sub-runs

    (bind / "claude").write_text('#!/bin/bash\nprintf "%s\\n" "$@" > "$CLAUDE_ARGV_FILE"\n')
    (bind / "pgrep").write_text(
        "#!/bin/bash\n"
        'case "$*" in\n'
        f'  *"claude"*) echo "{FAKE_PID} claude" ;;\n'
        "  *) exit 0 ;;\n"  # -f guards: pretend running so nothing else spawns
        "esac\n"
    )
    (bind / "ps").write_text(
        "#!/bin/bash\n"
        # Order matters: the env query (-E) also carries command=, and the ppid
        # query carries -p — match the most specific form first. PS_PPID /
        # PS_PARENT_ARGS are only set by the parent-fallback case.
        'case "$*" in\n'
        '  *-E*|*-e\\ *) printf "%s\\n" "$PS_ENV_BLOB" ;;\n'
        '  *ppid=*) printf "%s\\n" "${PS_PPID:-}" ;;\n'
        f'  *"-p {FAKE_PID}"*) printf "%s\\n" "$PS_ARGS_LINE" ;;\n'
        '  *args=*|*command=*) printf "%s\\n" "${PS_PARENT_ARGS:-}" ;;\n'
        "  *) exit 0 ;;\n"
        "esac\n"
    )
    for f in ("claude", "pgrep", "ps"):
        (bind / f).chmod(0o755)

    env = {
        # /usr/bin excluded: no real tmux, so the flow reaches the adopt branch
        # and, when adoption is refused, the no-tmux `exec claude` fallback.
        "PATH": f"{bind}:/bin",
        "HOME": str(td),
        "CLAUDE_ARGV_FILE": str(argv_file),
        "PS_ARGS_LINE": args_line,
        "PS_ENV_BLOB": env_blob,
        "PS_PPID": ppid,
        "PS_PARENT_ARGS": parent_args,
        "SUTANDO_TEST_MODE": "1",
        "SUTANDO_WORKSPACE": _isolated_workspace(td),
    }
    if socket is not None:
        env["SUTANDO_TMUX_SOCKET"] = socket

    proc = subprocess.run(
        ["/bin/bash", str(SCRIPT)],
        env=env, capture_output=True, text=True, timeout=30,
    )
    return proc.stdout + proc.stderr, argv_file.exists()


CORE_ARGV = "claude --name sutando-core --remote-control Sutando --dangerously-skip-permissions"


def case_foreign_socket_core_is_not_adopted() -> list[str]:
    """A core whose env names ANOTHER install's socket must not be adopted;
    the launcher must say whose it is and launch its own core."""
    fails = []
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        out, launched = _run(
            td,
            args_line=CORE_ARGV,
            env_blob=(CORE_ARGV
                      + " TMUX=/tmp/foreign-install-tmux.sock,4242,0"
                      + " SUTANDO_TMUX_SOCKET=/tmp/foreign-install-tmux.sock"),
            socket=str(td / "private" / "tmux.sock"),
        )
        if "reusing it" in out:
            fails.append(f"foreign) adopted a different install's core: {out.strip()[:200]!r}")
        if "another install" not in out:
            fails.append("foreign) refusal must name the cause (expected 'another install' in output)")
        if not launched:
            fails.append("foreign) launcher never started its own core after refusing the foreign one")
    return fails


def case_own_orphan_is_still_adopted() -> list[str]:
    """The legitimate case: OUR socket's core whose tmux server died. The env
    snapshot still names our socket; adoption must keep working."""
    fails = []
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        sock = str(td / "private" / "tmux.sock")
        out, launched = _run(
            td,
            args_line=CORE_ARGV,
            env_blob=CORE_ARGV + f" TMUX={sock},4242,0",
            socket=sock,
        )
        if "reusing it" not in out:
            fails.append(f"own-orphan) our own orphan was not adopted: {out.strip()[:200]!r}")
        if launched:
            fails.append("own-orphan) a second core was launched next to our adoptable orphan")
    return fails


def case_markerless_stray_not_adopted_on_private_socket() -> list[str]:
    """A core with no socket marker (legacy / launched outside tmux) belongs to
    the default-socket install; a private-socket launcher must not adopt it
    (issue #2884 fix shape, point 2)."""
    fails = []
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        out, launched = _run(
            td,
            args_line=CORE_ARGV,
            env_blob=CORE_ARGV,  # argv only: no TMUX=, no SUTANDO_TMUX_SOCKET=
            socket=str(td / "private" / "tmux.sock"),
        )
        if "reusing it" in out:
            fails.append(f"markerless-private) adopted a default-socket stray: {out.strip()[:200]!r}")
        if not launched:
            fails.append("markerless-private) launcher never started its own core")
    return fails


def case_markerless_stray_adopted_on_default_socket() -> list[str]:
    """Same stray, but the launcher IS the default-socket install: this is the
    pre-existing single-install contract and must keep adopting."""
    fails = []
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        out, launched = _run(
            td,
            args_line=CORE_ARGV,
            env_blob=CORE_ARGV,
            socket=None,  # default /tmp/sutando-tmux.sock
        )
        if "reusing it" not in out:
            fails.append(f"markerless-default) default-socket stray no longer adopted: {out.strip()[:200]!r}")
        if launched:
            fails.append("markerless-default) a second core was launched next to the adoptable stray")
    return fails


def case_private_twin_spelling_still_matches() -> list[str]:
    """macOS spells /tmp paths as /private/tmp in env markers; a socket that
    differs only by the /private prefix is the SAME socket. Also exercises a
    marker whose value contains spaces (app-support paths do)."""
    fails = []
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        out, launched = _run(
            td,
            args_line=CORE_ARGV,
            env_blob=CORE_ARGV + " TMUX=/private/tmp/twin-test-tmux.sock,7,0",
            socket="/tmp/twin-test-tmux.sock",
        )
        if "reusing it" not in out:
            fails.append(f"twin) /private twin spelling not recognized as ours: {out.strip()[:200]!r}")
        if launched:
            fails.append("twin) launched a second core over our own twin-spelled orphan")

        spaced = str(td / "App Support" / "run" / "tmux.sock")
        out, launched = _run(
            td,
            args_line=CORE_ARGV,
            env_blob=CORE_ARGV + f" TMUX={spaced},7,0 NEXT_VAR=1",
            socket=spaced,
        )
        if "reusing it" not in out:
            fails.append(f"twin) spaces-in-path socket not recognized as ours: {out.strip()[:200]!r}")
    return fails


def case_parent_tmux_server_decides_when_env_is_hidden() -> list[str]:
    """SIP hides procargs for some processes: a markerless blob must fall back
    to the parent — a live pane child's parent is the tmux server whose argv
    names its socket."""
    fails = []
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        sock = str(td / "private" / "tmux.sock")
        out, launched = _run(
            td,
            args_line=CORE_ARGV,
            env_blob=CORE_ARGV,  # markerless: env unreadable / hidden
            socket=sock,
            ppid="424242",
            parent_args="tmux -S /tmp/foreign-install-tmux.sock new-session -d -s sutando-core claude",
        )
        if "reusing it" in out:
            fails.append(f"parent) adopted a pane of a FOREIGN tmux server: {out.strip()[:200]!r}")
        if not launched:
            fails.append("parent) launcher never started its own core after refusing the foreign pane")

        out, launched = _run(
            td,
            args_line=CORE_ARGV,
            env_blob=CORE_ARGV,
            socket=sock,
            ppid="424242",
            parent_args=f"tmux -S {sock} new-session -d -s sutando-core claude",
        )
        if "reusing it" not in out:
            fails.append(f"parent) our own tmux server's pane was not adopted: {out.strip()[:200]!r}")
        if launched:
            fails.append("parent) launched a second core over our own live pane")
    return fails


def main() -> int:
    cases = [
        ("foreign-not-adopted", case_foreign_socket_core_is_not_adopted),
        ("own-orphan-adopted", case_own_orphan_is_still_adopted),
        ("markerless-private", case_markerless_stray_not_adopted_on_private_socket),
        ("markerless-default", case_markerless_stray_adopted_on_default_socket),
        ("private-twin", case_private_twin_spelling_still_matches),
        ("parent-fallback", case_parent_tmux_server_decides_when_env_is_hidden),
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
    print("\nstart-cli.sh adopts only cores that belong to its own socket.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
