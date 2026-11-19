# Sutando black-box benchmarks

`sutando-bench` submits ordinary task files to a running Sutando workspace,
waits for ordinary result files, scores responses outside the subject, and
writes machine-readable plus reviewer-readable artifacts. It exercises the
task queue, watcher, core agent, and result path without trusting Sutando to
grade itself.

## Run the smoke suite

Resolve the workspace belonging to the Sutando instance under test, then run:

```bash
python3 scripts/sutando-bench.py doctor --workspace /path/to/workspace
python3 scripts/sutando-bench.py run \
  --workspace /path/to/workspace \
  --sutando-config /path/to/that/engine/scripts/sutando-config.sh \
  --suite benchmarks/smoke.json \
  --label current \
  --repeat 3
```

`--sutando-config` must point at the engine under test. It defaults to the
script beside `sutando-bench`, which is correct when the runner is invoked from
that engine. The command refuses to start when the runtime descriptor belongs
to a different workspace or has no packaged/Git revision attribution.

`doctor` exits 0 for a writable workspace with a fresh core heartbeat, 1 when
the paths are usable but no live core is visible, and 2 for an unusable
workspace.

The command writes `run.json` and `report.md` under
`benchmark-runs/<run-id>/` by default. A suite is owner-trust input: Sutando
executes each prompt with the same capability as an owner message, and neither
the declared access tier nor the task envelope constrains it. The smoke suite
is intentionally read-only; inspect every custom or externally sourced suite
before running it.

Every run captures the runtime descriptor before the first task and after the
last task. `run.json` records the full revision, short commit, source, branch,
dirty state, Git tree SHA or packaged tree digest, build time, runtime id, and a
content-aware `version_key`. A clean Git revision, or a packaged revision with
its tree digest, is marked `exact`. If the identity changes or the final probe
fails, artifacts are still written for diagnosis but the command exits 2 and
the report marks the version as unstable. This prevents a long campaign from
being attributed to an engine that was upgraded midway through the run.

## Re-render and compare

```bash
python3 scripts/sutando-bench.py report benchmark-runs/<run-id>/run.json
python3 scripts/sutando-bench.py compare \
  benchmark-runs/baseline/run.json \
  benchmark-runs/candidate/run.json \
  --output benchmark-runs/comparison \
  --fail-on-regression
```

The comparison flags a lower pass rate, more timeouts, or a p95 latency
increase greater than 20%. `--fail-on-regression` turns those findings into a
non-zero exit for automation. Both the Markdown and JSON comparison artifacts
include baseline/candidate runtime identities, whether the versions are equal,
and warnings for legacy, unstable, dirty, or otherwise inexact attribution.

## Suite format

Suites are dependency-free JSON. Every case requires `id` and `prompt` and may
use `equals`, `contains`, `regex`, and `max_chars` assertions:

```json
{
  "schema": 1,
  "name": "example",
  "cases": [
    {
      "id": "capital",
      "prompt": "What is the capital of France?",
      "expect": {"contains": "Paris", "max_chars": 80}
    }
  ]
}
```

The suite must be an object with integer `schema: 1`, a nonempty string `name`,
and a nonempty `cases` list. Each case is an object with a unique nonempty
string `id` and a nonempty string `prompt`; whitespace-only values are rejected.
Optional metadata fields remain available.

`expect` may be omitted or `{}` to require a nonempty response. When supplied,
it must be an object containing only these assertions; all specified checks
must pass:

| Assertion | Value | Check |
| --- | --- | --- |
| `equals` | String, including empty | Exact match after stripping response whitespace |
| `contains` | String or nonempty list of strings | Every string occurs, ignoring case |
| `regex` | Valid Python regex string | Search with ignore-case and multiline flags |
| `max_chars` | Nonnegative integer, including zero | Stripped response length is at most this value |

The controller validates every case before probing the runtime or submitting
any task. Unknown assertion names, invalid values, malformed JSON and unreadable
suite files exit 2 with a diagnostic; fix the indicated field and retry. Direct
`run_suite` callers receive `ValueError` before task submission. Text assertions
are no longer implicitly converted from numbers or other types, and
`max_chars` no longer accepts strings, floats or booleans: use the types above.
Valid grading and saved-run schemas are unchanged. Validation checks regex
compilation, not execution time; suites remain trusted input.

Case IDs remain unchanged in saved results and comparisons. Task filenames
include the run ID, case position, a filename-safe case label, and repetition,
so distinct IDs such as `a/b` and `a b` cannot share a task or consume each
other's results even though their filename-safe labels are identical.

Exact assertions are deliberately the first scoring layer. Subjective quality,
tool traces, filesystem fixtures, and automatic checkout/startup management
can be added without changing the persisted run schema.
