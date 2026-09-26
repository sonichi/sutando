#!/usr/bin/env python3
"""Apply an AG2 Space profile's complete worker display-label override map."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

import pool_roster as pr  # noqa: E402
from workspace_default import resolve_workspace  # noqa: E402

MAX_INPUT_BYTES = 16 << 20


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--workspace", default=None)
    p.add_argument("--config-version", type=int, required=True)
    p.add_argument("--profile-mxid", required=True)
    args = p.parse_args(argv)
    workspace = args.workspace or str(resolve_workspace())
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        if len(raw) > MAX_INPUT_BYTES:
            raise pr.RosterError("worker label overrides exceed 16 MiB")
        labels = json.loads(raw)
        result = pr.apply_profile_label_overrides(workspace, labels, args.config_version,
                                                  args.profile_mxid)
    except (ValueError, pr.RosterError) as e:
        print(f"apply-profile-label-overrides: {e}", file=sys.stderr)
        return 2
    except pr.PublishError as e:
        print(f"apply-profile-label-overrides: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"apply-profile-label-overrides: {e}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
