"""Shared proactive-delivery helpers (#1335 sub-PR-2).

Python-only — proactive ``results/proactive-*.txt`` files are delivered
only by the Python bridges (discord / telegram / slack). TypeScript-side
voice agents do not deliver proactive messages.

Currently exports:

- ``recover_orphan_sending_files(results_dir)`` — startup-safety helper
  that renames any orphan ``proactive-*.sending`` files back to
  ``proactive-*.txt`` so the next poll iteration re-claims them. Closes
  the bug class fixed in #1046 (where a bridge crash between the
  atomic-claim rename and the actual delivery left the message
  stranded forever, because no poll iteration ever looks at the
  ``.sending`` suffix).

Behavioral contract documented in ``docs/bridge-helpers-design.md``
§ proactive-delivery sweep. Unit + parity tests live in
``tests/proactive-delivery.test.py``.

Usage::

    from proactive_delivery import recover_orphan_sending_files

    recover_orphan_sending_files(RESULTS_DIR)
"""
from __future__ import annotations

from pathlib import Path


def recover_orphan_sending_files(results_dir: Path) -> int:
    """Rename orphan ``proactive-*.sending`` files back to ``.txt``.

    Atomic-claim-by-rename (``proactive-*.txt`` → ``.sending``) prevents
    same-tick double-deliveries between concurrent poll iterations. But
    if the bridge crashes BETWEEN the rename and the delivery, the
    ``.sending`` file sits orphaned — no poll iteration ever looks at
    ``.sending`` suffixes, so the owner notification is silently
    dropped until next manual intervention.

    Run this on startup before the poll loop starts. Idempotent: a
    second call sees no ``.sending`` files and is a no-op. Fail-open:
    any per-file error is logged but does not block.

    Returns the number of files recovered (0 if ``results_dir`` does
    not exist).
    """
    if not results_dir.exists():
        return 0
    recovered = 0
    for f in results_dir.iterdir():
        if not (f.name.startswith("proactive-") and f.suffix == ".sending"):
            continue
        target = f.with_suffix(".txt")
        try:
            # Don't clobber a same-named .txt that somehow re-appeared
            # (e.g. an operator manually re-dropped the file). The
            # atomic-claim invariant guarantees they don't normally
            # coexist, but be defensive on startup.
            if target.exists():
                print(
                    f"  [startup] skipping orphan recovery: {target.name} "
                    f"already exists (collision with {f.name})",
                    flush=True,
                )
                continue
            f.rename(target)
            recovered += 1
            print(
                f"  [startup] recovered orphan {f.name} → {target.name}",
                flush=True,
            )
        except FileNotFoundError:
            # Lost the race to another process; that's fine.
            pass
        except Exception as exc:
            print(
                f"  [startup] failed to recover {f.name}: {exc}",
                flush=True,
            )
    if recovered:
        print(
            f"  [startup] recovered {recovered} orphan .sending file(s)",
            flush=True,
        )
    return recovered
