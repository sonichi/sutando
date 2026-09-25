#!/usr/bin/env python3
"""sparrowd resolves its worker script inside this skill; the daemon itself
lives in packages/room-collab."""
import sys

if __name__ == "__main__":
    import _edge

    _edge.install()
    import presence_daemon  # the package's module: install() put it first

    sys.exit(presence_daemon.main())
