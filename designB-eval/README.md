# Design B (rename-as-claim) evaluation materials

Prototype + harnesses for the Maildir-style alternative to src/outbox.py's claim protocol.
NOT production code; lives on this exp/ branch only (deliberately outside tests/ so CI does not
collect it). Handoff to the ClaimMachine operator per the same-operator fairness rule.

- `designB.py` — the prototype: ready/ -> inflight/<id>.<worker>.<pid>.<birth> -> archive|undelivered,
  every transition one atomic rename; reuses src/outbox.py's ALIVE/DEAD/UNKNOWN process oracle.
- `evalB_fault.py` — crash before AND after every rename, all 3 ops; invariants I1 (exactly-one-copy,
  deterministic recovery) + I2 (a successful claim() is never revoked by an earlier incarnation).
  Mutation-verified: copy-instead-of-rename mutant -> 2 FAIL; steal-from-ALIVE mutant -> I2 FAIL.
- `evalB_race.py` — 24-proc warm pool, 24 rounds + 300 amplified, exactly-one-winner invariant.
  Mutation-verified: copy-then-unlink TOCTOU mutant -> 20/24 and 296/300 bad rounds.

Measured on Darwin (Air host, 2026-08-17): fault 12/12, race 0/0, structure objects 3->1,
mutations 2/4/4 -> 1/1/1, crash windows 10->3. EXDEV boundary: all four dirs must share one
filesystem (verified satisfied on the live workspace: identical st_dev).

ClaimMachine integration point: gate `os.rename` (the entire mutation surface is three renames);
drive claim/complete/recover; the owner's asked metrics are boundary count, residual interleavings,
crash recovery, bounded on-disk state.
