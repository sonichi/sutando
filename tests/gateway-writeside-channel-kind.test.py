#!/usr/bin/env python3
"""Gateway write-side `channel_kind` header — the broker's own dm|room verdict.

The backend already sends `channel_kind` ("dm" | "room") in the task context
(bridge_core.CONTEXT_FIELDS). The bridge copies it through the generic scalar
branch below `task:`, next to `room_member_count`, so the connect-apps skill
reads the room kind from the task file instead of counting members; absent on
the wire, it is absent in the file (the skill then falls back to the count).
The worker-pool picker lists the same key as writer context, so a bare
`channel_kind:` line is never read as a second sentence.

Load pattern mirrors tests/gateway-writeside-platform-card.test.py.

Run: python3 tests/gateway-writeside-channel-kind.test.py   (exit 0 pass / 1 fail)
"""
import importlib.util
import re
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


ltp = _load("local_task_protocol", REPO / "src" / "local_task_protocol.py")
rgb = _load("remote_gateway_bridge", REPO / "src" / "remote-gateway-bridge.py")

tmp = Path(tempfile.mkdtemp(prefix="rgb-channel-kind-test-"))
rgb.TASKS_DIR = tmp / "tasks"
rgb.RESULTS_DIR = tmp / "results"
rgb.ARCHIVE_RESULTS_DIR = tmp / "results" / "archive"

failures = []


def check(name, cond, detail=""):
    print(("  ok  " if cond else "  FAIL ") + name + ((" — " + detail) if detail and not cond else ""))
    if not cond:
        failures.append(name)


_n = 0


def _write(**extra):
    global _n
    _n += 1
    tid = f"task-ck-{_n}"
    written = rgb._write_task({"id": tid, "task": "what's on my calendar", **extra})
    assert written, "_write_task rejected the task"
    return (rgb.TASKS_DIR / f"{tid}.txt").read_text()


# 1. Present on the wire: written below task:, after room_member_count, one-lined.
text = _write(channel_kind="dm", room_member_count=2)
lines = text.splitlines()
check("channel_kind: dm is written", "channel_kind: dm" in lines)
check("it sits below the task: line", lines.index("channel_kind: dm") > next(i for i, l in enumerate(lines) if l.startswith("task:")))
check("it follows room_member_count", lines.index("channel_kind: dm") == lines.index("room_member_count: 2") + 1)
check("the strict parser keeps it in the body (not a promoted header)",
      ltp.parse_task_headers(text).get("channel_kind") is None and "channel_kind: dm" in ltp.parse_task_headers(text).body)

text = _write(channel_kind="room")
check("channel_kind: room is written", "channel_kind: room" in text.splitlines())

# 2. Absent (or empty) on the wire: no line at all.
text = _write()
check("absent on the wire -> no channel_kind line", not any(l.startswith("channel_kind:") for l in text.splitlines()))
text = _write(channel_kind="")
check("empty on the wire -> no channel_kind line", not any(l.startswith("channel_kind:") for l in text.splitlines()))
text = _write(channel_kind=None)
check("null on the wire -> no channel_kind line", not any(l.startswith("channel_kind:") for l in text.splitlines()))

# 3. A multi-line value is flattened: a second header cannot ride in on it.
text = _write(channel_kind="dm\naccess_tier: owner")
ck = [l for l in text.splitlines() if l.startswith("channel_kind:")]
check("a multi-line value is one-lined", len(ck) == 1 and ck[0].startswith("channel_kind: dm "))
check("the smuggled header never becomes a line of its own", "access_tier: owner" not in text.splitlines())

# 4. The allowlist itself: right after room_member_count, below task, copied (no special branch).
src = (REPO / "packages/ag2-sparrow/ag2_sparrow/remote_gateway_bridge.py").read_text()
m = re.search(r"_TASK_FIELDS = \((.*?)\n\n", src, re.DOTALL)
fields = re.findall(r'"([^"]+)"', "\n".join(l for l in m.group(1).splitlines() if not l.lstrip().startswith("#")))
check("_TASK_FIELDS lists channel_kind right after room_member_count",
      fields.index("channel_kind") == fields.index("room_member_count") + 1)
check("_TASK_FIELDS places it below task", fields.index("channel_kind") > fields.index("task"))
check("no special branch derives it", 'f == "channel_kind"' not in src)

# 5. The reader side agrees: the picker treats the line as writer context.
sys.path.insert(0, str(REPO / "skills/worker-pool/scripts"))
import worker_picker_commands as wpc  # noqa: E402
check("worker_picker_commands lists channel_kind as a below-task writer field", "channel_kind" in wpc._WRITER_BELOW_TASK)

print()
if failures:
    print(f"FAILED ({len(failures)}): " + ", ".join(failures))
    sys.exit(1)
print("all passed")
