#!/usr/bin/env python3
"""Tests for skills/x-twitter/x-browser.py — everything above the osascript seam.

x-browser drives real Chrome, but ALL of its logic funnels through one I/O
function (`_osascript`). Stubbing that seam makes the script builders, the
target-identity binding, the base64 JS round-trip, every error branch and the
argv dispatch testable without a browser. Only the seam itself and the two
other direct-subprocess helpers stay uncovered.

Covers: recorded-id addressing (the property that stops focus changes
retargeting another tab), __TARGET_GONE__ / __NO_X_TAB__ / __JSERR__ handling,
status-URL construction, JS builder limits, and main()'s dispatch + exit codes.
Exit 0/1."""
import base64
import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "skills" / "x-twitter" / "x-browser.py"

FAILS: list = []


def check(cond, msg):
    print(("  ok  " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def load():
    """Fresh module each time — _TARGET is module-global state."""
    spec = importlib.util.spec_from_file_location("x_browser_under_test", SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def stub(mod, *returns):
    """Replace the ONE io seam. Records every script it was handed."""
    calls = []
    seq = list(returns)

    def fake(script, timeout=20):
        calls.append(script)
        return seq.pop(0) if seq else ""
    mod._osascript = fake
    return calls


# -- target identity ------------------------------------------------------

def test_target_script_addresses_the_recorded_ids():
    mod = load()
    mod._TARGET = {"win": 111, "tab": 222}
    s = mod._target_script("  return 1")
    check("is 111" in s, "binds the recorded WINDOW id")
    check("is 222" in s, "binds the recorded TAB id")
    check("  return 1" in s, "embeds the caller's body")
    check("__TARGET_GONE__" in s, "keeps the gone-sentinel branch")

    mod._TARGET = {"win": 999, "tab": 888}
    other = mod._target_script("  return 1")
    check(other != s, "a DIFFERENT target yields a different script")
    check("is 111" not in other, "does not leak the previous target's id")


def test_record_target_parses_the_id_pair():
    mod = load()
    stub(mod, "4321,8765")
    mod._record_target()
    check(mod._TARGET == {"win": 4321, "tab": 8765},
          f"records both ids as ints (got {mod._TARGET})")


def test_record_target_raises_when_no_x_tab():
    mod = load()
    stub(mod, "__NO_X_TAB__")
    try:
        mod._record_target()
        check(False, "no x.com tab must raise BrowserError")
    except mod.BrowserError as e:
        check("no x.com tab" in str(e), f"names the cause ({e})")


# -- run_js: the seam's four outcomes -------------------------------------

def test_run_js_round_trips_the_snippet_as_base64():
    mod = load()
    mod._TARGET = {"win": 1, "tab": 2}
    calls = stub(mod, "result-value")
    out = mod.run_js("1+1")
    check(out == "result-value", "returns the seam's value")
    payload = base64.b64encode(b"1+1").decode()
    check(payload in calls[0],
          "the JS is base64-encoded into the script (no quote fighting)")
    check(base64.b64decode(payload).decode() == "1+1",
          "and that encoding decodes back to the original snippet")


def test_run_js_records_target_on_first_use_only():
    mod = load()
    calls = stub(mod, "7,9", "value")
    mod.run_js("x")
    check(mod._TARGET == {"win": 7, "tab": 9}, "resolves the target when unset")
    check(len(calls) == 2, f"one resolve + one run (got {len(calls)})")
    stub(mod, "value2")
    mod.run_js("y")
    check(mod._TARGET == {"win": 7, "tab": 9}, "keeps the SAME target afterwards")


def test_run_js_refuses_to_retarget_when_the_tab_is_gone():
    mod = load()
    mod._TARGET = {"win": 1, "tab": 2}
    stub(mod, "__TARGET_GONE__")
    try:
        mod.run_js("x")
        check(False, "__TARGET_GONE__ must raise")
    except mod.BrowserError as e:
        check("refusing to retarget" in str(e),
              f"says it refuses rather than silently picking another tab ({e})")


def test_run_js_surfaces_page_js_errors():
    mod = load()
    mod._TARGET = {"win": 1, "tab": 2}
    stub(mod, "__JSERR__ReferenceError: boom")
    try:
        mod.run_js("x")
        check(False, "__JSERR__ must raise")
    except mod.BrowserError as e:
        check("boom" in str(e), f"carries the page's own message ({e})")


def test_run_js_raises_when_no_x_tab_at_run_time():
    mod = load()
    mod._TARGET = {"win": 1, "tab": 2}
    stub(mod, "__NO_X_TAB__")
    try:
        mod.run_js("x")
        check(False, "__NO_X_TAB__ must raise")
    except mod.BrowserError as e:
        check("no x.com tab" in str(e), f"names the cause ({e})")


# -- pure builders --------------------------------------------------------

def test_status_url_only_synthesises_for_bare_ids():
    mod = load()
    check(mod._status_url("123") == "https://x.com/i/web/status/123",
          "a bare id becomes a status URL")
    for passthrough in ("https://x.com/a/status/1", "http://x.com/b"):
        check(mod._status_url(passthrough) == passthrough,
              f"an existing URL passes through untouched ({passthrough})")


def test_extract_js_honours_the_limit():
    mod = load()
    check("out.length<5" in mod._extract_tweets_js(5), "embeds the limit")
    check(mod._extract_tweets_js(5) != mod._extract_tweets_js(9),
          "different limits produce different snippets")
    check("__JSERR__" in mod._extract_tweets_js(1),
          "keeps the in-page error channel")


def test_browser_error_is_a_runtime_error():
    mod = load()
    check(issubclass(load().BrowserError, RuntimeError),
          "BrowserError subclasses RuntimeError")


# -- argv dispatch --------------------------------------------------------

def _dispatch(mod, argv, **stubs):
    seen = {}
    for name, rc in stubs.items():
        def make(n, r):
            def f(*a, **k):
                seen[n] = a
                if isinstance(r, Exception):
                    raise r
                return r
            return f
        setattr(mod, name, make(name, rc))
    sys.argv = ["x-browser.py"] + argv
    return mod.main(), seen


def test_main_routes_each_subcommand():
    cases = [
        (["whoami"], "cmd_whoami", ()),
        (["home", "--limit", "3"], "cmd_home", (3,)),
        (["read", "42"], "cmd_read", ("42",)),
        (["search", "agents", "--limit", "2"], "cmd_search", ("agents", 2)),
        (["like", "42"], "cmd_like", ("42",)),
        (["reply", "42", "hi"], "cmd_reply", ("42", "hi")),
    ]
    for argv, fn, want in cases:
        mod = load()
        rc, seen = _dispatch(mod, argv, **{fn: 0})
        check(rc == 0 and seen.get(fn) == want,
              f"{argv[0]} -> {fn}{want} (got {seen.get(fn)}, rc={rc})")


def test_main_reports_browser_errors_as_exit_2():
    mod = load()
    rc, _ = _dispatch(mod, ["whoami"], cmd_whoami=mod.BrowserError("plain failure"))
    check(rc == 2, f"BrowserError exits 2 (got {rc})")


def test_main_hints_at_the_apple_events_setting():
    import io
    import contextlib
    mod = load()
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        rc, _ = _dispatch(mod, ["whoami"],
                          cmd_whoami=mod.BrowserError(
                              "Allow JavaScript from Apple Events is off"))
    check(rc == 2, "still exits 2")
    check("Allow JavaScript from Apple Events" in err.getvalue(),
          "prints the enable-it hint for the one error users always hit")


# -- ensure_tab: identity reset + id parsing ------------------------------

def _neuter_sleep(mod):
    mod.time.sleep = lambda *_a, **_k: None


def test_ensure_tab_clears_the_previous_identity_first():
    mod = load()
    _neuter_sleep(mod)
    mod._chrome_running = lambda: True
    mod._TARGET = {"win": 1, "tab": 2}
    seen = {}

    def fake_osa(script, timeout=20):
        seen["target_at_call"] = mod._TARGET
        return "5,6"
    mod._osascript = fake_osa
    mod.run_js = lambda *a, **k: "complete"
    mod.ensure_tab("https://x.com/home")
    check(seen["target_at_call"] is None,
          "the old target is cleared BEFORE resolving a new one "
          "(a new operation must never inherit the previous page)")
    check(mod._TARGET == {"win": 5, "tab": 6}, "then records the new pair")


def test_ensure_tab_requires_chrome():
    mod = load()
    mod._chrome_running = lambda: False
    try:
        mod.ensure_tab("https://x.com/home")
        check(False, "must raise when Chrome is not running")
    except mod.BrowserError as e:
        check("not running" in str(e), f"says so plainly ({e})")


def test_ensure_tab_rejects_an_unparseable_id_pair():
    mod = load()
    _neuter_sleep(mod)
    mod._chrome_running = lambda: True
    mod._osascript = lambda *a, **k: "not-an-id-pair"
    try:
        mod.ensure_tab("https://x.com/home")
        check(False, "garbage ids must raise, not silently proceed")
    except mod.BrowserError as e:
        check("could not identify" in str(e), f"names the failure ({e})")


def test_ensure_tab_base64s_the_url_into_the_script():
    mod = load()
    _neuter_sleep(mod)
    mod._chrome_running = lambda: True
    calls = []
    mod._osascript = lambda s, timeout=20: (calls.append(s), "1,2")[1]
    mod.run_js = lambda *a, **k: "complete"
    mod.ensure_tab("https://x.com/search?q=a b&f=live")
    want = base64.b64encode(b"https://x.com/search?q=a b&f=live").decode()
    check(want in calls[0], "the URL is base64-encoded (survives quoting)")


def test_ensure_tab_survives_a_failing_readystate_poll():
    mod = load()
    _neuter_sleep(mod)
    mod._chrome_running = lambda: True
    mod._osascript = lambda *a, **k: "1,2"

    def flaky(*a, **k):
        raise mod.BrowserError("transient")
    mod.run_js = flaky
    mod.ensure_tab("https://x.com/home", max_wait=0.0)
    check(mod._TARGET == {"win": 1, "tab": 2},
          "a failing readyState poll does not abort the operation")


# -- command surface ------------------------------------------------------

def _wire(mod, js_result):
    mod.ensure_tab = lambda *a, **k: None
    mod.run_js = lambda *a, **k: js_result


def _out(fn, *a):
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = fn(*a)
    return rc, buf.getvalue()


TWEETS = '[{"user":"@a","text":"hello","time":"2026-01-01"}]'


def test_cmd_home_renders_tweets_and_handles_empty():
    mod = load(); _wire(mod, TWEETS)
    rc, out = _out(mod.cmd_home, 5)
    check(rc == 0 and "@a" in out and "hello" in out, f"renders a tweet ({out!r})")
    mod2 = load(); _wire(mod2, "[]")
    rc2, out2 = _out(mod2.cmd_home, 5)
    check(rc2 == 0 and "no tweets visible" in out2,
          "empty result is a message, not a crash")


def test_cmd_search_quotes_the_query_into_the_url():
    mod = load(); _wire(mod, TWEETS)
    seen = {}
    mod.ensure_tab = lambda url, *a, **k: seen.setdefault("url", url)
    rc, out = _out(mod.cmd_search, "a b", 5)
    check(rc == 0, "returns 0")
    check("a%20b" in seen["url"] or "a+b" in seen["url"],
          f"the query is percent-encoded ({seen.get('url')})")
    mod2 = load(); _wire(mod2, "[]")
    rc2, out2 = _out(mod2.cmd_search, "q", 5)
    check("no results visible" in out2, "empty search says so")


def test_cmd_read_reports_a_missing_tweet_as_exit_1():
    mod = load(); _wire(mod, '{"error":"tweet not found"}')
    rc, out = _out(mod.cmd_read, "123")
    check(rc == 1 and "tweet not found" in out,
          f"missing tweet -> exit 1 with the reason (rc={rc}, {out!r})")
    mod2 = load(); _wire(mod2, '{"user":"@a","text":"t","time":"x"}')
    rc2, out2 = _out(mod2.cmd_read, "123")
    check(rc2 == 0 and "@a" in out2, "a found tweet prints and exits 0")


def test_cmd_read_accepts_a_full_url_unchanged():
    mod = load(); _wire(mod, '{"user":"u","text":"t","time":"x"}')
    seen = {}
    mod.ensure_tab = lambda url, *a, **k: seen.setdefault("url", url)
    _out(mod.cmd_read, "https://x.com/z/status/9")
    check(seen["url"] == "https://x.com/z/status/9",
          f"a full URL is not re-synthesised ({seen.get('url')})")


def test_cmd_whoami_prints_the_account():
    mod = load(); _wire(mod, '{"account":"@rui"}')
    rc, out = _out(mod.cmd_whoami)
    check(rc == 0 and "@rui" in out, f"prints the handle ({out!r})")


# -- like / reply: the confirm-or-admit paths -----------------------------

def _seq(mod, *results):
    """run_js returns each result in turn; ensure_tab and sleeps are no-ops."""
    mod.ensure_tab = lambda *a, **k: None
    mod.time.sleep = lambda *_a, **_k: None
    seq = list(results)
    mod.run_js = lambda *a, **k: seq.pop(0) if seq else ""


def test_cmd_like_confirms_before_claiming_success():
    mod = load()
    _seq(mod, '{"clicked":true}', '{"liked":true}')
    rc, out = _out(mod.cmd_like, "1")
    check(rc == 0 and "liked" in out, f"confirmed like -> 0 ({out!r})")

    mod2 = load()
    _seq(mod2, '{"clicked":true}', '{"liked":false}')
    rc2, out2 = _out(mod2.cmd_like, "1")
    check(rc2 == 1 and "not confirmed" in out2,
          f"an UNCONFIRMED like must not claim success (rc={rc2}, {out2!r})")


def test_cmd_like_reports_already_liked_and_missing_tweet():
    mod = load(); _seq(mod, '{"already":true}')
    rc, out = _out(mod.cmd_like, "1")
    check(rc == 0 and "already liked" in out, "already-liked is success, no re-click")

    mod2 = load(); _seq(mod2, '{"error":"no like button"}')
    rc2, out2 = _out(mod2.cmd_like, "1")
    check(rc2 == 1 and "no like button" in out2, "an error exits 1 with the reason")


def test_cmd_reply_raises_when_the_composer_never_opens():
    mod = load()
    _seq(mod, "ok", "noeditor")
    try:
        _out(mod.cmd_reply, "1", "hi")
        check(False, "a missing composer must raise, not post blind")
    except mod.BrowserError as e:
        check("composer did not open" in str(e), f"names it ({e})")


def test_cmd_reply_confirms_the_post_before_claiming_it():
    mod = load()
    mod._os_submit_via_keystroke = lambda: None
    _seq(mod, "ok", "hi", '{"posted":true}')
    rc, out = _out(mod.cmd_reply, "1", "hi")
    check(rc == 0 and "reply posted" in out, f"confirmed -> 0 ({out!r})")

    mod2 = load()
    mod2._os_submit_via_keystroke = lambda: None
    _seq(mod2, "ok", "hi", '{"posted":false}')
    rc2, out2 = _out(mod2.cmd_reply, "1", "hi")
    check(rc2 == 1 and "not confirmed" in out2,
          f"an unconfirmed reply admits it rather than reporting success "
          f"(rc={rc2}, {out2!r})")


def test_cmd_reply_embeds_the_text_as_json():
    mod = load()
    mod._os_submit_via_keystroke = lambda: None
    seen = []
    mod.ensure_tab = lambda *a, **k: None
    mod.time.sleep = lambda *_a, **_k: None
    seq = ["ok", "quote\"y", '{"posted":true}']
    def cap(js, *a, **k):
        seen.append(js)
        return seq.pop(0)
    mod.run_js = cap
    _out(mod.cmd_reply, "1", 'quote"y')
    check(any('quote\\"y' in j or 'quote\"y' in j for j in seen),
          "reply text is JSON-encoded into the snippet (quotes survive)")


def test_ensure_tab_breaks_the_poll_when_ready():
    mod = load()
    _neuter_sleep(mod)
    mod._chrome_running = lambda: True
    mod._osascript = lambda *a, **k: "3,4"
    mod.run_js = lambda *a, **k: "complete"
    mod.ensure_tab("https://x.com/home")
    check(mod._TARGET == {"win": 3, "tab": 4}, "readyState=complete ends the poll")


def main() -> int:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"# {name}")
            fn()
    if FAILS:
        print(f"\nFAILED ({len(FAILS)})")
        return 1
    print("\nPASS — x-browser logic (above the osascript seam)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
