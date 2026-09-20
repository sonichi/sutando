---
name: room-doc
description: Read and write a room's LIVE collaborative documents (Room Doc — the Yjs/CRDT documents behind the Doc tab, and the other kinds a room holds such as the whiteboard, selected with --kind). Use this when asked to write into, read, or collaborate in a room's document. NOT the same thing as `room_ops doc`, which is a room's Context-document folder — a different store entirely.
---

# Room Doc

A room's **live collaborative document**: the one a person sees in the Doc tab and
types into. Several writers edit it at once, merged character by character, with
each other's cursors visible.

## Not to be confused with the Context-document folder

| You want | Use |
|---|---|
| The live document people co-edit (Doc tab, whiteboard, deck) | **this skill** |
| A room's stored Context files (`get`/`put`/`rm` by name) | `room_ops context` (formerly `room_ops doc`) |

Two different stores. Writing to one never shows up in the other. This has already
sent one agent to the wrong place, which is why the warning is here and not further down.

## First contact — if you were @-mentioned and have never done this

```bash
P=skills/room-doc/scripts/room_doc.py
python3 $P doctor '!room:server'                       # 1. every setup step, one line each
python3 $P read   '!room:server'                       # 2. find the line that names you
python3 $P append '!room:server' $'\n\n@you — <your reply>'   # 3. answer UNDER it, signed
```

Then say one line in the room ("replied in the doc") — the person who called
you is watching the room, not the document. With the lane env loaded no flag
is needed; `doctor` tells you which step fails if one does.

Use `append` to reply, not `replace`: your text lands where nobody else is
typing, and the merge keeps everyone's characters. `replace` is for editing a
sentence you own.

**Global flags go BEFORE the subcommand.** `--url`, `--kind`, `--name`,
`--json` belong to the program, not the command: `room_doc.py --kind board
read <room>` works, `room_doc.py read <room> --kind board` is refused as
"unrecognized arguments".

## Requirements

```bash
pip install -r skills/room-doc/requirements.txt
```

## Credential

**The agent's ordinary relay token works.** The one every agent already holds
in `channels/<lane>/.env` as `REMOTE_TASK_TOKEN` (or `AG2_REMOTE_TOKEN`) opens
a room's documents; the service resolves it to the agent's own Matrix id. No
per-agent Matrix token and no extra grant are needed. A Matrix access token
also works.

The relay token ships in two shapes, **under the same variable names, on
different installs**: bare (`secret`) or compound (`https://host/relay|secret`).
Both are accepted here — the value is inspected, never the name. Passed to
anything else, the compound form must be split on `|`.

The document has exactly the **room's own ACL**. core-api distinguishes
non-member (404) from below-write-power (403), while the **WebSocket collapses
every refusal into one close** — from the client you can only see "refused".

Resolution order: `--token`, then `$AG2_MATRIX_TOKEN`, `$ROOM_DOC_TOKEN`,
`$MATRIX_ACCESS_TOKEN`, `$REMOTE_TASK_TOKEN`, `$AG2_REMOTE_TOKEN`. The service
URL comes from `--url`, then `$AG2_ROOM_DOC_URL`, `$AG2_API_ROOT`, the origin
of `$REMOTE_TASK_URL`, or the origin named inside a compound token. With the
lane env loaded, an agent needs neither flag.

## Command line

```bash
P=skills/room-doc/scripts/room_doc.py
python3 $P read   '!room:server'                      # print the document
python3 $P peers  '!room:server'                      # who is present
python3 $P append '!room:server' 'text to add'        # add at the end
python3 $P replace '!room:server' 'old text' 'new'    # refuses if absent, never writes blindly
python3 $P --name mars read '!room:server'            # publish presence while connected
```

Add `--insecure` only for a local rig with a self-signed certificate.

## The whiteboard is a different document

A room's board is a second document kind — `?kind=board` — and it holds a **map
of drawing elements**, not text. The text commands refuse on it rather than
answering: `read` on a board used to print an empty string, which is
indistinguishable from an empty whiteboard, and `append` used to succeed while
writing text no Excalidraw client ever reads.

```bash
python3 $P --kind board read  '!room:server'          # FIRST: what is already there, and where
python3 $P --kind board --json read '!room:server'    # …with x/y/width/height, to find free space
# A labelled box below whatever occupied y ≤ 400 — a box and its label are two elements:
python3 $P --kind board draw '!room:server' '[
  {"id":"w1","type":"rectangle","x":40,"y":460,"width":220,"height":80,"version":1},
  {"id":"w1t","type":"text","x":56,"y":488,"width":188,"height":24,"version":1,
   "text":"Worker 1","fontSize":20,"fontFamily":1,"textAlign":"left","verticalAlign":"top"}]'
python3 $P --kind board erase '!room:server' 'w1'     # marks isDeleted, the editor's own deletion
python3 $P --kind board peers '!room:server'          # presence is its own channel — works on any kind
```

**Where a drawing lands.** The board is usually not empty, and a drawing that
lands on what is already there is unreadable together with it — two agents
that both drew "at (0, 0)" produced two complete diagrams on top of each
other. So `draw` looks first: if any element you send would overlap a live
element someone else wrote, the whole batch is moved **below** the occupied
space (only `y` changes, the batch keeps its shape). Coordinates that already
sit in clear space are written exactly as given, and re-writing your own
elements (same ids, higher `version`) never moves them. Pass `--absolute` when
the coordinates are final and you mean to draw over something. `read` first
if you want to choose the spot yourself. Use ids of your own (a prefix that is
yours) — a write to an existing id is an edit of that element, not a new one.

An element needs `id` (equal to its key), a `type` the board draws, finite
`x`/`y`/`width`/`height`/`version`. A write lands only when it is **newer**
(higher `version`, ties broken on `versionNonce`), the same rule the web client
uses, so an agent and a person editing one board converge. Invalid elements are
refused rather than written — the web client validates on read, so a bad one
would be dropped by every viewer with no error anywhere.

Concurrent writes to one element are merged by Yjs on **client id**, which knows
nothing about element versions — so the older version can win and a shape
silently reverts. `put_elements` therefore arms an observer that re-asserts what
this session wrote whenever a remote change lands on it; `reconcile()` is there
for the rare case you want it by hand.

## Collaborating, rather than submitting

For anything beyond one edit, import the library and **hold the connection**:

```python
from room_doc_client import open_room_doc

async with open_room_doc(url, room_id, token) as doc:
    await doc.set_presence("mars")       # otherwise you edit invisibly
    print(doc.text, doc.peers)
    await doc.append("...")
    await doc.replace("old sentence", "new sentence")
```

Three things that matter more than they look:

1. **Publish presence.** Without it you are editing a document where nobody can see
   you — the person sharing it sees text appear from nowhere. Pass `user_id`
   (this agent's mxid) to get an avatar: the roster resolves faces by id, never
   by display name, so without it you appear by name with no face.
2. **One connection per agent per document.** Each connection is a separate peer:
   open a new one per edit and you appear in the presence list several times, as
   several people. Hold the context manager open instead.
3. **Send deltas, not the document.** `append`/`insert`/`replace` put only the change
   on the wire, which is why a human typing in the same paragraph loses nothing.
   Rewriting the whole text would be a last-writer-wins overwrite.

## Who wrote what

```bash
python3 $P --with-authors read '!room:server'
```

Prints, above the text, which Yjs client id belongs to which account and
whether it is a person or an agent (and whose agent). The document records
this on the server as writes land; an agent cannot claim authorship, only
read it. Use it to decide whether a paragraph is a human's to leave alone or
another agent's to continue.

## What a refusal means

The service **accepts the socket and only then closes** with a code, because a
close before accept cannot carry one. So these arrive as a failure to open, not
as an HTTP status:

| Close | Meaning |
|---|---|
| 4400 | The room id is malformed. **Not** "a room that exists and is empty". |
| 4404 | The document kind is malformed. |
| 4403 | Refused or withdrawn: not authorized for documents, or membership/write power changed. It can *also* mean core-api was briefly unreachable, so one 4403 is not proof of revocation. |
| HTTP 401 "bearer is not a valid Matrix user session" | The service has no record of this agent's token — a provisioning gap on that deployment (the local rig, typically), not a room permission. A different problem from 4403; ask whoever runs that deployment. |

A refusal is raised, never returned as an empty document — if it were, "this
room does not exist" and "this document has no content" would look identical.
