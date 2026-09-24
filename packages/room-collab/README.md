# room-collab

Client for AG2 Space's collaborative surfaces — the room doc, whiteboard and
kanban — over the room-collab Yjs service. An agent reads and writes a surface,
comments on it, summons someone into it, and can stay resident after a summon.

The wire contract (which CRDT shapes each surface uses, and why) is in
[`CRDT-SHAPES.md`](CRDT-SHAPES.md). The whiteboard's convergence and validity
rules in `room_collab_board.py` must match the web client's `boardDoc.ts`.

## Host hooks

The package names no host layout. A host fills two slots in `room_collab`:

- `WORKSPACE_RESOLVER` — callable returning the workspace path, used when
  `--workspace` is not given. Unset, those commands refuse.
- `ROOM_OPS_SCRIPT` — path to the script that posts room messages (comments,
  replies). Unset, posting refuses.

Sutando's `skills/room-collab/scripts/_edge.py` is the reference host.

## Usage

```bash
pip install ./packages/room-collab
room-collab --kind board read '!room:server'
```

Command reference: `skills/room-collab/SKILL.md`.
