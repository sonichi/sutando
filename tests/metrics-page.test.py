"""Tests for scripts/metrics-page.py — the local PostHog metrics page.

Covers the module without any network or vault/Keychain access:
  - QUERIES shape + the shared real-recurring-users filter
  - collect() success, per-metric error isolation, and the timestamp
  - hogql() success and transient-retry paths (urlopen mocked)
  - _api_key() (vault_intercept mocked into sys.modules)
  - the HTTP handler routes /api/metrics vs the page
  - main() wires a server (ThreadingHTTPServer mocked)
"""
import importlib.util
import io
import json
import os
import sys
import types
import unittest.mock as mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(REPO, "scripts", "metrics-page.py")

spec = importlib.util.spec_from_file_location("metrics_page", PATH)
mp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mp)

_pass = 0
_fail = 0


def ok(label):
    global _pass
    print(f"  PASS: {label}")
    _pass += 1


def fail(label, detail=""):
    global _fail
    print(f"  FAIL: {label}{' — ' + detail if detail else ''}", file=sys.stderr)
    _fail += 1


# ── QUERIES shape + shared filter ────────────────────────────────────────────
expected = {"dau_today", "wau", "mau", "tasks_30d", "dau_series",
            "tasks_by_source", "daily_by_source", "user_tasks_series", "feature_usage"}
if set(mp.QUERIES) == expected and all(mp.REAL in q for q in
        (mp.QUERIES["dau_today"], mp.QUERIES["tasks_by_source"], mp.QUERIES["daily_by_source"])):
    ok("QUERIES has the expected metrics and each carries the real-user filter")
else:
    fail("queries shape", f"{set(mp.QUERIES) ^ expected}")

# daily_by_source must carry an explicit LIMIT (the recent-days truncation fix)
if "limit 10000" in mp.QUERIES["daily_by_source"]:
    ok("daily_by_source has an explicit high LIMIT")
else:
    fail("daily_by_source limit missing")

# ── collect(): success + per-metric error isolation + timestamp ──────────────
calls = []


def fake_runner(q):
    calls.append(q)
    if "feature_used" in q:
        raise RuntimeError("boom")   # one metric fails
    return [[1]]


out = mp.collect(runner=fake_runner)
if (len(calls) == len(mp.QUERIES)
        and out["dau_today"] == [[1]]
        and isinstance(out["feature_usage"], dict) and "error" in out["feature_usage"]
        and "_generated_at" in out):
    ok("collect() runs all metrics, isolates a failure, stamps _generated_at")
else:
    fail("collect", f"{out}")

# ── hogql(): success + transient retry ───────────────────────────────────────
class _Resp:
    def __init__(self, payload):
        self._p = json.dumps(payload).encode()

    def read(self):
        return self._p

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


with mock.patch.object(mp.urllib.request, "urlopen", return_value=_Resp({"results": [[7]]})):
    r = mp.hogql("select 1", key="k")
    if r == [[7]]:
        ok("hogql() returns results on success")
    else:
        fail("hogql success", f"{r}")

# transient 503 then success (patch sleep so the test is instant)
seq = [mp.urllib.error.HTTPError("u", 503, "busy", {}, None), _Resp({"results": [[9]]})]


def _flaky(*a, **k):
    v = seq.pop(0)
    if isinstance(v, Exception):
        raise v
    return v


with mock.patch.object(mp.urllib.request, "urlopen", side_effect=_flaky), \
        mock.patch.object(mp.time, "sleep", lambda *_: None):
    r = mp.hogql("select 1", key="k")
    if r == [[9]]:
        ok("hogql() retries a transient 503 then succeeds")
    else:
        fail("hogql retry", f"{r}")

# a non-transient error propagates
with mock.patch.object(mp.urllib.request, "urlopen",
                       side_effect=mp.urllib.error.HTTPError("u", 403, "no", {}, None)):
    try:
        mp.hogql("select 1", key="k")
        fail("hogql non-transient", "did not raise")
    except mp.urllib.error.HTTPError:
        ok("hogql() propagates a non-transient error")

# ── _api_key(): vault mocked into sys.modules ────────────────────────────────
fake_vault = types.ModuleType("vault_intercept")
fake_vault.get_vault_key = lambda name: f"key-for-{name}"
sys.modules["vault_intercept"] = fake_vault
try:
    if mp._api_key() == "key-for-POSTHOG_PERSONAL_APIKEY":
        ok("_api_key() resolves the key via vault_intercept")
    else:
        fail("_api_key", "wrong key")
finally:
    del sys.modules["vault_intercept"]

# ── handler routing: /api/metrics (JSON) vs page (HTML) ──────────────────────
class FakeHandler(mp.Handler):
    def __init__(self, path):
        self.path = path
        self.sent = {}
        self.wfile = io.BytesIO()
        self._status = None
        self._headers = {}

    def send_response(self, code):
        self._status = code

    def send_header(self, k, v):
        self._headers[k] = v

    def end_headers(self):
        pass


with mock.patch.object(mp, "collect", return_value={"dau_today": [[3]]}):
    h = FakeHandler("/api/metrics")
    h.do_GET()
    body = h.wfile.getvalue()
    if h._headers.get("Content-Type") == "application/json" and json.loads(body)["dau_today"] == [[3]]:
        ok("handler serves JSON on /api/metrics")
    else:
        fail("handler api", f"{h._headers} {body[:80]!r}")

h2 = FakeHandler("/")
h2.do_GET()
if h2._headers.get("Content-Type", "").startswith("text/html") and b"Sutando Metrics" in h2.wfile.getvalue():
    ok("handler serves the HTML page on /")
else:
    fail("handler page", f"{h2._headers}")

# ── main(): server wired (ThreadingHTTPServer mocked) ────────────────────────
served = {}


class FakeServer:
    def __init__(self, addr, handler):
        served["addr"] = addr
        served["handler"] = handler

    def serve_forever(self):
        served["ran"] = True


with mock.patch.object(mp, "ThreadingHTTPServer", FakeServer), mock.patch("builtins.print"):
    mp.main()
if served.get("ran") and served["addr"][0] == "127.0.0.1" and served["handler"] is mp.Handler:
    ok("main() binds localhost and serves with Handler")
else:
    fail("main", f"{served}")


print(f"\n{_pass} passed, {_fail} failed")
sys.exit(1 if _fail else 0)
