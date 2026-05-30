# Contributing to Sutando

Thanks for your interest! Sutando is alpha software — the biggest need is **testing and hardening**.

## Contributor License Agreement (CLA)

Before your first contribution can be merged, you'll be asked to sign the project's CLA — a one-time, web-based "I agree" via the [CLA Assistant](https://cla-assistant.io) bot. The bot will comment on your PR with a link; just click through and sign. The CLA text is in [`CLA.md`](CLA.md). Subsequent PRs are auto-recognized.

## Quick ways to contribute

### Test a capability
Pick something from the "What's inside" table in [README.md](README.md), try it, and report what breaks.

```bash
# Clone and set up
git clone https://github.com/sonichi/sutando.git
cd sutando
npm install
cp .env.example .env  # add your GEMINI_API_KEY
bash src/startup.sh
```

### Report bugs
[Open an issue](https://github.com/sonichi/sutando/issues) using the bug report template. A good bug report includes:

1. **What happened** — describe the issue clearly
2. **Steps to reproduce** — numbered steps someone else can follow
3. **Expected behavior** — what should have happened
4. **Logs** — paste relevant lines from `$SUTANDO_WORKSPACE/logs/*.log` (defaults to `~/.sutando/workspace/logs/`; the old `<repo>/logs/` path was moved to the workspace per the workspace contract — `src/startup.sh` now writes to `$WORKSPACE/logs/`)
5. **Environment** — macOS version, Node.js version, Claude Code version

**Bonus (highly valued):**
- A validation script under `scripts/test-*.sh` that reproduces the bug programmatically
- A commit hash for the suspected origin (helpful for both regressions and bugs that have been there since the code was written)
- The specific tool call or function that failed (check voice-agent.log for `[Tool]` entries)

See [issue #1339](https://github.com/sonichi/sutando/issues/1339) for a recent worked example combining all three.

### Add a skill
Skills are modular capabilities in `skills/`. Each skill has:
- `SKILL.md` — description and usage instructions
- `scripts/` — the actual code

See existing skills for examples. Install with `bash skills/install.sh`.

## Code style

- **Python**: standard library preferred, no frameworks. Python 3.9+ compatible (avoid `str | None` union syntax — use `Optional[str]`).
- **TypeScript**: ESM modules, strict mode. Run `npx tsc --noEmit` before submitting.
- **Shell**: bash, `set -e`, use `$REPO` for paths
- **web-client.ts**: The entire web UI is an inline HTML template literal. Do NOT use TypeScript-only syntax (like `as Type` casts) inside the embedded `<script>` block — the browser runs it as plain JS.
- All scripts should work from a fresh clone with minimal setup

## Before starting a PR

The goal of this phase is to confirm the PR is necessary at all. In rough order of "what kills the PR earliest":

1. Is there already an open or recently-closed PR / issue covering this? Respect what's in flight rather than racing.
2. **Is the problem real?** For a bug-fix, reproduce the bug yourself end-to-end (**manual verify**) — or, if you can't repro locally, ask a maintainer's bot to verify (**bot verify**). For a feature, confirm the user need is real (issue with use case, owner ask, etc.). Don't open a PR for a problem that doesn't exist.
3. For a bug-fix: is the bug still on `upstream/main`? (`git show upstream/main:path | grep buggy-line`) — don't fix something that's already gone.
4. Is this a single concern? One bug or one feature — split if you find yourself bundling.
5. Does an existing helper or pattern cover your case? Use it instead of introducing a parallel abstraction.

## The PR body should answer

In the order a reviewer reads them. Say "N/A" if a question doesn't apply, so the reviewer doesn't wonder whether you forgot it.

- What changed, and why?
- What files / sections should reviewers look at first?
- What user behavior or bug does this prove?
- What tests did you run? Include commands and results.
- For bug-fixes: failing-before / passing-after evidence (commit + test command).
- What edge cases or non-happy paths did you check?
- Any migrations, config, permissions, rollback, or deployment risks?
- Any known gaps or follow-up work?

## After opening the PR

The goal of this phase is to provide evidence the maintainer can verify quickly. In order of what happens next:

1. **Provide verification evidence in the PR body** — both flavors when applicable:
   - **Manual verify**: a command you ran + the before/after observed behavior. ("I ran `bash scripts/repro.sh` against the unpatched code and got X; with the patch I got Y.")
   - **Bot verify (tests)**: the test you ran (or added) + the pass/fail outcome, ideally **fails-before / passes-after** for bug-fixes. ("`pytest tests/foo.py::test_repro` fails at `2e79ec7` and passes at HEAD.")
   The reviewer should not have to re-derive that your change works.
2. Check the CLA status — CLA-Assistant runs on PR open and flags any commits whose author email isn't mapped to a CLA-signed GitHub account. **A failing CLA check blocks merge**, no matter how green everything else is. Fix with `git config user.email YOUR_GH_MAPPED_EMAIL && git commit --amend --reset-author --no-edit && git push --force-with-lease`. (`git log -1 --format='%ae'` to check what's there now.)
3. Address every substantive review-thread comment before merge: fixed in a subsequent commit, replied with rationale for declining, or explicitly deferred to a follow-up issue.

(For reviewer-side norms — don't pile on duplicates, formal APPROVE event vs "LGTM" comment, evidence-first claim verification — see the `review-pr` skill rather than this checklist.)

## If a bot is contributing on your behalf

Read the diff before pushing. Cap your in-flight PRs (land or close existing ones before opening more). Take responsibility for what your bot ships — its PRs are *your* PRs (your CLA, your review feedback to address, your closes-link to file).

## Community

- [Discord](https://discord.gg/uZHWXXmrCS) — real-time dev, PR discussion, live debugging
- [GitHub Issues](https://github.com/sonichi/sutando/issues) — bug reports and feature requests
