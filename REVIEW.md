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
> reads this file and prints the criteria on every pre-review run (for the core agent);
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
   process-global patches with wide blast radius; and — per #1898 — for any auto-action,
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
7. **A verdict must state merge-readiness explicitly** — "ready to merge" /
   "changes requested: …" / "LGTM, non-blocking". And it is only honest if you actually
   ran these criteria on *this* PR — a readiness claim with no evidence attached
   (no test run, no failure-mode named, no blast-radius call) is an over-claim.

## Checks (machine-readable — consumed by scripts/review-checks.sh)

```yaml
checks:
  hardcoded-paths:
    # Added lines containing any of these substrings are flagged as errors...
    flag:
      - '/Users/'
      - '/home/'
      - '/opt/'
      - '/private/'
      - '~/.claude'
      - '~/.sutando'
    # ...unless the path token also matches one of these (fixtures / system noise).
    allow:
      # Standard package-manager prefix, not machine- or user-specific: identical
      # on every Apple-Silicon Mac with Homebrew, so it does not break on another
      # host the way rule 6 is written to prevent. It is caught only because the
      # flag list uses the broad '/opt/' token. The repo already resolves binaries
      # through candidate lists containing it (src/agent-api.py's tmux lookup,
      # health-check.py's _resolve_tmux_bin and _BRIDGE_INTERP_CANDIDATES,
      # skills/voice-agent-test-harness's `rec`) — those pass only because the scan
      # is diff-scoped, so the convention was effectively unextendable: adding a NEW
      # candidate list failed while the existing ones sailed. Note the asymmetry it
      # produced — '/usr/local/bin/ffmpeg' is NOT flagged, so the check permitted
      # the Intel half of a candidate list and rejected the Apple-Silicon half.
      # Safe because a path-like allow must match at the TOKEN START (see
      # scripts/review-checks.py:allowed), so this cannot mask '/Users/...'.
      - '/opt/homebrew/'
      - '/nonexistent'
      - '/usr/fake'
      - '/tmp/'
      - 'example.com'
```
