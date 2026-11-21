#!/usr/bin/env python3
"""Refuse unrouted intake when the pool declares worker bindings."""
import sys
from pool_roster import load_bindings, RosterError


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        return 2
    try:
        bindings = load_bindings(args[0])
    except (RosterError, OSError) as exc:
        print(f"cannot establish routing requirement: {exc}")
        return 1
    count = sum(any(member != "core" for member in
                    (bound if isinstance(bound, list) else [bound]))
                for bound in bindings.values())
    print(f"{count} room(s) bound to workers")
    return 1 if count else 0


if __name__ == "__main__":
    sys.exit(main())
