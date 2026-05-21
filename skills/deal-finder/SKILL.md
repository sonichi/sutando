---
name: deal-finder
description: Scan configured sources (Craigslist now; eBay + Facebook Marketplace planned) for used-item listings matching the owner's criteria. Currently configured for a Mac mini search (M2+, 16GB+, 512GB+, ≤$500, near 94566). Notify owner via SMS + Telegram on a match.
user-invocable: true
---

# Deal Finder

Watches configured sources for second-hand item listings matching owner-defined criteria. Sends an SMS + Telegram DM when a new matching listing appears.

**Usage**: `/deal-finder` (one-shot scan) — typically run from cron every 60 min.

## Searches

V1 keeps the original Mac Mini search inline (`state/criteria.json`). V2 will move this to a `state/searches.json` array with one entry per search, so adding "Pelican 1535 case", "Aeron chair size B", etc. becomes a JSON edit rather than a code change.

Current Mac Mini search criteria (from `state/criteria.json`):

- Chip family: `M2`, `M3`, `M4` (M1 explicitly excluded — owner asked for M2+)
- Min RAM: `16 GB`
- Min storage: `512 GB`
- Max price: `$500`
- ZIP: `94566` (Pleasanton, CA), search radius `50 mi`

Edit `state/criteria.json` to retune the Mac Mini search; v2 will lift this into a per-search config.

## Sources

V1 (implemented): **Craigslist** (`sfbay.craigslist.org/search/sss?...`). Uses an honest User-Agent (`Sutando-Personal-Agent/1.0`) that identifies the agent rather than cosplaying a browser — Craigslist can decide whether to allow.

V2 (planned, not yet implemented):
- **eBay** — `browse.ebay.com` has a local-pickup filter; HTML scraping works without auth.
- **Facebook Marketplace** — requires a headless browser (Playwright via the `macos-use` skill, or the browser-automation MCP) because the page is JS-rendered and gated.

## Behavior

1. Fetch the Craigslist search results page with the honest UA.
2. Parse each listing: title, URL, price, neighborhood.
3. For each listing not in `state/seen.json`:
   - Fetch the listing detail page (one extra HTTP req per listing — cheap, only on first sight).
   - Apply criteria filter (chip, RAM, storage, price). Listings missing fields fall through to "soft match" — flagged but still notified, since Craigslist sellers often omit specs.
   - On match: notify (SMS + Telegram), record URL in `seen.json`.
4. Trim `seen.json` deterministically to the last 1000 entries (a `deque(maxlen=1000)` ordered by insertion — replaces the prior set-slicing trim which was non-deterministic).

## Notification format

```
[Mac Mini Deal] $480 — M2 / 16GB / 512GB
Concord (35mi, local pickup)
Posted recent
https://sfbay.craigslist.org/...
```

SMS goes via `TWILIO_*` env vars to `OWNER_NUMBER`. Telegram goes via `results/proactive-deal-finder-{ts}.txt` (the bridge picks it up).

## Run it

```bash
python3 scripts/scan.py            # one-shot scan with default criteria
python3 scripts/scan.py --dry-run  # don't notify, print what would notify
python3 scripts/scan.py --reset    # clear seen.json (force re-notify everything)
```

Cron: every 60 min. Configured in `skills/schedule-crons/crons.json` as `deal-finder`.
