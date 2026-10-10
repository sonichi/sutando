#!/usr/bin/env python3
"""`[thread: <root event id>]` in the shared marker grammar.

Guards:
  1. a leading [thread: $id] emits a `thread` action and leaves the body clean
  2. it combines with [channel:] in either order
  3. a malformed value (no `$`, whitespace, empty) emits `thread-invalid`, no
     `thread` action, and is still stripped so it never leaks
  4. a root is kept only for the room it belongs to: [dm-only] or a second,
     different [channel:] turns it into `thread-foreign` (posted top level)
  5. a plain body and an inline mention of the marker are untouched
  6. skip markers stay terminal; neutralize_markers takes the marker out of play
  7. the vendored package copy parses identically

Run: python3 tests/result-markers-thread.test.py
Exit: 0 on pass, 1 on fail.
"""

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from result_markers import neutralize_markers, parse_markers  # noqa: E402

ROOT = "$AbCdEf123_thread-root"
ROOM = "!RoomIdAbCdEf:ag2.space"
failures = []


def check(name, ok, detail=""):
    print(("ok: " if ok else "FAIL: ") + name + ("" if ok else f" — {detail}"))
    if not ok:
        failures.append(name)


def acts(text, parse=parse_markers):
    r = parse(text)
    return [(a.kind, a.value) for a in r.actions], r.body


# 1
a, body = acts(f"[thread: {ROOT}]\nstill on it")
check("leading [thread:] emits a thread action", a == [("thread", ROOT)], str(a))
check("…and the marker is stripped", body == "still on it", repr(body))

# 2
for label, text in (("channel-first", f"[channel: {ROOM}]\n[thread: {ROOT}]\nupdate"),
                    ("thread-first", f"[thread: {ROOT}]\n[channel: {ROOM}]\nupdate")):
    a, body = acts(text)
    check(f"{label}: redirect and thread both parsed",
          sorted(a) == sorted([("redirect", ROOM), ("thread", ROOT)]), str(a))
    check(f"{label}: body clean", body == "update", repr(body))

# 3
for raw in ("AbCdEf", "$", "$has space", "", "!RoomIdAbCdEf:ag2.space"):
    a, body = acts(f"[channel: {ROOM}]\n[thread: {raw}]\nupdate")
    check(f"malformed {raw!r}: no thread action", not any(k == "thread" for k, _ in a), str(a))
    check(f"malformed {raw!r}: flagged thread-invalid", ("thread-invalid", raw.strip()) in a, str(a))
    check(f"malformed {raw!r}: redirect kept", ("redirect", ROOM) in a, str(a))
    check(f"malformed {raw!r}: stripped, never leaks", body == "update", repr(body))

# 4
OTHER = "!OtherRoomXyZ:ag2.space"
a, body = acts(f"[dm-only]\n[channel: {ROOM}]\n[thread: {ROOT}]\nprivate")
check("dm-only still suppresses the redirect", not any(k == "redirect" for k, _ in a), str(a))
check("dm-only: the room's root is not sent to the DM", not any(k == "thread" for k, _ in a), str(a))
check("dm-only: flagged thread-foreign", ("thread-foreign", ROOT) in a, str(a))
check("dm-only body clean", body == "private", repr(body))
a, _ = acts(f"[thread: {ROOT}]\nfor the owner\n[dm-only]")
check("dm-only without [channel:]: root dropped too (the DM is not the room it came from)",
      not any(k == "thread" for k, _ in a), str(a))
# The voice forwarder prepends the origin's [channel:] to `[thread:]\n[channel: X]`.
a, body = acts(f"[channel: {OTHER}]\n[thread: {ROOT}]\n[channel: {ROOM}]\nupdate")
check("two rooms named: the root of one never goes to the other",
      not any(k == "thread" for k, _ in a), str(a))
check("two rooms named: flagged thread-foreign", ("thread-foreign", ROOT) in a, str(a))
check("two rooms named: first redirect still decides", ("redirect", OTHER) in a, str(a))
check("two rooms named: body clean", body == "update", repr(body))
a, _ = acts(f"[channel: {ROOM}]\n[thread: {ROOT}]\n[channel: {ROOM}]\nupdate")
check("the same room named twice keeps the root", ("thread", ROOT) in a, str(a))

# 5
a, body = acts("plain body")
check("plain body: no actions", a == [], str(a))
inline = f"see the [thread: {ROOT}] marker docs"
a, body = acts(inline)
check("inline mention is not a marker", a == [] and body == inline, f"{a} {body!r}")

# 6
a, body = acts(f"[no-send]\n[thread: {ROOT}]\nx")
check("skip stays terminal", a == [("skip", "no-send")] and body == "", f"{a} {body!r}")
a, _ = acts(neutralize_markers(f"[thread: {ROOT}]\nquoted"))
check("neutralized marker emits no action", a == [], str(a))

# 7
spec = importlib.util.spec_from_file_location(
    "vendored_result_markers",
    REPO / "packages" / "ag2-sparrow" / "ag2_sparrow" / "result_markers.py")
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod  # dataclasses resolves the module by name
spec.loader.exec_module(mod)
text = f"[thread: {ROOT}]\n[channel: {ROOM}]\nupdate"
check("vendored copy parses identically",
      acts(text, mod.parse_markers) == acts(text), str(acts(text, mod.parse_markers)))

if failures:
    print(f"\n{len(failures)} failure(s)")
    sys.exit(1)
print("\nAll [thread:] marker invariants hold.")
