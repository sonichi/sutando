# Subscription Scanner

Scans Gmail for active paid subscriptions and tracks them over time. Surfaces additions / cancellations between scans.

## Usage

The scan is **agent-driven**, not script-driven, because Gmail access lives in the Claude Code MCP layer (not in Python). The proactive-loop or owner-on-demand fires the scan via:

```
/scan-subscriptions
```

…or the cron job below invokes the agent with the `scan-prompt.md` body verbatim.

## Files

- `scan-prompt.md` — the prompt text the agent runs to perform a scan. Updates here propagate via the cron config. **Personalize this file before first use** — the third Gmail-query line names specific senders tied to one user's actual subscriptions (Apple, Spotify, Anthropic, OpenAI, Netflix, Adobe, GitHub, 1Password, NYT, WSJ, Disney+, Hulu, Tesla insurance, Xfinity, …). Edit the `from:` list to match your subscriptions, or the scan will miss vendors not on the default list.
- `state/subscriptions.json` — current list (gitignored — contains personal financial data)
- `state/history/<YYYY-MM-DD>.json` — snapshots, for diff (also gitignored)

## State schema

```json
{
  "last_scan": "ISO8601 timestamp",
  "subscriptions": [
    {
      "vendor": "Apple iCloud+ 2TB",
      "category": "Storage|Streaming|Connectivity|Insurance|Software|Membership|Other",
      "amount": 9.99,
      "currency": "USD",
      "frequency": "monthly|annual|other",
      "account": "a.kunte@gmail.com or family member name",
      "last_charged": "YYYY-MM-DD",
      "next_charge": "YYYY-MM-DD or null",
      "status": "active|cancelled|uncertain",
      "source_sender": "email From: address that established this",
      "notes": "free-form caveats / corrections"
    }
  ],
  "scan_history": [
    {
      "date": "ISO8601",
      "active_count": <int>,
      "added": ["vendor names"],
      "removed": ["vendor names"],
      "amount_changed": [{"vendor": "...", "from": <num>, "to": <num>}]
    }
  ]
}
```

## How `/paidsubscriptions` reads this

`web-client.ts` route at `/paidsubscriptions`:
1. Reads `skills/subscription-scanner/state/subscriptions.json`
2. Renders a sortable table with vendor, amount, frequency, account, status, last/next charge
3. Highlights diffs from the previous snapshot (`scan_history[-1].added` in green, `removed` in strikethrough red)
4. Shows last-scan timestamp at the top
5. Provides a "Scan now" button that POSTs to `/paidsubscriptions/scan` — that endpoint writes a task file to `tasks/` triggering an out-of-cycle scan in the next loop pass

## Cron

Monthly: 1st of every month at 08:13 (off-peak minute) — see `skills/schedule-crons/crons.json` entry `subscription-scan`.

`crons.json` is gitignored (per-user), so the entry isn't in this repo. Add it manually after cloning:

```json
{
  "name": "subscription-scan",
  "cron": "13 8 1 * *",
  "prompt": "Run the monthly paid-subscription scan. Read the full instructions in skills/subscription-scanner/scan-prompt.md and follow them verbatim. Update skills/subscription-scanner/state/subscriptions.json with the latest list, snapshot the previous version to state/history/, and publish a proactive Telegram notification as results/proactive-{ts}.txt (write results/.proactive-{ts}.txt.tmp, then mv it to the final name; never write the final name in place) only if subscriptions were added, removed, or had price changes since the previous scan. Stay silent if nothing changed."
}
```

The cron expression `13 8 1 * *` fires at 08:13 on the 1st of every month. Bump to `*/10 * * * *` during development to trigger every 10 min.
