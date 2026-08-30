# Review guide — sonichi/sutando (`REVIEW.md`)

Canonical, in-repo review criteria for this repository. This is Claude Code's
conventional `REVIEW.md` at the repo root, so it is consumed by **three** audiences:

- **Claude Code's managed GitHub App PR reviews** inject this file's prose directly
  into every review agent's system prompt (that service reads a root `REVIEW.md`).
- **Humans + the codex reviewer** read the prose lessons below as review criteria.
- **Automated scanners** (`scripts/review-checks.sh`, run in CI on every PR) parse the
  fenced `checks:` block at the bottom — the *only* machine-read section.

This file is the single source of truth for the machine checks and the GitHub-App
criteria: adding a lesson or a check is a PR to *this* file, **not** a code change to
the review tooling. Another repo ships its own root `REVIEW.md`; the tooling is generic
and loads whichever repo it reviews.

> **Single source of truth.** These lessons live here in `REVIEW.md` only — they are
> **not** duplicated in `CLAUDE.md`. They reach reviewers three ways: `review-preflight.py`
> (**run it before reviewing**: `python3 scripts/review-preflight.py <PR>`) reads this file and
> prints the criteria on every pre-review run (for the core agent);
> `scripts/review-checks.sh` runs the machine `checks:` block below in CI; and Claude Code's
> managed GitHub-App reviewer reads this file directly. (The in-session `/code-review` reads
> only `CLAUDE.md`, not this file — but it is not part of our review flow.) Add or edit a
> lesson in one place: here.

## Lessons (criteria for reviewers)

1. **Confirm the bug exists on `main` before endorsing a fix.** A patch for a bug that
   isn't there is noise.
2. **Review the whole activated path, not just the diff.** Bugs hide in the unchanged
   code the diff now reaches. A parity change means reading both branches end-to-end.
   *Grounded by:* the #1600 M-fix — the watchdog's new forced-full-reset path reached
   unchanged wake-state code the diff never touched, leaving the bot permanently muted
   despite owner summons (#1663). Also #1403: the migration script's `rehome-state`
   classification was never traced against the reader's `personal_path()` resolution
   order, so post-migration bots lost their Stand names (fixed in #1540).
3. **Prove the fix by exercising the failure mode.** Happy-path green ≠ proof — a test
   must actually reproduce the original failure, or the fix is unverified.
4. **Destructive / auto-remediating actions: check default state and blast radius.** For
   anything that deletes, restarts, recovers, or prunes, ask *is it on by default, and
   how many hosts/files does it touch?* Prefer fail-closed (raise rather than proceed on
   an ambiguous result) over silently reporting success.
   *Grounded by:* #1428 — the `--recover-core` watchdog shipped **on by default** in the
   committed launchd template (every host that ran the installer), and its false-positive
   restarts killed the in-flight task and re-fired in a loop, leaving Chi's Sutando
   "not responsive or not functioning at many times, nearly unusable" for weeks
   (reported #dev 2026-07-21); Rui's host was bitten too (11 wedge-restarts) without
   anyone noticing.
5. **Disruption to existing users is part of correctness — ask the worst-case question.**
   "No bugs" is not a sufficient verdict. The explicit question every reviewer **and the
   maintainer (before merging)** must answer: **"What is the worst-case disruption to
   Sutando's users from this PR, and how do we mitigate it?"** (Chi 2026-07-25.) Concretely
   check: opt-in vs always-on; on-disk state-format/migration compatibility across the
   rolling-upgrade window; new hard-required config that breaks current installs;
   removal or rename of a path, command or flag that something outside the repo invokes —
   a registered cron, plist or saved prompt holds its own copy, so an in-repo grep answers
   about the wrong population (#3005); process-global patches with wide blast radius;
   and — per #1898 — for any auto-action,
   *what code or state does it act on* (does it verify the target is canonical, or run
   whatever's there?). A PR is not merge-ready until that worst case is named and mitigated.
   *Grounded by:* #1898 itself — the live test verified the claimed behavior, but the
   latent bug sat in exactly this un-asked worst-case question and surfaced 2026-07-25.
   The full reported-breakage corpus grounding these lessons lives in
   sutando-pr-triage `kb/breakage/breakages.csv` (reporter, verbatim quote, breaking
   merge, previously-working evidence per row).
6. **No hardcoded absolute paths.** Machine- or user-specific path literals
   (`/Users/…`, `/home/<user>/…`, `~/.claude`, `~/.sutando`, …) break on other hosts;
   resolve via the workspace/config helpers instead. Enforced by the `checks:` block.
7. **Never invoke a developer-tool binary at an absolute `/usr/bin/` path.** On macOS
   `/usr/bin/git`, `python3`, `swift`, `swiftc`, `clang`, `gcc` and `make` are not those
   tools — they are one inode hardlinked **78 ways** (`cc`, `c++`, `g++`, `clang++`,
   `gnumake`, `bison`, `flex`, `git-upload-pack` … re-derive with
   `ls -li /usr/bin | awk '$3==78'`) as the Xcode Command Line Tools *stub*. The
   file exists whether or not the tools are installed; running it without them raises a
   modal "install command line developer tools" dialog and returns nothing. Note the
   converse: a *longer* name in the same family is often a real binary —
   `/usr/bin/swift-inspect` has its own inode — so match these paths whole, never as a
   prefix. Three
   consequences a reviewer should check for: an absolute path **cannot be shadowed** by
   a real install on PATH, so the user's own git/python never wins; every existence
   probe (`test -x`, `command -v`, `shutil.which`, `FileManager.fileExists`) **passes
   against the stub**, so none of them is a usable guard; and on a timer or polling path
   the dialog **reappears forever**, not once. Resolve through the repo's helpers
   instead — `git_argv` (`src/git_binary.py`), `SutandoConfig.resolvePython`, the `$PY`
   cascade in `scripts/sutando-config.sh` — gate the system path on `xcode-select -p`
   (the one probe that does not prompt), and degrade when nothing runnable is found. A
   background health check must never be able to raise a system dialog.
   *Grounded by:* #2469 (`health-check.py`, `agent-api.py` hardcoding `/usr/bin/git`)
   and #2473 (`Sutando.app` falling back to `/usr/bin/env python3` behind a dead
   `python@3.11` probe) — both reported from a clean macOS VM installing a bundled
   Sutando, where the dialog returned every 60 seconds. Enforced by the `checks:` block —
   but note its **limit**: the machine gate matches explicit `/usr/bin/…` tokens only. A
   bare `git` or `python3` resolved through PATH lands on the same stub and the scanner
   cannot see it, so a green scan is *not* proof this class is absent. #2469 fixed exactly
   such a caller (`check_live_checkout_branch`, a bare `git`) that no pattern would flag.
   Read the activated path.
8. **A verdict must state merge-readiness explicitly** — "ready to merge" /
   "changes requested: …" / "LGTM, non-blocking". And it is only honest if you actually
   ran these criteria on *this* PR — a readiness claim with no evidence attached
   (no test run, no failure-mode named, no blast-radius call) is an over-claim.
   **Findings are not a verdict.** A review that ends on analysis leaves the author unable
   to tell "these are blockers" from "these are notes and I would merge it", so say which,
   and for anything short of ready, name the one thing that would change it.
   The verdict is a **recommendation, never a gate**: it does not substitute for the merge
   conditions in `CONTRIBUTING.md` (mergeable head, green required CI + CLA, two recorded
   approvals). *Grounded by:* a #2824 review that listed one clarity question and two minor
   notes and never said it was ready; the owner had to ask "do you recommend approval?".

9. **A negative result is not evidence until the instrument is shown able to produce a
   positive.** Much of a PR's evidence is a *zero*: "no other call sites", "no conflicts",
   "no hardcoded paths", "the check is silent at HEAD". Every one is produced by an
   instrument — a grep, a query, a script run — and an instrument that cannot fire returns
   the same zero as a genuinely clean tree. For any load-bearing negative, ask what was run
   and whether it was ever demonstrated to return non-zero. A control is cheap: run it where
   the thing DOES exist, or against the parent commit, and show it counting.
   *Grounded by:* four instances in one 2026-08-04 session, three of them the reviewer's own.
   (a) `gh pr list --json reviewDecision | select(==null)` was used to conclude "zero
   unreviewed PRs" — this repo requires reviews, so that field is never null and the query
   could not have returned anything; the same run also silently truncated at `--limit 100`
   of 105 open PRs. (b) An orphan-memory scan reported 4 unrecallable files; its pattern was
   `[a-z0-9_]+` and one filename contained an uppercase letter — the true count was 0.
   (c) Two before/after health-check runs both printed nothing, which reads as "the change
   didn't fire"; `MEMORY_DIR` is repo-slug scoped, so from a worktree it resolved to a
   directory that does not exist and the probe returned `None` — the code was correct both
   times and the harness was wrong. (d) A peer's probe reported `1 pending question` against
   a true 38 and delivered that number to the owner.
   The tell is that a broken instrument and a clean result are *byte-identical*, so the
   author's confidence carries no information. Reinforced 2026-08-17, three more in one
   hour, all failing in the *reassuring* direction: a timeline query filtered on
   `auto_merge_enabled` missed arms logged as `auto_squash_enabled` ("never armed");
   a hand-typed abbreviated SHA returned an empty check-run list ("no runs"); a
   banner-verification piped through `cut -c1-120` truncated the appended banner
   ("did not fire"). Each empty result would have closed its investigation.

10. **A fix that changes a decision rule is itself a decision rule — enumerate the
    adjacent inputs before pushing.** When a patch changes *how something is judged*
    (a readiness test, a routing check, a marker classification), the next reviewer will
    find the input one step to the side of the one you tested. Before pushing, list the
    other values that reach the same decision and exercise each; and when the repo already
    owns that judgement, ask the owner rather than restating its knowledge — the shared
    copy is where the edge cases have already been paid for.
    *Grounded by:* two PRs on 2026-08-13, five and six rounds each, every round a real
    finding and every one adjacent to the last. (a) #2867 — the reap's "does a result
    exist?" rule was rebuilt four times: a hand-rolled archive glob (a prefix collision
    satisfied the wrong task), then `-s` (accepted whitespace-only, which the shared
    `result_ready` contract rejects), then an overwrite that destroyed a late answer, then
    a check-then-`mv` that relocated one out of the delivery path. `local_task_protocol`
    and `result_ready` already answered the first two correctly. (b) #2868 — "is this
    session routed?" went display-suppression → durable burn history → any non-empty
    `ANTHROPIC_BASE_URL` → any scheme on the right host:port → a diagnosis line asserting
    a cause it had not checked → that same line echoing URL credentials into a shared
    self-diagnose bundle. Each fix was correct for the case its test named.
    The tell that you are in this pattern: your test suite grows by exactly one case per
    round, and each new case is the previous one with a single field changed.

11. **A change that removes a capability must relocate it, not just delete it — and one
    deployment's requirement is never a global default.** If a PR turns behaviour off for
    everyone to satisfy one environment, ask for the flag instead: the environment that
    wants the change carries it, and every other install is untouched. Then check the
    *teardown* side — code that stops, kills, or cleans up the removed thing usually
    survives the deletion and becomes a one-way operation. And check whether any existing
    opt-in path depended on the deleted code to produce its inputs.
    *Grounded by:* #2677 — "keep the open-source core headless" deleted the menu-bar app's
    build and launch from `startup.sh` (−100 lines) and shipped no replacement script, so
    every OSS install silently lost the app and the documented alternative became "run
    `swiftc` by hand". Three knock-on effects, none visible in the diff: `restart.sh:73`
    still `pkill`s the app while nothing starts it, making a restart a permanent stop;
    `install-sutando-app-launchd.sh` (#1294) still points at
    `src/Sutando/Sutando.app/Contents/MacOS/Sutando`, a bundle `startup.sh` was the only
    thing that built, so the pre-existing opt-in supervisor was silently disabled; and the
    PR's own new test asserted the removed strings appear *nowhere* in `startup.sh`, which
    made the previous default unrestorable without deleting the guard. Pin behaviour under
    the flag, never the absence of a string.

12. **Cut the diff against the merge-base, not against `main`.** `git diff origin/main <pr>`
    on a branch that is behind renders `main`'s own newer commits as *removals* by the PR,
    so a reviewer reads deletions the author never wrote. Use
    `git diff $(git merge-base origin/main <pr>) <pr>`, and for a stacked PR review the
    child-only commit as well as the cumulative result — the child layer is what this PR
    is being asked to add.
    *Grounded by:* #3020 (2026-08-17). Diffing it against `main` showed ~20 removed lines
    in `check_cron_runner`, a function the PR does not touch; the topic diff against its
    merge-base is 2 files, +105/-4. Verify a stated stack rather than trusting the body:
    `git merge-base --is-ancestor <parent-head> <child>` confirmed #2995's head really is
    an ancestor, which is what makes "merge the parent first" load-bearing rather than
    a courtesy — and what makes `--delete-branch` on the parent dangerous while the child
    is open.

13. **An unknown must not render as a value in the slot a measurement occupies.** When a
    field can be absent, unreadable, or unmeasurable, printing a number there is worse
    than printing nothing: the reader has no way to tell a measurement from a default, and
    the plausible ones are never questioned. Ask of any patch that formats a quantity:
    what does this print when the input is missing, zero-as-sentinel, or negative — and is
    that distinguishable from a real reading? The fix is always the same shape: carry the
    unknown (`None`) to the render site and say so there, rather than substituting a value
    upstream.
    *Grounded by:* five instances in one week, each found by a different reviewer and each
    resolved identically. (a) #2991 — a negative age rendered `-0.2h ago`; impossible, but
    it has the shape of a measurement, so nothing flags it. (b) #2994 — `age or 0.0`
    collapsed an unknown claim age into `oldest 0.0h`, one line above a formatter that
    cannot represent anything under 3.6 minutes anyway. (c) #3000 — a freshness guard
    existed only on the *exhausted* branch, so the reassuring reading was stated as current
    at any age; the guard sat where the author felt risk, and the branch without it was the
    one that lied. (d) #3020 — `int(data.get("updated_at", 0) or 0)` made an absent
    timestamp the whole unix epoch, printing `as of 1786962010s ago` (~56 years) on both
    the warn line *and* the all-satisfied line, which is the one a reader is least likely
    to question. (e) #3027 — `analyze_dev_activity` returned `None` for three distinct
    conditions, only one documented, with `subprocess.TimeoutExpired` invisible inside a
    generic `SubprocessError` handler; the same file already handled a sibling field
    correctly (`if landed is None: # Cannot tell what landed, so do not use the word`).
    The tell: the same expression supplies both the default and the measurement, usually as
    `x or 0`, `.get(k, 0)`, or an `except` that returns the empty case.

    **The same rule binds the REVIEW's own prose, not just the patch's code.** Every instance
    above is a formatter substituting a default for a measurement. A reviewer does it in
    sentences: a version number, a platform threshold, a library behaviour written from
    recall and set beside figures that were actually measured against the repo. On the page
    they are indistinguishable, and the recalled one is often the actionable one — it is
    what the author will go and act on. So **cite or label every external fact**: link the
    source, or write "unverified" next to it. A review that mixes six measured claims with
    one recalled claim does not read as five-sixths reliable; the measured majority launders
    the recalled sentence.
    *Grounded by:* 2026-08-30 — a review of `ag2-space/cinny-webclient#703` correctly
    measured that `color-mix()` had zero precedent in that codebase (with a positive control
    proving the search worked), then asserted it "needs WebKitGTK 2.42+" from recall. The
    number was invented and too conservative: the feature landed upstream at WebKit r273244
    in early 2021 and 2.42's release notes never mention it. Checking it produced the
    sharper finding the review should have carried — Tauri v2's documented Linux baseline
    (Ubuntu 22.04) ships WebKitGTK ~2.36 ≈ Safari 16, against a Safari 16.2 threshold. The
    unmeasured sentence sat in the paragraph the review named as the blocker.

14. **Never assert on source text as a stand-in for a behavioral claim.** When a module
    cannot be imported by tests (import-time side effects, heavy SDK deps), extract the
    decision into an importable unit and test THAT — do not regex the file. A source-text
    assertion fails in both directions: it stays green when the behavior is disabled
    outright (guard the call with `if (false && …)` and every token the regex matches is
    still present), and it goes red on a rename that changes nothing. Worked examples of
    the extraction convention already in-tree: `src/channel_token.py` (token-resolution
    policy extracted from four script consumers, tested behaviorally) and
    `src/result_markers.py` (marker grammar extracted from per-bridge private parsers,
    driven behaviorally by the bridge-marker-no-leak and dedup suites). When only content emitted verbatim is being pinned (an
    instruction template, a doc line), say so explicitly — that is a data pin, and it
    must be labeled as one, not passed off as a behavior test.
    Second exception: a source assertion is legitimate when the property is *structural*
    — a policy must not be duplicated, a path literal must not appear — because behavior
    cannot observe a duplicate that currently agrees (two copies in sync pass every
    behavioral test; the defect IS the duplication). This covers negative scans (no
    private parser, no `json.loads` in an adapter) and positive delegation pins (the
    adapter calls the shared owner — the form CLAUDE.md's "pin every adapter's
    delegation" already mandates), and it is what this file's own `checks:` block does.
    Pair it with the behavioral test of the extracted unit; never let it substitute
    for one.
    *Grounded by:* three independent instances across unrelated subsystems in one evening
    (2026-08-18) — the #3088 scroll-reporting test asserted on `browser-tools.ts` source
    text, and disabling the fix outright left its suite 5/5 green (verified via the
    if-false control during review); the same construct had just been found blocking a
    team-guard follow-up and in one earlier review the same night. Three authors, one
    evening: a missing convention, not a personal habit. (Lesson: air + 001.)
15. **A live-path PR is not approvable on harness proof alone — require a real
    post-restart round trip.** When a diff touches a bridge, the network/delivery loop,
    or startup, unit and harness tests can pass while the shipped path stays broken — a
    bridge that reconnects but drops the first message, a delivery claim that never
    releases, a watcher that respawns wedged. Before **approving**, require evidence that
    a real message or task flowed *through the restarted service* end-to-end. This is the
    same witness `CONTRIBUTING.md` already demands at merge time ("Live path (bridge /
    network / delivery loop / startup)? Include a real post-restart round trip, not just
    unit tests"); this lesson makes it a **review-time** gate so `review-preflight.py`
    surfaces it on every review, not only at the merge decision. A reviewer who has seen
    only green unit tests has not seen the behavior the PR changes. The witness is the
    author's to run; if it needs an owner-scheduled service window, that is an ASK with
    its cost named (which service goes down, for how long, whether inbound is replayed) —
    never grounds to approve without it, and never a disclosure footnote on an approval.
    *Grounded by:* #3174 (`fix(discord): suppress recreated task results after delivery`)
    — a Discord delivery-path change approved on its unit suite, then blocked back to
    CHANGES_REQUESTED on review because the delivery path had no post-restart witness. The
    approval read clean on every cheap signal (tests green, diff sensible); only exercising
    the restarted delivery loop end-to-end could have shown whether a recreated result is
    actually suppressed after a real delivery.

16. **An authorization or approval is a statement about the object as it stood at its
    timestamp — re-read the current state before acting on it.** Approvals, owner
    go-aheads, and "it's green, land it" messages all describe a specific head and
    review state. Before any merge-adjacent action (arming auto-merge, merging,
    dismissing a review), fetch the CURRENT `reviewDecision` and check whether any
    block, push, or review postdates the authorization being executed. A stored intent
    executed against a moved PR is unauthorized in substance even when the words were
    genuine.
    *Grounded by:* sonichi/sutando#3056 (2026-08-17) — an agent re-armed auto-merge
    citing an owner-side message sent when the PR had two approvals and zero blocks;
    by arm time a `CHANGES_REQUESTED` was 39 seconds old, nothing consulted the review
    state at the arm site, and a maintainer had to disarm manually 78 seconds later.
    The same night's counter-example proves the cheap check works: a half-written
    "armed over a stale approval" bug report was discarded by reading
    `reviewDecision` first — it was `REVIEW_REQUIRED`, so no authorization was
    outstanding to be stale. Blocks survive pushes; in this repo so do approvals
    (approvals are not dismissed on push on either gate surface — classic
    `dismiss_stale_reviews: false`, ruleset 19110427
    `dismiss_stale_reviews_on_push: false`) — an approval CAN be stale at arm
    time, which is exactly why the re-read is necessary rather than optional. The current state, not the
    remembered one, is what authorizes.

## Checks (machine-readable — consumed by scripts/review-checks.sh)

```yaml
checks:
  prose-cap:
    # Added COMMENT runs may not exceed this many PHYSICAL lines. Docstrings are
    # out of scope: CLAUDE.md caps "code comments" and never says "docstring".
    prose_cap: 2
    prose_exts: ['.py']

  root-artifacts:
    # Added files at the REPO ROOT matching these are PR-draft leftovers. Root
    # only; omitting the key uses these defaults rather than disabling the check.
    root_artifact_glob:
      - 'prbody*'
      - 'pr-body*'
      - 'pr_body*'
      - 'reply*.md'
      - 'comment*.md'
      - 'draft*.md'
      - '*.patch'
      - '*.diff'
      - '*.orig'
      - '*.rej'
      - 'nohup.out'

  hardcoded-paths:
    # Files matching these globs get a narrower exemption, not blanket exclusion
    # (checked against the WHOLE path, matched full-diff-line via fnmatch, so
    # '*.patch' also matches a nested path like 'skills/x/y.patch'). Within a
    # match, only a line whose SECOND character (the nested diff's own syntax)
    # is '-' is skipped — a stored .patch/.diff's own removal line reads as an
    # ADDED line in the outer PR diff, and that second character distinguishes
    # it from a real hardcoded path without inspecting content. An inner
    # addition or inner-context line stays in scope, since that's what a
    # re-applied patch (skills/plugin-patches/) actually introduces. Omitting
    # the key uses these defaults rather than disabling the exemption. Mirrors
    # the analogous glob under root-artifacts above, which lists the same two
    # extensions for the same underlying reason (a stored patch's own diff
    # syntax is not code to police).
    skip_glob:
      - '*.patch'
      - '*.diff'
    # Added lines containing any of these substrings are flagged as errors...
    flag:
      - '/Users/'
      - '/home/'
      - '/opt/'
      - '/private/'
      - '~/.claude'
      - '~/.sutando'
    # Whole-token matches (lesson 7): the Xcode-CLT stubs. Listed here rather
    # than under `flag` because these are full executable paths, and a substring
    # rule would also reject longer siblings in the same directory family —
    # '/usr/bin/swift-inspect' is a separate REAL binary (its own inode, link
    # count 1), unlike '/usr/bin/swift' and '/usr/bin/swiftc' which share the
    # stub inode with 76 other names. Blocking a legitimate platform tool is how
    # a mandatory gate gets disabled, so each stub is named exactly.
    #
    # To re-derive the family on a Mac: `ls -li /usr/bin | awk '$3==78'` — every
    # entry sharing that inode is the same stub. Only the names this repo
    # plausibly invokes are listed; add others as they come up.
    #
    # Non-stub /usr/bin binaries (env, pgrep, lsof, osascript, open, id, pmset,
    # xcode-select) are deliberately absent: they are real binaries and safe to
    # address absolutely.
    flag_exact:
      - '/usr/bin/git'
      - '/usr/bin/python3'
      - '/usr/bin/swift'
      - '/usr/bin/swiftc'
      - '/usr/bin/clang'
      - '/usr/bin/clang++'
      - '/usr/bin/gcc'
      - '/usr/bin/g++'
      - '/usr/bin/cc'
      - '/usr/bin/make'
      - '/usr/bin/gnumake'
    # ...unless the path token also matches one of these (fixtures / system noise).
    allow:
      - '/nonexistent'
      - '/usr/fake'
      - '/tmp/'
      - 'example.com'
      # The send-allowlist's OWN policy data, not a host path a helper could
      # resolve: is_path_sendable compares realpaths and macOS resolves /tmp to
      # /private/tmp, so both spellings must be listed or the prefix never
      # matches. Token-specific on purpose — a bare '/private/tmp/' allow would
      # also hide unrelated findings under that root.
      - '/private/tmp/sutando-'
      - '/private/tmp/echo-'
    # Tokens allowed ONLY when the SAME added line also carries a companion path
    # for the SAME binary — i.e. the portable candidate-list shape, never a naked
    # literal. Encoded as 'TOKEN_PREFIX :: COMPANION_PREFIX'.
    #
    # The companion is matched by BASENAME, not as a bare substring:
    # '/opt/homebrew/bin/ffmpeg' is exempt only beside '/usr/local/bin/ffmpeg'.
    # A substring test would exempt the naked form whenever any unrelated
    # '/usr/local/...' shared the line, re-opening the blind spot.
    #
    # Why paired and not a plain allow: '/opt/homebrew/' is Apple-Silicon-only
    # (Intel Homebrew installs under /usr/local), so an UNPAIRED use still breaks
    # a supported host — exactly the bug class rule 6 exists to catch. A global
    # prefix allow would hide that naked form too, which is broader than the need.
    # Pairing keeps the portable form legal while a bare
    # `X = "/opt/homebrew/bin/ffmpeg"` stays flagged.
    #
    # Scope note: this is a SAME-LINE test. The candidate lists it exists for are
    # written on one line (see skills/screen-record narration-tee.ts + record.py).
    # A list split across lines will still flag — deliberately: a multi-line
    # window would re-admit the naked form whenever a same-named companion
    # appeared anywhere nearby. Reformat the list onto one line, or resolve via a
    # shared helper. Controls for both directions live in
    # tests/review-checks.test.sh ('candidate-list' / 'naked' / 'unrelated').
    allow_paired:
      - '/opt/homebrew/ :: /usr/local/'
```
