#!/usr/bin/env python3
"""Entry point kept at the path SKILL.md and callers use; the code lives in
packages/room-collab."""
import sys

if __name__ == "__main__":
    import _edge

    sys.exit(_edge.install().main())
