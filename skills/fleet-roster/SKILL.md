# fleet-roster

Canonical name → Discord ID mapping for the Sutando fleet. Prevents wrong-ID bugs when agents @-mention each other.

**Provides:**
- `get_member(name)` → `{id, channels, role, guild}`
- `mention(name)` → `"<@id>"` string ready for Discord messages

**Source of truth:** `~/.sutando/workspace/data/fleet-roster.json` (synced via memory-sync across all fleet hosts)

**Usage:**
```python
from fleet_roster import mention, get_member
msg = f"{mention('pro')} done: shipped fleet-roster skill"
```
