---
name: github-attribution
description: Attach the locally authenticated GitHub account to a canonical Sutando agent and record an explicit owner policy for historical performer-kind classification.
user-invocable: true
---

# GitHub attribution

Run this once after GitHub authentication and whenever policy scope changes:

```bash
python3 "$SKILL_DIR/scripts/configure.py" \
  --agent-id agent:<uuid> \
  --owner-id human:<uuid> \
  --repository owner/repository \
  --object-type push \
  --object-type pullrequestreview
```

Use `--all-repositories` only for an intentional account-wide policy. Narrow it
with `--exclude-repository`, `--object-type`, `--exclude-object-type`,
`--not-before`, or `--not-after`. The command reads the immutable account ID
from `gh api user`; it does not accept a login or account ID from the caller.

The command writes two owner-local claims: the agent may use the observed
account, and matching activity is agent-produced. The latter classifies only
the performer kind. It never identifies the exact agent for an event and does
not authorize GitHub mutations.

Exact attribution appears only for writes routed through a governed executor
that publishes a provider-object receipt. Direct `gh` and `git` commands remain
unattributed.
