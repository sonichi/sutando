#!/usr/bin/env python3
"""The /stand-card route inlines runtime JSON into a <script> block.

A `</script>` inside any string value must not end that block early; the
route must also keep serving the baked sample when the runtime is absent."""

import http.client
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

os.environ["SUTANDO_TEST_MODE"] = "1"

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import dashboard  # noqa: E402

# The page itself reads window.SUTANDO_STAND_RAW; only the assignment marks an injection.
BREAKOUT = "Echo</script><img src=x onerror=alert(1)>"
LIVE = {"stand": {"name": BREAKOUT, "display": "Act IV"}}

failures = []


def check(label, cond):
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        failures.append(label)


class FakeRun:
    """Stands in for subprocess.run: returns a canned stdout or raises."""

    def __init__(self, stdout=None, exc=None):
        self.stdout, self.exc = stdout, exc

    def __call__(self, *a, **kw):
        if self.exc:
            raise self.exc
        return subprocess.CompletedProcess(a, 0, stdout=self.stdout, stderr="")


def with_run(fake, fn):
    real = dashboard.subprocess.run
    dashboard.subprocess.run = fake
    try:
        return fn()
    finally:
        dashboard.subprocess.run = real


def get(port, path):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("GET", path)
    r = c.getresponse()
    body = r.read().decode()
    c.close()
    return r.status, r.getheader("Content-Type"), body


def main():
    print("stand-card: live payload with a </script> in a value")
    code, ctype, body = with_run(FakeRun(stdout=json.dumps(LIVE)),
                                 dashboard.stand_card_response)
    html = body.decode()
    check("serves 200 text/html", code == 200 and ctype.startswith("text/html"))
    check("live payload is injected", "window.SUTANDO_STAND_RAW = " in html)
    check("no literal </script> from the payload survives",
          html.count("</script>") == html.count("<script>"))
    start = html.index("window.SUTANDO_STAND_RAW = ") + len("window.SUTANDO_STAND_RAW = ")
    end = html.index(";</script>", start)
    check("injected text is still valid JSON decoding to the same payload",
          json.loads(html[start:end]) == LIVE)

    print("stand-card: runtime absent -> baked sample, no injection")
    code, _, body = with_run(FakeRun(exc=FileNotFoundError("bin/sutando")),
                             dashboard.stand_card_response)
    check("still 200 when bin/sutando is missing", code == 200)
    check("no injection on failure", b"window.SUTANDO_STAND_RAW = " not in body)

    print("stand-card: runtime answers with an error object -> no injection")
    code, _, body = with_run(FakeRun(stdout=json.dumps({"error": "nope"})),
                             dashboard.stand_card_response)
    check("200 on error payload", code == 200)
    check("error payload is not injected", b"window.SUTANDO_STAND_RAW = " not in body)

    print("stand-card: /stand-card route through the real handler")
    httpd = dashboard.http.server.ThreadingHTTPServer(("127.0.0.1", 0), dashboard.Handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        code, ctype, html = with_run(FakeRun(stdout=json.dumps(LIVE)),
                                     lambda: get(port, "/stand-card"))
        check("route returns 200 text/html", code == 200 and ctype.startswith("text/html"))
        check("route body carries the escaped payload",
              "<\\/script>" in html and BREAKOUT not in html)
    finally:
        httpd.shutdown()

    print(f"\n{'FAILED: ' + ', '.join(failures) if failures else 'all passed'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
