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

1. Is there already an open or recently-closed PR / issue covering this? Search both open and recently-closed state — duplicate PRs are the #1 source of churn here. The same fix has been opened in 10+ different PRs before. Check with:

   ```bash
   gh pr list --repo sonichi/sutando --state all --limit 30 --search "your-keyword"
   gh issue list --repo sonichi/sutando --state open --search "your-keyword"
   ```

   If someone else's PR is already in flight (CLA-blocked or just stale), prefer pinging them or pushing onto their branch over opening a parallel one.
2. **Is the problem real?** For a bug-fix, **manually verify** — a human actually runs the failing path end-to-end. If you can't repro locally, ask a maintainer's bot to produce **scripted evidence the maintainer can inspect** (test output, repro logs) — that's "bot verification". Bot verification is *evidence*, not a substitute for manual testing when the change is user-visible, integration-heavy, or weakly covered by tests. For a feature, confirm the user need is real (issue with use case, owner ask, etc.). Don't open a PR for a problem that doesn't exist.
3. For a bug-fix: is the bug still on `upstream/main`? (`git show upstream/main:path | grep buggy-line`) — don't fix something that's already gone.
4. Is this a single concern? **One bug or one feature per PR.** If you're tempted to bundle several features into one PR ("while I'm here I'll also add Y, Z"), split them up front — open one PR per concern, each with its own closes-link. Mixing concerns triples the review burden, increases revert blast radius, and slows merge. "Drive-by" cleanup that happens to land in the same hunk is fine; net-new scope is not.

## The PR body should answer

In the order a reviewer reads them. Say "N/A" if a question doesn't apply, so the reviewer doesn't wonder whether you forgot it.

- What changed, and why?
- What files / sections should reviewers look at first?
- What user behavior or bug does this prove?
- What tests did you run? Include commands and results.
- **For voice / phone / audio PRs: include a manual test result.** A transcript excerpt (from `data/conversation.sqlite`) showing the live turn/`[Tool]` flow, or a voice recording demonstrating the change. Voice paths are weakly covered by unit tests, so a maintainer needs observable evidence the live session behaves — not just that the code compiles. See the gold-standard example below.

  <details>
  <summary><strong>Gold-standard voice test-evidence (real example)</strong> — how to pull the log + a complete excerpt + a video example</summary>

  Two accepted evidence forms. The strongest PRs include the transcript excerpt; a recording can stand alone for hard-to-transcribe UX changes.

  **1. How to get the transcript log.** Every live voice/phone/discord-voice turn is logged to `data/conversation.sqlite` (tables `voice`, `phone`, `discord_voice`). Pull the session you just exercised:

  ```bash
  DB="$(bash scripts/sutando-config.sh workspace)/data/conversation.sqlite"
  # find your most recent session id
  sqlite3 "$DB" "SELECT DISTINCT session_id FROM discord_voice ORDER BY ts_unix DESC LIMIT 5;"
  # dump that session's turns in order (swap discord_voice → voice / phone for other surfaces)
  sqlite3 -header -column "$DB" \
    "SELECT datetime(ts_unix,'unixepoch') ts, kind, speaker_name, text
     FROM discord_voice WHERE session_id='<SESSION_ID>' ORDER BY ts_unix;"
  ```

  Paste the rows that prove the change (don't fabricate — these are real captured turns). For a bug-fix, capture the **same scenario** on the unpatched build and on the patch, and show both — that's what makes it before/after.

  **2. Complete example excerpt** (real `discord_voice` session, owner reading to the agent — note the `tool_call` rows interleaved with transcribed turns, which is the live `[Tool]` flow a reviewer is looking for):

  ```
  ts                   kind       speaker_name  text
  2026-06-08 00:31:45  user       susanliu_     Oh, hi, Maddy, can you hear me?
  2026-06-08 00:31:49  agent      Maddy         Yes, I can hear you perfectly. What's up?
  2026-06-08 00:31:53  user       susanliu_     Okay, uh, are you Maddy?
  2026-06-08 00:31:56  agent      Maddy         I'm Sutando - Maddy.
  2026-06-08 00:32:11  user       susanliu_     Okay, so what are we working on right now?
  2026-06-08 00:32:11  tool_call                get_core_status
  2026-06-08 00:32:14  agent      Maddy         The core agent is currently working on a restart and test for the meeting-buddy…
  2026-06-08 00:32:38  user       susanliu_     Okay, thank you.
  2026-06-08 00:32:41  tool_call                dismiss
  2026-06-08 00:32:42  user       susanliu_     You can log off, bye.
  ```

  For a richer before/after example (owner-verified `vision_query` reads across three screen-companion modes), see the owner-verification review on [#1409](https://github.com/sonichi/sutando/pull/1409#pullrequestreview-4414966586).

  **3. Video example** (the recording form). A short screen/voice recording demonstrating the live behavior — attach it directly to the PR (GitHub hosts `.mp4`/`.mov` drops) or link an unlisted upload. Reference recording: [all-by-voice demo](https://youtu.be/NC0kdpLulUY).
  </details>
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
4. **If the PR ended up large, split it post-hoc.** If during review it becomes clear the diff covers more than one concern (a fix + a refactor, two unrelated features, etc.), close this PR and re-open it as N smaller PRs rather than negotiating reviewer patience. Easier than rebasing later; easier to revert one piece at a time.

## Reviewing PRs

If you're reviewing someone else's PR (including a bot's), keep the comment thread useful:

- **Prefer to add evidence, not noise.** If you have nothing new to add — no new evidence, no fresh angle, no concrete suggestion — stay silent. A "LGTM" comment under an existing APPROVE just buries real feedback. (A second reviewer surfacing *new* evidence on a point another reviewer raised is fine; a third "lgtm" in a row is not.)
- **APPROVE / REQUEST_CHANGES is a formal GitHub action.** A Discord "👍" or a `gh pr comment` saying "approved" does NOT register as a review — use `POST /repos/.../pulls/N/reviews` (or `gh pr review --approve`) so the state is recorded.
- **Be evidence-first.** When you claim something is broken, point at the commit, file, line, repro, or failing test. If you didn't verify, say so explicitly ("not verified — flagging for author to check").
- **Distinguish blockers from nits.** Mark each comment so the author knows what's gating merge vs what's deferrable.

For more detail (verification phases for fix PRs, sign trailers, sonichi-fix POC mechanics), see the `review-pr` skill if it's installed.

## If a bot is contributing on your behalf

**Do not flood the repo.** A bot can crank out PRs faster than a maintainer can review them — it's easy to dump 50–100+ PRs in a day. Even when each PR is individually correct, the volume buries real-user issues and burns the review channel. Concrete rules:

- **Cap your in-flight PRs.** Land or close existing ones before opening more. If a maintainer hasn't reviewed your last 3 PRs yet, do not open a 4th.
- **Read the diff before pushing.** "I trust the agent" is not enough — bots miss conventions, hallucinate referenced files, and sometimes regenerate unrelated areas. Skim every change.
- **No "drive-by" repo-wide refactors.** If the agent suggests one, open ONE small PR with the proposal first, get sign-off, then expand.
- **Take responsibility for what your bot ships.** Its PRs are *your* PRs — your CLA, your review feedback to address, your closes-link to file. If a maintainer closes the PR as duplicate / not-planned / scope-drift, that's data — diagnose the root cause before re-filing.

## Community

- [Discord](https://discord.gg/uZHWXXmrCS) — real-time dev, PR discussion, live debugging
- [GitHub Issues](https://github.com/sonichi/sutando/issues) — bug reports and feature requests
