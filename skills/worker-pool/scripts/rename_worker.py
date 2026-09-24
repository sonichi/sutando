#!/usr/bin/env python3
"""Rename a worker after creation: change its display label, keep its id.

A label was settable only at `create_worker --label`, so a worker created
without one showed its 32-hex id in the picker and the desktop terminal tab
for life. The roster is the one store of labels; `pool_roster.rename_worker`
rewrites it under the roster lock and the compile republishes the
advertisement. The tmux session name is derived from the id and stays.

Run: python3 skills/worker-pool/scripts/rename_worker.py --worker <id-or-label> --label "<new name>" [--workspace W]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
for _p in (str(_SCRIPTS), str(_SCRIPTS.parents[2] / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pool_roster as pr  # noqa: E402

from workspace_default import resolve_workspace  # noqa: E402

REFUSED = 2


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="rename a worker (its label; the id stays)")
    ap.add_argument("--worker", required=True, help="the worker's id or current label")
    ap.add_argument("--label", required=True, help="the new display name")
    ap.add_argument("--workspace", default=None)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    workspace = a.workspace or str(resolve_workspace())
    try:
        wid, old, roster = pr.rename_worker(workspace, a.worker, a.label)
    except pr.PublishError as e:
        print(f"rename-worker: the roster is renamed (v{e.roster['version']}), but the "
              f"advertisement could not be written: {e.__cause__}\n"
              f"  the picker keeps the old name until this succeeds: "
              f"python3 skills/worker-pool/scripts/pool_advertise.py --workspace {workspace} --write",
              file=sys.stderr)
        return 1
    except pr.RosterError as e:
        print(f"rename-worker: {e}", file=sys.stderr)
        return REFUSED
    except OSError as e:
        print(f"rename-worker: the roster could not be written: {e}", file=sys.stderr)
        return 1

    new = a.label.strip()
    if a.json:
        print(json.dumps({"worker_id": wid, "old_label": old, "label": new,
                          "changed": roster is not None,
                          "roster_version": roster["version"] if roster else None}, indent=2))
    elif roster is None:
        print(f"worker {wid} is already named {new!r}; nothing changed")
    else:
        print(f"worker {wid} renamed {old!r} -> {new!r} (roster v{roster['version']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
