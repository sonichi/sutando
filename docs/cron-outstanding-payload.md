# Outstanding cron payload preservation

Prompt cron fires coalesce into the oldest outstanding task for that job. The file identity and body remain immutable until normal retirement, including while a worker has accepted or claimed it. A per-job lock serializes emitters and the existing atomic writer publishes new payloads. Existing legacy backlogs are preserved. A changed prompt or schedule applies to the first emission after the prior task retires.

This deliberately replaces the previous newest-pending-fire behavior, which unlinked an outstanding payload before publishing another. A worker could claim between that ownership check and unlink. This does not introduce a second consumer, delete archived history, or grant authority to execute a task. A stuck outstanding task needs normal recovery; repeated cron ticks do not replace it.

Regression coverage calls the production writer with 12 concurrent processes, worker ownership sentinels, handler claims, legacy backlogs, publication failure and post-retirement configuration changes. See `tests/cron-outstanding-payload.test.py` and the existing cron runner suite. This was part of the locally evaluated reliability version; Rui's full-day experience is recorded in the separate learning-window results PR.
