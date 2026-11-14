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

for rule, warn in [
    ("live-updates", GOOD.replace("db.onChange(id, () => {});", "")),
    ("storage", GOOD.replace("await art.ready;", "await art.ready; localStorage.x = 1;")),
    ("touch-target", GOOD.replace("min-height:44px", "min-height:20px")),
    ("hover-only", GOOD.replace("</style>", ".row:hover .del{display:inline}</style>")),
    ("no-fake-send", GOOD.replace("Sign me up</button>", "Send to everyone</button>")),
]:
    got = cp.check(warn)
    check(f"warning-{rule}", got["ok"] and rule in rules(got, "warnings"))

check("own-confirm-function-allowed", cp.check(GOOD.replace("await art.ready;", "await art.ready; function confirm(x) {}"))["ok"])
check("commented-fetch-ignored", cp.check(GOOD.replace("await art.ready;", "await art.ready; /* fetch(x) */"))["ok"])
check("db-only-page-needs-no-ready",
      cp.check(GOOD.replace("await art.ready;", "").replace("art.me.name", "'x'"))["ok"])
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
