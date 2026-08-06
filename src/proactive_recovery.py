"""Restart recovery for proactively delivered result files.

Messaging bridges claim ``proactive-*.txt`` by renaming it to ``.sending``.
This module restores claims stranded by a crash so every adapter applies the
same collision, race, and failure policy at startup.
"""

from __future__ import annotations

from pathlib import Path


def recover_orphan_sending_files(results_dir: Path) -> int:
    """Restore orphan ``proactive-*.sending`` claims to the polling stream."""
    if not results_dir.exists():
        return 0

    recovered = 0
    for orphan in results_dir.iterdir():
        if not (orphan.name.startswith("proactive-") and orphan.suffix == ".sending"):
            continue

        target = orphan.with_suffix(".txt")
        try:
            if target.exists():
                print(
                    f"  [startup] skipping orphan recovery: {target.name} "
                    f"already exists (collision with {orphan.name})",
                    flush=True,
                )
                continue
            orphan.rename(target)
            recovered += 1
            print(f"  [startup] recovered orphan {orphan.name} → {target.name}", flush=True)
        except FileNotFoundError:
            # Another process recovered the same claim first.
            pass
        except Exception as exc:
            print(f"  [startup] failed to recover {orphan.name}: {exc}", flush=True)

    if recovered:
        print(f"  [startup] recovered {recovered} orphan .sending file(s)", flush=True)
    return recovered
