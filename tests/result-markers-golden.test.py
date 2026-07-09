#!/usr/bin/env python3
"""Golden net over src/result_markers.py (#873) — the Result Router v1 "S0"
regression baseline. Pins the disposition `parse_markers()` produces for a
corpus that exercises EVERY marker path, so any later Router slice that touches
marker handling must reproduce these byte-for-byte.

Bodies are synthetic but mirror real archived `results/*.txt` shapes (no PII in
the repo). Each case declares the expected (actions, body) the current module
produces; a drift breaks the test loudly.

Run: python3 tests/result-markers-golden.test.py
"""
import importlib.util
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("result_markers", _ROOT / "src" / "result_markers.py")
rm = importlib.util.module_from_spec(spec)
sys.modules["result_markers"] = rm
spec.loader.exec_module(rm)

_failures = []
def check(cond, msg):
    if not cond:
        _failures.append(msg)

def kinds(pr):
    return [a.kind for a in pr.actions]

# ── the corpus: (label, body, expected_kinds, expected_body_or_None) ─────────
# expected_body None = don't assert body (only disposition)

# 1. plain deliver — no markers
pr = rm.parse_markers("Here is your answer: 42.")
check(kinds(pr) == [], "plain: no actions")
check(pr.body == "Here is your answer: 42.", "plain: body untouched")

# 2. [no-send] — skip, terminal, body emptied
pr = rm.parse_markers("[no-send]\nInternal note, do not deliver.")
check(kinds(pr) == ["skip"], "no-send: single skip action")
check(pr.actions[0].value == "no-send", "no-send: reason")
check(pr.body == "", "no-send: body emptied (invisible to user)")

# 3. [REPLIED] — skip
pr = rm.parse_markers("[REPLIED]")
check(kinds(pr) == ["skip"] and pr.actions[0].value == "REPLIED", "REPLIED: skip")

# 4. [deduped: task-X] — skip with the holder id in .extra
pr = rm.parse_markers("[deduped: task-1783000000000]\nfull reply lives in holder")
check(kinds(pr) == ["skip"], "deduped: skip")
check(pr.actions[0].value == "deduped", "deduped: reason")
check(pr.actions[0].extra == "task-1783000000000", "deduped: holder id captured in extra")

# 5. [channel: <id>] — redirect, body is text AFTER the marker line
pr = rm.parse_markers("[channel: 1509576272920182874]\nPosting this to #design instead.")
check(kinds(pr) == ["redirect"], "channel: redirect action")
check(pr.actions[0].value == "1509576272920182874", "channel: target id")
check(pr.body == "Posting this to #design instead.", "channel: body is text after marker")

# 6. [file:/path] — attach, marker stripped from body
pr = rm.parse_markers("Here's the screenshot.\n[file: /tmp/shot.png]")
check(kinds(pr) == ["attach"], "file: attach action")
check(pr.actions[0].value == "/tmp/shot.png", "file: path extracted")
check("[file:" not in pr.body, "file: marker stripped from body")
check("screenshot" in pr.body, "file: prose preserved")

# 7. send/attach aliases + multiple attachments in document order
pr = rm.parse_markers("two files [send: /a.pdf] and [attach: /b.png] done")
check(kinds(pr) == ["attach", "attach"], "send/attach: two attach actions")
check([a.value for a in pr.actions] == ["/a.pdf", "/b.png"], "attach: document order")

# 8. skip precedence — a skip marker wins over any redirect/attach in the body
pr = rm.parse_markers("[no-send]\n[channel: 123] [file: /x] should NOT be parsed")
check(kinds(pr) == ["skip"], "skip precedence: only skip, no redirect/attach")

# 9. D7 reply-header preserved on a normal deliver
pr = rm.parse_markers("**[core: 3]**\n_(switched)_\nActual answer here.")
check(kinds(pr) == [], "D7: no actions on plain body")
check(pr.body.startswith("**[core: 3]**"), "D7: header restored onto user-facing body")
check("Actual answer here." in pr.body, "D7: body preserved")

# 10. D7 header + [channel:] — header must not shadow the redirect
pr = rm.parse_markers("**[core: 2]**\n[channel: 999]\nrerouted body")
check(kinds(pr) == ["redirect"], "D7+redirect: redirect still recognized under header")
check(pr.actions[0].value == "999", "D7+redirect: target id")

# 11. D7 header + skip — still invisible (header discarded with body)
pr = rm.parse_markers("**[core: 1]**\n[no-send]\nx")
check(kinds(pr) == ["skip"], "D7+skip: skip terminal")
check(pr.body == "", "D7+skip: body emptied despite header")

# 12. empty input
pr = rm.parse_markers("")
check(kinds(pr) == [] and pr.body == "", "empty input → empty result")

# 13. redirect + attach combined (non-skip): both actions, correct order
pr = rm.parse_markers("[channel: 555]\nsee file [file: /r.txt]")
check(kinds(pr) == ["redirect", "attach"], "redirect+attach: both, redirect first")
check("[file:" not in pr.body, "redirect+attach: attach marker stripped")

if _failures:
    print(f"FAIL ({len(_failures)}):")
    for m in _failures:
        print("  -", m)
    raise SystemExit(1)
print(f"result-markers-golden: all {13} corpus cases passed")
