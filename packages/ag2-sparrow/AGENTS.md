# ag2-sparrow

**ag2-sparrow is not Sutando-specific.** It carries tasks for several agent engines: Sutando
(Claude Code / Codex), AG2 Assistant "Spacer" pods, Hermes, OpenClaw and others.

- **Facts go in plain task headers any engine can parse:** one `key: value` line above `task:`,
  listed in `local_task_protocol.KNOWN_HEADER_KEYS`. Never only inside a prose block.
- **Engine-specific prose is opt-in per engine, never always on.** Skill preludes, script paths
  (`room_ops.py`), results-file steps: gate them on a declared switch such as
  `SPARROW_TASK_PRELUDE`, with the default keeping today's behaviour.
- **A new field or behaviour says what a non-Sutando engine sees** (in the PR body and the README),
  not only what Sutando does with it.
