"""Smoke test for skills/claude-gemini/scripts/agy-browser.sh.

A fake Chrome serves /json/version on a free port and a mock agy records its argv, then:
  1. start launches Chrome on the given profile and port, bound to 127.0.0.1 and headless,
     and registers the chrome-devtools MCP server pointed at that port.
  2. A second start launches nothing and registers nothing.
  3. status succeeds while running; stop ends only the profile's Chrome; status then fails.
  4. A port answered by a Chrome on another profile (here a prefix-sharing `<profile>-extra`) is
     refused: start launches and registers nothing, status fails, stop leaves it running.
  5. A chrome-devtools entry on another URL (port 9222 vs 92222 style prefix) is never overwritten.
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

Run: python3 tests/claude-gemini-agy-browser.test.py
"""
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
  cat "$MCP_STATE" 2>/dev/null; exit 0
fi
if [[ "$1 $2" == "mcp add" ]]; then
  [[ -n "${AGY_ADD_FAIL:-}" ]] && exit 1
  printf '%s\\n' "$@" >>"$AGY_LOG"
  shift 2; name="$1"; shift
  grep -v "^$name " "$MCP_STATE" >"$MCP_STATE.new" 2>/dev/null; mv "$MCP_STATE.new" "$MCP_STATE"
  echo "$name  stdio  enabled  $*" >>"$MCP_STATE"; exit 0
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
            rc, out = run(env, "start", *args)
            assert rc == 0 and "re-registered with chrome-devtools-mcp@1.10.1" in out, f"start kept an unpinned registration: {out}"
            assert (tmp / "agy.log").read_text().splitlines() == added * 2, "re-registration did not use the pinned package"
            added = (tmp / "agy.log").read_text().splitlines()
            entries = [l for l in (tmp / "mcp.state").read_text().splitlines() if l.startswith("chrome-devtools ")]
            assert len(entries) == 1, f"re-registration left {len(entries)} entries: {entries}"
            rc, out = run(env, "status", *args)
            assert rc == 0, f"status should succeed after re-registration: {out}"
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

        (bin_dir / "agy").unlink()
        rc, out = run(env, "start", *args)
        assert rc != 0 and "agy not found" in out, f"start without agy should refuse: {out}"
    print("PASS 15/15 agy-browser.sh")


if __name__ == "__main__":
    main()
