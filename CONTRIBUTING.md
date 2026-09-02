# Contributing to Sutando

Thanks for your interest! Sutando is alpha software — the biggest need is **testing and hardening**.

This file is the **canonical process for every contribution**, human or AI-agent. When Sutando's own core agent (Claude Code or Codex) opens or reviews a PR, `CLAUDE.md` / `AGENTS.md` direct it here. Authoring a PR is governed by **["Before starting a PR"](#before-starting-a-pr)**, **["The PR body should answer"](#the-pr-body-should-answer)**, and **["After opening the PR"](#after-opening-the-pr)**; reviewing one by **["Reviewing PRs"](#reviewing-prs)**. Keep those section headings stable — other files link to them by name.

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
4. **Logs** — paste relevant lines from `<repo>/workspace/logs/*.log` (the default in-repo workspace; see [`docs/workspace-config.md`](docs/workspace-config.md) for overrides)
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

5. **Run the repo's own scanner before you push**, not after CI tells you:

   ```bash
   git commit ...                                              # it reads COMMITS, not your worktree
   bash scripts/review-checks.sh --diff <(git diff origin/main...HEAD)
   ```

   It runs the machine-readable `checks:` block in [`REVIEW.md`](REVIEW.md) — the same one CI runs — so a red result here is a red result there.

   Three things that make it report something other than what you expect, each measured:

   - **Commit first.** Against an uncommitted or merely staged worktree the three-dot range is empty and you get `ERROR — empty diff; nothing was scanned, so this is NOT a pass`. It fails loudly rather than passing, but it is easy to read as "clean".
   - **Three dots, not two.** `origin/main..HEAD` renders `main`'s own newer commits as your removals.
   - **`PARTIAL` is not `PASS`.** With no `.py` in the diff it prints `prose-cap had no in-scope files`; from a `gh pr diff` file with no tree to read it prints `prose-cap SKIPPED`. Neither one scanned any prose.

   Worth the second it costs because these findings are mechanical and invisible while writing. `prose-cap` counts **consecutive added comment lines**, so a trailing comment on a code line folds into the block beneath it — `return None  # why` above a 2-line block is a 3-line run, and the fix is a blank line between them, not a rewrite. On 2026-09-01 one file cost four separate CI round-trips across two authors for that class alone.

## The PR body should answer

In the order a reviewer reads them. Say "N/A" if a question doesn't apply, so the reviewer doesn't wonder whether you forgot it.

- What changed, and why?
- What files / sections should reviewers look at first?
- What user behavior or bug does this prove?
- **Feature/documentation consistency.** If the PR adds, changes, or removes a
  user-visible feature, capability, or behavior, update the corresponding
  canonical document under `docs/` in the same PR. Use `docs/catalog.json` to
  identify the canonical document, update its `last_verified` date, and update
  `docs/README.md` when navigation changes. If no documentation change is
  needed, say `N/A` and explain why.
- **Before/after evidence — the single most-requested thing on this repo.** Paste the *actual output* for both states, not a description of it: the failing command/test at the parent commit, then passing at HEAD. "I verified X changes to Y" is not evidence; the pasted command that shows X→Y is. A reviewer should not have to reproduce your result to believe it.
- What tests did you run? Include commands and results.
- **Touching a live path (bridge, network, delivery loop, startup)?** A unit test or harness is not sufficient on its own — include a real post-restart round trip on the affected host (input received → reply delivered), with timings when latency is the point. This is the most common reason an otherwise-correct live-path fix is held from approval. The only exception is `REVIEW.md` lesson 15's first-deploy gate: no witness target can be provisioned (installer output pasted) AND the owed witness is filed as a `src/witness_owed.py` record that `self-upgrade` refuses to activate over.

  <details>
  <summary><strong>How to capture that round trip without checking the branch out over your live checkout</strong></summary>

  The obvious way — `git checkout <branch>` in the running checkout, restart, capture, switch back — puts your live service on a branch and leaves the checkout dirty if anything interrupts you. It also weakens the evidence: a reviewer cannot tell from the log which commit actually ran.

  Run the service from a **detached worktree at the PR head** instead, with the workspace pinned to the real one. **The same applies to any ad-hoc command you run in a worktree to measure the parent commit** — the before/after evidence bullet above sends you there routinely, and a workspace-reading probe run in an unpinned worktree answers about an empty directory. No service need be involved for this to bite. `discord-bridge.py` (and the other `src/` entry points) resolve their modules via `Path(__file__).resolve().parent`, so the worktree's `src/` is genuinely what executes.

  ```bash
  OID=$(gh pr view <N> --json headRefOid --jq .headRefOid)
  git fetch origin "pull/<N>/head:pr<N>"
  [ "$(git rev-parse pr<N>)" = "$OID" ] || { echo "ref != PR head — stop"; exit 1; }
  git worktree add --detach /tmp/wt-pr<N> pr<N>

  # Point the worktree at the LIVE workspace. Without this it resolves to its OWN
  # empty workspace/ and every probe reports clean no matter what the code does.
  cp sutando.config.local.json /tmp/wt-pr<N>/
  python3 - <<'EOF'
  import json, pathlib
  p = pathlib.Path("/tmp/wt-pr<N>/sutando.config.local.json"); c = json.loads(p.read_text())
  c["workspace"] = {"path": "<the live workspace path>"}; p.write_text(json.dumps(c, indent=2))
  EOF
  ```

  **Pinning `workspace.path` is not always enough, and the failure is silent.** Some probes derive
  their path from the *repo slug*, not the workspace — `MEMORY_DIR` resolves to
  `<workspace>/.claude-sutando/projects/<slug-of-cwd>/memory`, so from a worktree it becomes
  `projects/-private-tmp-…-wt-name/memory`, which does not exist. The check then returns `None` and
  emits **no line at all** — and an empty grep is indistinguishable from "the change didn't fire".
  Pin `SUTANDO_MEMORY_DIR` as well when the thing under test reads memory:

  ```bash
  SUTANDO_MEMORY_DIR="$LIVE_WS/.claude-sutando/projects/<real-slug>/memory" \
    python3 src/health-check.py
  ```

  The general rule: **before trusting an empty result, print the path the code resolved.** Two of my
  before/after runs on #2610 were empty for this reason and neither was measuring what I thought.

  Preflight **before** stopping the running service, because a bridge restart takes down the owner's live path:

  - `rev-parse` equals `headRefOid` (above) — otherwise you are testing a stale ref;
  - `python3 -m py_compile src/<entrypoint>.py` in the worktree;
  - credentials resolve from `$CLAUDE_CONFIG_DIR`, not the checkout;
  - nothing will auto-restart the old process on top of yours (`health-check.py --fix` restarts bridges — do not run it during the window);
  - write the rollback command down first.

  Then capture the log lines showing the same input shape before and after. Quote the task file, not just the log, when the discriminator is a field: a task file records `reply_to_author_id`, `access_tier`, `channel_id`, so it proves *which* case you exercised rather than asserting it.

  Restore the service from the normal checkout when you are done, remove the worktree (`git worktree remove /tmp/wt-pr<N>`, then `git worktree prune` — `rm -rf` leaves it registered and the next `git worktree add` at that path fails), and say in the PR that you restored it.
  </details>
- **Stacked PR?** Name the parent PR and intended merge order. Keep each layer to one concern, and after a parent lands, rebase/update the child and rerun its full checks before asking reviewers to treat it as merge-ready.
- **Hardcoded-path check.** Scan added lines for host-specific paths (`/Users/<name>`, `/home/<name>`, clone-specific absolute paths, and inline home/config fallbacks). Production code must use the repo's path helpers; test fixtures must be clearly scoped as fixtures rather than silently exempting an entire line from review.
- **For voice / phone / audio PRs: include a manual test result.** A transcript excerpt (from `data/conversation.sqlite`) showing the live turn/`[Tool]` flow, or a voice recording demonstrating the change. Voice paths are weakly covered by unit tests, so a maintainer needs observable evidence the live session behaves — not just that the code compiles. See the gold-standard example below.

  <details>
  <summary><strong>Gold-standard voice test-evidence (real example)</strong> — how to pull the log + a complete excerpt + a video example</summary>

  Two accepted evidence forms. The strongest PRs include the transcript excerpt; a recording can stand alone for hard-to-transcribe UX changes.

  **1. How to get the transcript log.** Every live voice/phone turn is logged to `data/conversation.sqlite` (per-surface tables: `voice`, `phone`, …). Pull the session you just exercised:

  ```bash
  DB="$(bash scripts/sutando-config.sh workspace)/data/conversation.sqlite"
  # find your most recent session id
  sqlite3 "$DB" "SELECT DISTINCT session_id FROM voice ORDER BY ts_unix DESC LIMIT 5;"
  # dump that session's turns in order (swap `voice` → `phone` for other surfaces)
  sqlite3 -header -column "$DB" \
    "SELECT datetime(ts_unix,'unixepoch') ts, kind, speaker_name, text
     FROM voice WHERE session_id='<SESSION_ID>' ORDER BY ts_unix;"
  ```

  Paste the rows that prove the change (don't fabricate — these are real captured turns). For a bug-fix, capture the **same scenario** on the unpatched build and on the patch, and show both — that's what makes it before/after.

  **2. Complete example excerpt** (real voice session, owner reading to the agent — note the `tool_call` rows interleaved with transcribed turns, which is the live `[Tool]` flow a reviewer is looking for):

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

   **`license/cla` FAILING and `license/cla` ABSENT are different problems with different fixes.** The remedy above is for a check that ran and said no. Sometimes the check never posts at all — and because it is a *required* context, "never posted" reads as pending forever and the PR sits at `BLOCKED` with every other check green and both approvals in place. It is easy to misread as a review problem. Tell them apart by asking for the commit statuses directly, since an absent context simply does not appear in the PR's check list:

   ```bash
   gh pr view <N> --json headRefOid --jq .headRefOid \
     | xargs -I{} gh api repos/<owner>/<repo>/commits/{}/status \
         --jq '"state=\(.state) total=\(.total_count) contexts=" + ([.statuses[]?|.context]|join(","))'
   # absent : state=pending total=0 contexts=
   # present: state=success total=1 contexts=license/cla
   ```

   **If it is absent, close and reopen the PR.** Reopening fires `pull_request.reopened`, which CLA-Assistant does act on. Reviews are not dismissed — reopening is not a push — but it *does* re-trigger the full CI run, so expect a few minutes of pending checks afterward.

   **Give it a minute or two before concluding it failed.** The status is posted asynchronously: on one PR it was still `total=0` immediately after reopening and `license/cla=success` on the next check. Re-running the command above is the way to tell; an immediate zero means nothing yet.

   **If the status is PRESENT and `pending`, that is a third case with its own cause.** `state=pending` with the description *"Contributor License Agreement is not signed yet."* means CLA-Assistant ran and is waiting on a signature — so close+reopen does nothing, and the `--reset-author` fix above only applies if the identity is your own. Ask GitHub which commit identities it cannot vouch for:

   ```bash
   gh api repos/<owner>/<repo>/pulls/<N>/commits \
     --jq '.[] | "\(.sha[0:8]) \(.commit.author.email) -> \(.author.login // "NULL — maps to no account")"'
   ```

   Two shapes turn up: an email mapping to **no account** (`author: null`), and an email mapping to a **real account that has not signed**.

   Neither is identifiable from a single PR — every login on it looks equally plausible. The unsigned one is the login that appears on CLA-**pending** PRs and on **no green** one, which takes a comparison across the open queue:

   ```bash
   for n in $(gh pr list --state open --limit 100 --json number --jq '.[].number'); do
     h=$(gh pr view "$n" --json headRefOid --jq .headRefOid)
     state=$(gh api "repos/<owner>/<repo>/commits/$h/status" \
               --jq '[.statuses[]|select(.context=="license/cla")]
                     | if length==0 then "absent" else (sort_by(.updated_at)|last|.state) end')
     logins=$(gh api "repos/<owner>/<repo>/pulls/$n/commits" --jq '[.[].author.login // "NULL"]|unique|join(" ")')
     echo "$state $logins"
   done | awk '$1=="pending"{for(i=2;i<=NF;i++) p[$i]=1} $1=="success"{for(i=2;i<=NF;i++) g[$i]=1}
                END{for(k in p) if(!(k in g)) print "unsigned candidate:", k}'
   ```

   Treat the output as a **candidate list, not a verdict**: confirm against the PR's CLA-Assistant page before attributing the pending status to a person.

   **Do not guess from the shape of the email.** One contributor here commits under two addresses that map to two different accounts, and it is the personal-looking one that is unsigned while the institutional one is signed — the opposite of the natural assumption. A suspect that also appears on a CLA-**green** PR is disproved, which is the cheap check to run before naming anyone.

   **`author: null` does not mean the commit is yours.** It proves only that GitHub could not map that email to an account. Before recommending `git commit --amend --reset-author` — or running it — establish that the commit is the current contributor's own work: the author NAME, the branch it arrived on, and where there is any doubt, their confirmation. Rewriting the author of someone else's commit misattributes it, and where the unsigned identity is a third party the only correct remedy is that account signing.

   Note the corollary of the status being SHA-bound: **pushing to the branch after this drops the status again.** If you reopen to fix the CLA and then push a review fix, expect to be back where you started.

   **When to expect it.** `license/cla` is a *commit status*, so it binds to one SHA. **Every push gives the PR a new head SHA that carries no CLA status**, which is what [`.github/workflows/cla-recheck-on-push.yml`](.github/workflows/cla-recheck-on-push.yml) exists to repair — it comments the `@cla-assistant check` trigger on each `synchronize`. That repair is not reliable, so a PR you have pushed to can end up permanently short of a required check.

   **The model the evidence supports: the status is SHA-bound, and the write can fail *after* CLA-Assistant has received and acknowledged the event.** A push guarantees you need a *fresh* delivery, so pushed PRs are over-represented among the missing — but a push is not required for the status to be absent, and **close+reopen is a retry, not a push-specific fix.**

   The webhook delivery log distinguishes "the event never arrived" from "it arrived and nothing was written", and it is the second one. Twelve `pull_request/synchronize` deliveries on 2026-08-11, each mapped to its PR through the delivery payload and cross-referenced against whether the head carries a status:

   ```
   all 12:  HTTP 200,  body 'OK - Will be working on it'
     9 -> license/cla written        3 -> never written   (#2342, #2785, #2790)
   ```

   Identical request, identical acknowledgement, different outcome — so nothing observable on the GitHub side predicts it, which is why author email, committer identity, push recency and update-branch-button-vs-local all fail as explanations when tested against the full population. Read the log yourself with `gh api "repos/<owner>/<repo>/hooks/<id>/deliveries?per_page=60"`, then `.../deliveries/<delivery-id>` for the payload (`.number`, `.pull_request.head.sha`). Note `&page=N` is silently ignored there — the endpoint is cursor-paginated and returns an empty list, which reads exactly like an empty log.

   Three PRs on 2026-08-04 — `#2605`, `#2606`, `#2607` — were opened directly on their final head, had no push before the status went missing, and still had no `license/cla`. `#2607` is the sharpest case and is easy to measure wrong: it *was* pushed later, at `05:07`, but its close+reopen recovery happened at `04:50`, so the absence pre-dated any push. Comparing the head commit's date to the PR's creation date reports it as "pushed after open" and hides that entirely — read the issue **timeline** (`committed` / `closed` / `reopened` events in order), not the current head.

   **The `@cla-assistant check` comment cannot work on this repo — it is inert, not merely unreliable.** The CLA webhook subscribes to exactly two events, and `issue_comment` is not one of them, so a comment never reaches the service at all:

   ```
   $ gh api repos/<owner>/<repo>/hooks/<id> --jq '.config.url, (.events|join(", "))'
   https://cla-assistant.io/github/webhook/sutando
   merge_group, pull_request
   ```

   The distinction matters because "unreliable" invites a retry and "inert" ends it: no number of trigger comments, from a workflow or a human, can produce a status. Use a fresh head or close+reopen instead — `pull_request/reopened` acknowledges at 200, while `closed` returns 204 and is discarded, so the reopen is the half that does the work. (`.github/workflows/cla-recheck-on-push.yml` posts that comment on every `synchronize` and reports success every run; it is tracked separately as an open issue, since removing versus reworking it is a maintainer call.)

   This section previously said only "do not rely on" the comment, which was right about the behaviour and silent about the cause. Of the 99 open non-draft PRs on 2026-08-04, **91 had the status and 8 did not**; all 8 had been pushed since opening, and they span eight days (2026-07-27 to 2026-08-04), so this is not a time window. Read that 8 as a *snapshot of what was still broken*, not as a population of causes: PRs recovered earlier the same day had already left the set, and those are exactly the ones that show `opened`-side failure.

   The practical consequence is the same either way: the trigger comment is neither sufficient nor necessary, and the check below is what tells you where you actually stand. A manual trigger comment on `#2605` did not restore it; close+reopen did, and its two approvals survived. Four PRs were recovered this way across two authors.
3. Address every substantive review-thread comment before merge: fixed in a subsequent commit, replied with rationale for declining, or explicitly deferred to a follow-up issue.
4. **If the PR ended up large, split it post-hoc.** If during review it becomes clear the diff covers more than one concern (a fix + a refactor, two unrelated features, etc.), close this PR and re-open it as N smaller PRs rather than negotiating reviewer patience. Easier than rebasing later; easier to revert one piece at a time.
5. **Solicit and NOTIFY reviewers.** You need to solicit reviewers for your PR and keep making progress on their comments, change requests and blocking comments until you have enough approvals and the PR is merged. Use a GitHub review request **and** the [`collaboration-intelligence`](skills/collaboration-intelligence/SKILL.md) skill to resolve *whom* to ask and *where they read*: it maps each reviewer to the agent stand-in that acts on their behalf.
   - **On open** — request on GitHub **and** address each reviewer in a channel they are in. Requesting on GitHub alone is filing, not asking.
   - **On every update that changes the diff** — notify them again in a channel they are in. A push re-notifies no one, and this repo sets `dismiss_stale_reviews_on_push: false`, so a standing `CHANGES_REQUESTED` latch survives the fix and its author gets no signal to look again. **A mechanical base-merge is not such an update**: clearing BEHIND carries no author work, so there is nothing for the reviewer to look at again and the ping is pure noise. The test is whether the head moved because *you* answered something.
   - **As the author, read `reviewDecision`, not the thread.** The same latch survives the *reviewer's own comment* saying it is resolved: a `COMMENTED` review is not decisive, so a reviewer who writes "your condition was met" has said so and not recorded it. If `gh pr view N --json reviewDecision` still reads `CHANGES_REQUESTED`, you are still blocked no matter what the thread says — tell them, because they cannot see the gap either.
   - **Address both the human and their AI stand-in's handle** — the stand-in acts, the human decides, and only one of them is watching any given channel.
   - Look up collaborators and their stand-in handles per platform with `python3 skills/collaboration-intelligence/scripts/lookup.py <name>`.

## Reviewing PRs

If you're reviewing someone else's PR (including a bot's), keep the comment thread useful:

- **Prefer to add evidence, not noise.** If you have nothing new to add — no new evidence, no fresh angle, no concrete suggestion — stay silent. A "LGTM" comment under an existing APPROVE just buries real feedback. (A second reviewer surfacing *new* evidence on a point another reviewer raised is fine; a third "lgtm" in a row is not.)
- **APPROVE / REQUEST_CHANGES is a formal GitHub action.** A Discord "👍" or a `gh pr comment` saying "approved" does NOT register as a review — use `POST /repos/.../pulls/N/reviews` (or `gh pr review --approve`) so the state is recorded.
- **Be evidence-first.** When you claim something is broken, point at the commit, file, line, repro, or failing test. If you didn't verify, say so explicitly ("not verified — flagging for author to check").
- **Distinguish blockers from nits.** Mark each comment so the author knows what's gating merge vs what's deferrable.
- **Review the current head and the right layer.** For a stacked PR, identify the parent and inspect the child-only change as well as the cumulative interaction. After an update/rebase, re-check the head SHA, required checks, and whether prior approvals still apply.
- **Scan added lines for hardcoded host paths on every review.** Do not rely only on CI: look for `/Users/<name>`, `/home/<name>`, clone-specific absolute paths, and inline workspace/home fallbacks. Keep fixture exclusions token-specific so an allowed fixture on a line cannot hide a real production path on that same line.
- **Clear stale formal blockers.** When the author pushes a fix, re-review the current head. If the request is genuinely resolved, dismiss/replace the stale REQUEST_CHANGES state; if it remains, cite the exact unresolved line or behavior. **"Replace" means an `APPROVED` review or a dismissal — a `COMMENTED` review does not clear your own `CHANGES_REQUESTED`.** GitHub takes the latest *decisive* review per login, and a comment is not one, so writing that the block is satisfied reads as closure in the thread while `reviewDecision` still shows it blocking. Do not leave a resolved change-request blocking merge through automation inertia.
- **Apply the complete merge gate.** Merge only when the current head is mergeable, required CI and CLA checks are green, and two maintainers have recorded formal approvals. A comment, Discord acknowledgement, bot recommendation, stale approval on an old head, or admin bypass is not a substitute for any gate.

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
