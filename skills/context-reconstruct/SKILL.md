---
name: context-reconstruct
description: Re-anchor on the durable record (current-track, live owner thread, pending-questions, relay, build_log) before acting on anything that depends on earlier context. Read, do not recall.
---

# context-reconstruct

**Goal:** never act on lost/eroded memory of the ongoing work. The agent can be interrupted, compacted, or run dozens of interleaved cron passes and still pick up exactly where the thread left off — *without the owner re-reminding it*.

**Why a skill, not a script:** a hardcoded bundler (fixed N reads, one channel, fixed depth) is rigid — and easy to *skip* (narrate "re-anchor" then assert from memory anyway). The fix isn't a rigid script; it's flexible judgment + practice. This file is living — improve it whenever a reconstruction misses.

## The one rule

**Before interpreting or acting on anything that depends on earlier context, READ the durable record. Don't recall — read.** A "re-anchor" you claim but don't actually read is the failure.

## What to read — judgment, fit to the moment (not a checklist to run blindly)

**Read the current-track record FIRST** — `<workspace>/hosts/<hostname>/current-track.md` (this skill owns it; see "Maintain" below).
  **It is a bounded head, not the whole history.** Every pass pays this read, so the file is rotated: entries older than the newest ~32 KB live in `current-track-archive.md` beside it (`scripts/current-track-rotate.py`, run when health-check's `context-read-budget` probe warns; head + archive is the original, nothing is deleted). Read the head; open the archive only for a specific older referent.
  Resolve `<hostname>` with `bash scripts/sutando-config.sh host-label`, the same way `pending-questions.md` does. **Legacy fallback:** if that file is absent but `<workspace>/state/current-track.md` exists, read the legacy path and migrate it to the per-host path on the next write — the flat path was shared across hosts and is being retired (#2567).
 It's the fast anchor: the **current main-track goal**, the active sub-task, and the live open decisions. This is what's missing when "continue your main track" gets guessed — the goal must be a pinned record, not inferred from luck.

Then, as the situation needs (pick what's *relevant*; skip what isn't):

- **The live thread** — the channel(s) the owner is actually active on: `python3 src/discord-read.py <channel_id> --serving <task channel_id>` when serving a task (the contextNotFrom gate runs before the fetch), or `--operator` on autonomous passes with no serving context (Discord), telegram task `[Replying to…]` quotes (Telegram has no history fetch). Go **as deep as the thread needs** with `--until <id|iso>` — not a fixed message count. If unsure which channel is live, check the most recent task's `channel_id` / `state/last-owner-activity.json`.
- **Open decisions** — per-host `pending-questions.md`.
- **Recent judgment/decisions** — latest `relay/relay-*.md`.
- **What's built / next** — `build_log.md` tail.
- **Deep history** (older than the channel can cheaply reach) — the session transcript JSONL.

Effective > exhaustive: read enough to make *this* message/decision stand on its own, then stop.

## Maintain the current-track record (the skill owns it)

The skill both **uses** and **maintains** `<workspace>/hosts/<hostname>/current-track.md` (NOT the legacy flat `state/current-track.md` — writing there again re-creates the cross-host delivery of one host's anchor onto another at the same local path; see the 2026-08-03 practice-log entry below, which retracts the "clobber"/data-loss framing this line used to carry) — the owner doesn't dictate its content and the agent doesn't invent it from memory; it's **derived from the reconstruction**:

- **Create it if absent** (first run): write the current main-track goal + active sub-task + key open decisions, derived from what the durable record (thread / build_log / pending-questions / relevant project memory) actually shows.
- **Update it when the track moves** — after a reconstruction reveals the goal/sub-task/decisions changed (owner redirected, a thing shipped, a decision resolved), rewrite it. Keep it short (a pinned summary, not a log).
- Next reconstruction reads it first → "what's the main track" is never a guess again.

## Then

Compare what you read against what you *think* is true. **Where they differ, trust the record.** If the current track is a still-open owner thread, continue THAT.

## When to reconstruct

When the thing in front of you isn't self-contained — terse ("y", "no", "?", a pronoun), a reply, refers to something not stated, or you're resuming after a gap/compaction. Keyed on the *message/situation*, not on felt confidence (felt confidence is what fails — the agent is confidently wrong).

## Practice log (improve this skill here)

- v0: created after a hardcoded `reanchor.sh` was rejected for being rigid + skippable. Open problem: making "actually read" reliable (it's a habit, not a one-liner). Iterate as misses happen.
- invocation test PASSED: wired into proactive-loop step 0.7 as an actual Skill-tool **invocation** (not a "see X" reference — references don't load). Verified: invoking loads this body, then following it (read the live thread) confirms the current track. Invocation is the reliable-load half; doing the read is still the habit half.
- added current-track ownership: the skill now READS `state/current-track.md` first and MAINTAINS it (derive from the record, don't dictate/invent). Seeded the file from the durable record. Closes the "reconstructs context but not the persistent goal" gap.
- **frontmatter is what makes it invocable.** This file originally shipped without YAML frontmatter while most sibling skills carry `name:`/`description:`. A skill that isn't discoverable can't be invoked, and step 0.7 then no-ops *silently* — no error, no warning, indistinguishable from having run. Whenever this skill is changed, the check that matters is an actual Skill-tool invocation, not the file's presence on disk.
- 2026-08-03 PATH MOVED: the anchor now lives at `<workspace>/hosts/<hostname>/current-track.md`, not the flat `state/current-track.md`. The flat path was added to the shipped carrier set by #2534 and is **shared across hosts**, so two cores write the same vault path and a peer's anchor is delivered into your working copy. `hosts/<label>/` is already carried by `hosts/*/`, so the per-host path needs no carrier entry and cannot collide — structurally impossible rather than correctly configured (#2568, merged 2026-08-03T12:44:58Z; refinement credit: Sutando-Mini).

  **⚠ CORRECTED 2026-08-04 — this entry previously said "after a live data loss" and claimed a peer "overwrote this host's 1056-line anchor … three writes, all destructive". THAT IS FALSE, and Chi corrected it.** The vault uses **per-host branches** (`host/<host>/<wsid>`): a host only ever merges a peer INTO its own branch and never writes to the peer's. Checked afterwards — both branches were byte-identical, and this host's index referenced **263** memory files against the discarded copy's **262**, a strict superset with nothing missing. Sutando-Pro independently confirmed it from the file's own two-commit history. The correction is written into `src/health-check.py` (see the `UNSAFE_TO_READD` comment, ~line 1396), which is the authority.

  **The guidance is unchanged — do not re-add the flat path — but the reason is cross-host CONTENT DELIVERY on a shared path, NOT data loss.** Keeping the wrong reason here mattered: this file is loaded on every proactive-loop pass via step 0.7, so it re-taught a claim the owner had already retracted, and on 2026-08-04 I repeated "destroyed a 1056-line anchor" back to Chi from it. A retraction has to reach the file that gets *read*, not only the file that learned it.

  Note the 2026-06-25 entry above is left as written — it was true then.

- **2026-08-13 — the first entry here that is NOT "I did not read." I read, and the reader returned a
  plausible partial result.** Every entry above assumes the agent is the unreliable part, and the fix
  is always *go read the durable record*. This one is about the record's **reader** being unreliable,
  and it defeats the whole step silently.

  A history read returned only messages from a recent cutoff onward — at every limit up to 1000 —
  while a local archive held months of earlier traffic from the same source. Probed with known ids
  from before the cutoff: **0 returned**, with a positive control confirming the id field was
  populated on every message it *did* return, so the zero was absence rather than an empty field.

  **The property to carry: an agent cannot distinguish a plausible partial result from a complete
  one.** A windowed read and a genuinely short thread are the same object from inside. So step 0.7's
  "reconstruct the live thread" can report success while returning almost nothing, and felt
  compliance is exactly as unreliable here as felt confidence is everywhere else in this file.

  **What to do instead.** When a reconstruction returns fewer items than the situation implies, treat
  the shortfall as *unexplained* until something **outside that reader** accounts for it. Three cheap
  discriminating probes, in order:

  1. **Ask for FEWER items than certainly exist.** This pins what any "complete" flag means. If the
     flag is just `returned == requested`, it is not a truncation signal at all.
  2. **Ask again after new activity.** Does the count grow, or does the window slide? A *wider*
     window returning *fewer* results proves the reader is not enumerating.
  3. **Check against an independent record of what arrived** — and verify that record measures the
     thing you are about to name.

  **Probe 3 is where the second mistake lives.** An outside record is not automatically trustworthy:

  - **Match on the field, never a bare substring.** A substring search over a message corpus also
    matches items that merely *quote* the identifier, which silently widens the population.
  - **A file mtime is a proxy that is `>=` the event time, and its tail is brutal.** Calibrated over
    2678 files carrying both a declared timestamp and an mtime: median lag **+1s**, maximum lag
    **8.1 days**. An excellent proxy almost always and catastrophically wrong occasionally — the worst
    possible shape, because a small sample lands in the well-behaved majority and reads as
    confirmation. **Quote a max, never a rate**: measured across three populations the tail frequency
    spans two orders of magnitude (0.1% / 5.5% / 20%), so the rate is a property of the corpus, not of
    the archive. Name the corpus and the filter with any such number.
  - Look for **an era or subset where ground truth was recorded for a different reason** — here, older
    files still carried a declared timestamp that the current schema had dropped. That is usually
    where calibration data hides when the present has none.

  **And "two independent methods" must mean methods with different FAILURE MODES**, not two
  invocations. Two calls that share a cutoff, a corpus, or a path convention can agree perfectly while
  omitting the same thing. Independence is semantic, and the only way to establish it is to have seen
  the two disagree on a case where you know the answer.
