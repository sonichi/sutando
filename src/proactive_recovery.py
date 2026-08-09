"""Restart recovery for proactively delivered result files.

Bridges claim ``proactive-*.txt`` as ``.sending``; this restores claims a crash
stranded so every adapter applies one collision and failure policy at startup.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


def claim_for_delivery(path: Path, recipient: Optional[object]) -> Optional[Path]:
    """Claim only when a recipient exists: a claim moves the file out of the
    ``*.txt`` glob every other bridge polls, stranding mail meant for a peer."""
    if recipient is None:
        return None
    claim = path.with_suffix(".sending")
    # link, not rename: POSIX rename REPLACES an existing claim and destroys a
    # peer's in-flight body. link fails with EEXIST, which is the collision gate.
    try:
        os.link(path, claim)
    except (FileExistsError, FileNotFoundError):
        return None
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    return claim


def release_claim(claim: Path) -> bool:
    """Return a ``.sending`` claim to the polling stream; True if released.
    Deleting it instead discards a message that is still being written."""
    target = claim.with_suffix(".txt")
    try:
        # Same clobber hazard as the claim: exists()-then-rename is check-then-act.
        os.link(claim, target)
        claim.unlink()
        return True
    except (FileExistsError, FileNotFoundError):
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
                # Both names on ONE inode = a claim whose unlink never ran; the
                # .txt is already correct. Different inodes ARE a real collision.
                if orphan.stat().st_ino == target.stat().st_ino:
                    orphan.unlink()
                    print(
                        f"  [startup] dropped half-made claim {orphan.name} "
                        f"(same inode as {target.name}; claim never completed)",
                        flush=True,
                    )
                    continue
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
