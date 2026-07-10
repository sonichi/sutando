# Unit-test coverage gate

CI enforces **≥ 95% unit-test coverage on every changed Python line** in a PR
(`.github/workflows/coverage-gate.yml` → `scripts/coverage-gate.sh`).

## The rule

If your PR adds or modifies executable Python lines, at least 95% of those
lines must be exercised by the standalone test suite (`tests/**/*.test.py`).
PRs that touch no Python pass trivially.

Run it locally before pushing:

```bash
python3 -m pip install coverage diff-cover   # once (venv on Homebrew pythons)
bash scripts/coverage-gate.sh                # gates your branch vs origin/main
```

The failure output lists exactly which changed lines are uncovered, per file.

## Why diff coverage, not a global 95% floor

The tree's historical global coverage is far below 95% — there is a large
legacy surface (bridges with live APIs, GUI automation, launchd installers)
that predates the test push. A global floor at 95% would red every PR
immediately and block all merges, punishing whoever shows up next for debt
they didn't create. The diff gate applies the full 95% bar to **new code
only**: the number the gate protects can't get worse, every merged PR is
held to the target, and the global number converges upward with each change
to legacy files (touch a file → cover your changes).

The whole-tree number is printed in every gate run (job log, "whole-tree
coverage (informational)") so the trend is visible. Once it approaches the
target, flip the gate to a global floor by adding a
`python3 -m coverage report --fail-under=95` step.

## Where the numbers show up

Every gate run posts a **sticky PR comment** (one comment, updated per push
— marker `<!-- coverage-gate-comment -->`) with the diff-coverage verdict,
the whole-tree percentage, and the per-file uncovered-lines table on
failure. The same content lands in the Actions job summary.

Mechanically this is two workflows: the gate (`coverage-gate.yml`) runs on
`pull_request` — where fork PRs get a read-only token — and uploads
`coverage-summary.md` as an artifact; `coverage-comment.yml` fires on
`workflow_run` in the base-repo context with `pull-requests: write` and
posts it. It never checks out PR code, which is what keeps the write token
safe. Note `workflow_run` executes the default branch's copy of the file,
so the comment half activates once merged to main; until then the numbers
are in the job summary.

## What counts / doesn't count

- **Scope**: `src/`, `scripts/`, `skills/` Python (see `.coveragerc`).
- **Excluded lines**: `# pragma: no cover` and `if __name__ == "__main__":`
  blocks. Use the pragma sparingly and only for genuinely untestable
  branches (interactive prompts, macOS-only GUI calls) — a reviewer should
  be able to agree at a glance.
- **Subprocess boundaries**: code exercised only via `subprocess` calls in
  tests is not traced. Prefer importing the module under test (importlib
  pattern — see `tests/_helpers/`) so coverage sees it; this also makes
  tests faster and failures more debuggable.
- **Other languages**: TypeScript and bash are not yet gated. TS is the
  natural next step (`c8` around the existing `tsx --test` run + diff-cover
  on its lcov output); bash has no practical coverage tooling worth the
  complexity. Tracked as a follow-up.

## Escape hatch

`COVERAGE_GATE_FAIL_UNDER=<n>` overrides the bar locally for
experimentation. CI always runs the default (95). If a specific PR
legitimately cannot meet the bar (rare — e.g. a pure launchd-installer
change), the owner can merge over a red gate; the gate is a required
conversation, not an unappealable veto.
