#!/usr/bin/env python3
"""daily-insight must read tags from frontmatter, not any line containing "tags:".

Regression test for owner-visible garbage in the 2026-07-28 briefing:

    Top tags: lives in `.github/workflows/ios-release.yaml`, whose trigger is
    `push:  ios-v*` (+, code-review

`analyze_note_activity()` did `if "tags:" in content` and then took the FIRST
line containing that substring — anywhere in the note. A real note quoting a
GitHub Actions workflow (`push: tags: [ios-v*]`) had its prose parsed as tag
names and shipped to the owner as a statistic.

Test 3 is the control: it feeds exactly that prose and asserts nothing is
extracted, so the fix cannot pass by extracting everything.

Run: python3 tests/daily-insight-frontmatter-tags.test.py
"""
from __future__ import annotations

import importlib.util
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("daily_insight", REPO / "src" / "daily-insight.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

# Fail informatively against a pre-fix module rather than dying on AttributeError,
# so the control run states WHY it failed instead of raising.
if not hasattr(_mod, "_frontmatter_tags"):
    print("  FAIL: daily-insight has no _frontmatter_tags — tags are still parsed "
          "by substring match anywhere in the note body")
    print("daily-insight-frontmatter-tags: 0/1 passed — 1 FAILED")
    raise SystemExit(1)

_passed = 0
_failed = 0


def ok(name: str, cond: bool, detail: str = "") -> None:
    global _passed, _failed
    if cond:
        _passed += 1
    else:
        _failed += 1
        print(f"  FAIL: {name}{(' — ' + detail) if detail else ''}")


# 1. Normal frontmatter, the shape real notes use.
ok("frontmatter list parses",
   _mod._frontmatter_tags("---\ntags: [code-review, pr-2, stando-ui]\nrepo: x\n---\n\nbody\n")
   == ["code-review", "pr-2", "stando-ui"])

# 2. tags: need not be the first frontmatter key.
ok("tags below other keys",
   _mod._frontmatter_tags("---\ntitle: Self-diagnose\ndate: 2026-07-05\ntags: [diagnose, self]\n---\n")
   == ["diagnose", "self"])

# 3. THE CONTROL — the exact prose that poisoned the briefing. A `tags:` inside
#    the body is not a tag declaration, and there is no frontmatter here at all.
POISON = (
    "# fluffy macho guard validation\n"
    "\n"
    "lives in `.github/workflows/ios-release.yaml`, whose trigger is `push: tags: [ios-v*]` (+\n"
    "workflow_dispatch).\n"
    "\n"
    "- Triggers: `push: tags: [ios-v*]` **and `workflow_dispatch`**.\n"
)
ok("prose `tags:` with no frontmatter yields nothing",
   _mod._frontmatter_tags(POISON) == [],
   f"got {_mod._frontmatter_tags(POISON)!r}")

# 4. Same prose BELOW a real frontmatter block must not override the real tags —
#    the old code took the first matching line wherever it was.
ok("body prose does not override real frontmatter tags",
   _mod._frontmatter_tags("---\ntags: [workflow, learned]\n---\n\n" + POISON)
   == ["workflow", "learned"],
   f"got {_mod._frontmatter_tags('---' + chr(10) + 'tags: [workflow, learned]' + chr(10) + '---' + chr(10) + POISON)!r}")

# 5. Degenerate inputs must return empty rather than raise or guess.
ok("no frontmatter at all", _mod._frontmatter_tags("# just a heading\n") == [])
ok("unterminated frontmatter", _mod._frontmatter_tags("---\ntags: [a, b]\nstill open\n") == [])
ok("frontmatter without tags", _mod._frontmatter_tags("---\ntitle: x\n---\nbody\n") == [])
ok("empty tag list", _mod._frontmatter_tags("---\ntags: []\n---\n") == [])
ok("empty file", _mod._frontmatter_tags("") == [])

# 6. Whitespace/format tolerance on the real field.
ok("bare comma list without brackets",
   _mod._frontmatter_tags("---\ntags: alpha, beta\n---\n") == ["alpha", "beta"])
ok("indented tags key",
   _mod._frontmatter_tags("---\n  tags: [x]\n---\n") == ["x"])


# 7. INTEGRATION — exercise analyze_note_activity(), the production path, not just
#    the helper. The first version of this test only called _frontmatter_tags()
#    directly, so the actual note scan (read each file, increment the counter) was
#    never executed and the coverage gate correctly flagged it uncovered. A test
#    that exercises a helper in isolation does not prove the shipped function uses
#    it — that is the same "test a copy, not what ships" defect this repo has hit
#    three separate ways today, and I repeated it here.
with tempfile.TemporaryDirectory() as _td:
    _notes = Path(_td)
    # a) a real note with frontmatter tags
    (_notes / "a-real-note.md").write_text(
        "---\ntags: [alpha, beta]\ncreated: 2026-07-28\n---\n\n# Body\n\nsome text\n")
    # b) the poisoning note: NO frontmatter, `tags:` only inside quoted prose
    (_notes / "b-workflow-quote.md").write_text(POISON)
    # c) both at once — real frontmatter AND the prose below it. The pre-fix code
    #    took whichever `tags:` line came first, so this is the case that decides
    #    whether the scan reads structure or text.
    (_notes / "c-mixed.md").write_text("---\ntags: [gamma]\n---\n\n" + POISON)

    _orig_dir = _mod.NOTES_DIR
    _mod.NOTES_DIR = _notes
    try:
        _stats = _mod.analyze_note_activity()
    finally:
        _mod.NOTES_DIR = _orig_dir

    _tags = {name for name, _count in _stats["top_tags"]}
    ok("integration: only frontmatter tags are counted",
       _tags == {"alpha", "beta", "gamma"}, f"got {sorted(_tags)}")
    ok("integration: no prose fragment leaks into top_tags",
       not any("ios-release" in n or "workflows" in n or "trigger" in n for n in _tags),
       f"got {sorted(_tags)}")
    ok("integration: the mixed note contributes its frontmatter tag",
       "gamma" in _tags, f"got {sorted(_tags)}")
    ok("integration: note total counts every .md",
       _stats["total"] == 3, f"got {_stats['total']}")


print(f"daily-insight-frontmatter-tags: {_passed}/{_passed + _failed} passed"
      + (f" — {_failed} FAILED" if _failed else ""))
raise SystemExit(1 if _failed else 0)
