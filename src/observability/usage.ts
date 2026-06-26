/**
 * The usage record — the durable, billable/attributable primitive. Distinct
 * from an obs event (§11.2: you cannot bill on a fire-and-forget log). Shares
 * the obs vocabulary (`Actor`, `Source`, `trace_id`, dotted namespaces) but has
 * its own delivery contract: append-only, at-least-once, idempotent on
 * `usage_id`. Twin of types.py.
 */

import type { Actor, Source } from './events.js';

/** Open payload bag. Field names align with Claude Code's gen_ai conventions so
 *  the CC mappers populate them mechanically. */
export interface UsageAttrs {
	model?: string;
	input_tokens?: number;
	output_tokens?: number;
	cache_read?: number;
	cache_creation?: number;
	cost_usd?: number; // ADVISORY estimate only — never the billed figure
	[k: string]: unknown;
}

export interface UsageRecord {
	schema: 1;
	usage_id: string; // idempotency key — "ux_..."
	ts: number;
	tenant_id: string | null; // paying account (managed); null in BYOK
	trace_id: string;
	actor: Actor;
	source: Source; // which process recorded the usage
	source_file?: string;
	meter: string; // open dotted: "claude.tokens" | "voice.seconds" | ...
	quantity: number;
	unit: string; // "tokens" | "seconds" | "minutes" | "characters" | "count" | "usd"
	provider: string; // "anthropic" | "gemini-live" | "twilio" | ...
	provider_ref: string | null; // request_id / Call SID — reconciliation hook
	attrs: UsageAttrs;
}

/** Caller-supplied shape: the facade stamps schema/usage_id/ts/trace_id and
 *  defaults tenant_id from config when absent. A caller MAY supply `usage_id`
 *  so a crash-retry re-append carries the same idempotency key. */
export type UsageRecordInput = Omit<
	UsageRecord,
	'schema' | 'usage_id' | 'ts' | 'trace_id' | 'tenant_id' | 'attrs'
> & {
	usage_id?: string;
	ts?: number;
	trace_id?: string;
	tenant_id?: string | null;
	attrs?: UsageAttrs;
};
