# ag2-sparrow v1 — the frozen delivery contract

One page, one rule: **v1 is a narrow, complete chain** — durable ingress →
eligibility → mailbox → claim/recovery → delivery provider →
receipt/reconciliation → terminal disposition. Everything here is grounded in
shipped code; each link names its owner module and its v1 status. Consumers
(Browser, Channel, Automation) adopt the chain as-is; anything not in this page
is not in v1.

## The chain

| # | link | contract | owner (shipped) | v1 status |
|---|------|----------|-----------------|-----------|
| 1 | Durable ingress | An inbound intent becomes a durable item BEFORE any processing; the write is atomic (tmp + rename), and the item id is assigned here and never changes. | task files under `<workspace>/tasks/` (bridges); `task_priority.py` orders consumption | **GAP**: no `TaskSink` interface — each bridge writes its own file shape. v1 freezes the header schema (id / source / channel_id / access_tier / priority) as the de-facto contract; a `TaskSink` type is the first post-freeze refactor. |
| 2 | Eligibility | Who may produce and consume an item (tier dispatch, collaborator attestation, allowlists) is decided at ingress and recorded ON the item, never re-derived downstream. | bridge tier injection + `docs/access-control.md`; `send_allowlist.py` for attachment paths | frozen as-is |
| 3 | Mailbox | Published items live in a single-writer namespace with one live slot per item id (structural, `EEXIST`), publish refused while the id is live anywhere. | `outbox.py` (Design A shipped; Design C accepted on the outbox substrate — striped `_item_lock`, bare `ready/<key>`, `_move`, constructor-time `init()`) | frozen: Design A is v1's shipped backend; Design C lands behind the same `ClaimBackend` Protocol when its pair report closes |
| 4 | Claim / recovery | Claim is one atomic transfer producing a claim token; recovery re-arms only DEAD owners (ALIVE/UNKNOWN never touched); token identity = key + worker + pid + incarnation (+ generation, Design C). | `outbox.py` acquire/release/reclaim; `ag2_sparrow.delivery_core.backend_a.DesignAClaimBackend` | frozen |
| 5 | Delivery provider | `deliver(item_id, payload, idempotency_key) -> DeliveryReceipt`; SINGLE attempt; **no private retries** (invisible retries corrupt the core's attempt budget); provider declares capabilities. | `delivery_core.contract.DeliveryProvider`; first production impl `src/channels/discord/delivery_provider.py` (lands with open #3095 — not on `main` yet) | frozen |
| 6 | Receipt / reconciliation | Three-state receipts only: CONFIRMED (provider-issued id), NOT_DELIVERED (definitive refusal), OUTCOME_UNKNOWN (anything else — timeout, 5xx, 2xx-without-id). UNKNOWN parks unless the provider declared `reconcile_capable` or `idempotent_send`. Endpoint-specific exception: a provider MAY classify a bare 2xx as CONFIRMED only where that endpoint's contract defines the 2xx itself as the receipt (prod-verified server-side rid-dedup; the AG2 Space gateway result endpoint, open #3110) — declared in the provider, never in the core. | `outbox_adapter.classify_response`; `delivery_core.core.DeliveryCore.deliver_one` | frozen |
| 7 | Terminal disposition | Every item ends in exactly one of: archived (confirmed), parked (needs operator), quarantined (collision/duplicate precursor). No silent drops; parking is the safe default. | `outbox.py` terminal moves; `send_failure_policy.py` (transient-with-cap vs permanent) | frozen |

## Identity rules (owner-ratified, gating any impl PR)

1. **Stable delivery identity.** `item_id`, `claim_token`, and
   `delivery_idempotency_key` are three different things. The idempotency key
   is `f"{item_id}#{resend_epoch}"` — NEVER claim material; claim, restart,
   and migration must not change it. Effectively-once holds only "within the
   provider's idempotency and receipt-retention contract".
2. **Migration fencing.** One claim protocol per namespace per epoch. Moving a
   claim protocol (e.g. the cross-bridge `proactive-*.txt` `.sending` files)
   requires a migration lock + one-shot conversion + version fence; old
   delivered-sentinels map to OUTCOME_UNKNOWN, never CONFIRMED.
3. **Local/server separation.** The core sees `deliver` / `reconcile` /
   capability declarations; it never sees a lease. Server-side finalization is
   an adapter concern behind the same interface.

## Provider capability declarations

```python
@dataclass(frozen=True)
class ProviderCapabilities:
    reconcile_capable: bool = False  # receipt queryable after UNKNOWN
    idempotent_send: bool = False    # key-deduped; safe-resend on UNKNOWN
```

(Shipped shape — `delivery_core/contract.py`. Both fields default to `False`:
a provider that declares nothing gets the conservative park-on-UNKNOWN path.)

**v1 fix (from #3095 review, rui):** the Protocol must declare `capabilities`
as an attribute/property, not a method — a literal implementer handing the
core a bound method makes every capability check truthy-pass. The core reads
it as an attribute (`core.py`); the Protocol text follows the core.

## Adopted policies (from the 2026-08 delivery-system survey)

- **Per-part receipts**: a multi-part send (chunks, file batches) earns a
  receipt per part; the first non-CONFIRMED part stops the send and reports
  progress (`sent_chunks`) so the caller's budget sees partial delivery.
- **ACK policy**: in `DeliveryCore`, only a confirmed NOT_DELIVERED
  auto-retries; UNKNOWN parks unless the provider's capabilities license
  another step — `reconcile_capable` (resolve the receipt, act on it) or
  `idempotent_send` (one safe re-send). Ambiguous is never auto-relabeled
  NOT_DELIVERED. The capped transient class
  (`send_failure_policy.UnconfirmedDelivery`) is the bridge-migration
  adapters' bounded analog on legs not yet driven by the core (e.g. #3098's
  proactive text leg) — budget-visible, then parked loudly. Unbounded retry
  of an UNKNOWN is the duplicate-send machine (2026-08-16, one message
  delivered 12×).
- **Durability policy**: intent is durable before side effects; side-effect
  markers (`.sending` claims) are evidence, not proof — written post-call,
  crash window acknowledged.

## Known v1 gaps (named so they expire visibly)

- `TaskSink` ingress interface (link 1) — de-facto file schema frozen instead.
- `RouteResolution` — routing control still travels in result-body markers
  (`result_markers.py`) rather than structured fields; the structured-route
  design is recorded at ag2-space/ag2space-backend#678.
- Migration fence implementation for the cross-bridge proactive namespace
  (rule 2 above) — designed, not yet built; #3098 deliberately excludes it.
