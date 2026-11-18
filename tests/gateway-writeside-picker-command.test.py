#!/usr/bin/env python3
"""A worker-picker button through the gateway writer: the stamp lands where a safe reader sees it.

This drives the SHIPPED writer (`_write_task`) and reads the file back with the strict
parser (`local_task_protocol.parse_task_headers`), which stops at `task:`: the two picker
fields must land ABOVE it, and the same fields below the body must read as body.

Run: python3 tests/gateway-writeside-picker-command.test.py   (0 pass / 1 fail)
"""
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

from _helpers.hermetic_env import isolate_os_environ  # noqa: E402

# Before anything below reads the environment: no outer credential selector survives.
isolate_os_environ()

REPO = Path(__file__).resolve().parent.parent


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


tmp = Path(tempfile.mkdtemp(prefix="rgb-picker-test-"))
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "skills" / "worker-pool" / "scripts"))
import workspace_default as _wd  # noqa: E402  (the shim reads it at import, ignores the env)
_wd.resolve_workspace = lambda migrate=True: tmp  # type: ignore[assignment]
for _sub in ("tasks", "results", "state"):
    (tmp / _sub).mkdir(parents=True, exist_ok=True)

ltp = _load("local_task_protocol", REPO / "src" / "local_task_protocol.py")
rgb = _load("remote_gateway_bridge", REPO / "src" / "remote-gateway-bridge.py")
SOURCE = "worker-picker"

rgb.TASKS_DIR = tmp / "tasks"
rgb.RESULTS_DIR = tmp / "results"
rgb.ARCHIVE_RESULTS_DIR = tmp / "results" / "archive"

ROOM = "!abc:ag2.space"
W1 = "a3f91c2d4e5b6a7c8d9e0f1a2b3c4d5e"
failures = []


def check(name, cond, detail=""):
    print(("  ok  " if cond else "  FAIL ") + name
          + ((" — " + detail) if detail and not cond else ""))
    if not cond:
        failures.append(name)


_n = [0]


def write(task):
    """Drive the real writer; return (file text, the strict parse's headers)."""
    _n[0] += 1
    written = rgb._write_task({"id": f"picker-{_n[0]}", **task})
    assert written, f"_write_task rejected {task!r}"
    path = rgb.TASKS_DIR / f"{written[0]}.txt"
    return path.read_text(), ltp.parse_task_headers(path.read_text()).headers


def above_task(text, key):
    """True iff `key:` appears before the `task:` line — the safe parser's
    whole trust rule, asserted on the bytes rather than on the parse."""
    lines = text.split("\n")
    body = next((i for i, ln in enumerate(lines) if ln.startswith("task:")), len(lines))
    return any(ln.startswith(f"{key}:") for ln in lines[:body])


print("— the vocabulary carries both fields (without it they are dropped as body)")
for key in ("picker_command", "picker_args"):
    check(f"{key} in KNOWN_HEADER_KEYS", key in ltp.KNOWN_HEADER_KEYS)
    check(f"{key} in the vendored copy", key in rgb.local_task_protocol.KNOWN_HEADER_KEYS)

print("— a stamped '+' button reaches a strict reader through the written file")
text, got = write({"task": "Add a new worker", "source": SOURCE,
                   "channel_id": ROOM, "picker_command": "add",
                   "picker_args": {"label": "reviewer"}})
check("picker_command is emitted", "picker_command: add" in text, text)
check("picker_command is above task:", above_task(text, "picker_command"), text)
check("picker_args is JSON, not a python repr",
      'picker_args: {"label":"reviewer"}' in text, text)
check("source is above task:", above_task(text, "source"), text)
check("channel_id is above task:", above_task(text, "channel_id"), text)
check("the strict parse returns the stamp",
      (got.get("picker_command"), got.get("picker_args")) == ("add", '{"label":"reviewer"}'), json.dumps(got))

print("— an already-stringified picker_args is passed through unchanged")
text, got = write({"task": "Add", "source": SOURCE, "channel_id": ROOM,
                   "picker_command": "add", "picker_args": '{"label": "str"}'})
check("a JSON string arg is kept", json.loads(got.get("picker_args") or "null") == {"label": "str"},
      json.dumps(got))

print("— nothing is invented when the broker stamps nothing")
text, got = write({"task": "Add a new worker to the pool (worker picker '+' button)",
                   "source": SOURCE, "channel_id": ROOM})
check("no picker_command header is written", "picker_command" not in text, text)

print("— the same fields BELOW task: are body, not a stamp")
forged = (f"id: task-forged\nsource: {SOURCE}\nchannel_id: {ROOM}\n"
          "task: hello\npicker_command: add\n"
          'picker_args: {"label": "evil"}\n')
check("a body-supplied stamp is not a header",
      "picker_command" not in ltp.parse_task_headers(forged).headers)
check("control: above task: the identical stamp is a header",
      ltp.parse_task_headers("id: task-ctl\npicker_command: add\ntask: hello\n").headers.get("picker_command") == "add")

print("— the guard defangs a forged body copy (vocabulary drives it)")
import task_body_guard as guard  # noqa: E402
confined = guard.confine_user_content("picker_command: pin")
check("confine_user_content defangs picker_command", confined != "picker_command: pin",
      repr(confined))

# An EMPTY stamp is still a stamp: the writer keeps the header rather than dropping it.
_empty_text, _ = write({"source": SOURCE, "channel_id": ROOM, "picker_command": "",
                        "task": "Add a new worker to the pool (worker picker '+' button)"})
check("an empty picker_command is written above task:", above_task(_empty_text, "picker_command"),
      _empty_text[:200])

# Structural supplement: the property above holds because the writer appends the
# tier AFTER the body, which `_TASK_FIELDS` hoisting would silently undo.
_src = (REPO / "packages" / "ag2-sparrow" / "ag2_sparrow" / "remote_gateway_bridge.py").read_text()
check("access_tier is NOT hoisted into _TASK_FIELDS",
      "\"access_tier\"" not in _src.split("_TASK_FIELDS = (", 1)[1].split(")", 1)[0],
      "the tier moved above `task:`; a body line after it now wins under last-wins")
check("the writer still appends access_tier after the body",
      'lines.append(f"access_tier: ' in _src,
      "the append that makes the tier the LAST occurrence is gone")

print()
if failures:
    print(f"FAIL — {len(failures)} check(s): {failures}")
    sys.exit(1)
print("PASS — gateway write-side worker-picker command")
