#!/usr/bin/env python3
"""Tests for skills/commons-app-builder/scripts/check_page.py. Hermetic, no network."""
import importlib.util
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "check_page", _ROOT / "skills" / "commons-app-builder" / "scripts" / "check_page.py")
cp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cp)

passed = []

GOOD = """<!doctype html><html><head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>:root{color-scheme:light dark} button{min-height:44px}
@media (prefers-color-scheme: dark){:root{--bg:#111}}</style></head><body>
<button id="go">Sign me up</button>
<script>(async () => {
  const art = window.artifact, db = art.db;
  await art.ready;
  const id = art.app.database;
  db.onChange(id, () => {});
  await db.read(id);
  const r = await db.createRow(id, {Name: art.me.name});
  if (!r.ok && /waiting for the viewer/.test(r.reason)) {}
})();</script></body></html>"""


def rules(result, bucket):
    return {item["rule"] for item in result[bucket]}


def check(name, cond):
    assert cond, f"FAIL {name}"
    passed.append(name)


good = cp.check(GOOD)
check("good-page-passes", good["ok"] and not good["warnings"])

for rule, bad in [
    ("no-network", GOOD.replace("await art.ready;", "await art.ready; fetch('/x');")),
    ("no-modals", GOOD.replace("await art.ready;", "await art.ready; alert('hi');")),
    ("no-form-submit", GOOD.replace("<button", "<form><button")),
    ("no-history", GOOD.replace("await art.ready;", "await art.ready; history.replaceState({}, '');")),
    ("no-hardcoded-db", GOOD.replace("await db.read(id);", "await db.read('d123');")),
    ("viewport", GOOD.replace('name="viewport"', 'name="x"')),
    ("dark-mode", GOOD.replace("color-scheme:light dark", "").replace("prefers-color-scheme: dark", "min-width: 1px")),
    ("await-ready", GOOD.replace("await art.ready;", "")),
    ("consent-retry", GOOD.replace("waiting for the viewer", "nope")),
]:
    got = cp.check(bad)
    check(f"error-{rule}", not got["ok"] and rule in rules(got, "errors"))

for label, snippet in [
    ("line-comment", "// {call}\n"),
    ("block-comment", "/* {call} */"),
    ("single-quoted", "const hint = '{call}';"),
    ("double-quoted", 'const hint = "{call}";'),
    ("template", "const hint = `{call}`;"),
    ("regex", "const hint = /{call}/;"),
]:
    for rule, statement, bucket in [
        ("await-ready", "await art.ready;", "errors"),
        ("live-updates", "db.onChange(id, () => {});", "warnings"),
    ]:
        page = GOOD.replace(statement, snippet.replace("{call}", statement))
        check(f"{rule}-not-silenced-by-{label}", rule in rules(cp.check(page), bucket))

for rule, warn in [
    ("live-updates", GOOD.replace("db.onChange(id, () => {});", "")),
    ("storage", GOOD.replace("await art.ready;", "await art.ready; localStorage.x = 1;")),
    ("touch-target", GOOD.replace("min-height:44px", "min-height:20px")),
    ("hover-only", GOOD.replace("</style>", ".row:hover .del{display:inline}</style>")),
    ("no-fake-send", GOOD.replace("Sign me up</button>", "Send to everyone</button>")),
]:
    got = cp.check(warn)
    check(f"warning-{rule}", got["ok"] and rule in rules(got, "warnings"))

for call in ("artifact.room.post({text: 'hi'})", "artifact.agent.sendTask({agentId: a, text: 'hi', key: 'k1'})"):
    page = GOOD.replace("Sign me up</button>", "Send to everyone</button>").replace(
        "await art.ready;", f"await art.ready; await {call};")
    check(f"send-button-with-real-send-{call.split('(')[0]}", "no-fake-send" not in rules(cp.check(page), "warnings"))
check("own-confirm-function-allowed", cp.check(GOOD.replace("await art.ready;", "await art.ready; function confirm(x) {}"))["ok"])
# A call that is not a call does not silence no-fake-send: a // comment, a string, a regex.
SEND_BTN = GOOD.replace("Sign me up</button>", "Send to everyone</button>")
for label, snippet in [
    ("line-comment", "// art.room.post({text: 'hi'});"),
    ("block-comment", "/* art.room.post({text: 'hi'}) */"),
    ("single-quoted", "const help = 'call art.room.post( to send';"),
    ("double-quoted", 'const help = "art.agent.sendTask( is how";'),
    ("template", "const help = `art.room.post(${1})`;"),
    ("regex", "const re = /room.post\\(/;"),
    ("regex-with-slash-in-class", "const re = /[/]room.post\\(/;"),
    ("regex-after-if-condition", 'if (true) /room.post(abc)/.test("room.postabc");'),
    ("regex-after-nested-while-condition", "while (f(x)) /sendTask(a)/.test(s);"),
    ("regex-after-else", "if (a) {} else /room.post(abc)/.test(s);"),
]:
    page = SEND_BTN.replace("await art.ready;", "await art.ready; " + snippet + "\n")
    check(f"fake-send-not-silenced-by-{label}", "no-fake-send" in rules(cp.check(page), "warnings"))
# Real calls still count, also next to a URL, a division and an escaped quote.
for label, snippet in [
    ("after-url-string", "const u = 'https://x.example'; await art.room.post({text: u});"),
    ("after-division", "const half = 4 / 2; await art.room.post({text: 'a/b'});"),
    ("after-call-division", "const q = f(4) / g(2); await art.room.post({text: 'x'});"),
    ("after-if-call-division", "if (ok) { const q = f(4) / 2; } await art.room.post({text: 'x'});"),
    ("after-escaped-quote", "const q = 'it\\'s'; await art.agent.sendTask({agentId: 'x', text: q, key: 'k'});"),
    ("after-regex-with-quote", "const re = /'/g; await art.room.post({text: 'x'.replace(re, '')});"),
    ("after-unterminated-slash", "const x = (1\n/ 2); const y = [/\n]; await art.room.post({text: 'x'});"),
]:
    page = SEND_BTN.replace("await art.ready;", "await art.ready; " + snippet + "\n")
    check(f"real-send-counts-{label}", "no-fake-send" not in rules(cp.check(page), "warnings"))
check("commented-fetch-ignored", cp.check(GOOD.replace("await art.ready;", "await art.ready; /* fetch(x) */"))["ok"])
check("db-only-page-needs-no-ready",
      cp.check(GOOD.replace("await art.ready;", "").replace("art.me.name", "'x'"))["ok"])
check("runtime-mentioned-only-in-string-needs-no-ready",
      cp.check(GOOD.replace("await art.ready;", "").replace("art.me.name", "'art.me.name'"))["ok"])
no_database = GOOD.replace("db.onChange(id, () => {});", "").replace(
    "await db.read(id);", "const hint = 'db.read(id)';").replace(
    "const r = await db.createRow(id, {Name: art.me.name});", "const r = {ok: true};")
check("database-mentioned-only-in-string-needs-no-subscription",
      "live-updates" not in rules(cp.check(no_database), "warnings"))
check("body-size-warns", "body-size" in rules(cp.check(GOOD + " " * cp.MAX_BODY_BYTES), "warnings"))

with tempfile.TemporaryDirectory() as tmp:
    page = Path(tmp) / "page.html"
    page.write_text(GOOD, encoding="utf-8")
    check("main-exit-0", cp.main([str(page)]) == 0)
    page.write_text(GOOD.replace('name="viewport"', 'name="x"'), encoding="utf-8")
    check("main-exit-1", cp.main([str(page), "--json"]) == 1)
    check("main-exit-2-missing", cp.main([str(Path(tmp) / "absent.html")]) == 2)
    check("main-exit-2-usage", cp.main([]) == 2)

print(f"commons-app-builder check_page: {len(passed)} passed")
