# Sutando reliability evaluation: final results

Concluded at Rui's request on 2026-10-07 after a full day of satisfactory real use. The recurring improvement monitor has been deleted. The local improvements remain installed. This closes the evaluation by owner acceptance; the observer's stricter minimum-eight certification remains unsupported.

## Owner evidence

Rui supplied this first-hand assessment directly on 2026-10-07, describing use yesterday for a whole day:

> Rui used this version of Sutando for a whole day to help team perform code reviews, tests, coding, engaging with Codex core sutando for PR verifications. The Sutando was able to perform as intended in most of the time that it only get back to Rui with necessary TODOs and then perform the work quite autonomously and accurately. Rui gives 9 in accuracy and 8 in reliability.

Owner ratings: **accuracy 9/10; reliability 8/10**. Capability was not separately rated. These are Rui's experiential ratings; the observer did not independently replay every task in that day.

The reported activities span team code reviews, tests, coding, and PR verification with Codex core Sutando. Rui reports that Sutando usually worked as intended, escalated necessary TODOs, and otherwise worked autonomously and accurately.

## Observer measurements

Final deployed source: `32aa78011113f8616cd82fc244576dc12e0492cf`. The report commit adds documentation only. The experiment accumulated these scoped observations through the last check, refreshed 2026-10-07T13:27:36.686269UTC:

| Measure | Result | Scope |
|---|---|---|
| Numeric reporting correctness |283/290 (97.6%) across28 reports | Selected mechanical numeric claims |
| Collection admission |36/43 (83.7%) | All configured scopes admitted before consumer execution |
| Distinct substantive tasks partly assessed |32:research 1, review 26, fix 5 | Individual evidence stages |
| Complete substantive task outcomes independently assessed |0/30 target | Original broader observer rubric |
| Pending candidates retained |511 | Pending evidence, not learned dossier facts |
| Overall observer capability/accuracy/reliability | Unrated | Component percentages do not supply overall scores |

Seven errors in the fixed numeric family remain counted. An additional selected-account count error (222 versus 225) remains recorded outside that family. Failed collection admissions, initial retries and visibility gaps remain part of the history. Unknown/coalesced slots were not invented as additional successful or failed attempts. Current collection membership grew to54 production + 25 dev; it was re-enumerated rather than held fixed at the initial 77 rooms.

At the latest bounded read, 54 production and 25 dev rooms had no read errors. The 33 owned runtime files and 5 workspace overrides matched the deployment, and source status was clean. Successful observer reads are not evidence of natural task completion or deleted-history recovery.

Of 97 copied natural 13 proposals, 24 were assessed against all retained selected full bodies: 5 supported attributed statements, 12 underqualified, 7 unsupported extensions; 73 remain unassessed. Three new proposals were previously underqualified. Missing selected support is not proof that a claim is false. Literal quotes, process exit 0, pending acceptance and unchanged document hashes do not establish semantic truth, human identity, authority or successful fact learning.

A separate PR #5169 comment reported a 21-second owner reply after correcting a scratch pairing setup. Its exact-head comment metadata and elapsed-time arithmetic were verified; raw Discord exchange, authenticated cron processing and cleanup effects were not independently established. It adds no full-outcome credit here.

## Changes under evaluation

The historically evaluated local implementation included the following changes. The learning-window changes are in this PR; cron and GitHub verification are separate review concerns:

- Retain a single outstanding cron payload; preserve delegated work and effective Claude evidence-hook registration.
- Collect all configured room scopes using real cursors and bounded pages; persist immutable receipts before progress and refuse incomplete admission.
- Validate fresh consumer proposal returns against exact scoped receipts; preserve pending candidates, rejected output and provenance. Provide a read-only preflight using the same production validator.
- Compare full document hashes and identities before/after attempts with bounded physical-retention classifications.
- Keep detached learning consumers outside the core inbox role; expose bounded failure stages and overlapping versus unique receipt populations.
- Render current PR/check/readiness claims separately; route optional publication through the existing runtime approval owner and bind supplied task context to approval and replay.

Optional integrations and same-user helper access do not establish a comprehensive authorization boundary. Arbitrary direct publication paths, authentic human grants, task principal authentication and general semantic validation remain outside the demonstrated guarantees.

## Regression evidence

A fresh conclusion run on the evaluated source passed **215 tests across22 unittest suites**, plus **12/12 mutation checks** (23 commands total, all exit 0). Detailed commands and actual output tails are attached in [reliability-v3-results.json](reliability-v3-results.json).

Representative stored before/after evidence follows. The before outputs were captured during development, not rerun or relabelled as current upstream results.

Before receipt-population reporting, `python3 tests/learning-proposal-preflight.test.py`:

```text
KeyError: 'receipt_population'
Ran 4 tests in 0.191s
FAILED (errors=1)
```

At evaluated HEAD, the same command with the expanded suite:

```text
Ran 5 tests in 0.249s
OK
```

Before task-context binding, `python3 tests/runtime-api-task-binding-cli.test.py` against parent implementation `e8baebb1688700211cb01832bf48429a0277370e` with the new tests:

```text
Ran 6 tests in 2.446s
FAILED (failures=3)
```

At evaluated HEAD:

```text
Ran 6 tests in 2.969s
OK
```

The isolated task-context fixtures use synthetic approvals and fake senders, with zero external messages. They prove the tested refusal/replay contract, not authentic owner authorization. Rui's full-day use is the manual experience supplied for this conclusion; no additional restart or network round trip was performed merely to publish the report.

## Review and remaining work

Start with this report, [the learning implementation notes](learning-window.md), and the attached JSON. The original62-file branch was reviewed for scope and separated into learning windows, cron payload preservation, and GitHub verification/publication. This PR is based on current main `95fbd566a`; the evaluated source remains `32aa78011113f8616cd82fc244576dc12e0492cf`. The intrusive Claude startup guard registration and new Node startup requirement are excluded. No installed engine files were changed while preparing the PRs. CI and maintainer approval must be established before merge.

Raw local task transcripts, room bodies, owner identifiers, credentials, authentication material, private monitor paths and document contents are excluded from the published attachments. Existing private observations, score history, deployment/rollback receipts and earlier failures are preserved.

## Codex compatibility follow-up

Rui requested a separate Codex-core compatibility check. The Codex core reported on exact heads #5195 `5614b34d`, #5197 `c28773f3`, and #5198 `3ce4f3c4`. It recommended holding #5195 after reproducing a timed-out consumer's surviving descendant and the optional launcher using PATH Python rather than the configured interpreter. Companion fixture checks passed in their stated scope; no authenticated Codex consumer witness was claimed. The prior #5169 runtime witness does not establish compatibility of these new features.

Both #5195 defects were independently reproduced locally against `5614b34d` with the same new regression tests: two tests, two failures. The corrected dispatcher creates a consumer process group, kills remaining group members and reaps its direct child before final readback and dispatch-lock release, including timeout, normal exit and SIGTERM/SIGINT cancellation. The launcher now uses the repository Python resolver. Tests cover a TERM-resistant descendant attempting a late write during the next attempt, cancellation, background work after leader exit, and a failing PATH Python stub with a valid configured interpreter. Deliberate escape from the process group is outside the guarantee.

Codex also identified failed 95% diff-coverage gates on the original #5195 and #5197 heads. Missing CLI instrumentation is addressed with direct entry-point controls paired with actual subprocess fixtures; no gate was lowered. New validation and CI must be assessed on the updated heads. Claude's Stop/evidence hooks are not Codex enforcement. A detached Codex adapter must explicitly arrange its cwd, receipt/checker access and private output write permission. Authenticated Codex integration remains unverified pending an authorized isolated runtime test; passing these local fixtures is not that witness.
