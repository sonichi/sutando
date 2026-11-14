# PR-watch runner lifecycle

The [pr-watch skill](../skills/pr-watch/SKILL.md) documents setup, watch selection,
GitHub permissions, and event handling. This document defines the runner's local
shutdown contract.

`pr_watch.py run` owns its repository forwarders, their helper threads, and the
GitHub CLI processes used by the running watcher. On SIGTERM or SIGINT, shutdown
closes admission before stopping those processes. Repository loops and hook
recorders finish before final runtime records are cleared and the runner lock is
released. A removed repository's loop remains part of that drain until it exits.
Once shutdown returns, those writers cannot recreate the runner's state files.

Runner-owned GitHub calls have a ten-second timeout and can be cancelled during
shutdown. Termination allows up to two seconds of grace before killing the
owned process group and reaping the child. Final recorded-hook cleanup has one
ten-second API-call budget across repositories, with the same bounded termination
grace after a timeout. Failed deletions remain recorded for later cleanup; a
cleanup failure does not justify leaving a background writer alive.

The standalone configuration and cleanup commands retain their existing
interfaces. After updating an installed runner, repeat the skill's `install-mod`
step so the installed copy contains the lifecycle fix. Enabling the mod remains
the owner's action.

## Verification

Run the isolated regression suite:

```bash
SUTANDO_TELEMETRY=0 python3 tests/skills/pr-watch/pr_watch.test.py
```

The suite uses fixture repositories and fake GitHub CLI processes. Lifecycle
controls force shutdown to overlap pending state writes and exercise cancellation
of a stuck child. This establishes local process and thread behavior; it does not
establish real GitHub webhook installation or remote hook deletion.
