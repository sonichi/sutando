"""Smoke test for skills/claude-gemini/scripts/agy-browser.sh.

A fake Chrome serves /json/version on a free port and a mock agy records its argv, then:
  1. start launches Chrome on the given profile and port, bound to 127.0.0.1 and headless,
     and registers the chrome-devtools MCP server pointed at that port.
  2. A second start launches nothing and registers nothing.
  3. status succeeds while running; stop ends only the profile's Chrome; status then fails.
  4. A port answered by a Chrome on another profile (here a prefix-sharing `<profile>-extra`) is
     refused: start launches and registers nothing, status fails, stop leaves it running.
  5. A chrome-devtools entry on another URL (port 9222 vs 92222 style prefix) is never overwritten,
     nor is one on this URL with another package: agy cannot list its env, so it could not be restored.
  6. Without npx, start fails before launching Chrome, even with the MCP already registered, and
     status fails; if `agy mcp add` fails, start stops every process on the profile it launched,
     including a child a launcher wrapper forked before exiting.
  7. start refuses when agy is absent.
  8. The port is owned only if its listener PID is on the profile: a foreign listener is refused even
     while a non-listening process carries the profile and port argv, both before and after launch.
  9. An MCP entry on another URL that appears during the launch wait stops the launched Chrome and
     is not overwritten.
 10. stop waits for a slow-to-exit Chrome and fails when one ignores SIGTERM.
 11. A relative --profile is made absolute: stop from one cwd leaves another cwd's `profile` alone.
 12. Ownership compares whole argv elements: a Chrome on `<profile> other` (a path with a space) is
     neither adopted by start nor stopped by stop.
 13. start refuses a profile already running on another port, so a failed add cannot kill that Chrome.
 14. A failing `agy mcp list` stops start before launch, or after it with the launched Chrome stopped.
 15. The registry read is the last probe before `agy mcp add`: an entry on another URL written during
     the final listener check is not overwritten.
 16. stop fails, not "stopped", when an argv read after the signal fails.
 17. python3 comes from the repo resolver: $SUTANDO_PY wins over PATH, and on macOS without the
     developer tools the system stub is never run.
 18. A Chrome another run starts on the profile between the busy check and the launch survives this
     run's abort; only the process group this run launched is stopped.
 19. A second run reaching the registry while the first is inside its read-to-add window waits for
     it, then sees the first entry and refuses instead of overwriting it.
 20. A listener swapped in while start waits for the lock is refused and left running.
 21. An abort before the launcher reports its group leaves the cancel file in place until the launcher
     reads it: start exits without removing it (a hooked rm would let the launcher's report land just
     before the removal), the launcher then exits without exec'ing Chrome and removes the directory.
 22. A listener swapped in while the registry read under the lock is blocked is refused, not registered.
 23. A listener swapped in while `agy mcp add`, or the list after it, is blocked fails start and the
     foreign listener is left running. agy has no compare-and-remove, so no entry is removed: an
     external `agy mcp add` landing in that window keeps its entry.
 24. A start killed by SIGTERM after the launcher reported its group, but before start stopped that
     group, still cancels the launch: no Chrome comes up and the handshake directory is removed.
 25. A start killed by SIGTERM with a TERM-ignoring Chrome already listening leaves no process on the
     profile: the group is killed, and only then is the handshake directory removed.
 26. A launcher probe whose ps fails is unknown, never dead: a start killed while that probe fails
     keeps the cancel file (a hooked rm would let the launcher's report land just before the removal),
     so the launcher never exec's Chrome.
 27. Ownership of the reported group is revalidated before every signal: a group whose leader's start
     time changed (a reused PGID), or one that is not the launcher's own, is never signalled; start
     reports it and keeps the handshake directory.

Run: python3 tests/claude-gemini-agy-browser.test.py
"""
import fcntl
import os
import signal
import socket
import sys
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "skills" / "claude-gemini" / "scripts" / "agy-browser.sh"

FAKE_CHROME = f"#!{sys.executable}\n" + """
import http.server, os, signal, sys, time
port = int(next(a.split("=", 1)[1] for a in sys.argv if a.startswith("--remote-debugging-port=")))
with open(os.environ["CHROME_LOG"], "a") as f:
    f.write(" ".join(sys.argv[1:]) + "\\n")
if os.environ.get("CHROME_MCP_WRITE"):
    with open(os.environ["MCP_STATE"], "a") as f:
        f.write(os.environ["CHROME_MCP_WRITE"] + "\\n")
if os.environ.get("TERM_IGNORE"):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
if os.environ.get("TERM_DELAY"):
    def slow_exit(*_):
        time.sleep(float(os.environ["TERM_DELAY"]))
        os._exit(0)
    signal.signal(signal.SIGTERM, slow_exit)
if os.environ.get("READY_FILE"):
    with open(os.environ["READY_FILE"], "w") as f:
        f.write(str(os.getpid()))
if os.environ.get("NOBIND"):
    while True:
        time.sleep(60)
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200 if self.path == "/json/version" else 404)
        self.end_headers()
        self.wfile.write(b'{"Browser": "Fake/1.0"}')
    def log_message(self, *a):
        pass
http.server.HTTPServer(("127.0.0.1", port), H).serve_forever()
"""

MOCK_AGY = """#!/bin/bash
if [[ "$1 $2" == "mcp list" ]]; then
  n=$(( $(cat "$AGY_LIST_COUNT" 2>/dev/null || echo 0) + 1 )); echo "$n" >"$AGY_LIST_COUNT"
  [[ "$n" == "${AGY_LIST_FAIL_ON:-}" ]] && exit 7
  out="$(cat "$MCP_STATE" 2>/dev/null)"
  if [[ "$n" == "${AGY_LIST_HOLD_ON:-}" ]]; then
    : >"$HOLD_MARK"
    for _ in $(seq 1 300); do [[ -f "$HOLD_RELEASE" ]] && break; sleep 0.1; done
  fi
  [[ -n "$out" ]] && printf '%s\n' "$out"; exit 0
fi
if [[ "$1 $2" == "mcp add" ]]; then
  [[ -n "${AGY_ADD_FAIL:-}" ]] && exit 1
  printf '%s\\n' "$@" >>"$AGY_LOG"
  shift 2; name="$1"; shift
  grep -v "^$name " "$MCP_STATE" >"$MCP_STATE.new" 2>/dev/null; mv "$MCP_STATE.new" "$MCP_STATE"
  echo "$name  stdio  enabled  $*" >>"$MCP_STATE"
  if [[ -n "${AGY_ADD_HOLD:-}" ]]; then
    : >"$HOLD_MARK"
    for _ in $(seq 1 300); do [[ -f "$HOLD_RELEASE" ]] && break; sleep 0.1; done
  fi
  exit 0
fi
if [[ "$1 $2" == "mcp remove" ]]; then
  echo "remove $3" >>"$AGY_LOG"
  grep -v "^$3 " "$MCP_STATE" >"$MCP_STATE.new" 2>/dev/null; mv "$MCP_STATE.new" "$MCP_STATE"; exit 0
fi
exit 1
"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def write_exec(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)
    return path


def answers(port: int) -> bool:
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2)
        return True
    except OSError:
        return False


def run(env: dict, *args: str, cwd=None) -> tuple[int, str]:
    proc = subprocess.run(["bash", str(SCRIPT), *args], env=env, capture_output=True, text=True, timeout=60, cwd=cwd)
    return proc.returncode, proc.stdout + proc.stderr


def profile_procs(profile) -> list[int]:
    needle = f"--user-data-dir={profile} "
    out = subprocess.run(["ps", "-Ao", "pid=,command="], capture_output=True, text=True).stdout
    return [int(l.split()[0]) for l in out.splitlines() if needle in l + " "]


def kill_profile(profile) -> None:
    for pid in profile_procs(profile):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def wait_up(port: int, what: str) -> None:
    for _ in range(40):
        if answers(port):
            return
        time.sleep(0.25)
    raise AssertionError(f"{what} never came up")


def main() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d).resolve()
        bin_dir = tmp / "bin"
        write_exec(bin_dir / "agy", MOCK_AGY)
        write_exec(bin_dir / "npx", "#!/bin/bash\nexit 0\n")
        chrome = write_exec(tmp / "fake-chrome", FAKE_CHROME)
        port = free_port()
        profile = tmp / "profile"
        env = os.environ.copy()
        env.update(
            HOME=str(tmp / "home"),
            PATH=f"{bin_dir}:/usr/bin:/bin",
            CHROME_LOG=str(tmp / "chrome.log"),
            AGY_LOG=str(tmp / "agy.log"),
            MCP_STATE=str(tmp / "mcp.state"),
            AGY_LIST_COUNT=str(tmp / "agy-list.count"),
            SUTANDO_PY=sys.executable,
        )
        args = ["--port", str(port), "--profile", str(profile), "--chrome", str(chrome)]
        try:
            rc, out = run(env, "start", *args)
            assert rc == 0, f"start failed: {out}"
            launch = (tmp / "chrome.log").read_text().split()
            for flag in (f"--user-data-dir={profile}", f"--remote-debugging-port={port}",
                         "--remote-debugging-address=127.0.0.1", "--headless=new"):
                assert flag in launch, f"{flag} missing from Chrome launch: {launch}"
            added = (tmp / "agy.log").read_text().splitlines()
            assert added == ["mcp", "add", "chrome-devtools", "npx", "-y", "chrome-devtools-mcp@1.10.1",
                             "--browserUrl", f"http://127.0.0.1:{port}"], f"unexpected mcp add: {added}"

            rc, out = run(env, "start", *args)
            assert rc == 0 and "already listening" in out and "already registered" in out, out
            assert len((tmp / "chrome.log").read_text().splitlines()) == 1, "second start relaunched Chrome"
            assert (tmp / "agy.log").read_text().splitlines() == added, "second start re-registered the MCP"

            rc, out = run(env, "status", *args)
            assert rc == 0, f"status should succeed while running: {out}"

            (tmp / "mcp.state").write_text(
                f"chrome-devtools  stdio  enabled  npx -y chrome-devtools-mcp@latest --browserUrl http://127.0.0.1:{port}\n")
            rc, out = run(env, "status", *args)
            assert rc != 0 and "package other than" in out, f"status accepted an unpinned registration: {out}"
            unpinned = (tmp / "mcp.state").read_text()
            rc, out = run(env, "start", *args)
            assert rc != 0 and "not overwriting it" in out, f"start overwrote an unpinned registration: {out}"
            assert (tmp / "mcp.state").read_text() == unpinned, "start changed the user's unpinned entry"
            assert (tmp / "agy.log").read_text().splitlines() == added, "start ran agy mcp add over an unpinned entry"
            (tmp / "mcp.state").write_text("")
            rc, out = run(env, "start", *args)
            assert rc == 0 and "registered with agy" in out, f"start after removal did not register: {out}"
            assert (tmp / "agy.log").read_text().splitlines() == added * 2, "registration did not use the pinned package"
            added = (tmp / "agy.log").read_text().splitlines()
            entries = [l for l in (tmp / "mcp.state").read_text().splitlines() if l.startswith("chrome-devtools ")]
            assert len(entries) == 1, f"registration left {len(entries)} entries: {entries}"
            rc, out = run(env, "status", *args)
            assert rc == 0, f"status should succeed after registration: {out}"
            saved = (tmp / "mcp.state").read_text()
            (tmp / "mcp.state").write_text("")
            rc, out = run(env, "status", *args)
            assert rc != 0 and "not registered" in out and "other than" not in out, f"empty registry misread: {out}"
            (tmp / "mcp.state").write_text(saved)
            sibling_port = free_port()
            sibling = subprocess.Popen(
                [str(chrome), f"--user-data-dir={profile}-extra", f"--remote-debugging-port={sibling_port}"], env=env)
            try:
                for _ in range(40):
                    if answers(sibling_port):
                        break
                    time.sleep(0.25)
                assert answers(sibling_port), "sibling fake Chrome never came up"
                rc, out = run(env, "stop", *args)
                assert rc == 0 and "stopped" in out, out
                time.sleep(0.5)
                assert sibling.poll() is None and answers(sibling_port), "stop killed the Chrome on <profile>-extra"
            finally:
                sibling.kill()
                sibling.wait()
            for _ in range(20):
                rc, out = run(env, "status", *args)
                if rc != 0:
                    break
                time.sleep(0.25)
            assert rc != 0 and "not listening" in out, f"status should fail after stop: {out}"
        finally:
            kill_profile(profile)

        try:
            rc, out = run(dict(env, TERM_DELAY="1.5"), "start", *args)
            assert rc == 0, f"start failed: {out}"
            rc, out = run(env, "stop", *args)
            assert rc == 0 and "stopped" in out, out
            assert not profile_procs(profile), "stop reported stopped while the Chrome was still exiting"
            assert not answers(port), "stop returned with CDP still up"
            rc, out = run(dict(env, TERM_IGNORE="1"), "start", *args)
            assert rc == 0, f"start failed: {out}"
            rc, out = run(env, "stop", *args)
            assert rc != 0 and "did not exit" in out and "stopped the" not in out, f"stop claimed success: {out}"
            assert profile_procs(profile), "the TERM-ignoring Chrome is gone; the test proved nothing"
        finally:
            kill_profile(profile)

        foreign_profile = Path(f"{profile}-extra")
        foreign_port = free_port()
        foreign = subprocess.Popen(
            [str(chrome), f"--user-data-dir={foreign_profile}", f"--remote-debugging-port={foreign_port}", "about:blank"],
            env=env,
        )
        try:
            for _ in range(40):
                if answers(foreign_port):
                    break
                time.sleep(0.25)
            assert answers(foreign_port), "foreign fake Chrome never came up"
            decoy = subprocess.Popen(
                [str(chrome), f"--user-data-dir={profile}", f"--remote-debugging-port={foreign_port}"],
                env=dict(env, NOBIND="1", CHROME_LOG=str(tmp / "decoy.log")))
            for _ in range(40):
                if profile_procs(profile):
                    break
                time.sleep(0.1)
            assert profile_procs(profile), "decoy never started"
            (tmp / "mcp.state").write_text("")
            launches = (tmp / "chrome.log").read_text().splitlines()
            fargs = ["--port", str(foreign_port), "--profile", str(profile), "--chrome", str(chrome)]
            rc, out = run(env, "start", *fargs)
            assert rc != 0 and "not using it" in out, f"start adopted a foreign listener: {out}"
            assert (tmp / "chrome.log").read_text().splitlines() == launches, "start launched Chrome over a foreign port"
            assert (tmp / "agy.log").read_text().splitlines() == added, "start registered the MCP against a foreign Chrome"
            rc, out = run(env, "status", *fargs)
            assert rc != 0 and "held by a process not running on" in out, f"status accepted a foreign listener: {out}"
            rc, out = run(env, "stop", *fargs)
            assert rc == 0 and "stopped" in out and decoy.wait(timeout=5) is not None, out
            assert foreign.poll() is None and answers(foreign_port), "stop killed a Chrome on a different profile"
        finally:
            decoy.kill()
            decoy.wait()
            foreign.kill()
            foreign.wait()

        hijack_pid = tmp / "hijack.pid"
        hijack = write_exec(tmp / "hijack-chrome", f"""#!/bin/bash
port=""; for a in "$@"; do [[ "$a" == --remote-debugging-port=* ]] && port="${{a#*=}}"; done
"{chrome}" --user-data-dir="{tmp}/hijacker" --remote-debugging-port="$port" >/dev/null 2>&1 &
echo $! >"{hijack_pid}"
NOBIND=1 exec "{chrome}" "$@"
""")
        (tmp / "mcp.state").write_text("")
        try:
            rc, out = run(env, "start", "--port", str(port), "--profile", str(profile), "--chrome", str(hijack))
            assert rc != 0 and "not using it" in out, f"start registered a listener it did not launch: {out}"
            assert (tmp / "agy.log").read_text().splitlines() == added, "start registered the MCP against a hijacker"
            assert not profile_procs(profile), "start left its launched process running after refusing"
        finally:
            kill_profile(profile)
            if hijack_pid.exists():
                try:
                    os.kill(int(hijack_pid.read_text()), signal.SIGKILL)
                except OSError:
                    pass
        for _ in range(20):
            if not answers(port):
                break
            time.sleep(0.25)

        other_url = f"http://127.0.0.1:{port}2"
        other_entry = f"chrome-devtools  stdio  enabled  npx -y chrome-devtools-mcp@1.10.1 --browserUrl {other_url}"
        (tmp / "mcp.state").write_text("")
        try:
            rc, out = run(dict(env, CHROME_MCP_WRITE=other_entry), "start", *args)
            assert rc != 0 and "not overwriting" in out and "stopped the Chrome" in out, f"start overwrote a new entry: {out}"
            assert (tmp / "agy.log").read_text().splitlines() == added, "start registered over an entry added mid-launch"
            assert (tmp / "mcp.state").read_text().splitlines() == [other_entry], "the mid-launch entry was changed"
            assert not profile_procs(profile) and not answers(port), "start left its Chrome running after refusing"
        finally:
            kill_profile(profile)

        prefix_url = f"http://127.0.0.1:{port}2"
        (tmp / "mcp.state").write_text(
            f"chrome-devtools  stdio  enabled  npx -y chrome-devtools-mcp@1.10.1 --browserUrl {prefix_url}\n")
        launches = (tmp / "chrome.log").read_text().splitlines()
        rc, out = run(env, "status", *args)
        assert rc != 0 and "another browser URL" in out, f"status accepted a {prefix_url} registration: {out}"
        rc, out = run(env, "start", *args)
        assert rc != 0 and "not overwriting" in out, f"start overwrote an entry on another URL: {out}"
        assert prefix_url in (tmp / "mcp.state").read_text(), "the other URL's entry was changed"
        assert (tmp / "chrome.log").read_text().splitlines() == launches, "start launched Chrome before refusing"
        assert (tmp / "agy.log").read_text().splitlines() == added, "start registered over another URL"

        (tmp / "mcp.state").write_text("")
        (bin_dir / "npx").unlink()
        rc, out = run(env, "start", *args)
        assert rc != 0 and "npx not found" in out, f"start without npx should refuse: {out}"
        assert (tmp / "chrome.log").read_text().splitlines() == launches, "start launched Chrome without npx"
        (tmp / "mcp.state").write_text(
            f"chrome-devtools  stdio  enabled  npx -y chrome-devtools-mcp@1.10.1 --browserUrl http://127.0.0.1:{port}\n")
        rc, out = run(env, "start", *args)
        try:
            assert rc != 0 and "npx not found" in out, f"pinned start without npx should refuse: {out}"
            assert (tmp / "chrome.log").read_text().splitlines() == launches, "pinned start launched Chrome without npx"
        finally:
            kill_profile(profile)
        rc, out = run(env, "status", *args)
        assert rc != 0 and "npx not found" in out, f"status without npx should fail: {out}"
        (tmp / "mcp.state").write_text("")
        write_exec(bin_dir / "npx", "#!/bin/bash\nexit 0\n")

        rc, out = run(dict(env, AGY_ADD_FAIL="1"), "start", *args)
        try:
            assert rc != 0 and "mcp add failed" in out and "stopped the Chrome" in out, f"failed add not reported: {out}"
            assert len((tmp / "chrome.log").read_text().splitlines()) == len(launches) + 1, "Chrome was not launched"
            for _ in range(20):
                if not answers(port):
                    break
                time.sleep(0.25)
            assert not answers(port), "start left its Chrome running after mcp add failed"
        finally:
            kill_profile(profile)

        handoff = write_exec(tmp / "handoff-chrome", f'#!/bin/bash\n"{chrome}" "$@" >/dev/null 2>&1 &\nexit 0\n')
        launches = (tmp / "chrome.log").read_text().splitlines()
        rc, out = run(dict(env, AGY_ADD_FAIL="1"), "start", "--port", str(port), "--profile", str(profile),
                      "--chrome", str(handoff))
        try:
            assert rc != 0 and "mcp add failed" in out and "stopped the Chrome" in out, f"failed add not reported: {out}"
            assert len((tmp / "chrome.log").read_text().splitlines()) == len(launches) + 1, "handoff Chrome not launched"
            assert not profile_procs(profile) and not answers(port), "start left the handed-off Chrome running"
        finally:
            kill_profile(profile)

        cwd_a, cwd_b = tmp / "cwd-a", tmp / "cwd-b"
        port_a, port_b = free_port(), free_port()
        env_a = dict(env, MCP_STATE=str(tmp / "mcp-a.state"))
        env_b = dict(env, MCP_STATE=str(tmp / "mcp-b.state"))
        for c in (cwd_a, cwd_b):
            c.mkdir()
        try:
            rc, out = run(env_a, "start", "--port", str(port_a), "--profile", "profile", "--chrome", str(chrome), cwd=cwd_a)
            assert rc == 0, f"relative start failed: {out}"
            rc, out = run(env_b, "start", "--port", str(port_b), "--profile", "profile", "--chrome", str(chrome), cwd=cwd_b)
            assert rc == 0, f"relative start failed: {out}"
            launched = (tmp / "chrome.log").read_text().split()
            for c in (cwd_a, cwd_b):
                assert f"--user-data-dir={c / 'profile'}" in launched, f"{c} profile not made absolute: {launched}"
            rc, out = run(env_a, "stop", "--port", str(port_a), "--profile", "profile", cwd=cwd_a)
            assert rc == 0 and "stopped" in out, out
            assert not profile_procs(cwd_a / "profile"), "stop left cwd-a's Chrome running"
            assert profile_procs(cwd_b / "profile") and answers(port_b), "stop from cwd-a killed cwd-b's Chrome"
        finally:
            kill_profile(cwd_a / "profile")
            kill_profile(cwd_b / "profile")

        spaced, spaced_port = tmp / "sp", free_port()
        spaced_foreign = subprocess.Popen(
            [str(chrome), f"--user-data-dir={tmp / 'sp other'}", f"--remote-debugging-port={spaced_port}"],
            env=dict(env, CHROME_LOG=str(tmp / "spaced.log")))
        try:
            wait_up(spaced_port, "spaced-profile fake Chrome")
            (tmp / "mcp.state").write_text("")
            added = (tmp / "agy.log").read_text().splitlines()
            sargs = ["--port", str(spaced_port), "--profile", str(spaced), "--chrome", str(chrome)]
            rc, out = run(env, "start", *sargs)
            assert rc != 0 and "not using it" in out, f"start adopted the Chrome on '<profile> other': {out}"
            assert (tmp / "agy.log").read_text().splitlines() == added, "start registered a '<profile> other' Chrome"
            rc, out = run(env, "stop", *sargs)
            assert rc == 0 and "none running" in out, f"stop claimed the Chrome on '<profile> other': {out}"
            assert spaced_foreign.poll() is None and answers(spaced_port), "stop killed the Chrome on '<profile> other'"
        finally:
            spaced_foreign.kill()
            spaced_foreign.wait()

        port_b = free_port()
        (tmp / "mcp.state").write_text("")
        try:
            rc, out = run(env, "start", *args)
            assert rc == 0, f"start failed: {out}"
            (tmp / "mcp.state").write_text("")
            launches = (tmp / "chrome.log").read_text().splitlines()
            rc, out = run(dict(env, AGY_ADD_FAIL="1"), "start", "--port", str(port_b), "--profile", str(profile),
                          "--chrome", str(chrome))
            assert rc != 0 and "already in use" in out, f"start launched onto a busy profile: {out}"
            assert (tmp / "chrome.log").read_text().splitlines() == launches, "start launched onto a busy profile"
            assert answers(port) and profile_procs(profile), "a failed start killed the profile's existing Chrome"
        finally:
            kill_profile(profile)
        for _ in range(20):
            if not answers(port):
                break
            time.sleep(0.25)

        (tmp / "mcp.state").write_text("")
        (tmp / "agy-list.count").unlink(missing_ok=True)
        launches = (tmp / "chrome.log").read_text().splitlines()
        rc, out = run(dict(env, AGY_LIST_FAIL_ON="1"), "start", *args)
        try:
            assert rc != 0 and "mcp list' failed" in out, f"start ignored a failing mcp list: {out}"
            assert (tmp / "chrome.log").read_text().splitlines() == launches, "start launched after mcp list failed"
        finally:
            kill_profile(profile)
        (tmp / "agy-list.count").unlink()
        rc, out = run(dict(env, AGY_LIST_FAIL_ON="2"), "start", *args)
        try:
            assert rc != 0 and "mcp list' failed" in out and "stopped the Chrome" in out, f"late mcp list failure: {out}"
            assert len((tmp / "chrome.log").read_text().splitlines()) == len(launches) + 1, "Chrome was not launched"
            assert not profile_procs(profile) and not answers(port), "start left its Chrome running after mcp list failed"
        finally:
            kill_profile(profile)
        (tmp / "agy-list.count").unlink()
        for _ in range(20):
            if not answers(port):
                break
            time.sleep(0.25)

        lsof_wrap = write_exec(bin_dir / "lsof", f"""#!/bin/bash
n=$(( $(cat "{tmp}/lsof.count" 2>/dev/null || echo 0) + 1 )); echo "$n" >"{tmp}/lsof.count"
[[ "$n" == "${{LSOF_INSERT_ON:-}}" ]] && echo "{other_entry}" >>"$MCP_STATE"
for c in /usr/sbin/lsof /usr/bin/lsof; do [[ -x "$c" ]] && exec "$c" "$@"; done
exit 1
""")
        (tmp / "mcp.state").write_text("")
        try:
            rc, out = run(env, "start", *args)
            assert rc == 0, f"start failed: {out}"
            (tmp / "mcp.state").write_text("")
            (tmp / "lsof.count").unlink()
            added = (tmp / "agy.log").read_text().splitlines()
            rc, out = run(dict(env, LSOF_INSERT_ON="2"), "start", *args)
            assert (tmp / "lsof.count").read_text().strip() == "2", "the final listener check never ran"
            assert rc != 0 and "not overwriting" in out, f"start overwrote an entry written before its add: {out}"
            assert (tmp / "mcp.state").read_text().splitlines() == [other_entry], "the concurrent entry was replaced"
            assert (tmp / "agy.log").read_text().splitlines() == added, "start registered over a concurrent entry"
        finally:
            lsof_wrap.unlink()
            kill_profile(profile)

        py_wrap = write_exec(bin_dir / "python3", f"""#!/bin/bash
n=$(( $(cat "{tmp}/py.count" 2>/dev/null || echo 0) + 1 )); echo "$n" >"{tmp}/py.count"
[[ "$n" == "${{PY_FAIL_ON:-}}" ]] && exit 1
exec "{sys.executable}" "$@"
""")
        (tmp / "mcp.state").write_text("")
        try:
            rc, out = run(dict(env, SUTANDO_PY=str(py_wrap)), "start", *args)
            assert rc == 0, f"start failed: {out}"
            (tmp / "py.count").unlink()
            rc, out = run(dict(env, PY_FAIL_ON="3", SUTANDO_PY=str(py_wrap)), "stop", *args)
            assert int((tmp / "py.count").read_text()) >= 3, "stop never re-read argv after signalling"
            assert rc != 0 and "stopped" not in out, f"stop reported success after a failed argv read: {out}"
        finally:
            py_wrap.unlink()
            kill_profile(profile)
        for _ in range(20):
            if not answers(port):
                break
            time.sleep(0.25)

        log = tmp / "py-which.log"
        sut_py = write_exec(tmp / "sut-py", f'#!/bin/bash\necho sutando >>"{log}"\nexec "{sys.executable}" "$@"\n')
        path_py = write_exec(bin_dir / "python3", f'#!/bin/bash\necho path >>"{log}"\nexec "{sys.executable}" "$@"\n')
        try:
            run(dict(env, SUTANDO_PY=str(sut_py)), "stop", *args)
            assert set(log.read_text().split()) == {"sutando"}, f"PATH python3 used over $SUTANDO_PY: {log.read_text()}"
        finally:
            path_py.unlink()
        if sys.platform == "darwin":
            no_clt = {k: v for k, v in env.items() if k != "SUTANDO_PY"}
            xs = write_exec(bin_dir / "xcode-select", "#!/bin/bash\nexit 2\n")
            try:
                rc, out = run(no_clt, "status", *args)
                assert rc != 0 and "no runnable python3" in out, f"the system python3 ran without developer tools: {out}"
            finally:
                xs.unlink()

        ready = tmp / "rival.ready"
        py_hook = write_exec(tmp / "py-hook", f"""#!/bin/bash
n=$(( $(cat "{tmp}/hook.count" 2>/dev/null || echo 0) + 1 )); echo "$n" >"{tmp}/hook.count"
if [[ "$n" == "${{PY_HOOK_ON:-}}" ]]; then
  NOBIND=1 READY_FILE="{ready}" "{sys.executable}" -c 'import os, sys; os.setsid(); os.execv(sys.argv[1], sys.argv[1:])' \
    "{chrome}" --user-data-dir="{profile}" --remote-debugging-port=1 >/dev/null 2>&1 &
  for _ in $(seq 1 100); do [[ -s "{ready}" ]] && break; sleep 0.05; done
fi
exec "{sys.executable}" "$@"
""")
        (tmp / "mcp.state").write_text("")
        rc, out = run(dict(env, SUTANDO_PY=str(py_hook), PY_HOOK_ON="2", AGY_ADD_FAIL="1"), "start", *args)
        try:
            assert ready.read_text(), "the rival Chrome was never started between check and launch"
            rival = int(ready.read_text())
            assert rc != 0 and "stopped the Chrome this run started" in out, f"failed add not reported: {out}"
            for _ in range(20):
                if not answers(port):
                    break
                time.sleep(0.25)
            assert not answers(port), "start left its own Chrome running"
            assert rival in profile_procs(profile), "start's abort killed a Chrome another run started on the profile"
        finally:
            kill_profile(profile)

        profile_b, port_b = tmp / "profile-b", free_port()
        hold, release, b_err = tmp / "hold", tmp / "release", tmp / "b.out"
        (tmp / "mcp.state").write_text("")
        adds = len((tmp / "agy.log").read_text().splitlines())
        run_a = subprocess.Popen(["bash", str(SCRIPT), "start", *args], env=dict(
            env, AGY_LIST_COUNT=str(tmp / "a.count"), AGY_LIST_HOLD_ON="2", HOLD_MARK=str(hold),
            HOLD_RELEASE=str(release)), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        run_b = None
        try:
            for _ in range(200):
                if hold.exists():
                    break
                time.sleep(0.1)
            assert hold.exists(), "the first run never reached its registry read"
            with open(b_err, "w") as f:
                run_b = subprocess.Popen(["bash", str(SCRIPT), "start", "--port", str(port_b), "--profile",
                                          str(profile_b), "--chrome", str(chrome)],
                                         env=dict(env, AGY_LIST_COUNT=str(tmp / "b.count")), stdout=f, stderr=f)
                for _ in range(300):
                    if run_b.poll() is not None or "waiting for another run" in b_err.read_text():
                        break
                    time.sleep(0.1)
            release.touch()
            out_a = run_a.communicate(timeout=60)[0]
            rc_b, out_b = run_b.wait(timeout=60), b_err.read_text()
            assert run_a.returncode == 0, f"the first run failed: {out_a}"
            assert rc_b != 0 and "not overwriting" in out_b, f"the second run overwrote the first's entry: {out_b}"
            entries = [l for l in (tmp / "mcp.state").read_text().splitlines() if l.startswith("chrome-devtools ")]
            assert len(entries) == 1 and f"127.0.0.1:{port} " in entries[0] + " ", f"registry lost the first entry: {entries}"
            assert len((tmp / "agy.log").read_text().splitlines()) == adds + 8, "the second run registered too"
        finally:
            release.touch()
            for proc in (run_a, run_b):
                if proc and proc.poll() is None:
                    proc.kill()
            kill_profile(profile)
            kill_profile(profile_b)

        kill_profile(profile)
        (tmp / "mcp.state").write_text("")
        rc, out = run(env, "start", *args)
        assert rc == 0, f"start before the lock-wait case failed: {out}"
        (tmp / "mcp.state").write_text("")
        lock_out, foreign = tmp / "lock.out", None
        try:
            with open(tmp / "home" / ".gemini" / "agy-browser.lock", "a") as held:
                fcntl.flock(held, fcntl.LOCK_EX)
                with open(lock_out, "w") as f:
                    waiter = subprocess.Popen(["bash", str(SCRIPT), "start", *args], env=env, stdout=f, stderr=f)
                for _ in range(300):
                    if waiter.poll() is not None or "waiting for another run" in lock_out.read_text():
                        break
                    time.sleep(0.1)
                assert "waiting for another run" in lock_out.read_text(), f"start never waited on the lock: {lock_out.read_text()}"
                kill_profile(profile)
                for _ in range(40):
                    if not answers(port):
                        break
                    time.sleep(0.25)
                foreign = subprocess.Popen([str(chrome), f"--user-data-dir={tmp}/foreign", f"--remote-debugging-port={port}"], env=env)
                wait_up(port, "the foreign listener")
            rc, out = waiter.wait(timeout=60), lock_out.read_text()
            assert rc != 0 and "not running on" in out, f"a listener swapped during the lock wait was registered: {out}"
            assert f"127.0.0.1:{port}" not in (tmp / "mcp.state").read_text(), "the swapped listener was registered"
            assert foreign.poll() is None and answers(port), "start's abort killed the foreign listener"
        finally:
            if foreign:
                foreign.kill()
            kill_profile(profile)

        launches = len((tmp / "chrome.log").read_text().splitlines())
        release = tmp / "launch.release"
        rm_hook = write_exec(bin_dir / "rm", f"""#!/bin/bash
for a in "$@"; do
  if [[ -d "$a" && -e "$a/pgid.tmp" ]]; then
    : >"{tmp}/rm.called"
    for _ in $(seq 1 100); do [[ -f "{tmp}/launch.renamed" ]] && break; sleep 0.1; done
  fi
done
exec /bin/rm "$@"
""")
        held_py = write_exec(tmp / "held-py", f"""#!/bin/bash
if [[ "$2" == *setsid* ]]; then
  exec "{sys.executable}" -c '
import os, sys, time
code, sys.argv = sys.argv[1], ["-c"] + sys.argv[2:]
real = os.rename
def rename(a, b):
    for _ in range(600):
        if os.path.exists("{tmp}/rm.called") or os.path.exists("{release}"):
            break
        time.sleep(0.1)
    real(a, b)
    open("{tmp}/launch.renamed", "w").close()
    time.sleep(1)
os.rename = rename
exec(compile(code, "<launcher>", "exec"))' "$2" "${{@:3}}"
fi
exec "{sys.executable}" "$@"
""")
        (tmp / "mcp.state").write_text("")
        try:
            rc, out = run(dict(env, SUTANDO_PY=str(held_py)), "start", *args)
            assert rc != 0 and "has not started it yet" in out, f"abort before the launcher's report misreported: {out}"
            hs = Path(out.split("will read ", 1)[1].split("/cancel", 1)[0])
            assert (hs / "cancel").exists(), f"the cancel file is gone before the launcher read it: {out}"
            assert not (tmp / "rm.called").exists(), "start removed the cancel file before the launcher read it"
            release.touch()
            for _ in range(40):
                if (tmp / "launch.renamed").exists() and not hs.exists():
                    break
                time.sleep(0.25)
            assert (tmp / "launch.renamed").exists(), "the held launcher never resumed"
            time.sleep(2)
            assert not answers(port), "Chrome came up after start reported the abort"
            assert profile_procs(profile) == [], "a process on the profile outlived the abort"
            assert len((tmp / "chrome.log").read_text().splitlines()) == launches, "the cancelled launcher still exec'd Chrome"
            assert not hs.exists(), "the launcher left its handshake directory after reading the cancel"
        finally:
            rm_hook.unlink()
            kill_profile(profile)

        (tmp / "mcp.state").write_text("")
        rc, out = run(env, "start", *args)
        assert rc == 0, f"start before the registry-read swap case failed: {out}"
        (tmp / "mcp.state").write_text("")
        hold, release, foreign, swapper = tmp / "swap.hold", tmp / "swap.release", None, None
        try:
            swapper = subprocess.Popen(["bash", str(SCRIPT), "start", *args], env=dict(
                env, AGY_LIST_COUNT=str(tmp / "swap.count"), AGY_LIST_HOLD_ON="2", HOLD_MARK=str(hold),
                HOLD_RELEASE=str(release)), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            for _ in range(200):
                if hold.exists():
                    break
                time.sleep(0.1)
            assert hold.exists(), "start never reached its registry read under the lock"
            kill_profile(profile)
            for _ in range(40):
                if not answers(port):
                    break
                time.sleep(0.25)
            foreign = subprocess.Popen([str(chrome), f"--user-data-dir={tmp}/foreign", f"--remote-debugging-port={port}"], env=env)
            wait_up(port, "the foreign listener")
            release.touch()
            out = swapper.communicate(timeout=60)[0]
            assert swapper.returncode != 0 and "not running on" in out, f"a listener swapped during the registry read was registered: {out}"
            assert f"127.0.0.1:{port}" not in (tmp / "mcp.state").read_text(), "the swapped listener was registered"
            assert foreign.poll() is None and answers(port), "start's abort killed the foreign listener"
        finally:
            release.touch()
            if swapper and swapper.poll() is None:
                swapper.kill()
            if foreign:
                foreign.kill()
            kill_profile(profile)

        external = f"chrome-devtools  stdio  enabled  external-mcp --browserUrl http://127.0.0.1:{free_port()}"
        for case, hold_env, writer in (("add", {"AGY_ADD_HOLD": "1"}, False),
                                       ("post-add list", {"AGY_LIST_HOLD_ON": "3"}, False),
                                       ("external add", {"AGY_LIST_HOLD_ON": "3"}, True)):
            (tmp / "mcp.state").write_text("")
            rc, out = run(env, "start", *args)
            assert rc == 0, f"start before the {case} swap case failed: {out}"
            (tmp / "mcp.state").write_text("")
            hold, release, foreign, swapper = tmp / "add.hold", tmp / "add.release", None, None
            hold.unlink(missing_ok=True)
            release.unlink(missing_ok=True)
            try:
                swapper = subprocess.Popen(["bash", str(SCRIPT), "start", *args], env=dict(
                    env, AGY_LIST_COUNT=str(tmp / f"{case}.count"), HOLD_MARK=str(hold),
                    HOLD_RELEASE=str(release), **hold_env), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                for _ in range(200):
                    if hold.exists() or swapper.poll() is not None:
                        break
                    time.sleep(0.1)
                assert hold.exists(), f"start never reached the held {case}"
                kill_profile(profile)
                for _ in range(40):
                    if not answers(port):
                        break
                    time.sleep(0.25)
                foreign = subprocess.Popen([str(chrome), f"--user-data-dir={tmp}/foreign", f"--remote-debugging-port={port}"], env=env)
                wait_up(port, "the foreign listener")
                if writer:
                    (tmp / "mcp.state").write_text(external + "\n")
                release.touch()
                out = swapper.communicate(timeout=60)[0]
                assert swapper.returncode != 0 and "not running on" in out, f"a listener swapped during the {case} was reported as ours: {out}"
                assert "was left in place" in out, f"the {case} swap did not report the entry it left: {out}"
                assert "remove" not in (tmp / "agy.log").read_text(), f"start removed an entry after the {case} swap"
                if writer:
                    assert (tmp / "mcp.state").read_text() == external + "\n", "start deleted the external writer's entry"
                assert foreign.poll() is None and answers(port), f"start's abort after the {case} killed the foreign listener"
            finally:
                release.touch()
                if swapper and swapper.poll() is None:
                    swapper.kill()
                if foreign:
                    foreign.kill()
                kill_profile(profile)

        for _ in range(40):
            if not answers(port):
                break
            time.sleep(0.25)
        launches = len((tmp / "chrome.log").read_text().splitlines())
        marks = {k: tmp / f"sig.{k}" for k in ("go", "renamed", "blocked", "sent")}
        cat_hook = write_exec(bin_dir / "cat", f"""#!/bin/bash
if [[ "$1" == */pgid && ! -e "{marks['blocked']}" ]]; then
  : >"{marks['go']}"
  for _ in $(seq 1 100); do [[ -f "$1" ]] && break; sleep 0.1; done
  : >"{marks['blocked']}"
  for _ in $(seq 1 300); do sleep 0.1; done
fi
exec /bin/cat "$@"
""")
        sig_py = write_exec(tmp / "sig-py", f"""#!/bin/bash
if [[ "$2" == *setsid* ]]; then
  echo "$3" >"{tmp}/sig.hs"
  exec "{sys.executable}" -c '
import os, sys, time
code, sys.argv = sys.argv[1], ["-c"] + sys.argv[2:]
real = os.rename
def rename(a, b):
    for _ in range(600):
        if os.path.exists("{marks['go']}"):
            break
        time.sleep(0.1)
    real(a, b)
    open("{marks['renamed']}", "w").close()
    for _ in range(600):
        if os.path.exists("{marks['sent']}"):
            break
        time.sleep(0.1)
os.rename = rename
exec(compile(code, "<launcher>", "exec"))' "$2" "${{@:3}}"
fi
exec "{sys.executable}" "$@"
""")
        (tmp / "mcp.state").write_text("")
        starter = None
        try:
            starter = subprocess.Popen(["bash", str(SCRIPT), "start", *args], env=dict(env, SUTANDO_PY=str(sig_py)),
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)
            for _ in range(400):
                if marks["blocked"].exists() or starter.poll() is not None:
                    break
                time.sleep(0.1)
            assert marks["blocked"].exists(), f"start never blocked reading the reported group: {starter.poll()}"
            assert marks["renamed"].exists(), "the launcher had not reported its group before the signal"
            os.killpg(starter.pid, signal.SIGTERM)
            out = starter.communicate(timeout=30)[0]
            marks["sent"].touch()
            assert starter.returncode != 0, f"start killed by SIGTERM exited 0: {out}"
            time.sleep(3)
            assert not answers(port), "Chrome came up after start was killed mid-abort"
            assert profile_procs(profile) == [], "a process on the profile outlived the killed start"
            assert len((tmp / "chrome.log").read_text().splitlines()) == launches, "the launcher exec'd Chrome after start was killed"
            assert not Path((tmp / "sig.hs").read_text().strip()).exists(), "the killed start left its handshake directory"
        finally:
            marks["sent"].touch()
            cat_hook.unlink()
            if starter and starter.poll() is None:
                starter.kill()
            kill_profile(profile)

        (tmp / "mcp.state").write_text("")
        hs_py = write_exec(tmp / "hs-py", f"""#!/bin/bash
[[ "$2" == *setsid* ]] && echo "$3" >"{tmp}/ign.hs"
exec "{sys.executable}" "$@"
""")
        hold, release, starter = tmp / "ign.hold", tmp / "ign.release", None
        try:
            starter = subprocess.Popen(["bash", str(SCRIPT), "start", *args], env=dict(
                env, SUTANDO_PY=str(hs_py), TERM_IGNORE="1", AGY_LIST_COUNT=str(tmp / "ign.count"),
                AGY_LIST_HOLD_ON="2", HOLD_MARK=str(hold), HOLD_RELEASE=str(release)),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)
            for _ in range(300):
                if hold.exists() or starter.poll() is not None:
                    break
                time.sleep(0.1)
            assert hold.exists(), f"start never reached the registry read under the lock: {starter.poll()}"
            assert answers(port) and profile_procs(profile), "Chrome was not listening before the signal"
            os.killpg(starter.pid, signal.SIGTERM)
            out = starter.communicate(timeout=60)[0]
            assert starter.returncode != 0, f"start killed by SIGTERM exited 0: {out}"
            assert profile_procs(profile) == [], f"a TERM-ignoring Chrome outlived the killed start: {out}"
            assert not answers(port), "CDP is still up after the killed start"
            assert not Path((tmp / "ign.hs").read_text().strip()).exists(), "the killed start left its handshake directory"
        finally:
            release.touch()
            if starter and starter.poll() is None:
                starter.kill()
            kill_profile(profile)

        (tmp / "mcp.state").write_text("")
        pm = {k: tmp / f"probe.{k}" for k in ("fail", "go", "renamed", "release", "lstart", "arm", "armed", "nolstart", "pstable")}
        ps_hook = write_exec(bin_dir / "ps", f"""#!/bin/bash
[[ -e "{pm['fail']}" && "$*" == "-o stat= -p "* ]] && exit 1
[[ -e "{pm['pstable']}" && "$*" == "-Ao pgid=,stat=" ]] && exit 1
if [[ -e "{pm['lstart']}" && "$*" == "-o lstart= -p "* ]]; then echo "Thu Jan  1 00:00:00 1970"; exit 0; fi
[[ -e "{pm['nolstart']}" && "$*" == "-o lstart= -p "* ]] && exit 1
if [[ -e "{pm['arm']}" && "$*" == "-o lstart= -p "* ]]; then
  [[ -e "{pm['armed']}" ]] && {{ echo "Thu Jan  1 00:00:00 1970"; exit 0; }}
  : >"{pm['armed']}"
fi
exec /bin/ps "$@"
""")
        probe_py = write_exec(tmp / "probe-py", f"""#!/bin/bash
if [[ "$2" == *setsid* ]]; then
  echo "$3" >"{tmp}/probe.hs"
  exec "{sys.executable}" -c '
import os, sys, time
code, sys.argv = sys.argv[1], ["-c"] + sys.argv[2:]
real = os.rename
def rename(a, b):
    for _ in range(600):
        if os.path.exists("{pm['go']}"):
            break
        time.sleep(0.1)
    real(a, b)
    open("{pm['renamed']}", "w").close()
    for _ in range(600):
        if os.path.exists("{pm['release']}"):
            break
        time.sleep(0.1)
os.rename = rename
exec(compile(code, "<launcher>", "exec"))' "$2" "${{@:3}}"
fi
exec "{sys.executable}" "$@"
""")
        probe_rm = write_exec(bin_dir / "rm", f"""#!/bin/bash
for a in "$@"; do
  if [[ -e "{pm['fail']}" && -d "$a" && -e "$a/cancel" ]]; then
    : >"{pm['go']}"
    for _ in $(seq 1 100); do [[ -f "{pm['renamed']}" ]] && break; sleep 0.1; done
  fi
done
exec /bin/rm "$@"
""")
        launches, starter = len((tmp / "chrome.log").read_text().splitlines()), None
        try:
            starter = subprocess.Popen(["bash", str(SCRIPT), "start", *args], env=dict(env, SUTANDO_PY=str(probe_py)),
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)
            for _ in range(100):
                if (tmp / "probe.hs").exists() or starter.poll() is not None:
                    break
                time.sleep(0.1)
            assert (tmp / "probe.hs").exists(), f"the launcher never started: {starter.poll()}"
            time.sleep(0.5)
            pm["fail"].touch()
            os.killpg(starter.pid, signal.SIGTERM)
            out = starter.communicate(timeout=60)[0]
            pm["go"].touch()
            pm["release"].touch()
            assert starter.returncode != 0, f"start killed by SIGTERM exited 0: {out}"
            for _ in range(40):
                if pm["renamed"].exists():
                    break
                time.sleep(0.25)
            assert pm["renamed"].exists(), f"the held launcher never resumed: {out}"
            time.sleep(3)
            assert not answers(port), f"Chrome came up after a failed launcher probe: {out}"
            assert profile_procs(profile) == [], "a process on the profile outlived the failed launcher probe"
            assert len((tmp / "chrome.log").read_text().splitlines()) == launches, "the launcher exec'd Chrome after a failed probe removed its cancel file"
        finally:
            pm["release"].touch()
            pm["fail"].unlink(missing_ok=True)
            probe_rm.unlink()
            if starter and starter.poll() is None:
                starter.kill()
            kill_profile(profile)

        foreign = subprocess.Popen(["sleep", "120"], start_new_session=True)
        # revalidated: the leader proves ownership before TERM, ignores it, then stops proving it before KILL.
        for case, extra in (("reused", {}), ("foreign", {"FOREIGN": str(foreign.pid)}),
                            ("revalidated", {"TERM_IGNORE": "1"}), ("unreadable", {})):
            (tmp / "mcp.state").write_text("")
            hold, release, starter = tmp / f"{case}.hold", tmp / f"{case}.release", None
            wrap = write_exec(tmp / f"{case}-py", f"""#!/bin/bash
if [[ "$2" == *setsid* ]]; then
  echo "$3" >"{tmp}/{case}.hs"
  [[ -n "${{FOREIGN:-}}" ]] && exec "{sys.executable}" -c '
import os, sys
code, sys.argv = sys.argv[1], ["-c"] + sys.argv[2:]
real = os.rename
def rename(a, b):
    real(a, b)
    open(b, "w").write(os.environ["FOREIGN"])
os.rename = rename
exec(compile(code, "<launcher>", "exec"))' "$2" "${{@:3}}"
fi
exec "{sys.executable}" "$@"
""")
            trace = open(tmp / f"{case}.trace", "w")
            try:
                starter = subprocess.Popen(["bash", "-x", str(SCRIPT), "start", *args], env=dict(
                    env, SUTANDO_PY=str(wrap), AGY_LIST_COUNT=str(tmp / f"{case}.count"),
                    AGY_LIST_HOLD_ON="2", HOLD_MARK=str(hold), HOLD_RELEASE=str(release),
                    BASH_XTRACEFD=str(trace.fileno()), **extra), pass_fds=(trace.fileno(),),
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)
                for _ in range(300):
                    if hold.exists() or starter.poll() is not None:
                        break
                    time.sleep(0.1)
                assert hold.exists(), f"{case}: start never reached the registry read: {starter.poll()}"
                chrome_pids = profile_procs(profile)
                assert answers(port) and chrome_pids, f"{case}: Chrome was not listening before the signal"
                groups = subprocess.run(["/bin/ps", "-o", "pid=,pgid=,sid=,stat=", "-p", ",".join(map(str, chrome_pids + [starter.pid]))],
                                        capture_output=True, text=True).stdout
                if case == "reused":
                    pm["lstart"].touch()
                if case == "revalidated":
                    pm["arm"].touch()
                if case == "unreadable":
                    pm["nolstart"].touch()
                os.killpg(starter.pid, signal.SIGTERM)
                out = starter.communicate(timeout=60)[0]
                assert starter.returncode != 0, f"{case}: start killed by SIGTERM exited 0: {out}"
                hs_dir = Path((tmp / f"{case}.hs").read_text().strip())
                state = (f"hs exists={hs_dir.exists()} pgid={(hs_dir / 'pgid').read_text() if (hs_dir / 'pgid').exists() else None!r} "
                         f"profile procs before={chrome_pids} after={profile_procs(profile)} cdp={answers(port)}\nstarter={starter.pid} groups before the signal:\n{groups}"
                         + "".join([l for l in (tmp / f"{case}.trace").read_text().splitlines(True) if l.startswith("+")][-150:]))
                assert "could not prove process group" in out, f"{case}: an unproved group was not reported: {out}\n{state}"
                if case in ("reused", "revalidated", "unreadable"):
                    assert profile_procs(profile) == chrome_pids, f"{case}: a group whose leader changed identity was signalled: {out}"
                assert foreign.poll() is None, f"{case}: start signalled a foreign process group: {out}"
                assert Path((tmp / f"{case}.hs").read_text().strip()).exists(), f"{case}: the handshake was removed without proving ownership"
            finally:
                release.touch()
                pm["lstart"].unlink(missing_ok=True)
                pm["arm"].unlink(missing_ok=True)
                pm["armed"].unlink(missing_ok=True)
                pm["nolstart"].unlink(missing_ok=True)
                trace.close()
                if starter and starter.poll() is None:
                    starter.kill()
                kill_profile(profile)
        foreign.kill()

        # ps fails during the exit trap: the group is still signalled (lstart still proves it) but never
        # proved gone, so the dir is kept; reading the failure as "gone" would skip the signal and remove it.
        (tmp / "mcp.state").write_text("")
        hold, release, starter = tmp / "pstable.hold", tmp / "pstable.release", None
        wrap = write_exec(tmp / "pstable-py", f"""#!/bin/bash
[[ "$2" == *setsid* ]] && echo "$3" >"{tmp}/pstable.hs"
exec "{sys.executable}" "$@"
""")
        try:
            starter = subprocess.Popen(["bash", str(SCRIPT), "start", *args], env=dict(
                env, SUTANDO_PY=str(wrap), AGY_LIST_COUNT=str(tmp / "pstable.count"),
                AGY_LIST_HOLD_ON="2", HOLD_MARK=str(hold), HOLD_RELEASE=str(release)),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)
            for _ in range(300):
                if hold.exists() or starter.poll() is not None:
                    break
                time.sleep(0.1)
            assert hold.exists(), f"pstable: start never reached the registry read: {starter.poll()}"
            chrome_pids = profile_procs(profile)
            assert answers(port) and chrome_pids, "pstable: Chrome was not listening before the signal"
            pm["pstable"].touch()
            os.killpg(starter.pid, signal.SIGTERM)
            out = starter.communicate(timeout=60)[0]
            assert starter.returncode != 0, f"pstable: start killed by SIGTERM exited 0: {out}"
            hs_dir = Path((tmp / "pstable.hs").read_text().strip())
            # TERM ends the fake Chrome; with the table unreadable the group is never proved gone, and a
            # dead leader can no longer prove ownership before KILL, so the exit reports one or the other.
            assert "could not prove process group" in out or "did not exit; kill it with" in out, \
                f"pstable: an unreadable table was not reported: {out}"
            assert hs_dir.exists(), "pstable: the handshake dir was removed although the group was never proved gone"
            for _ in range(40):
                if profile_procs(profile) == []:
                    break
                time.sleep(0.25)
            assert profile_procs(profile) == [], f"pstable: the owned group was not signalled while ps failed: {out}"
        finally:
            release.touch()
            pm["pstable"].unlink(missing_ok=True)
            if starter and starter.poll() is None:
                starter.kill()
            kill_profile(profile)
        ps_hook.unlink()

        # on_exit with no launcher forked: nothing will ever read the cancel file, so the dir goes
        # silently. Once one exists, a dead launcher also removes the dir; a live one keeps it and says so.
        funcs = tmp / "on_exit.sh"
        subprocess.run(["bash", "-c", f"sed -n '/^on_exit()/,/^}}/p;/^unproved()/p;/^launcher_state()/,/^}}/p' {SCRIPT} >{funcs}"], check=True)
        probe = ("set -u; source {funcs}; settled=''; PROFILE=p; HS=$(mktemp -d); " "{setup}"
                 "on_exit 2>{tmp}/on_exit.err; rc=$?; {check}")
        for name, setup, check in (
                ("none", "", "[[ ! -d $HS ]] && [[ ! -s {tmp}/on_exit.err ]]"),
                ("dead", "sleep 300 & launcher=$!; kill $launcher; wait $launcher 2>/dev/null; ", "[[ ! -d $HS ]]"),
                ("forked", "sleep 300 & launcher=''; ", "[[ -e $HS/cancel ]] && grep -q 'has not reported' {tmp}/on_exit.err; r=$?; kill $! ; exit $r"),
                ("alive", "sleep 300 & launcher=$!; ", "[[ -e $HS/cancel ]] && grep -q 'has not reported' {tmp}/on_exit.err; r=$?; kill $launcher; exit $r")):
            # The shipped interpreter, not PATH's: /bin/bash is 3.2 on macOS and is what #!/bin/bash runs.
            r = subprocess.run(["/bin/bash", "-c", probe.format(funcs=funcs, setup=setup, check=check.format(tmp=tmp), tmp=tmp)],
                               capture_output=True, text=True)
            assert r.returncode == 0, f"on_exit/{name}: {r.stdout}{r.stderr}{(tmp / 'on_exit.err').read_text()}"

        (bin_dir / "agy").unlink()
        rc, out = run(env, "start", *args)
        assert rc != 0 and "agy not found" in out, f"start without agy should refuse: {out}"
    print("PASS 29/29 agy-browser.sh")


if __name__ == "__main__":
    main()
