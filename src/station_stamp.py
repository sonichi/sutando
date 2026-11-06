"""The desktop's station stamp: what the running core's sutando-station server was started with.

`<workspace>/state/station-core-stamp.json`, written by the desktop at each core spawn:
`{"version":1,"has_station_entry":bool,"cloud_user_id":string|null,"spawned_at":"<ISO8601 UTC>"}`.
The station's tools act as `cloud_user_id` until the core restarts, whoever signs in meanwhile.
"""

from __future__ import annotations

import json
from pathlib import Path

STAMP_PATH = ("state", "station-core-stamp.json")


def read_station_stamp(workspace: Path | None) -> dict | None:
    """The desktop's record of what the running core loaded; None when absent or unusable."""
    if not workspace:
        return None
    try:
        stamp = json.loads(Path(workspace).joinpath(*STAMP_PATH).read_text())
    except (OSError, ValueError):
        return None
    return stamp if isinstance(stamp, dict) and stamp.get("version") == 1 else None
