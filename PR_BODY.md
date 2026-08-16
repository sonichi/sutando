## What this adds

A durable delivery claim keyed `(outbox_instance, item_id)`, a three-state
delivery outcome, and the transport seam between them. Based on `a0518ea2`.

**Scoped deliberately narrow.** #2959 already landed the retry ceiling and a
proactive claim protocol, and this does not duplicate either. An earlier local
draft of mine included a `proactive_disposition` module that decided
retry-vs-park; `send_failure_policy.py` now owns that, so I dropped it rather
than ship a second implementation of one policy.

**It changes no production behaviour.** Nothing imports these modules yet.
`remote_gateway_bridge.py` is untouched. This is the substrate; wiring it is a
separate change to the live delivery path.

## What it is for

Two properties that `proactive_recovery.py` does not currently provide.

### 1. A claim that survives PID reuse

`_holder_is_live` gets the hard part right — `PermissionError` maps to `True`, so
an EPERM/root-owned holder reads *alive* rather than dead. That is the failure
mode that matters most, and it is handled.

It keys on **pid alone**, though, so a recycled pid reads as the original holder.
Measured on Darwin: four processes spawned in 5 ms share one `ps lstart` second
but have four distinct `(start_sec, start_usec)` pairs. `process_identity()` here
carries that microsecond birth token via `proc_pidinfo`, so identity is
pid *plus* birth time.

The same call gives three states rather than two:

| case | ret | errno | means |
|---|---|---|---|
| own process | 136 | 0 | **ALIVE**, with identity |
| pid 1 / root-owned live pid | 0 | `EPERM` | **UNKNOWN** — alive, not inspectable |
| absent pid | 0 | `ESRCH` | **DEAD** |

A probe that returns a bool must fold `UNKNOWN` into one of the others, and on
this machine every root-owned live process answers `EPERM`.

### 2. An outcome model the core can reason about without seeing transport

`CONFIRMED` / `NOT_DELIVERED` / `OUTCOME_UNKNOWN`. The core never reads a status
code or a response body — it gets a `DeliveryReceipt`. Two contracts enforce that
structurally: one asserts `outbox.py` contains no transport token at all, and one
asserts the adapter exposes no retry/attempt/backoff surface, because an adapter
that retries privately is invisible to any attempt budget.

## Verification

```
tests/sparrow-outbox-claim-protocol.test.py    6 pass, 0 FAIL, 0 ERROR
tests/outbox-adapter-contract.test.py          6 pass, 0 FAIL, 0 ERROR
tests/outbox-race.test.py    12 rounds x 24 concurrent processes -> 1 winner per round
```

Green suites were not taken as proof. Mutations were applied to each
implementation and checked against their own contracts — drop `O_EXCL`, map
`EPERM` to `DEAD`, skip the liveness check before a TTL steal, map a 2xx-with-no-id
to `CONFIRMED`, give the adapter a retry method. Each was caught.

**Two of those mutations caught defects in the tests instead**, which is the part
worth reading:

- the requeue contract could not fail: it never burned the attempt budget before
  checking that requeue reset it, so deleting the reset changed no outcome. It now
  records three attempts and asserts the setup took.
- the suites originally had three states, and a mutation that made an
  implementation *raise* killed the run at that contract while every later one
  silently never executed — the output read short rather than broken. They now
  report `ERROR` as a fourth state and continue.

The race check is a real multi-process race against the production writer, not a
sequential loop where `O_EXCL` is trivially satisfied. Against an
`O_EXCL`-dropped mutant it reports 24 winners per round and exits 1.

## Notes for review

- `process_identity` is Darwin-only today and returns `UNKNOWN` where it cannot
  answer, which is the safe direction. Linux and Windows are unimplemented on
  purpose rather than guessed at; the platform detail stays behind that one
  function so the core never names `proc_pidinfo`.
- `resolve_outcome(OUTCOME_UNKNOWN, UNSAFE)` parks rather than retries. That is a
  deliberate difference from `send_failure_policy`'s transient-with-cap for the
  same case: parking costs zero duplicates and needs a requeue to recover, the cap
  recovers automatically and costs up to N duplicates. Both are defensible; this
  module is not wired to anything, so nothing depends on the choice yet, and it is
  worth settling before it is.
