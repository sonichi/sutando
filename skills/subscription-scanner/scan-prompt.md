# Subscription scan — prompt body

Scan the user's Gmail for active paid subscriptions and update `skills/subscription-scanner/state/subscriptions.json`. Then snapshot to `state/history/<YYYY-MM-DD>.json` and update `scan_history` in the main file with the diff (added/removed/amount-changed) vs the previous scan.

## What to search

Use these Gmail queries:
1. `subject:(subscription OR renewal OR "auto-renewed" OR "automatic renewal" OR "your subscription" OR "your monthly" OR "your annual" OR "renewed your") newer_than:90d`
2. `subject:(receipt OR "your invoice" OR "payment confirmation" OR "thanks for your payment" OR "thank you for your payment") newer_than:90d`
3. `from:(no_reply@email.apple.com OR no-reply@spotify.com OR billing@anthropic.com OR invoice+statements@mail.anthropic.com OR billing@openai.com OR no-reply@netflix.com OR mail@adobe.com OR billing@github.com OR no-reply@dropbox.com OR billing@notion.so OR no-reply@youtube.com OR billing@1password.com OR billing@nytimes.com OR billing@wsj.com OR no-reply@disneyplus.com OR no-reply@hulumail.com OR googleplay-noreply@google.com OR googlestore-noreply@google.com OR payments-noreply@google.com OR xfinity@account.xfinity.com OR noreply@teslainsuranceservices.com) newer_than:120d`

For each receipt found, read the message body if needed via `get_thread` to extract: vendor, amount, frequency, last charge date, next charge date.

## What counts as a "paid subscription"

**Include:**
- Recurring charges (monthly / annual / other cadence) for a service
- Family-account subscriptions billed to the user's card (iCloud+ for spouse / kids, Apple TV / Apple News+, etc.)
- Connectivity (cell/internet)
- Insurance (auto, home — recurring premiums)
- Streaming (Netflix, Spotify, Apple TV+, Disney+, etc.)
- News/publication (NYT, WSJ, Substack paid tiers)
- Software (Adobe, GitHub, 1Password, Notion, etc.)
- AI services (Anthropic Claude, OpenAI, Google AI Premium)
- Memberships with recurring dues (Costco, AAA — **only if** there's a renewal receipt)

**Exclude:**
- One-time purchases (single Costco order, Amazon books, Apple Books)
- Bills that are not subscriptions (utilities like electricity / water service if metered, medical bills, government fees, HOA dues are borderline — include if monthly/quarterly recurring)
- Newsletters / promo emails (NYT marketing, Substack free posts)
- Brokerage statements (Wealthfront, Schwab) — informational, not subscriptions
- Credit-card statements / renewal cards
- Newsletters / "your monthly summary" that are free (RideWithGPS recap, Wholefoods receipts)
- Per-flight billing from flight clubs (treat as service usage, not subscription)

## Format

Update `state/subscriptions.json` with the schema in `SKILL.md`. Always:
1. Preserve the `id` field if a subscription was previously known (match by vendor + account)
2. Compute the diff vs the previous scan: new vendors → `added`, missing/cancelled → `removed`, different amount → `amount_changed`
3. Set `last_scan` to the current ISO8601 timestamp (with timezone)
4. Append a `scan_history` entry
5. Snapshot the previous `subscriptions.json` to `state/history/<previous-scan-date>.json` (only if it doesn't already exist)

## Ambiguous cases

If you can't tell whether something is a real subscription (e.g. unclear if it's recurring, or you can only see promo not receipt), set `status: "uncertain"` and add a note explaining what to verify.

## Notify on changes

If the scan finds anything `added` or `removed` since the previous run, write a `proactive-{ts}.txt` to `results/` with a brief summary (not the full table — just "+1 added: X. -1 removed: Y") so the change is surfaced via Telegram.
