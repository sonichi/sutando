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

New runs also record `suite.case_fingerprints`, one SHA-256 digest per case ID.
Each digest covers the exact prompt and expectation declaration, with JSON
object keys sorted recursively and an omitted `expect` treated as `{}`.
Descriptions, categories, case order and repetition count do not affect it.
The digest uses ASCII-escaped compact JSON of `{"prompt": ..., "expect": ...}`
and is stored as `sha256:<64 lowercase hexadecimal characters>`.
This adds definition identity without adding raw prompts to saved artifacts;
responses and grading checks can still contain sensitive information.

Comparisons refuse a shared case ID whose recorded definitions differ, even
if both responses passed. The CLI exits 2 with a diagnostic and does not create
or overwrite comparison artifacts; rerun with matching prompts and expectations.
Fingerprint maps must contain exactly the distinct case IDs in the saved rows,
with valid digests. A present but malformed or incomplete map is an input error,
including when the other run is legacy. Read errors and invalid run JSON also
exit 2; an observed regression still exits 1 only with `--fail-on-regression`.
Saved runs reject duplicate JSON object keys, including conflicting or escaped
duplicate case IDs inside a fingerprint map, rather than keeping the last value.

Historical runs without the field remain readable and comparable. JSON
`suite_compatibility` and the Markdown report explicitly mark shared definitions
unverified and name which side lacks provenance. These warnings do not change
the exit code, including with `--fail-on-regression`; a legacy comparison cannot
establish that its prompts or grading rules matched. Do not backfill invented
fingerprints into old results.

Case additions and removals remain listed separately and add `case_set_changed`
to the suite warnings. Shared definitions can be verified while aggregate rates
and latency still describe different case populations. With no shared cases,
shared definitions are unverified. Fingerprints identify declarations rather
than every semantic equivalence: `json_equals: 1` and `json_equals: 1.0`, or
`contains: "OK"` and `contains: ["OK"]`, conservatively differ. They do not
authenticate artifacts or identify the controller's scoring implementation.

## Suite format

Suites are dependency-free JSON. Every case requires `id` and `prompt` and may
use `equals`, `contains`, `regex`, `max_chars`, and `json_equals` assertions:

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
| `contains` | Nonempty string or nonempty list of nonempty strings | Every string occurs, ignoring case |
| `regex` | Nonempty valid Python regex string | Search with ignore-case and multiline flags |
| `max_chars` | Nonnegative integer, including zero | Stripped response length is at most this value |
| `json_equals` | Any finite JSON value, including `null` | Complete response is equivalent JSON; object-key order and whitespace do not matter |

Use `json_equals` to evaluate structured task answers, such as a plan or a
tool-result summary, without requiring one particular JSON serialization:

```json
{
  "id": "structured-summary",
  "prompt": "Do not use tools or change state. Reply only with a JSON object whose ok is true and whose items is the array [1, 2].",
  "expect": {"json_equals": {"ok": true, "items": [1, 2]}}
}
```

Both `{"ok":true,"items":[1,2]}` and `{"items": [1, 2], "ok": true}` pass.
Arrays keep their order; extra or missing object keys fail. Booleans differ
from numbers, while decoded finite numbers compare by value (`1` and `1.0`
match). Numeric decoding uses Python's integers and floating-point values;
use `equals` when the exact numeric spelling or decimal precision matters.
JSON strings remain case-sensitive. Scalars and empty objects/arrays are
valid expected answers, including `false`, `0`, `""`, and `null`.

The entire response must be JSON: Markdown fences, surrounding prose,
trailing documents, duplicate object keys, and non-finite numbers (including
overflow such as `1e400`) fail. A failed parse is a failed check, so the run
continues and records a `json_equals` check with its expected value and
verdict. Other assertions may be combined with it and must also pass.
Expected values must contain only JSON types, string object keys and finite
numbers; invalid values are rejected before runtime probing or task submission.
Suite files also reject duplicate object keys at every level, including nested
expectations and escaped spellings of the same key. The earlier decoder kept
only the last value; remove conflicting duplicate entries before retrying.
Interpreter nesting and integer-decoding limits apply; exceeding them fails
the check. This assertion does not impose a response-size or scoring-time budget.

The controller validates every case before probing the runtime or submitting
any task. Unknown assertion names, invalid values, malformed JSON and unreadable
suite files exit 2 with a diagnostic; fix the indicated field and retry. Direct
`run_suite` callers receive `ValueError` before task submission. Text assertions
are no longer implicitly converted from numbers or other types, and
`max_chars` no longer accepts strings, floats or booleans: use the types above.
Empty `contains` strings/list items and empty `regex` patterns are rejected;
use `equals: ""` or `max_chars: 0` when an empty or whitespace-only response is
the intended answer. To require zero characters, use `regex: "\\A\\Z"` in JSON.
Regexes search the untrimmed response with multiline matching, so `^$` also
matches an empty line in a nonempty response, such as `"WRONG\n"` or
`"WRONG\n\nMORE"`; it does not require an empty response.
Valid grading and saved-run schemas are unchanged. Validation checks regex
compilation, not execution time: backtracking on an agent response can stall
scoring, so suites remain trusted input. Permissive patterns such as `.*` are
accepted and may pass every answer; choose a pattern that checks the intended
response.

Case IDs remain unchanged in saved results and comparisons. Task filenames
include the run ID, case position, a filename-safe case label, and repetition,
so distinct IDs such as `a/b` and `a b` cannot share a task or consume each
other's results even though their filename-safe labels are identical.

Exact assertions are deliberately the first scoring layer. Subjective quality,
tool traces, filesystem fixtures, and automatic checkout/startup management
can be added without changing the persisted run schema.
