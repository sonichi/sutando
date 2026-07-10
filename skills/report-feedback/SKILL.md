---
name: report-feedback
description: File a bug report, feature request, or feedback about Sutando to the team from any surface (chat, Discord, Telegram, or a voice-delegated task). Reuses the cloud /api/feedback API and auto-attaches diagnostic context. Use when the user says "report a bug", "something's broken, file it", "I have a feature request", etc.
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
  [--no-logs]
```

- Ask the user for a short **title** and a **description** if they're not already clear from the conversation. Infer `kind` (default `bug`) and `severity` (default `medium`) from context.
- **Announce the log attachment before sending, then honor an opt-out.** Recent diagnostic logs are attached by default. Since there's no visible checkbox on voice/chat (unlike the desktop form), *say so first* — e.g. "I'll attach recent diagnostic logs to help debug, unless you'd rather I didn't." If the user declines, pass `--no-logs`. This makes it an informed opt-out rather than a silent default (especially important on voice, where the user can't see what's being sent). Log excerpts are redaction-scrubbed (Bearer tokens, `token=`/`api_key=`/`secret=` values, common key formats, and the home-dir username are masked) as a backstop, but announcing is still required.

## Behavior

- Requires the user to be **signed in to Sutando Cloud** (Settings → Sutando Cloud). If not, the script prints `NOT_SIGNED_IN` and exits 2 — relay that and ask them to sign in, then retry.
- On success it prints `OK: filed <kind> report`. On API error it prints `ERROR: …` — relay a brief apology and offer to retry.

## Access tier

**Owner-tier only** — it files under the owner's Sutando Cloud identity, and it reads the owner's cloud token + attaches the owner's workspace log tail. Do not run it for non-owner (team/other) Discord, Slack, or Telegram tiers.

Non-owner tasks never reach this skill: the bridges route team/other tiers to a sandboxed `codex exec --sandbox read-only` agent (see CLAUDE.md access-control), which has no cloud token and cannot execute this script — so a non-owner can't ship the owner's logs into an issue. Only `access_tier: owner` (or an unauthenticated local/voice owner task) is processed with full capabilities that can invoke this skill.
