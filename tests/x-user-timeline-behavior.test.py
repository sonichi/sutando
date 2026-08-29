#!/usr/bin/env python3
"""`get_user_timeline` itself — the function, not the dispatch to it.

keweichen, on the dispatch suite: replacing `get_user_timeline`'s entire body
with `return` left it at 36/36, because that suite substitutes a recorder for the
function before every case. It proves the CLI routes correctly and proves nothing
about the resolver call, the timeline URL, `max_results`/`exclude` encoding, or
response handling.

This file stubs the one HTTP seam (`_bearer_get`) and asserts the requests the
function actually builds and what it does with the reply. Every check here was
confirmed to FAIL against a no-op body.
"""
import contextlib
import importlib.util
import io
import pathlib
import shutil
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = REPO / "skills" / "x-twitter" / "x-post.py"
failures, checked = [], 0


def check(label, ok, detail=""):
    global checked
    checked += 1
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f": {detail}" if not ok else ""))
    if not ok:
        failures.append(label)


_TMP = tempfile.TemporaryDirectory(prefix="xp-beh-")
_ISO = pathlib.Path(_TMP.name) / "skills" / "x-twitter"
_ISO.mkdir(parents=True)
shutil.copy2(SCRIPT, _ISO / SCRIPT.name)


def load():
    spec = importlib.util.spec_from_file_location("xp_beh", _ISO / SCRIPT.name)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run(username, replies, **kw):
    """Drive the real function with _bearer_get stubbed. Returns (urls, output, exit)."""
    mod = load()
    urls = []

    def _get(url):
        urls.append(url)
        return replies.pop(0) if replies else None

    mod._bearer_get = _get
    buf, code = io.StringIO(), 0
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            try:
                mod.get_user_timeline(username, **kw)
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else 1
    except Exception as e:  # a no-op body must not look like success
        return urls, f"RAISED {e}", 99
    return urls, buf.getvalue(), code

TWEET = {"id": "42", "created_at": "2026-08-26T00:00:00Z", "text": "hello world",
         "public_metrics": {"like_count": 7, "retweet_count": 3, "impression_count": 900}}

# --- the resolver request is made, and the handle is stripped of '@' ----------
urls, out, code = run("@Chi_Wang_", [{"data": {"id": "99"}}, {"data": [TWEET]}])
check("resolver request issued first", bool(urls) and "users/by/username/" in urls[0], f"urls={urls[:1]}")
check("leading @ stripped from the handle", bool(urls) and urls[0].endswith("/Chi_Wang_"), f"url={urls[:1]}")
check("timeline request uses the RESOLVED id, not the handle",
      len(urls) > 1 and "/users/99/tweets" in urls[1], f"urls={urls[1:2]}")
check("resolver and timeline are two distinct calls", len(urls) == 2, f"{len(urls)} call(s)")

# --- max_results and exclude reach the URL -----------------------------------
urls, out, code = run("Chi_Wang_", [{"data": {"id": "7"}}, {"data": [TWEET]}], max_results=100)
check("max_results encoded in the timeline URL", len(urls) > 1 and "max_results=100" in urls[1], f"{urls[1:2]}")
urls, out, code = run("Chi_Wang_", [{"data": {"id": "7"}}, {"data": [TWEET]}],
                      max_results=5, exclude="retweets,replies")
check("exclude encoded and percent-escaped",
      len(urls) > 1 and "exclude=retweets%2Creplies" in urls[1], f"{urls[1:2]}")
_u = run("Chi_Wang_", [{"data": {"id": "7"}}, {"data": [TWEET]}])[0]
check("exclude absent from the URL when not given",
      len(_u) > 1 and "exclude=" not in _u[1], f"urls={_u}")

# --- the response is actually rendered ---------------------------------------
urls, out, code = run("Chi_Wang_", [{"data": {"id": "7"}}, {"data": [TWEET]}])
check("tweet text printed", "hello world" in out, out[:70])
check("metrics printed from public_metrics", "likes:7" in out and "rt:3" in out, out[:90])
check("status URL printed with the tweet id", "status/42" in out, out[:90])
check("header names the handle and the count", "@Chi_Wang_ — 1 tweet(s)" in out, out[:70])

# --- failure paths ------------------------------------------------------------
urls, out, code = run("nobody", [{"data": {}}])
check("unresolvable user exits 1", code == 1, f"rc={code}")
check("unresolvable user names the handle", "No such user: @nobody" in out, out[:70])
check("unresolvable user makes no timeline call", len(urls) == 1, f"{len(urls)} call(s)")
urls, out, code = run("Chi_Wang_", [{"data": {"id": "7"}}, {"data": []}])
check("empty timeline says so and does not crash", "No tweets returned" in out and code == 0, f"rc={code} {out[:50]}")

EXPECTED = 16
check(f"all {EXPECTED} checks ran", checked + 1 == EXPECTED, f"ran {checked + 1}")
print(f"\n{checked - len(failures)}/{checked} passed")
sys.exit(1 if failures else 0)
