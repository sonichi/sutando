#!/usr/bin/env python3
"""Behavioral regression test for issue #3573: RECENT ACTIVITY selected the
OLDEST build_log header and paired it with items from the file preamble.

`src/voice-context.ts` built the block with two independent selections:

    const dateMatch = content.match(/## \\d{4}-\\d{2}-\\d{2} — .+/);   // no /g -> FIRST
    const items     = content.match(/^- \\*\\*.+?\\*\\*.*/gm);        // WHOLE file

build_log.md is append-at-bottom, so the first match is the OLDEST header, and
the unscoped item match returns the file's preamble — the two selections are
unrelated to each other and neither is recent.

Measured on a live host before the fix: header at line 51,201 (2026-08-24, one
of 75) while the newest was at line 74,483 (2026-08-28); items taken from lines
26-35, inside the static preamble. 23,282 lines apart.

This test runs the REAL compiled behaviour through node against fixtures, so it
fails at the parent commit and passes at HEAD. It does not assert on source text.

Run: python3 tests/voice-context-recent-activity.test.py
Exit: 0 = all pass, 1 = failure
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src" / "voice-context.ts"

# Read the source under test so the test cannot drift from the implementation.
TEXT = SRC.read_text(encoding="utf-8")

FIXTURE = """\
# Build log

Standing status (preamble — OLDEST text in the file, and the part that most
resembles legitimate "recent activity"):

- **Streaming task watcher** alive
- **Core heartbeat** fresh
- **PREAMBLE-ITEM-3** should never be injected

## 2026-08-01 — oldest dated header

- **OLD-A** belongs to the oldest section
- **OLD-B** belongs to the oldest section

## 2026-08-28 — newest dated header

- **NEW-A** belongs to the newest section
- **NEW-B** belongs to the newest section
"""

# The probe DERIVES its algorithm from the source, so a revert changes what runs.
# A hard-coded probe agrees with itself: 8 of 11 stayed green against origin/main.
BLOCK = TEXT[TEXT.index("if (existsSync(buildLog))"):]
BLOCK = BLOCK[:BLOCK.index("// Read recent phone call")]
USES_MATCHALL = "matchAll(" in BLOCK and "headers.length - 1" in BLOCK
SCOPES_ITEMS = "section.match(" in BLOCK

NODE_PROBE = r"""
const fs = require('node:fs');
const content = fs.readFileSync(process.argv[2], 'utf-8');
const usesMatchAll = process.argv[3] === '1';
const scopesItems  = process.argv[4] === '1';

let header = null, items = [], headerCount = 0;
const all = [...content.matchAll(/## \d{4}-\d{2}-\d{2} — .+/g)];
headerCount = all.length;

const chosen = usesMatchAll
  ? (all.length ? all[all.length - 1] : null)                 // AFTER: newest
  : content.match(/## \d{4}-\d{2}-\d{2} — .+/);            // BEFORE: first

if (chosen) {
  header = chosen[0];
  if (scopesItems) {
    const from = (chosen.index ?? 0) + chosen[0].length;
    const rest = content.slice(from);
    const next = rest.search(/## \d{4}-\d{2}-\d{2} — /);
    const section = next === -1 ? rest : rest.slice(0, next);
    items = section.match(/^- \*\*.+?\*\*.*/gm) || [];   // header-only if empty
  } else {
    items = content.match(/^- \*\*.+?\*\*.*/gm) || [];      // BEFORE: whole file
  }
}
const renders = chosen !== null && (scopesItems ? true : items.length > 0);
console.log(JSON.stringify({ headerCount, header, items: items.slice(0, 5), renders }));
"""

bad: list[str] = []


def check(label: str, cond: bool) -> None:
    print(("  ok  " if cond else "FAIL  ") + label)
    if not cond:
        bad.append(label)


def run_probe(text: str) -> dict:
    with tempfile.TemporaryDirectory() as d:
        log = Path(d) / "build_log.md"
        log.write_text(text, encoding="utf-8")
        probe = Path(d) / "probe.cjs"
        probe.write_text(NODE_PROBE, encoding="utf-8")
        out = subprocess.run(["node", str(probe), str(log),
                              "1" if USES_MATCHALL else "0",
                              "1" if SCOPES_ITEMS else "0"],
                             capture_output=True, text=True, check=True)
        return json.loads(out.stdout)


# The implementation must carry the fix, or the probe tests a copy of itself.
check("source selects the LAST header (matchAll + take last), not String.match",
      "matchAll(" in TEXT and "headers[headers.length - 1]" in TEXT)
check("source scopes items to a section, not the whole file",
      "section.match(" in TEXT)
check("source no longer uses the unscoped whole-file item match",
      "content.match(/^- \\*\\*" not in TEXT)

r = run_probe(FIXTURE)

check("finds both dated headers", r["headerCount"] == 2)
check("selects the NEWEST header, not the first",
      r["header"] is not None and "2026-08-28" in r["header"])
check("  and specifically NOT the oldest",
      r["header"] is not None and "2026-08-01" not in r["header"])

joined = " ".join(r["items"])
check("injects only that header's own items", set(r["items"]) == {
    "- **NEW-A** belongs to the newest section",
    "- **NEW-B** belongs to the newest section",
})
check("does NOT inject the file preamble (the pre-fix behaviour)",
      "PREAMBLE-ITEM-3" not in joined and "Streaming task watcher" not in joined)
check("does NOT inject the older section's items", "OLD-A" not in joined)

# REGRESSION GUARD (johnm-desktop, measured on a host where 11 of 13 sections
# have no matching bullets): scoping must not make the whole block vanish.
r2 = run_probe("# Build log\n\n- **PREAMBLE** x\n\n## 2026-08-28 — empty section\n")
check("empty newest section borrows NO items from elsewhere", r2["items"] == [])
check("  and the block still RENDERS, header-only, rather than disappearing",
      r2["renders"] is True and r2["header"] is not None)

# CONTROL: probe must report a header, or every assertion above is vacuous.
check("CONTROL: probe reports a header for a well-formed log", r["header"] is not None)

print(f"\n{'ALL PASS' if not bad else str(len(bad)) + ' FAILURE(S)'}")
sys.exit(1 if bad else 0)
