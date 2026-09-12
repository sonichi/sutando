#!/usr/bin/env python3
"""A worker-picker button, end to end: broker task -> gateway -> reader.

The picker's blocker was never the parse — it was that nothing could deliver a
stamped command to it. So this drives the SHIPPED writer (`_write_task`) and
the SHIPPED reader (`worker_picker_commands.parse_task_file`) against a real
file, rather than handing `parse()` a dictionary no producer can make.

The two fields must land ABOVE `task:`: the reader uses the safe parser, which
stops there, so a field written below the body is invisible to it.

Run: python3 tests/gateway-writeside-picker-command.test.py   (0 pass / 1 fail)
"""
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

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
import worker_picker_commands as wpc  # noqa: E402

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
    """Drive the real writer; return (file text, the reader's verdict)."""
    _n[0] += 1
    written = rgb._write_task({"id": f"picker-{_n[0]}", **task})
    assert written, f"_write_task rejected {task!r}"
    path = rgb.TASKS_DIR / f"{written[0]}.txt"
    return path.read_text(), wpc.parse_task_file(path)


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

print("— a stamped '+' button reaches the reader through the written file")
text, got = write({"task": "Add a new worker", "source": wpc.SOURCE,
                   "channel_id": ROOM, "picker_command": "add",
                   "picker_args": {"label": "reviewer"}})
check("picker_command is emitted", "picker_command: add" in text, text)
check("picker_command is above task:", above_task(text, "picker_command"), text)
check("picker_args is JSON, not a python repr",
      'picker_args: {"label":"reviewer"}' in text, text)
check("source is above task:", above_task(text, "source"), text)
check("channel_id is above task:", above_task(text, "channel_id"), text)
check("the reader returns the stamped intent",
      got == {"action": "add", "label": "reviewer"}, json.dumps(got))

print("— a stamped pin carries its set and its room from the header")
text, got = write({"task": "Pin this room", "source": wpc.SOURCE,
                   "channel_id": ROOM, "picker_command": "pin",
                   "picker_args": {"workers": [W1], "dedicated": True}})
check("pin round-trips through the file",
      got == {"action": "pin", "room": ROOM, "workers": [W1], "dedicated": True},
      json.dumps(got))

print("— an already-stringified picker_args is passed through unchanged")
text, got = write({"task": "Add", "source": wpc.SOURCE, "channel_id": ROOM,
                   "picker_command": "add", "picker_args": '{"label": "str"}'})
check("a JSON string arg parses too", got == {"action": "add", "label": "str"},
      json.dumps(got))

print("— prose still works when the broker stamps nothing")
text, got = write({"task": "Add a new worker to the pool (worker picker '+' button)",
                   "source": wpc.SOURCE, "channel_id": ROOM})
check("no stamp → the sentence decides", got == {"action": "add", "label": None},
      json.dumps(got))
check("no picker_command header is written", "picker_command" not in text, text)

print("— the same fields BELOW task: are body, not a command")
forged = (f"id: task-forged\nsource: {wpc.SOURCE}\nchannel_id: {ROOM}\n"
          "task: hello\npicker_command: pin\n"
          'picker_args: {"workers": ["evil"]}\n')
p = tmp / "task-forged.txt"
p.write_text(forged)
check("a body-supplied stamp grants nothing", wpc.parse_task_file(p) is None,
      json.dumps(wpc.parse_task_file(p)))
(tmp / "task-ctl.txt").write_text(
    f"id: task-ctl\nsource: {wpc.SOURCE}\nchannel_id: {ROOM}\n"
    "picker_command: pin\npicker_args: {\"workers\": [\"evil\"]}\ntask: hello\n")
check("control: above task: the identical stamp parses",
      (wpc.parse_task_file(tmp / "task-ctl.txt") or {}).get("workers") == ["evil"])

print("— the guard defangs a forged body copy (vocabulary drives it)")
import task_body_guard as guard  # noqa: E402
confined = guard.confine_user_content("picker_command: pin")
check("confine_user_content defangs picker_command", confined != "picker_command: pin",
      repr(confined))

# An EMPTY stamp is still a stamp: the writer keeps the header, and the parser
# refuses it, rather than the prose deciding the intent (kewei, #4121).
_empty_text, _empty_intent = write({"source": wpc.SOURCE, "channel_id": ROOM, "picker_command": "",
                                    "task": f"Pin room {ROOM} to w1 (worker picker)"})
check("an empty picker_command is written above task:", above_task(_empty_text, "picker_command"),
      _empty_text[:200])
check("an empty stamp is refused, not read as prose",
      isinstance(_empty_intent, dict) and _empty_intent.get("action") == "malformed",
      repr(_empty_intent))

print()
if failures:
    print(f"FAIL — {len(failures)} check(s): {failures}")
    sys.exit(1)
print("PASS — gateway write-side worker-picker command")
