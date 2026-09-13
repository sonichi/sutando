# Recovery issue metrics

Attempt metrics cannot identify how many distinct issues eventually recovered.
The additive issue stream keeps retries attached to a durable random UUID.
Existing `health_fix_*` and `core_*` events retain their current meanings.

| Event | Meaning |
| --- | --- |
| `recovery_issue_detected` | First recovery attempt for this observed issue |
| `recovery_issue_attempted` | A recovery pass for the issue, including retries |
| `recovery_issue_recovered` | The issue's recovery was subsequently observed |

Every issue event contains `issue_id` and `issue_type` (`health_check` or `core`).
Check names, task identities, paths, and details stay local. Each health check has
its own UUID even though its exported type is the same privacy-safe category.
A partial batch can therefore recover two checks while a third stays open.

Health issues begin when a non-OK check enters a fix pass. They remain open across
retries, missing checks, warnings, and process restarts until that check explicitly
reports OK. A recurrence after OK gets a new UUID. These are observed check
episodes, not deduplicated underlying root causes across different checks.

Core issues begin at a restart attempt, including a failed launch or exception.
Dead/wedged transitions and retries share the same UUID. Recovery requires an
alive core with no queued task, a changed oldest task, or an advanced status
timestamp relative to the first attempt. A launched process alone is not recovery.
This observes recovery after intervention; it does not prove which repair caused it.

## Query contract

Group by installation (`distinct_id`) and `issue_id`, never by attempt count.
For a cohort first detected in a selected period, count unique issues with a
subsequent recovered event as of the observation cutoff:

`observed recovery percentage = recovered issues / detected issues * 100`

For example, five attempts for issue A followed by recovery count as one detected
and one recovered issue (100%). Two recovered issues and one still open yield
66.7%; show the open count alongside the rate. A zero denominator is N/A.
Follow the cohort beyond the detection window when looking for recovery, and show
the cutoff date: recent cohorts have had less time to recover.

An unresolved attempt or cooldown/give-up does not close an issue as failed.
There is no terminal failure signal today; label unmatched issues open or
"no recovery observed", not failed. A missing detected event is incomplete data,
not an extra success in the cohort. Historical events without IDs cannot be
backfilled reliably; retain their attempt charts separately. Dashboard adoption
is a separate change after clients emit this stream.

## Persistence and delivery

Issue state uses sibling `*-issues.json` files beside the existing health-fix and
core-recovery state files in the resolved workspace. Writers use an exclusive
nonblocking lock and atomic replacement; recovered records are removed. Open
records are retained without a time cutoff. State must survive upgrades/restarts.
Deleting it loses correlation; corrupt state suppresses issue tracking until
repaired rather than generating new identities on every tick.

Telemetry remains best effort through the existing capture/opt-out path. State is
committed before sending; offline delivery or a crash between commit and capture
can lose an event. UUID grouping removes retry inflation, not telemetry loss.
No new configuration or dependency is needed. Rollback leaves unused issue-state
files and does not change recovery behavior or existing dashboard queries.

Locking is supported on macOS/Linux; without `fcntl` this stream is disabled.
A contended writer skips that tick rather than delaying repairs. The next explicit
OK can still close the original issue; an entire episode inside contention may
be missed. A renamed or removed check stays unmatched until explicitly migrated
or observed OK; do not treat those orphaned records as confirmed failures.
