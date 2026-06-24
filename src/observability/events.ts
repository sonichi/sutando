/**
 * The universal observability event envelope. EVERY service, bridge, adapter,
 * watcher, and the core (via mappers) emits this same shape through `emit()`.
 *
 * Twin of types.py (which defines the same shape as a TypedDict). The envelope
 * is thin and stable; everything kind-specific lives in the open `data` bag and
 * the open dotted `kind` namespace, so new event shapes need no schema bump.
 */

export type Outcome = 'ok' | 'error' | 'denied';

export type AccessTier = 'owner' | 'team' | 'public';

/**
 * The emitting process/service — WHICH PROCESS produced the event (distinct from
 * `node` = which machine, and `actor.channel` = which ingress surface). Open
 * string; the union lists the known emitters for autocomplete without closing it.
 */
export type Source =
	| 'voice-agent'
	| 'core-cli'
	| 'phone'
	| 'chat'
	| 'discord-bridge'
	| 'slack-bridge'
	| 'telegram-bridge'
	| 'agent-api'
	| 'task-bridge'
	| 'filewatcher'
	| 'health-check'
	| 'core-heartbeat'
	// eslint-disable-next-line @typescript-eslint/ban-types
	| (string & {});

/** WHO the request is from/for — the ingress surface + identity. */
export interface Actor {
	user_id: string;
	channel: string;
	access_tier: AccessTier;
	tenant_id?: string | null;
}

/** Advisory usage mirror carried inline on an obs event. The BILLED copy is the
 *  meter ledger — never bill off this. */
export interface UsageAdvisory {
	provider?: string;
	model?: string;
	input_tokens?: number;
	output_tokens?: number;
	cache_read?: number;
	cache_creation?: number;
	audio_seconds?: number;
	cost_usd?: number;
}

export interface ObsEvent {
	schema: 1;
	ts: number; // float unix seconds, ms precision
	trace_id: string;
	span_id?: string;
	parent_span_id?: string;
	node: string; // which machine
	source: Source; // which emitting process/service
	source_file?: string; // the SUBJECT file the event is about (changed/read/consumed)
	actor: Actor;
	kind: string; // open dotted namespace: "tool.call" | "file.change" | "voice.session.end" | ...
	outcome: Outcome;
	duration_ms?: number;
	usage?: UsageAdvisory;
	data?: Record<string, unknown>; // open, kind-specific payload bag
}

/** Caller-supplied shape: the facade stamps schema/ts/node/trace_id. */
export type ObsEventInput = Omit<ObsEvent, 'schema' | 'ts' | 'node' | 'trace_id'> & {
	trace_id?: string;
	node?: string;
};
