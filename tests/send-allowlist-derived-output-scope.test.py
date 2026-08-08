#!/usr/bin/env python3
"""`data/generated/` is sendable; the rest of `data/` must stay refused.

Skills need somewhere to put derived deliverables (rendered video, exports) that
is (a) attachable and (b) outside the vault carrier. `data/` satisfies (b)
already — the carrier whitelist emits `*` and never un-ignores it — so the only
missing piece was a send root.

The obvious version of that change is what this test exists to prevent.
Allowlisting `data/` WHOLESALE would expose, on a live host:

    data/conversation.sqlite            (+ -wal, -shm)   the conversation store
    data/memory-snapshots/*.tar.gz                       the whole memory corpus
    data/usage/usage-*.jsonl                             usage telemetry

All three were verified unsendable before this change, and must stay that way.
So the root is scoped one level down, and the negative half is asserted here —
a future widening to `data/` fails this file rather than shipping quietly.

`generated/` is deliberately generic rather than per-skill: the product repo has
no business naming a skill, and any skill's derived output belongs here.
"""
import importlib.util
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"


def _load():
    sys.path.insert(0, str(SRC))
    spec = importlib.util.spec_from_file_location("sa", SRC / "send_allowlist.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    sa = _load()
    failures = []

    def check(desc, cond):
        print(f"  {'OK  ' if cond else 'FAIL'}: {desc}")
        if not cond:
            failures.append(desc)

    roots = sa.SEND_ALLOWED_ROOTS

    # --- the root exists, and is SCOPED -------------------------------------
    check("data/generated is an allowed root",
          any(r.endswith("/data/generated") for r in roots))
    check("bare data/ is NOT an allowed root (would expose the conversation store)",
          not any(r.rstrip("/").endswith("/data") for r in roots))

    # --- behaviour, on real files in a real tree ----------------------------
    # `is_path_sendable` requires an existing regular file, so the fixture has
    # to live under the resolved workspace rather than a tmpdir.
    #
    # Derive from the module's own _REPO, NOT by searching SEND_ALLOWED_ROOTS:
    # a `next()` over the roots raises StopIteration when the root is absent,
    # which killed this half silently on the very states it must report.
    root = Path(sa._REPO) / "data" / "generated"
    made = []
    try:
        (root / "ep999-bundle").mkdir(parents=True, exist_ok=True)
        deliverable = root / "ep999-bundle" / "ep999.mp4"
        deliverable.write_bytes(b"\x00")
        made.append(deliverable)
        check("a derived deliverable under data/generated/ IS sendable",
              sa.is_path_sendable(str(deliverable)))

        # siblings one level up that must never become attachable
        parent = root.parent
        for name in ("conversation.sqlite", "usage/usage-probe.jsonl",
                     "memory-snapshots/memory-live-probe.tar.gz"):
            p = parent / name
            p.parent.mkdir(parents=True, exist_ok=True)
            if not p.exists():
                p.write_bytes(b"\x00")
                made.append(p)
            check(f"data/{name} stays REFUSED", not sa.is_path_sendable(str(p)))

        # a symlink out of the scoped root must not launder anything in
        outside = parent / "conversation.sqlite"
        if outside.exists():
            link = root / "sneaky.sqlite"
            if not link.exists():
                link.symlink_to(outside)
                made.append(link)
            check("a symlink from data/generated/ to data/ is still REFUSED "
                  "(realpath collapse)", not sa.is_path_sendable(str(link)))
    finally:
        for p in reversed(made):
            try:
                p.unlink()
            except OSError:
                pass

    print()
    if failures:
        print(f"FAILED ({len(failures)}): " + "; ".join(failures))
        sys.exit(1)
    print("All derived-output scope tests passed.")


if __name__ == "__main__":
    main()
