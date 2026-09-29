"""Smoke test for skills/claude-gemini/scripts/agy-browser.sh.

A fake Chrome serves /json/version on a free port and a mock agy records its argv, then:
  1. start launches Chrome on the given profile and port, bound to 127.0.0.1 and headless,
     and registers the chrome-devtools MCP server pointed at that port.
  2. A second start launches nothing and registers nothing.
  3. status succeeds while running; stop ends only the profile's Chrome; status then fails.
  4. start refuses when agy is absent.

Run: python3 tests/claude-gemini-agy-browser.test.py
"""
import os
import socket
import subprocess
import tempfile
import time
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "skills" / "claude-gemini" / "scripts" / "agy-browser.sh"

FAKE_CHROME = """#!/usr/bin/env python3
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
  printf '%s\\n' "$@" >>"$AGY_LOG"
  shift 2; name="$1"; shift
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
            assert added == ["mcp", "add", "chrome-devtools", "npx", "-y", "chrome-devtools-mcp@latest",
                             "--browserUrl", f"http://127.0.0.1:{port}"], f"unexpected mcp add: {added}"

            rc, out = run(env, "start", *args)
            assert rc == 0 and "already listening" in out and "already registered" in out, out
            assert len((tmp / "chrome.log").read_text().splitlines()) == 1, "second start relaunched Chrome"
            assert (tmp / "agy.log").read_text().splitlines() == added, "second start re-registered the MCP"

            rc, out = run(env, "status", *args)
            assert rc == 0, f"status should succeed while running: {out}"
            rc, out = run(env, "stop", *args)
            assert rc == 0 and "stopped" in out, out
            for _ in range(20):
                rc, out = run(env, "status", *args)
                if rc != 0:
                    break
                time.sleep(0.25)
            assert rc != 0 and "not listening" in out, f"status should fail after stop: {out}"
        finally:
            subprocess.run(["pkill", "-f", f"--user-data-dir={profile}"], capture_output=True)

        (bin_dir / "agy").unlink()
        rc, out = run(env, "start", *args)
        assert rc != 0 and "agy not found" in out, f"start without agy should refuse: {out}"
    print("PASS 4/4 agy-browser.sh")


if __name__ == "__main__":
    main()
