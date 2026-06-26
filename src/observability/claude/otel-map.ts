/**
 * `otelMap(rec, ctx)` — map one Claude Code OpenTelemetry record (metric, log,
 * or span) to spine primitives. Pure: all wall-clock/host facts come from the
 * record or `ctx`, never from `Date.now()`. See the routing table in
 * docs/migration. Twin-less (mappers are TS-only; the executor sources are TS).
 */

import type { ObsEvent, Outcome, UsageAdvisory } from '../events.js';
import type { UsageRecord } from '../usage.js';
import type { MapContext, MapResult, OtelLogRecord, OtelMetricRecord, OtelSpanRecord, OtelRecord } from './cc-records.js';
import {
	actorFromResourceAttrs,
	spanIdFromCcSpan,
	tenantFromResource,
	traceIdFromSession,
	usageIdFromRequest,
} from './ids.js';
import { CORE_SOURCE, baseEvent, clean, num, str, tsBucket, tsOf } from './_map-util.js';

const TOKEN_SLOT: Record<string, string> = {
	input: 'input_tokens',
	output: 'output_tokens',
	cacheRead: 'cache_read',
	cacheCreation: 'cache_creation',
};

const COUNTER_KIND: Record<string, string> = {
	'claude_code.lines_of_code.count': 'cc.code.lines',
	'claude_code.commit.count': 'cc.git.commit',
	'claude_code.pull_request.count': 'cc.git.pr',
	'claude_code.session.count': 'cc.session.count',
	'claude_code.code_edit_tool.decision': 'cc.tool.decision',
};

function mapMetric(m: OtelMetricRecord, ctx: MapContext): MapResult {
	// CC active-session time is intentionally NOT metered (owner: we don't time CC) — drop it.
	if (m.name === 'claude_code.active_time.total') return { events: [], usage: [] };
	const res = m.resource;
	const ts = tsOf(m.ts, ctx);
	const sessionId = res?.['session.id'];
	const a = m.attrs ?? {};
	const requestId = typeof a.request_id === 'string' ? a.request_id : undefined;

	if (m.name === 'claude_code.token.usage') {
		const type = String(a.type ?? '');
		const slot = TOKEN_SLOT[type];
		const attrs: Record<string, unknown> = clean({
			_cc_source: 'otel-metric',
			model: a.model,
			query_source: a.query_source,
			type,
		});
		if (slot) attrs[slot] = m.value;
		const usage: UsageRecord = {
			schema: 1,
			usage_id: usageIdFromRequest(requestId, { sessionId, tsBucket: tsBucket(ts) }, type),
			ts,
			tenant_id: tenantFromResource(res),
			trace_id: traceIdFromSession(sessionId),
			actor: actorFromResourceAttrs(res),
			source: CORE_SOURCE,
			meter: 'claude.tokens',
			quantity: m.value,
			unit: 'tokens',
			provider: 'anthropic',
			provider_ref: requestId ?? null,
			attrs,
		};
		return { events: [], usage: [usage] };
	}

	if (m.name === 'claude_code.cost.usage') {
		const usage: UsageRecord = {
			schema: 1,
			usage_id: usageIdFromRequest(requestId, { sessionId, tsBucket: tsBucket(ts) }, 'cost'),
			ts,
			tenant_id: tenantFromResource(res),
			trace_id: traceIdFromSession(sessionId),
			actor: actorFromResourceAttrs(res),
			source: CORE_SOURCE,
			meter: 'claude.cost',
			quantity: m.value,
			unit: 'usd',
			provider: 'anthropic',
			provider_ref: requestId ?? null,
			attrs: clean({ _cc_source: 'otel-metric', model: str(a.model), cost_usd: m.value }),
		};
		return { events: [], usage: [usage] };
	}

	// counters → obs
	const kind = COUNTER_KIND[m.name] ?? 'cc.metric.' + m.name.replace(/^claude_code\./, '');
	const ev = baseEvent({ res, kind, outcome: 'ok', ctx, ts });
	ev.data = clean({ value: m.value, ...a });
	return { events: [ev], usage: [] };
}

function mapLog(l: OtelLogRecord, ctx: MapContext): MapResult {
	const res = l.resource;
	const ts = tsOf(l.ts, ctx);
	const a = l.attrs ?? {};
	const promptId = a['prompt.id'] ?? a.prompt_id;

	const make = (kind: string, outcome: Outcome, data: Record<string, unknown>): ObsEvent => {
		const ev = baseEvent({ res, kind, outcome, ctx, ts });
		ev.data = clean({ prompt_id: promptId, ...data });
		return ev;
	};

	switch (l.event) {
		case 'claude_code.user_prompt':
			return { events: [make('cc.prompt', 'ok', { prompt_length: a.prompt_length, prompt: a.prompt })], usage: [] };
		case 'claude_code.tool_result': {
			const outcome: Outcome = a.tool_decision === 'reject' ? 'denied' : a.error ? 'error' : 'ok';
			return {
				events: [
					make('tool.result', outcome, {
						tool_name: a.tool_name,
						tool_decision: a.tool_decision,
						decision_source: a.decision_source,
						tool_use_id: a.tool_use_id,
					}),
				],
				usage: [],
			};
		}
		case 'claude_code.tool_decision':
			return {
				events: [make('cc.tool.decision', a.decision === 'reject' ? 'denied' : 'ok', { tool_name: a.tool_name, decision: a.decision })],
				usage: [],
			};
		case 'claude_code.api_request':
			return { events: [make('cc.api.request', 'ok', { body: a.body })], usage: [] };
		case 'claude_code.api_response_body':
			return {
				events: [make('cc.api.response', typeof a.status_code === 'number' && a.status_code >= 400 ? 'error' : 'ok', { status_code: a.status_code })],
				usage: [],
			};
		case 'mcp_server_connection':
			return { events: [make('cc.mcp.connection', 'ok', { mcp_server: a['mcp_server.name'] ?? a.mcp_server })], usage: [] };
		case 'permission_mode_changed':
			return { events: [make('cc.permission.mode', 'ok', { new_mode: a.new_mode })], usage: [] };
		default:
			return { events: [make('cc.log.' + l.event.replace(/^claude_code\./, ''), 'ok', a)], usage: [] };
	}
}

function mapSpan(s: OtelSpanRecord, ctx: MapContext): MapResult {
	const res = s.resource;
	const ts = tsOf(s.ts, ctx);
	const a = s.attrs ?? {};
	const spanId = spanIdFromCcSpan(s.spanId);
	const parent = spanIdFromCcSpan(s.parentSpanId);
	const withSpan = (e: ObsEvent): ObsEvent => {
		if (spanId) e.span_id = spanId;
		if (parent) e.parent_span_id = parent;
		if (typeof s.durationMs === 'number') e.duration_ms = s.durationMs;
		return e;
	};

	switch (s.name) {
		case 'claude_code.interaction': {
			const e = withSpan(baseEvent({ res, kind: 'cc.interaction', outcome: 'ok', ctx, ts }));
			e.data = clean({ interaction_sequence: a['interaction.sequence'], user_prompt_length: a.user_prompt_length });
			return { events: [e], usage: [] };
		}
		case 'claude_code.llm_request': {
			const requestId = typeof a.request_id === 'string' ? a.request_id : undefined;
			const sessionId = res?.['session.id'];
			const input = num(a.input_tokens);
			const output = num(a.output_tokens);
			const advisory: UsageAdvisory = clean({
				provider: 'anthropic',
				model: typeof a.model === 'string' ? a.model : undefined,
				input_tokens: input,
				output_tokens: output,
				cache_read: num(a.cache_read_tokens),
				cache_creation: num(a.cache_creation_tokens),
			});
			const usage: UsageRecord = {
				schema: 1,
				usage_id: usageIdFromRequest(requestId, { sessionId, tsBucket: tsBucket(ts) }),
				ts,
				tenant_id: tenantFromResource(res),
				trace_id: traceIdFromSession(sessionId),
				actor: actorFromResourceAttrs(res),
				source: CORE_SOURCE,
				meter: 'claude.tokens',
				quantity: (input ?? 0) + (output ?? 0),
				unit: 'tokens',
				provider: 'anthropic',
				provider_ref: requestId ?? null,
				attrs: clean({
					_cc_source: 'otel-span',
					model: str(a.model),
					input_tokens: input,
					output_tokens: output,
					cache_read: num(a.cache_read_tokens),
					cache_creation: num(a.cache_creation_tokens),
				}),
			};
			const e = withSpan(baseEvent({ res, kind: 'cc.llm_request', outcome: a.success === false ? 'error' : 'ok', ctx, ts }));
			e.usage = advisory;
			e.data = clean({
				request_id: requestId,
				ttft_ms: a.ttft_ms,
				stop_reason: a.stop_reason,
				status_code: a.status_code,
				query_source: a.query_source,
			});
			return { events: [e], usage: [usage] };
		}
		case 'claude_code.tool': {
			const e = withSpan(baseEvent({ res, kind: 'tool.call', outcome: 'ok', ctx, ts }));
			e.data = clean({ tool_name: a.tool_name, tool_use_id: a.tool_use_id, result_tokens: a.result_tokens });
			if (typeof a.file_path === 'string') e.source_file = a.file_path;
			return { events: [e], usage: [] };
		}
		case 'claude_code.tool.execution': {
			const e = withSpan(baseEvent({ res, kind: 'cc.tool.execution', outcome: a.success === false ? 'error' : 'ok', ctx, ts }));
			e.data = clean({ error: a.error });
			return { events: [e], usage: [] };
		}
		case 'claude_code.tool.blocked_on_user': {
			const e = withSpan(baseEvent({ res, kind: 'cc.tool.blocked', outcome: 'ok', ctx, ts }));
			e.data = clean({ decision: a.decision, source: a.source });
			return { events: [e], usage: [] };
		}
		default: {
			const e = withSpan(baseEvent({ res, kind: 'cc.span.' + s.name.replace(/^claude_code\./, ''), outcome: 'ok', ctx, ts }));
			e.data = clean(a);
			return { events: [e], usage: [] };
		}
	}
}

export function otelMap(rec: OtelRecord, ctx: MapContext): MapResult {
	switch (rec.signal) {
		case 'metric':
			return mapMetric(rec.metric, ctx);
		case 'log':
			return mapLog(rec.log, ctx);
		case 'span':
			return mapSpan(rec.span, ctx);
	}
}
