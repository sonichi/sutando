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
  8. a bare [thread] (task result: thread on the ask) emits `thread-ask`, is
     stripped in any leading order, is distinct from [thread: $id], and is
     neutralized like the others

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
text = f"[thread]\n[channel: {ROOM}]\nupdate"
check("vendored copy parses bare [thread] identically",
      acts(text, mod.parse_markers) == acts(text), str(acts(text, mod.parse_markers)))

# 8
a, body = acts("[thread]\nall green")
check("bare [thread] emits thread-ask", a == [("thread-ask", "")] and body == "all green", f"{a} {body!r}")
a, body = acts(f"[channel: {ROOM}]\n[thread]\n[file: /tmp/x.txt]\nmoved")
check("bare [thread] after [channel:], attach kept",
      a == [("redirect", ROOM), ("thread-ask", ""), ("attach", "/tmp/x.txt")] and body == "moved",
      f"{a} {body!r}")
a, body = acts(f"[thread]\n[thread: {ROOT}]\nboth")
check("bare and rooted forms are distinct actions",
      a == [("thread-ask", ""), ("thread", ROOT)] and body == "both", f"{a} {body!r}")
a, body = acts("[Thread]\n[thread]\nonce")
check("repeated bare [thread] is one action", a == [("thread-ask", "")] and body == "once", f"{a} {body!r}")
for prose in ("[thread]ing is a library primitive", "[thread] prose on one line",
              "[thread]\t- not alone", "[thread]x\nnext"):
    for label, parse in (("src", parse_markers), ("vendored", mod.parse_markers)):
        a, body = acts(prose, parse)
        check(f"{label}: {prose!r} is prose, not a marker", a == [] and body == prose, f"{a} {body!r}")
a, body = acts("[thread] \t\r\nCRLF and trailing blanks")
check("standalone [thread] with trailing blanks and CRLF", a == [("thread-ask", "")]
      and body == "CRLF and trailing blanks", f"{a} {body!r}")
a, body = acts("[thread]")
check("[thread] alone at end of body", a == [("thread-ask", "")] and body == "", f"{a} {body!r}")
# 9. the whole-line boundary holds after another leading marker on the same line
for lead in ("[reply: 12345678901234567]", f"[channel: {ROOM}]", f"[thread: {ROOT}]", "[dm-only]"):
    text = f"{lead} [thread]\nbody"
    for label, parse in (("src", parse_markers), ("vendored", mod.parse_markers)):
        a, body = acts(text, parse)
        check(f"{label}: {lead} [thread] on one line is not a thread-ask",
              ("thread-ask", "") not in a and "[thread]" in body, f"{a} {body!r}")
a, body = acts(f"[channel: {ROOM}]\n[thread]\nbody")
check("on the next line it still counts", ("thread-ask", "") in a and body == "body", f"{a} {body!r}")
# 10. neutralize_markers rewrites only what the parser would read
for prose in ("[thread]ing is a library primitive", "[thread] prose on one line", "use [thread] inline"):
    for label, fn in (("src", neutralize_markers), ("vendored", mod.neutralize_markers)):
        check(f"{label}: neutralize leaves prose {prose!r}", fn(prose) == prose, repr(fn(prose)))
for label, fn, parse in (("src", neutralize_markers, parse_markers), ("vendored", mod.neutralize_markers, mod.parse_markers)):
    out = fn("[thread]\nquoted")
    check(f"{label}: a standalone [thread] is neutralized", out != "[thread]\nquoted" and acts(out, parse)[0] == [], repr(out))
# 11. a skip marker right after the leading markers is a skip, as on the broker
for lead in ("[thread]", f"[thread: {ROOT}]", "[dm-only]", "[reply: 12345678901234567]"):
    for marker, reason in (("[no-send]", "no-send"), ("[REPLIED]", "REPLIED"), ("[deduped: task-9]", "deduped")):
        for label, parse in (("src", parse_markers), ("vendored", mod.parse_markers)):
            a, body = acts(f"{lead}\n{marker}\nvisible", parse)
            check(f"{label}: {lead} then {marker} is a skip", a == [("skip", reason)] and body == "", f"{a} {body!r}")
# after [channel:] it stays text: the team guard withholds redirect-plus-skip for owner review
a, body = acts(f"[channel: {ROOM}]\n[no-send]\nvisible")
check("[channel:] then [no-send] is not turned into a skip", ("redirect", ROOM) in a and body.startswith("[no-send]"), f"{a} {body!r}")
# a delivery consumer reads the guarded body: there a skip after [channel:] is a skip
for marker, reason in (("[no-send]", "no-send"), ("[REPLIED]", "REPLIED"), ("[deduped: task-9]", "deduped")):
    for label, parse in (("src", parse_markers), ("vendored", mod.parse_markers)):
        res = parse(f"[channel: {ROOM}]\n{marker}\nvisible", skip_after_channel=True)
        got = [(x.kind, x.value) for x in res.actions]
        check(f"{label}: delivery verdict, [channel:] then {marker} is a skip",
              got == [("skip", reason)] and res.body == "", f"{got} {res.body!r}")
a, body = acts("[thread]\nsee [no-send] below")
check("a skip word inside prose is not a skip", ("thread-ask", "") in a and "[no-send]" in body, f"{a} {body!r}")
# 12. the shared table (tests/fixtures/thread-ask-cases.json), also run by the TS suite
import json as _json  # noqa: E402
for case in _json.loads((REPO / "tests" / "fixtures" / "thread-ask-cases.json").read_text())["cases"]:
    text = case["body"]
    for label, parse, neutral in (("src", parse_markers, neutralize_markers),
                                  ("vendored", mod.parse_markers, mod.neutralize_markers)):
        a, body = acts(text, parse)
        if case["skip"]:
            check(f"table {label}: {text!r} is a skip", len(a) == 1 and a[0][0] == "skip", f"{a} {body!r}")
        else:
            check(f"table {label}: {text!r} thread-ask={case['thread_ask']}",
                  (("thread-ask", "") in a) is case["thread_ask"] and not any(k == "skip" for k, _ in a), f"{a} {body!r}")
        delivery = [x.kind for x in parse(text, skip_after_channel=True).actions]
        check(f"table {label}: {text!r} delivery skip={case.get('delivery_skip', case['skip'])}",
              (delivery == ["skip"]) is case.get("delivery_skip", case["skip"]), f"{delivery}")
        out = neutral(text)
        check(f"table {label}: neutralize {text!r} quotes [thread]={case['neutralize_quotes_thread']}",
              ("[ thread]" in out) is case["neutralize_quotes_thread"]
              and ("thread-ask", "") not in acts(out, parse)[0], repr(out))
a, body = acts("use [thread] inline")
check("inline bare [thread] is prose", a == [] and body == "use [thread] inline", f"{a} {body!r}")
a, body = acts("[no-send]\n[thread]\nx")
check("skip stays terminal over bare [thread]", a == [("skip", "no-send")], str(a))
a, _ = acts(neutralize_markers("[thread]\nquoted"))
check("neutralized bare [thread] emits no action", a == [], str(a))

if failures:
    print(f"\n{len(failures)} failure(s)")
    sys.exit(1)
print("\nAll [thread:] marker invariants hold.")
