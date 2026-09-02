<!--
First-time contributor? Read CONTRIBUTING.md before submitting. The checks
below mirror its "Before starting a PR" section. If you haven't run them,
please do — it saves a lot of round-trips.
-->

## Priority

<!-- Add one of the labels below to this PR (Labels → gear icon in the sidebar). -->
<!-- priority: high — blocks a release, security fix, or another PR's merge -->
<!-- priority: medium — meaningful improvement, should land this week -->
<!-- priority: low — nice-to-have, no deadline pressure -->

**Level:** <!-- high / medium / low -->
**Reason:** <!-- one line — why this urgency? e.g. "blocks v2 cutover", "requested by external contributor", "cleanup with no urgency" -->

## Closes

<!-- e.g. closes #123. Leave empty if this isn't tied to an issue. -->

## Summary

<!-- 1-3 sentences. What problem does this solve? -->

## Checklist

- [ ] Confirmed no other open PR closes the same issue (`gh pr list --repo sonichi/sutando --search "closes #N"`)
- [ ] Git author email matches my CLA-signed email (`git log -1 --format='%ae'` shows a GH-mapped email, not `*.local` or `noreply@anthropic.com`)
- [ ] Single concern per PR — no bundled refactors / drive-by feature additions
- [ ] Confirmed bug exists on `upstream/main` (or feature isn't already covered)
- [ ] Test added (or N/A explained below)
- [ ] **Before/after evidence pasted below** — the actual command output for both states, not a description of it (this is the #1 reason PRs bounce)
- [ ] Every claim in this PR body is verifiable from the diff or the pasted output — no statement the reviewer can't check
- [ ] **Live path?** If this touches a bridge, network path, delivery loop, or startup, I included a real post-restart round-trip — unit tests / harnesses alone are not accepted for these (or: no witness target can be provisioned, the installer failure is pasted, and the owed witness is filed with `src/witness_owed.py open` per REVIEW.md lesson 15)
- [ ] **Stacked PR?** I named the parent + merge order and will rebase/update + rerun full checks after the parent lands (or N/A)
- [ ] Scanned added lines for host-specific hardcoded paths and inline home/workspace fallbacks; fixtures are narrowly scoped
- [ ] If this PR adds or edits a CI workflow, I confirmed it actually runs on this PR (see the Checks tab — a `branches:` filter can silently exclude a stacked PR)
- [ ] Doesn't reinvent workspace / home-path resolution — uses the `resolve_workspace` / `resolveWorkspace` / `claude_home_path` helpers (see `CLAUDE.md` "Workspace contract"; enforced by the `lint-workspace-resolution`, `lint-claude-home-path`, and `workspace-leak-check` CI checks)

## Before / after evidence

<!--
Paste the ACTUAL output, both states — this is what a reviewer looks for first
and its absence is the most common change-request on this repo.

  Bug fix:   the failing command/test at the parent commit, then passing at HEAD.
  Live path: a post-restart round trip on the affected host (input → delivery),
             with timings if latency is the point.
  Lint/CI:   the finding count / check output before, and at zero/green after.

"N/A — <why>" is fine for pure docs or config with nothing runnable.
-->

## Test plan

<!-- How did you verify this works? `npx tsc --noEmit` + actual run; tests; manual repro. Be specific. -->
