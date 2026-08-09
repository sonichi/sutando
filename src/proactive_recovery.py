"""Restart recovery for proactively delivered result files.

Messaging bridges claim ``proactive-*.txt`` by renaming it to ``.sending``.
This module restores claims stranded by a crash so every adapter applies the
same collision, race, and failure policy at startup.
"""

from __future__ import annotations

from pathlib import Path


def claim_for_delivery(path: Path, recipient: object | None) -> Path | None:
    """Claim ``path`` for delivery, but only when there IS somewhere to deliver.

    Returns the ``.sending`` claim, or None when this adapter has no recipient —
    a claim renames the file out of the ``*.txt`` glob every other bridge polls,
    so claiming what you cannot deliver strands mail addressed to a peer.
    """
    if recipient is None:
        return None
    claim = path.with_suffix(".sending")
    try:
        path.rename(claim)
    except FileNotFoundError:
        return None
    return claim


def release_claim(claim: Path) -> bool:
    """Return a ``.sending`` claim to the polling stream. True if released.

    For a claim whose file is not ready yet. Deleting it instead discards a
    message that is still being written.
    """
    target = claim.with_suffix(".txt")
    try:
        if target.exists():
            return False
        claim.rename(target)
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        print(f"  [proactive] could not release claim {claim.name}: {exc}", flush=True)
        return False


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
