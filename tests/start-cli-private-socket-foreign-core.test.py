#!/usr/bin/env python3
"""start-cli.sh must not adopt a `claude --name sutando-core` that lives outside
its own private tmux socket.

Desktop installs run the core on a private socket (SUTANDO_TMUX_SOCKET). The
core lookup was machine-wide (`pgrep -ax claude`), so a stray or foreign
same-name core made start-cli print "already running (no tmux session) —
reusing it" and exit 0 without ever starting a server on that socket; every
later `tmux -S <socket> new-window` then failed with "error connecting to
<socket>". The same lookup also let a foreign core make an existing-but-empty
session on the socket read as "already running".

Harness: PATH-shimmed tmux/pgrep/ps/claude plus a SUTANDO_PY that no-ops the core daemons, so no real
tmux server, core, heartbeat or monitor is touched. Markers:
  SESS_MARK  a session exists on the socket
  CORE_MARK  a `claude --name sutando-core` process exists somewhere
  PANE_MARK  that process runs inside a pane of the socket's server
Every tmux call (minus `-S <socket>`) is appended to TMUX_LOG.

Run:  python3 tests/start-cli-private-socket-foreign-core.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "src" / "agent" / "claude" / "cli" / "start-cli.sh"
FAKEPID = "999999"

TMUX_STUB = r"""#!/bin/bash
while [ "$1" = "-S" ]; do shift 2; done
sub="$1"; shift
echo "$sub $*" >> "$TMUX_LOG"
case "$sub" in
  has-session) [ -f "$SESS_MARK" ] && exit 0 || exit 1 ;;
  new-session) touch "$SESS_MARK"; exit 0 ;;
  new-window) echo 0; exit 0 ;;
  list-panes)
    [ -f "$SESS_MARK" ] || exit 1
    if [ -f "$PANE_MARK" ]; then
      case "$*" in
        *-a*) echo "%(pid)s" ;;
        *) echo "0 %%0 %(pid)s" ;;
      esac
    fi
    exit 0 ;;
  *) exit 0 ;;
esac
""" % {"pid": FAKEPID}

PGREP_STUB = r"""#!/bin/bash
case "$*" in *core-input-watch*) exit 0 ;; esac
case "$*" in
  *claude*) if [ -f "$CORE_MARK" ]; then echo "%s claude --name sutando-core"; exit 0; fi; exit 1 ;;
esac
exit 1
""" % FAKEPID

PS_STUB = r"""#!/bin/bash
want=""; prev=""
for a in "$@"; do [ "$prev" = "-p" ] && want="$a"; prev="$a"; done
case "$*" in
  *ppid=*) echo 1; exit 0 ;;
esac
if [ "$want" = "%s" ] && [ -f "$CORE_MARK" ]; then echo "claude --name sutando-core"; fi
exit 0
""" % FAKEPID

# Real interpreter for config resolution; long-lived core daemons are no-ops.
PY_STUB = r"""#!/bin/bash
for a in "$@"; do
  case "$a" in
    *core_heartbeat.py|*core-input-watch.py|*core-supervisor-relay.py|*notifier*) exit 0 ;;
  esac
done
exec "$REAL_PY" "$@"
"""


def _run(*, private: bool, session: bool, core: bool, in_pane: bool):
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        bind = td / "bin"
        bind.mkdir()
        marks = {k: td / k for k in ("sess", "core", "pane")}
        if session:
            marks["sess"].write_text("")
        if core:
            marks["core"].write_text("")
        if in_pane:
            marks["pane"].write_text("")
        (td / "workspace" / "state").mkdir(parents=True)
        tmux_log = td / "tmux.log"
        tmux_log.write_text("")
        for name, body in (("tmux", TMUX_STUB), ("pgrep", PGREP_STUB), ("ps", PS_STUB),
                           ("claude", "#!/bin/bash\nexit 0\n"),
                           ("py", PY_STUB)):
            p = bind / name
            p.write_text(body)
            p.chmod(0o755)
        env = {
            "PATH": f"{bind}:/usr/bin:/bin",
            "HOME": str(td),
            "SESS_MARK": str(marks["sess"]),
            "CORE_MARK": str(marks["core"]),
            "PANE_MARK": str(marks["pane"]),
            "TMUX_LOG": str(tmux_log),
            "SUTANDO_PY": str(bind / "py"),
            "REAL_PY": sys.executable,
            # Skip the credential-proxy probe, which reads this host's launchd.
            "ANTHROPIC_BASE_URL": "http://127.0.0.1:9",
            "SUTANDO_TEST_MODE": "1",
            "SUTANDO_WORKSPACE": str(td / "workspace"),
            "SUTANDO_RESTART_GRACE_S": "1",
        }
        if private:
            env["SUTANDO_TMUX_SOCKET"] = str(td / "run" / "tmux.sock")
        r = subprocess.run(["/bin/bash", str(SCRIPT)], env=env, capture_output=True,
                           text=True, timeout=120)
        calls = tmux_log.read_text().splitlines()
        return r.returncode, r.stdout + r.stderr, calls


def _core_created(calls):
    """A core session (not the watcher session) or core window was created."""
    return any(c.startswith("new-session -d -s sutando-core ") or c.startswith("new-window")
               for c in calls)


def case_private_socket_sessionless_foreign_core_not_reused():
    """The P0: private socket, no session, a foreign core elsewhere → start-cli
    must create the session on its own socket, not 'reuse' the foreign core."""
    rc, out, subs = _run(private=True, session=False, core=True, in_pane=False)
    fails = []
    if "reusing it" in out:
        fails.append(f"reused a core outside the private socket (rc={rc}): {out.strip()[-300:]!r}")
    if not any(c.startswith("new-session -d -s sutando-core ") for c in subs):
        fails.append(f"no core session created on the private socket; tmux calls={subs}")
    return fails


def case_private_socket_foreign_core_does_not_satisfy_session():
    """Session exists on the private socket but its core is gone; a same-name
    claude elsewhere must not make it read as 'already running'."""
    rc, out, subs = _run(private=True, session=True, core=True, in_pane=False)
    fails = []
    if "already running" in out:
        fails.append(f"foreign core counted as this socket's core: {out.strip()[-300:]!r}")
    if not any(c.startswith("new-window") for c in subs):
        fails.append(f"core window was not healed; tmux calls={subs}")
    return fails


def case_private_socket_own_core_still_running():
    """Over-reach guard: the core inside a pane of this socket is still found."""
    rc, out, subs = _run(private=True, session=True, core=True, in_pane=True)
    fails = []
    if "already running" not in out or rc != 0:
        fails.append(f"own core on the socket not recognised (rc={rc}): {out.strip()[-300:]!r}")
    if _core_created(subs):
        fails.append(f"started a second core beside the live one; tmux calls={subs}")
    return fails


def case_default_socket_orphan_still_adopted():
    """Default/global socket CLI keeps the orphan-adopt contract."""
    rc, out, subs = _run(private=False, session=False, core=True, in_pane=False)
    fails = []
    if rc != 0 or "reusing it" not in out:
        fails.append(f"default-socket orphan should still be reused (rc={rc}): {out.strip()[-300:]!r}")
    return fails


def main() -> int:
    cases = [v for k, v in sorted(globals().items()) if k.startswith("case_")]
    failed = 0
    for c in cases:
        fails = c()
        if fails:
            failed += 1
            print(f"FAIL {c.__name__}")
            for f in fails:
                print(f"    {f}")
        else:
            print(f"ok   {c.__name__}")
    print(f"{len(cases) - failed}/{len(cases)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
