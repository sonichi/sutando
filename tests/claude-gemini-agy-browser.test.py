"""Smoke test for skills/claude-gemini/scripts/agy-browser.sh.

A fake Chrome serves /json/version on a free port and a mock agy records its argv, then:
  1. start launches Chrome on the given profile and port, bound to 127.0.0.1 and headless,
     and registers the chrome-devtools MCP server pointed at that port.
  2. A second start launches nothing and registers nothing.
  3. status succeeds while running; stop ends only the profile's Chrome; status then fails.
  4. A port answered by a Chrome on another profile (here a prefix-sharing `<profile>-extra`) is
     refused: start launches and registers nothing, status fails, stop leaves it running.
  5. A chrome-devtools entry on another URL (port 9222 vs 92222 style prefix) is never overwritten.
  6. Without npx, start fails before launching Chrome; if `agy mcp add` fails, start stops the
     Chrome it launched.
  7. start refuses when agy is absent.

Run: python3 tests/claude-gemini-agy-browser.test.py
"""
import os
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
import http.server, os, sys
port = int(next(a.split("=", 1)[1] for a in sys.argv if a.startswith("--remote-debugging-port=")))
with open(os.environ["CHROME_LOG"], "a") as f:
    f.write(" ".join(sys.argv[1:]) + "\\n")
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
if [[ "$1 $2" == "mcp list" ]]; then cat "$MCP_STATE" 2>/dev/null; exit 0; fi
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


def run(env: dict, *args: str) -> tuple[int, str]:
    proc = subprocess.run(["bash", str(SCRIPT), *args], env=env, capture_output=True, text=True, timeout=60)
    return proc.returncode, proc.stdout + proc.stderr


def main() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
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
            subprocess.run(["pkill", "-f", f"--user-data-dir={profile}"], capture_output=True)

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
            assert rc == 0 and "none running" in out, out
            time.sleep(0.5)
            assert foreign.poll() is None and answers(foreign_port), "stop killed a Chrome on a different profile"
        finally:
            foreign.kill()
            foreign.wait()

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
            subprocess.run(["pkill", "-f", f"--user-data-dir={profile} "], capture_output=True)

        (bin_dir / "agy").unlink()
        rc, out = run(env, "start", *args)
        assert rc != 0 and "agy not found" in out, f"start without agy should refuse: {out}"
    print("PASS 8/8 agy-browser.sh")


if __name__ == "__main__":
    main()
