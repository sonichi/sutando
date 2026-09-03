# Sutando Server & the Agent ID card — design

**Status:** Design note, written 2026-08-24 from the shipped modules under
`src/runtime-api/`. Every motivation below is quoted or paraphrased from those
modules' own contracts, not reconstructed after the fact.

> **Naming caveat.** The phrase "ID card" appears nowhere in this repository.
> This document maps it to the **read-only identity surface** in
> `identity_view.py` — `sutando.info` / `status` / `owner` / `allowlist`, which
> that module calls "the Sutando Server *smallest slice*". If you meant
> something else by ID card, the second half of this doc is aimed at the wrong
> target and should be corrected before anyone builds from it.

---

## Part 1 — Sutando Server

### Motivation: an unattended agent still needs a human sometimes

A Sutando core runs long, alone, and with real capability. Two things follow
that a single process cannot solve for itself:

1. **It must be able to ask.** Approval before a governed action, elicitation
   when input is missing. An agent that cannot ask either stalls forever or
   proceeds without consent; both are failures.
2. **The asking must survive a crash.** If the process dies between "approval
   granted" and "action performed", the system must know which side of that
   line it was on. Ephemeral in-memory state cannot answer that question.

The Server exists to be the component that owns those two problems: a local
JSON-RPC daemon over a private Unix socket, with approval, elicitation and
capability execution as **durable** requests (SQLite), while discovery and
read traffic stay deliberately **ephemeral**.

### The load-bearing split: transport vs. request domain

`server.py` owns the daemon — socket ownership and permissions, framing,
connection lifecycle, supervision. `dispatcher.py` owns the request domain —
method dispatch, approval and elicitation validation, governed-capability
authorization, idempotency, durable state transitions, crash recovery.

The split is not tidiness. Its stated purpose:

> so the security policy is directly testable without a socket, and so a future
> transport cannot reimplement (or quietly diverge from) approval and
> capability behavior.

That second clause is the real motivation. A second transport — a different
socket, a remote bridge, a test harness — is exactly the kind of thing that
grows its own slightly-wrong copy of an authorization check. Putting policy
somewhere a transport cannot reach makes divergence structurally impossible
rather than merely discouraged.

### Security contracts that must not be "simplified"

The dispatcher's policies encode prior review findings and are marked as
contracts:

- Governed actions **fail closed** without an approved, unconsumed approval.
- An approval is bound to an exact **action + resource + input fingerprint**
  and authorizes **exactly one** execution.
- Approval consumption and capability creation are **one transaction**.
- Recovery **never claims a side effect did not run**.

The last one deserves emphasis: after a crash the honest report is often
"unknown", and a recovery path that resolves ambiguity toward "didn't happen"
will eventually double-fire a real side effect.

### Identity is resolved daemon-side

> the actor is resolved DAEMON-SIDE from the environment
> (`SUTANDO_AGENT_ID` > `AGENT_MXID` > `AGENT_ID`), never from CLI-supplied
> params — a client cannot self-report who it is.

A client that can name itself can name someone else. Resolving identity at the
daemon removes the option rather than validating it.

### Why discovery must stay ephemeral

`capability_registry.py` is provider-neutral, receives read-only callables at
composition time, and **never receives a RequestStore** — "discovery/read
traffic must not become durable request state."

Motivation: durability is expensive and it is also a liability. A listing that
persists becomes a record to reconcile, expire, and leak. Only the things that
need to survive a crash — approvals, elicitations, executions — earn durability.

### The instance manifest: existence ≠ process

`instance_registry.py` states the distinction its whole design serves:

> **Agent existence ≠ agent process existence.**

One small, versioned, human-readable JSON per instance, which **never carries
tokens, keys, or memory**. The Server is its single writer: atomic write at
boot, `mark_stopped` on clean shutdown.

The deliberate part is the crash behaviour: a crash **leaves `status: running`
behind on purpose**, because *manifest-says-running + dead socket* is precisely
the `stale_or_crashed` signal. A manifest that tidied itself up on crash would
destroy the only evidence that a crash occurred.

And discovery reads must work **with no daemon running** — otherwise "is
anything installed here?" would be unanswerable exactly when you most need to
ask it.

---

## Part 2 — The Agent ID card

### Motivation: an agent has to be able to say who it is — and be doubted

Once agents talk to other agents, every interesting question is an identity
question. Who owns this thing? Who is allowed to address it? Is it even alive?
Is what it says about itself true?

The ID card is the answer to the first three and, crucially, is designed around
the fourth being **no**.

### What the card carries

Four fields, from `identity_view.py`, each sourced from existing workspace
records — "**nothing is invented here**":

| Field | Source | Meaning |
|---|---|---|
| `info` | daemon-resolved actor id + self descriptors | Who this agent is |
| `status` | `state/core-status.json` + own heartbeat age | What it is doing, how recently |
| `owner` | explicit ownership metadata per channel `access.json` | Who it answers to |
| `allowlist` | `channels/<source>/access.json` allowFrom, verbatim | Who may address it |

### Two design rules that carry the whole thing

**1. Absent fields stay absent — and an allowlist entry is NOT assumed to be
the owner.**

This is the card's most important line. Ownership and permission-to-speak are
different relations, and the cheap move — treating "is allowed to message it"
as "owns it" — manufactures authority out of access. The card refuses to
infer. A missing owner is reported missing, not guessed.

**2. Self-reported metadata is passed through; mtime is the only trust
signal.**

From `agents_view.py`: liveness is the heartbeat file's mtime, and the payload
is "passed through as self-reported metadata — mtime stays the only trust
signal."

So the card is explicitly **two-tier**: claims the agent makes about itself,
and one fact the filesystem asserts on its behalf. A graceful shutdown unlinks
the heartbeat, so absence means offline; a crash leaves it to age out.

### The failure this prevents

Without the separation, an agent's self-description and its provable state
merge into one blob that consumers treat uniformly — and every consumer
independently decides how much to trust it, usually by not thinking about it.
Naming the trust boundary in the data model means a reader cannot accidentally
promote a claim into a fact.

### Known sharp edge

Liveness proves the *beat*, not the *drivability*. The heartbeat is written by
a sidecar bound to the process, so a session wedged at its input layer
heartbeats perfectly while accepting no work. Anything gating on "is this agent
usable?" needs a work-derived signal — assigned-but-unclaimed age is the one
the pool uses — not the card's `status` field alone.

---

## Open questions

1. **Naming.** Confirm or correct the ID-card → `identity_view` mapping above.
2. **Card as a wire format?** Today these are RPC methods on a local socket.
   If the card is meant to be presentable *between* agents, it needs a
   serialization, a freshness rule, and an answer to "who vouches for it" —
   none of which exist yet.
3. **Owner migration.** The `owner` field is a single scalar identity. An owner
   who changes accounts has no supported rebind at the card level; see the
   registry-side discussion for the same problem one layer down.
