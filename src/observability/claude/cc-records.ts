/**
 * Input types for the Claude Code telemetry mappers — the shapes the three
 * sources (OTel, hooks, .jsonl transcript) deliver. These are the claude source's
 * model of CC's default emissions; the mappers turn them into the spine's
 * `ObsEvent` / `UsageRecord`. Pure data; no behavior.
 *
 * A live source enriches each record with the host `node` and a `receivedAt`
 * (for hook/.jsonl records that carry no reliable timestamp) before mapping —
 * passed via `MapContext` so the mappers stay referentially transparent
 * (no `Date.now()`/random inside a mapper, so output is byte-exact testable).
 */

import type { ObsEvent } from '../events.js';
import type { UsageRecord } from '../usage.js';

/** OTel resource attributes ride on every OTLP record. All optional. */
export interface ResourceAttrs {
	'session.id'?: string;
	'user.id'?: string;
	'user.email'?: string;
	'user.account_uuid'?: string;
	'organization.id'?: string;
	'app.version'?: string;
	'terminal.type'?: string;
	'app.entrypoint'?: string;
	[k: string]: unknown;
}

export interface OtelMetricRecord {
	name: string; // "claude_code.token.usage" | "claude_code.cost.usage" | ...
	value: number;
	ts?: number; // unix seconds
	attrs?: Record<string, unknown>; // datapoint attrs (type, model, query_source, ...)
	resource?: ResourceAttrs;
}

export interface OtelLogRecord {
	event: string; // "claude_code.user_prompt" | "claude_code.tool_result" | ...
	ts?: number;
	attrs?: Record<string, unknown>;
	resource?: ResourceAttrs;
}

export interface OtelSpanRecord {
	name: string; // "claude_code.llm_request" | "claude_code.tool" | "claude_code.interaction"
	spanId?: string;
	parentSpanId?: string;
	ts?: number;
	durationMs?: number;
	attrs?: Record<string, unknown>;
	resource?: ResourceAttrs;
}

export type OtelRecord =
	| { signal: 'metric'; metric: OtelMetricRecord }
	| { signal: 'log'; log: OtelLogRecord }
	| { signal: 'span'; span: OtelSpanRecord };

// Hook payloads now have a strict, discriminated home: `cc-hooks.ts`
// (ClaudeCodeHook + decodeClaudeCodeHook). The old loose `HookPayload` flat
// interface was removed in favor of that per-event union.

/** One parsed line of the `~/.claude/.../<session>.jsonl` transcript. */
export interface TranscriptLine {
	type: string; // "assistant" | "tool_use" | "tool_result" | "session_start" | ...
	ts?: number;
	usage?: {
		input_tokens?: number;
		output_tokens?: number;
		cache_read_tokens?: number;
		cache_creation_tokens?: number;
	};
	model?: string;
	request_id?: string;
	session_id?: string;
	tool_name?: string;
	tool_input?: Record<string, unknown>;
	tool_use_id?: string;
	tool_output?: unknown;
	error?: string | null;
	source?: string;
	[k: string]: unknown;
}

/** Host + timing context a live source injects so mappers stay pure. */
export interface MapContext {
	node: string;
	receivedAt: number; // unix SECONDS; used when the record carries no ts
}

/** Every mapper returns this. */
export interface MapResult {
	events: ObsEvent[];
	usage: UsageRecord[];
}
