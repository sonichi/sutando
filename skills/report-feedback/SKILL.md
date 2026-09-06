---
name: report-feedback
description: File a bug report, feature request, or feedback about Sutando to the team from any surface (chat, Discord, Telegram, or a voice-delegated task) — or automatically (--auto) when the agent itself hits a Sutando/AG2 Space bug. Reuses the cloud /api/feedback API and auto-attaches diagnostic context. Use when the user says "report a bug", "something's broken, file it", "I have a feature request", etc.
---

# Report Feedback

When the user asks to **report a bug / issue / feature request / feedback about Sutando itself** — e.g. "report a bug", "something's broken, file it", "I have a feature request" — use this skill to file it.

It posts to the cloud `/api/feedback` route (the same one the desktop "Report an issue" form uses, which mirrors into GitHub issues) and auto-attaches diagnostic context (platform + a tail of recent workspace logs), so you don't need to gather logs yourself.

This is the **single reporting path for all surfaces** — chat, Discord, Telegram, and voice (which reaches it by delegating the task to the core agent). There is intentionally no separate voice tool, to avoid duplicating the same capability.

## Usage

```bash
python3 skills/report-feedback/report-feedback.py \
  --title "<short one-line summary>" \
  [--body "<what happened, steps to reproduce, what was expected>"] \
  [--kind bug|feature|other] \
  [--severity low|medium|high|critical] \
  [--no-logs] [--auto]
```

- Ask the user for a short **title** and a **description** if they're not already clear from the conversation. Infer `kind` (default `bug`) and `severity` (default `medium`) from context.
- **Announce the log attachment before sending, then honor an opt-out.** Recent diagnostic logs are attached by default. Since there's no visible checkbox on voice/chat (unlike the desktop form), *say so first* — e.g. "I'll attach recent diagnostic logs to help debug, unless you'd rather I didn't." If the user declines, pass `--no-logs`. This makes it an informed opt-out rather than a silent default (especially important on voice, where the user can't see what's being sent). Log excerpts are redaction-scrubbed (Bearer tokens, `token=`/`api_key=`/`secret=` values, common key formats, and the home-dir username are masked) as a backstop, but announcing is still required.

## Ask first (`--ask`, or `askFirst` in the owner's prefs)

The owner's approval step for automatic reports. Instead of filing, the report is parked as a draft under
`<workspace>/state/feedback-drafts/<id>.json` and registered in the engine's HITL store (`src/hitl`) as a
`HumanRequirement`: the gateway bridge projects it as the owner's card — **File this bug report · File
without logs · Skip** — into its proactive room, and applies the click with the same stale-revision guard
every other card gets. Nothing is posted by this script, so it needs no gateway env.

```bash
# ask (also what --auto does when feedback-prefs.json has "askFirst": true):
python3 skills/report-feedback/report-feedback.py --auto --title "..." --body "..."
# after the owner answers (the click is durable in the HITL store): run the clicked choices
python3 skills/report-feedback/report-feedback.py --apply
python3 skills/report-feedback/report-feedback.py --drafts        # pending drafts, oldest first
python3 skills/report-feedback/report-feedback.py --decide <draft-id> file|file_no_logs|skip   # by hand
```

- The click is applied **automatically, in the turn it causes**: the requirement is created with
  `turn_on_action`, so the bridge records the click and then lets the same relay task through to the
  core; that task carries the header `hitl_click: true` and the card label as its body — it is a click
  already recorded, not an instruction: answer `[no-send]`, do not file by hand, and let the turn end —
  and the skill's `Stop` hook (`manifest.json` → `hooks/apply-clicks.py`, registered by
  `bash src/install-claude-hooks.sh` like every skill hook) runs `apply_clicks()` as that turn ends.
  `--apply` is the same routine by hand. It also registers any parked draft whose card was never created
  (a store write that failed at ask time exits 3 and keeps the draft).
- Filing is exactly-once by markers: before the post the draft becomes `<id>.posting`; a 2xx renames it
  to `<id>.filed`, the card is resolved, the receipt removed; a definite server error renames it back
  to a draft. No answer at all (a transport error, a death mid-request) leaves `<id>.posting`, and
  `--apply` **holds** it — never re-posted on a guess. A held draft is owner-visible: the answered card
  closes and a new card asks "Bug report: filed or not? — File it again / Skip"; its click runs through
  the same path (`file` re-posts on purpose, `skip` drops the in-flight draft). `--drafts` lists parked
  drafts; in-flight ones are visible to `list_drafts(ws, state="posting")`, and `--decide <id> file|skip`
  settles one by hand and closes its cards. The payload carries `context.idempotency_key = <draft id>`
  for a server-side check.
- `file` attaches logs only if `sendLogs` is on; `file_no_logs` never does; `skip` drops the draft.
  Logs are gathered only after the choice — the card carries the title only.
- The ask is the throttled event: dedupe and the daily cap are checked and **recorded** when the card
  is asked, and the off-switch applies to `--ask` as well as `--auto`.
- Draft ids are `fb_` + 10 lowercase hex; anything else is refused before any read or unlink.

## Automatic reports (`--auto`)

When **you** (not the user) determine that a bug or error is caused by Sutando itself or AG2 Space — engine services, bridges, the desktop app, AG2 Space connectivity, or the AG2 cloud — file it automatically with `--auto`. Never `--auto`-file problems in the user's own projects or code, third-party tools/sites/APIs, or expected failures (bad input, credentials the owner simply hasn't provided).

`--auto` enforces the owner's Settings toggles (read from `<workspace>/state/feedback-prefs.json`, written by the desktop app). When the file is absent the two defaults differ:

- **File automatic bug reports** — defaults **ON**. Off → the script prints `SKIPPED` and exits 3. Respect it — do not retry or route around it.
- **Send logs with bug reports** — defaults **OFF**, i.e. opt-in. Off → the log excerpt is omitted from every report (auto and manual), same as `--no-logs`.

The split is deliberate. Absence of the file must not disable reporting on installs that predate the toggles, but absence is not consent either, and the log excerpt is the part that carries incidental owner data — paths containing usernames, hostnames, workspace content. An owner who has never opened Settings ships no logs.

Auto reports are also deduped (an identical title within 24h) and rate-limited (5 per 24h) via `<workspace>/state/feedback-auto-reports.json`. A `SKIPPED` exit (3) is a normal outcome, not an error — just move on. After filing an auto report, tell the owner in one short sentence (e.g. "I've filed a bug report about this"); the Settings toggle is the consent surface, so don't ask permission first.

## Behavior

- Requires the user to be **signed in to Sutando Cloud** (Settings → Sutando Cloud). If not, the script prints `NOT_SIGNED_IN` and exits 2 — relay that and ask them to sign in, then retry. For `--auto` reports, don't nag: mention it at most once.
- On success it prints `OK: filed <kind> report`. On API error it prints `ERROR: …` — relay a brief apology and offer to retry.
- Exit codes: `0` filed, `1` error, `2` not signed in, `3` skipped (auto reports disabled, duplicate, or rate-limited).

## Access tier

**Owner-tier only** — it files under the owner's Sutando Cloud identity, and it reads the owner's cloud token + attaches the owner's workspace log tail. Do not run it for non-owner (team/other) Discord, Slack, or Telegram tiers.

Non-owner tasks never reach this skill: the bridges route team/other tiers to a sandboxed `codex exec --sandbox read-only` agent (see CLAUDE.md access-control), which has no cloud token and cannot execute this script — so a non-owner can't ship the owner's logs into an issue. Only `access_tier: owner` (or an unauthenticated local/voice owner task) is processed with full capabilities that can invoke this skill.
