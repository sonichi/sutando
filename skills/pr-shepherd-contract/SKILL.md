---
name: pr-shepherd-contract
description: "GitHub adapter binding for the shepherd contract: turn a real pull request into observed events, resolve the responsible actor from commit authorship, and persist the waiting contract under the workspace so a later pass (or process) can resume it."
---

# PR Shepherd Contract (GitHub adapter)

The GitHub binding for the provider-neutral shepherd contract
(`src/shepherd_contract.py`). The contract defines WHAT a shepherd task is
responsible for and WHICH observed events count; this skill supplies the
GitHub-specific half:

- **Observation** — `observe(repo, number)` reads live PR state via the
  GitHub CLI (`gh api`) and emits one `ObservedEvent` (merged / closed
  unmerged / updated).
- **Actor resolution** — `resolve_actor(repo, number)` uses the last
  non-merge commit's author email (`git.commit_author_email`), not the
  account login, because several actors can push under one login.
- **Persistence** — `save(task_id, scope, state)` / `load(task_id)` /
  `resume(task_id)` keep the durable contract record under
  `<workspace>/state/shepherd/`, create-or-advance only (never rebind), so a
  different process can pick the objective up later.

## Usage

Import from the skill's scripts directory (the module bootstraps `src/` onto
`sys.path` itself):

```python
import sys, pathlib
sys.path.insert(0, str(repo_root / "skills" / "pr-shepherd-contract" / "scripts"))
import shepherd_github as g

scope = g.scope_for("owner/repo", 123, g.resolve_actor("owner/repo", 123))
g.save("task-abc", scope, "waiting", "shepherding PR 123")
# ... later, any process:
state, why = g.resume("task-abc")
```

Tests: `tests/shepherd-github-durability.test.py`,
`tests/shepherd-github-integrity.test.py` (contract policy itself:
`tests/shepherd-contract-admission.test.py`).
