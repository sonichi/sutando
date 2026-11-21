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

# --- asking the viewer's own agent, and posting in the room -------------------------------
ASK = GOOD.replace("await db.read(id);", """await db.read(id);
  const a = (await art.agent.list())[0];
  document.getElementById('go').onclick = async () => {
  const sent = await art.agent.sendTask({ agentId: a.id, text: 'Review row 1', key: 'ask.row1.0' });
  if (!sent.ok) console.log(sent.reason);
  };""")
ask = cp.check(ASK)
check("ask-my-agent-page-passes", ask["ok"] and not ask["warnings"])
no_key = cp.check(ASK.replace(", key: 'ask.row1.0'", ""))
check("error-agent-task-key", not no_key["ok"] and "agent-task-key" in rules(no_key, "errors"))
for unstable in ("'ask.' + Date.now()", "String(Math.random())", "crypto.randomUUID()"):
    got = cp.check(ASK.replace("key: 'ask.row1.0'", "key: " + unstable))
    check(f"error-agent-task-key-unstable:{unstable}",
          not got["ok"] and "agent-task-key-unstable" in rules(got, "errors"))
old_name = cp.check(ASK.replace("art.agent.sendTask(", "art.sendToMyAgent(").replace(", key: 'ask.row1.0'", ""))
check("error-agent-task-key-on-the-old-name", "agent-task-key" in rules(old_name, "errors"))
# The key is judged on the call's own argument, not on any `key` elsewhere in the page.
other_key = cp.check(ASK.replace("await db.read(id);", "await db.read(id); const unrelated = { key: 'x' };")
                     .replace(", key: 'ask.row1.0'", ""))
check("error-agent-task-key-despite-another-key-in-the-page",
      "agent-task-key" in rules(other_key, "errors"))
other_clock = cp.check(ASK.replace("await db.read(id);", "await db.read(id); const unrelated = { key: Date.now() };"))
check("a-clock-key-elsewhere-is-not-this-requests-key",
      other_clock["ok"] and "agent-task-key-unstable" not in rules(other_clock, "errors"))
nested = cp.check(ASK.replace("text: 'Review row 1'", "text: label({ key: Date.now() })"))
check("a-key-nested-in-another-argument-is-not-the-request-key", nested["ok"])
check("bracket-call-without-a-key-is-caught",
      "agent-task-key" in rules(cp.check(ASK.replace("art.agent.sendTask(", "art.agent['sendTask'](")
                                         .replace(", key: 'ask.row1.0'", "")), "errors"))
# A key that comes from a helper cannot be proved from the source: a warning names the helper.
HELPER = "const rid = (k) => 'hp-' + k + Date.now().toString(36) + Math.random().toString(36);"
helper = cp.check(ASK.replace("await db.read(id);", "await db.read(id); " + HELPER)
                  .replace("key: 'ask.row1.0'", "key: rid('x')"))
check("warning-agent-task-key-unproven-helper",
      helper["ok"] and "agent-task-key-unproven" in rules(helper, "warnings"))
check("the-unproven-warning-names-the-helper",
      any("`rid`" in w["message"] for w in helper["warnings"]))
alias = cp.check(ASK.replace("await db.read(id);", "await db.read(id); const key = Date.now().toString();")
                 .replace("key: 'ask.row1.0'", "key"))
check("warning-agent-task-key-unproven-alias", "agent-task-key-unproven" in rules(alias, "warnings"))
fn = cp.check(ASK.replace("await db.read(id);", "await db.read(id); function makeKey(r) { return r + Math.random(); }")
              .replace("key: 'ask.row1.0'", "key: makeKey(1)"))
check("warning-agent-task-key-unproven-function", "agent-task-key-unproven" in rules(fn, "warnings"))
stable_helper = cp.check(ASK.replace("await db.read(id);", "await db.read(id); const keyFor = (r) => 'ask.' + r;")
                         .replace("key: 'ask.row1.0'", "key: keyFor(1)"))
check("a-stable-helper-is-clean", stable_helper["ok"] and not stable_helper["warnings"])
# What cannot be judged is left alone: a request built elsewhere, or spread in.
check("a-request-passed-as-a-variable-is-not-judged",
      cp.check(ASK.replace("{ agentId: a.id, text: 'Review row 1', key: 'ask.row1.0' }", "request"))["ok"])
check("a-spread-request-is-not-judged",
      cp.check(ASK.replace("{ agentId: a.id, text: 'Review row 1', key: 'ask.row1.0' }", "{ ...request }"))["ok"])
check("a-key-with-a-comma-in-a-string-is-read-whole",
      cp.check(ASK.replace("key: 'ask.row1.0'", "key: ['ask', 'a,b'].join('.')"))["ok"])

check("stable-key-from-a-variable-passes",
      cp.check(ASK.replace("key: 'ask.row1.0'", "key: keyFor(row.id)"))["ok"])
check("shorthand-key-passes", cp.check(ASK.replace("key: 'ask.row1.0'", "key"))["ok"])
silent = cp.check(ASK.replace("if (!sent.ok) console.log(sent.reason);", "").replace("r.reason", "r.why"))
check("warning-agent-task-refusal", silent["ok"] and "agent-task-refusal" in rules(silent, "warnings"))
POST = GOOD.replace("await db.read(id);", """await db.read(id);
  document.getElementById('go').onclick = async () => {
  const p = await art.room.post({ text: 'Lunch is ready', key: 'lunch.1' });
  if (!p.posted) console.log(p.reason);
  };""")
check("room-post-page-passes", cp.check(POST)["ok"] and not cp.check(POST)["warnings"])
unread = cp.check(POST.replace("if (!p.posted) console.log(p.reason);", "").replace("r.reason", "r.why"))
check("warning-room-post-refusal", "room-post-refusal" in rules(unread, "warnings"))
# A button that says Send is honest once the page really posts or asks.
check("send-button-allowed-with-room-post",
      "no-fake-send" not in rules(cp.check(POST.replace("Sign me up</button>", "Send to the room</button>")), "warnings"))
check("send-button-allowed-with-sendTask",
      "no-fake-send" not in rules(cp.check(ASK.replace("Sign me up</button>", "Send to my agent</button>")), "warnings"))

check("own-confirm-function-allowed", cp.check(GOOD.replace("await art.ready;", "await art.ready; function confirm(x) {}"))["ok"])
check("commented-fetch-ignored", cp.check(GOOD.replace("await art.ready;", "await art.ready; /* fetch(x) */"))["ok"])
check("db-only-page-needs-no-ready",
      cp.check(GOOD.replace("await art.ready;", "").replace("art.me.name", "'x'"))["ok"])
# --- only a deliberate click sends ------------------------------------------------------------
SEND = "await art.agent.sendTask({ agentId: a.id, text: 'Review row 1', key: 'ask.row1.0' })"
CLICK = "document.getElementById('go').onclick = async () => {"
check("click-fixture-has-the-pieces", SEND in ASK and CLICK in ASK)
for name, page in [
    ("timer", ASK.replace(CLICK, "setInterval(async () => {").replace("\n  };", "\n  }, 60000);")),
    ("data-watch", ASK.replace(CLICK, "db.onChange(id, async () => {").replace("\n  };", "\n  });")),
    ("page-event", ASK.replace(CLICK, "art.on('ping', async () => {").replace("\n  };", "\n  });")),
    ("load", ASK.replace(CLICK, "window.addEventListener('load', async () => {").replace("\n  };", "\n  });")),
    ("input-change", ASK.replace(CLICK, "document.getElementById('go').addEventListener('change', async () => {")
     .replace("\n  };", "\n  });")),
    ("no-click-handler-at-all", ASK.replace(CLICK, "{").replace("\n  };", "\n  }")),
    ("room-post-from-a-timer", ASK + "<script>document.body.onclick = () => {}; setTimeout(() => "
     "window.artifact.room.post({ text: 'hi' }).then((r) => r.posted), 5);</script>"),
]:
    got = cp.check(page)
    check(f"error-send-not-from-click:{name}", not got["ok"] and "send-not-from-click" in rules(got, "errors"))
for name, page in [
    ("addEventListener-click", ASK.replace(CLICK, "document.getElementById('go').addEventListener('click', async () => {")
     .replace("\n  };", "\n  });")),
    ("named-function-called-from-a-click", ASK.replace(CLICK, "const askNow = async () => {")
     .replace("\n  };", "\n  };\n  document.getElementById('go').onclick = () => askNow();")),
    ("through-two-named-functions", ASK.replace(CLICK, "async function askNow() {")
     .replace("\n  };", "\n  }\n  const press = () => askNow();\n  document.getElementById('go').onclick = press;")),
    ("a-timer-started-by-the-click", ASK.replace(CLICK, CLICK + " setTimeout(async () => {")
     .replace("\n  };", "\n  }, 0); };")),
    ("a-watch-that-draws-the-button", ASK.replace(CLICK, "db.onChange(id, () => { document.getElementById('go').onclick = async () => {")
     .replace("\n  };", "\n  }; });")),
    ("a-line-comment-naming-the-function", ASK.replace(CLICK, "// askNow runs on a click\n  const askNow = async () => {")
     .replace("\n  };", "\n  };\n  document.getElementById('go').onclick = askNow;")),
]:
    got = cp.check(page)
    check(f"click-only-send-passes:{name}", got["ok"] and not got["warnings"])
for name, page in [
    ("the-top-of-the-script", ASK.replace(CLICK, "document.body.onclick = () => {}; {").replace("\n  };", "\n  }")),
    ("a-named-function-also-called-on-load", ASK.replace(CLICK, "const askNow = async () => {")
     .replace("\n  };", "\n  };\n  document.getElementById('go').onclick = askNow;\n  askNow();")),
    ("a-named-function-handed-to-a-timer", ASK.replace(CLICK, "const askNow = async () => {")
     .replace("\n  };", "\n  };\n  document.getElementById('go').onclick = askNow;\n  setInterval(askNow, 9000);")),
]:
    got = cp.check(page)
    check(f"warning-send-click-unproven:{name}",
          got["ok"] and "send-click-unproven" in rules(got, "warnings"))
check("a-page-that-sends-nothing-has-neither-finding",
      not {"send-not-from-click", "send-click-unproven"} & (rules(good, "errors") | rules(good, "warnings")))

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
