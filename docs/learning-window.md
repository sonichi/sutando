# Retained learning windows

The optional `learning-window` skill persists all configured gateway scopes before admitting a detached consumer. Configure explicit capability argument vectors, an immutable bootstrap time, and a consumer command in its manifest. It is disabled by default. See [the skill contract](../skills/learning-window/SKILL.md) for configuration and return fields.

The room-history adapter re-enumerates joined memberships and uses real server cursors with bounded pages. Each immutable digest receipt includes the exact scope/window, per-room errors and available-history coverage. Collection progress advances only after receipt persistence; incomplete scopes prevent consumer admission. Collection progress is separate from learned-fact progress. Smaller100-message pages reduce read latency; exhausting the page budget remains incomplete.

Each attempt supplies all retained receipts and a fresh proposal output path. Read-only preflight and the final pending writer use the same literal-reference validator. Accepted candidates remain pending, retain contributing receipt digests, and deduplicate only identical scoped evidence/text. The optional readback adapter delegates the existing generic cloud GET/auth owner, captures full hashes/identities around attempts, and reports unchanged, changed without attribution, or unknown within300seconds. These observations do not establish semantic truth, authority or learned facts.

Detached consumers set the existing explicit non-core role and do not inherit a worker identity. The Stop hook exempts only an explicitly non-core, non-worker session from inbox ownership; core, worker and unidentified gates remain unchanged. This is role ownership, not an authorization grant.

Regression suites exercise the production receipt/pending writers under concurrency and failure, exact timestamp transport, rejected/missing/stale output, same-attempt quote repair, partial admission, full-content readbacks and retained population reporting. Tests use isolated capabilities and workspaces; no external messages or cloud writes. The read-only adapter capabilities are injected rather than rediscovered inside the dispatcher.

See [the concluded local evaluation](reliability-v3-results.md) for owner testimony and measured components. The evaluated implementation included separate cron/GitHub changes, now reviewed independently. The evidence is historical and does not certify this PR's new integrated head.
