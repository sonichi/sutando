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
- [ ] Doesn't reinvent workspace / home-path resolution — uses the `resolve_workspace` / `resolveWorkspace` / `claude_home_path` helpers (see `CLAUDE.md` "Workspace contract"; enforced by the `lint-workspace-resolution`, `lint-claude-home-path`, and `workspace-leak-check` CI checks)

## Test plan

<!-- How did you verify this works? `npx tsc --noEmit` + actual run; tests; manual repro. Be specific. -->
