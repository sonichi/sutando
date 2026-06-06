# fleet-roster

Canonical name → Discord ID resolution for the Sutando fleet.
**The skill code ships with ZERO IDs.** IDs live in a private per-host file.

## Privacy contract

| Location | IDs | Committed | Memory-synced |
|---|---|---|---|
| `skills/fleet-roster/scripts/fleet_roster.py` | NONE | Yes | N/A |
| `~/.claude/fleet-roster.local.json` | YES | NO | NO |

## Setup (per host)

Create `~/.claude/fleet-roster.local.json` (never commit, never sync):
```json
{
  "air":  {"id": "1485364006297534584", "role": "agent"},
  "mini": {"id": "1490412828065267872", "role": "agent"},
  "pro":  {"id": "1509329143110565888", "role": "agent"},
  "lucy": {"id": "1494435872949665953", "role": "agent"}
}
```

Set `FLEET_ROSTER_PATH=/custom/path` to override the default location.

## API

```python
from fleet_roster import mention, mention_verified

# Static lookup (fast, uses local file)
mention("pro")                           # → "<@id>"
mention("pro", platform="ag2.space")    # → "@pro"

# Live membership check (requires discord-bridge + GUILD_MEMBERS intent)
import asyncio
asyncio.run(mention_verified("pro", channel_id=1234))
```

`mention()` raises `FileNotFoundError` if roster not installed.
`mention()` raises `ValueError` for unknown names.
`mention_verified()` raises `ValueError` if member not in channel; falls back to unverified if GUILD_MEMBERS unavailable.
