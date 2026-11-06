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

Exact assertions are deliberately the first scoring layer. Subjective quality,
tool traces, filesystem fixtures, and automatic checkout/startup management
can be added without changing the persisted run schema.
