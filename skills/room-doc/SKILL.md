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

## Requirements

```bash
pip install -r skills/room-doc/requirements.txt
```

## Credential

The bearer must authorize **this agent** for documents:

- a **Matrix access token**, or
- an **AG2 agent ticket carrying the `doc.write` grant** (see ag2space-backend #1247).

An AG2 ticket minted only to pull tasks (`events.pull`) is refused — that is
deliberate, not a misconfiguration. The document has exactly the **room's own
ACL**, but note the difference between the policy and what you can observe:
core-api distinguishes non-member (404) from below-write-power (403), while the
**WebSocket collapses every refusal into one close**. From the client you cannot
tell "not a member" from "not enough power" — only "refused".

Resolution order: `--token`, then `$AG2_MATRIX_TOKEN`, `$ROOM_DOC_TOKEN`,
`$MATRIX_ACCESS_TOKEN`. The service URL comes from `--url`, then
`$AG2_ROOM_DOC_URL`, then `$AG2_API_ROOT`.

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
python3 $P --kind board read  '!room:server'          # list elements in drawing order
python3 $P --kind board draw  '!room:server' '[{"id":"r1","type":"rectangle","x":0,"y":0,"width":100,"height":60,"version":1}]'
python3 $P --kind board erase '!room:server' 'r1'     # marks isDeleted, the editor's own deletion
```

An element needs `id` (equal to its key), a `type` the board draws, finite
`x`/`y`/`width`/`height`/`version`. A write lands only when it is **newer**
(higher `version`, ties broken on `versionNonce`), the same rule the web client
uses, so an agent and a person editing one board converge. Invalid elements are
refused rather than written — the web client validates on read, so a bad one
would be dropped by every viewer with no error anywhere.

Holding the board open and drawing repeatedly? Call `reconcile()` after remote
changes. Concurrent writes to one element are merged by Yjs on client id, which
knows nothing about element versions, so the older version can otherwise win.

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

## What a refusal means

The service **accepts the socket and only then closes** with a code, because a
close before accept cannot carry one. So these arrive as a failure to open, not
as an HTTP status:

| Close | Meaning |
|---|---|
| 4400 | The room id is malformed. **Not** "a room that exists and is empty". |
| 4404 | The document kind is malformed. |
| 4403 | Refused or withdrawn: not authorized for documents, or membership/write power changed. It can *also* mean core-api was briefly unreachable, so one 4403 is not proof of revocation. |

A refusal is raised, never returned as an empty document — if it were, "this
room does not exist" and "this document has no content" would look identical.
