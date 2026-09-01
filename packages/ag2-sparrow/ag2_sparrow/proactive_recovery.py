"""Restart recovery for proactively delivered result files.

Bridges claim ``proactive-*.txt`` as ``.sending``; this restores claims a crash
stranded so every adapter applies one collision and failure policy at startup.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

# A claim this process took but had not finished when it died.
_PRIVATE_CLAIM_RE = re.compile(r"^(proactive-.*\.sending)\.recover-(\d+)-\d+$")


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


def release_claim(claim: Path, target: "Optional[Path]" = None) -> bool:
    """Return a ``.sending`` claim to the polling stream; True if released.
    Deleting it instead discards a message that is still being written.
    ``target`` overrides the ``.txt`` sibling for pid-scoped claim names."""
    if target is None:
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


def _recovery_target(name: str) -> Optional[str]:
    """Map a claim name to its ``.txt`` destination, or None if not a claim.
    The private ``.recover-`` form counts: a crash must not strand it unscanned."""
    match = _PRIVATE_CLAIM_RE.match(name)
    base = match.group(1) if match else name
    if not (base.startswith("proactive-") and base.endswith(".sending")):
        return None
    return base[: -len(".sending")] + ".txt"


def _holder_is_live(name: str) -> bool:
    """True if a private claim names a still-running OTHER process."""
    match = _PRIVATE_CLAIM_RE.match(name)
    if not match:
        return False
    pid = int(match.group(2))
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        return True
    return True


def recover_orphan_sending_files(results_dir: Path) -> int:
    """Restore orphan ``proactive-*.sending`` claims to the polling stream."""
    if not results_dir.exists():
        return 0

    recovered = 0
    seq = 0
    for orphan in sorted(results_dir.iterdir()):
        target_name = _recovery_target(orphan.name)
        if target_name is None:
            continue
        # A private claim whose owner is still running is mid-recovery, not orphaned.
        # Say so: silence here cannot be told from a body stranded behind a reused pid.
        if _holder_is_live(orphan.name):
            print(f"  [startup] deferring {orphan.name}: holder still running", flush=True)
            continue

        target = orphan.with_name(target_name)
        # Derive from the BASE claim name, never from orphan.name: re-suffixing an
        # already-private name yields one this scan no longer matches.
        base = target_name[: -len(".txt")] + ".sending"
        private = orphan.with_name(f"{base}.recover-{os.getpid()}-{seq}")
        seq += 1
        try:
            # link, not rename: rename REPLACES a colliding private claim and
            # destroys its body. EEXIST is the gate, exactly as claim_for_delivery.
            os.link(orphan, private)
        except FileExistsError:
            print(
                f"  [startup] skipping {orphan.name}: {private.name} already holds a body",
                flush=True,
            )
            continue
        except FileNotFoundError:
            continue  # another recovery took this claim first
        except OSError as exc:
            print(f"  [startup] failed to recover {orphan.name}: {exc}", flush=True)
            continue
        try:
            orphan.unlink()
        except FileNotFoundError:
            pass

        try:
            if target.exists():
                # Both names on ONE inode = a claim whose unlink never ran; the
                # .txt is already correct. Different inodes ARE a real collision.
                if private.stat().st_ino == target.stat().st_ino:
                    private.unlink()
                    print(
                        f"  [startup] dropped half-made claim {orphan.name} "
                        f"(same inode as {target.name}; claim never completed)",
                        flush=True,
                    )
                    continue
                # A real collision never justifies deleting a body: put the claim
                # back if the name is free, else keep it under the private name.
                restore = orphan.with_name(base)
                try:
                    os.link(private, restore)
                    private.unlink()
                    stranded = restore.name
                except (FileExistsError, OSError):
                    stranded = private.name
                print(
                    f"  [startup] skipping orphan recovery: {target.name} "
                    f"already exists (collision with {stranded})",
                    flush=True,
                )
                continue
            os.link(private, target)
            private.unlink()
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
