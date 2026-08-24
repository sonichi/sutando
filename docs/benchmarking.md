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
  --suite benchmarks/smoke.json \
  --label current \
  --repeat 3
```

`doctor` exits 0 for a writable workspace with a fresh core heartbeat, 1 when
the paths are usable but no live core is visible, and 2 for an unusable
workspace.

The command writes `run.json` and `report.md` under
`benchmark-runs/<run-id>/` by default. The smoke suite is intentionally
read-only, but the task subject still has normal Sutando permissions; inspect
custom suites before running them.

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
non-zero exit for automation.

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
