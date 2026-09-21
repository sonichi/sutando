#!/usr/bin/env python3
"""Every documented launch of the task watcher carries `--role session --inbox`.

The external standby supervisor recognises an in-session watcher only by that
tag. A doc, skill step or health-check hint that tells an agent to run the bare
`bash src/watch-tasks-stream.sh` produces a watcher the supervisor cannot see:
the standby arms after its grace period and the inbox has two watcher trees.

Text-level guard, deliberately: the command an agent is told to type is the
defect, so the assertion belongs on the text that tells it. Behavioural
coverage of the tag lives in the watcher-identity and supervisor suites.

Run: python3 tests/docs-watcher-launch-is-tagged.test.py   (exit 0 pass / 1 fail)
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# The canonical startup instructions and the health-check hint an operator is
# told to paste, plus every skill step that names the command.
CANONICAL = [
    REPO / "CLAUDE.md",
    REPO / "AGENTS.md",
    REPO / "docs" / "proactive-loop-rationale.md",
    REPO / "src" / "health-check.py",
]
SKILLS = sorted((REPO / "skills").glob("*/SKILL.md"))
TARGETS = CANONICAL + SKILLS

# A launch is `bash <anything/>watch-tasks-stream.sh`; a tagged one carries
# both flags later on the same line, in either spelling.
LAUNCH = re.compile(r"bash\s+(?:\"?\$?[\w{}./-]*/)?watch-tasks-stream\.sh\"?(?P<rest>[^\n]*)")
TAG = re.compile(r"--role[= ]session\b.*--inbox[= ]")

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ok   " if cond else "  FAIL ") + name + ((" — " + detail) if detail and not cond else ""))
    if not cond:
        failures.append(name)


def bare_launches(text: str) -> list[tuple[int, str]]:
    """(line number, line) for every launch whose line lacks the session tag."""
    hits = []
    for n, line in enumerate(text.splitlines(), 1):
        for m in LAUNCH.finditer(line):
            if not TAG.search(m.group("rest")):
                hits.append((n, line.strip()))
                break
    return hits


# The detector itself, against the exact shapes that shipped: a guard that
# cannot flag the old text would pass vacuously over a regression.
check("detector flags the bare Monitor form",
      bare_launches("stream `bash src/watch-tasks-stream.sh` — it never exits") != [])
check("detector flags the bare command: form",
      bare_launches("`command: 'bash src/watch-tasks-stream.sh'`, `persistent: true`") != [])
check("detector flags a role without an inbox",
      bare_launches("bash src/watch-tasks-stream.sh --role session") != [])
check("detector accepts the tagged form",
      bare_launches('bash src/watch-tasks-stream.sh --role session --inbox "$(bash scripts/sutando-config.sh workspace)/tasks"') == [])
check("detector accepts the = spelling",
      bare_launches("bash src/watch-tasks-stream.sh /w/tasks --role=session --inbox=/w/tasks") == [])
check("detector ignores a mention that is not a launch",
      bare_launches("the stream watcher (watch-tasks-stream.sh) emits TASK_FILE") == [])

# A missing target or an empty glob would make the sweep vacuously pass.
for t in CANONICAL:
    check(f"target exists: {t.relative_to(REPO)}", t.is_file())
check("the skills glob found SKILL.md files", len(SKILLS) >= 3, f"found {len(SKILLS)}")

# Only the files that name a launch at all have something to assert on; the
# count proves the sweep saw the canonical ones rather than an empty glob.
naming = 0
for t in TARGETS:
    if not t.is_file():
        continue
    text = t.read_text(encoding="utf-8")
    if not LAUNCH.search(text):
        continue
    naming += 1
    hits = bare_launches(text)
    check(f"{t.relative_to(REPO)}: every watcher launch is tagged", not hits,
          "; ".join(f"line {n}: {line[:120]}" for n, line in hits))

check("the canonical startup docs name a launch (sweep not vacuous)", naming >= 4,
      f"only {naming} target(s) name the command")

print()
if failures:
    print(f"FAILED: {len(failures)} check(s): " + ", ".join(failures))
    sys.exit(1)
print("PASSED: every documented watcher launch carries --role session --inbox")
