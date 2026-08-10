"""dm-ban.sentinel gate, shared by every proactive/DM delivery consumer.
Fails closed: any resolution error means banned, never silently delivers."""

import os


def is_dm_banned(workspace_dir) -> bool:
    sentinel = os.path.join(str(workspace_dir), "state", "dm-ban.sentinel")
    try:
        os.stat(sentinel)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True
