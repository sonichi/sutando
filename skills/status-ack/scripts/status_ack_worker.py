#!/usr/bin/env python3
"""The status-ack runner's hosted loop, as sparrowd starts it from the manifest's supervised_worker.

It runs status_ack.serve() on the resolved workspace forever. While STATUS_ACK_ENABLED is off it
reads and writes nothing and keeps waiting, so the supervisor never restarts it in a loop.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
import status_ack  # noqa: E402
from workspace_default import resolve_workspace  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="status-ack hosted loop (off unless enabled)")
    ap.add_argument("--workspace", default=None, help="default: the resolved workspace")
    a = ap.parse_args(argv)
    status_ack.serve(Path(a.workspace) if a.workspace else resolve_workspace(migrate=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
