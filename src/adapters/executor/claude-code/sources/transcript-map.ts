/**
 * `transcriptMap(line, ctx)` — map one `.jsonl` transcript line to spine
 * primitives. The transcript is Anthropic-INTERNAL and fragile, so it is treated
 * as ENRICHMENT/FALLBACK: usage records are stamped `_cc_source: "jsonl"` (the
 * lowest triangulation rank) and unrecognized lines are DROPPED (drift-safe),
 * never thrown on. Pure.
 */

import type { Outcome } from '../../../../kernel/obs/types.js';
import type { UsageRecord } from '../../../../kernel/meter/types.js';
import type { MapContext, MapResult, TranscriptLine } from './cc-records.js';
import { actorFromResourceAttrs, traceIdFromSession, usageIdFromRequest } from './ids.js';
import { CORE_SOURCE, baseEvent, clean, fileOpFor, num, pathFromToolInput, tsBucket, tsOf } from './_map-util.js';

export function transcriptMap(line: TranscriptLine, ctx: MapContext): MapResult {
	const ts = tsOf(line.ts, ctx);
	const sessionId = line.session_id;

	switch (line.type) {
		case 'assistant': {
			if (!line.usage) return { events: [], usage: [] };
			const u = line.usage;
			const requestId = line.request_id;
			const input = num(u.input_tokens);
			const output = num(u.output_tokens);
			const usage: UsageRecord = {
				schema: 1,
				usage_id: usageIdFromRequest(requestId, { sessionId, tsBucket: tsBucket(ts) }),
				ts,
				tenant_id: null,
				trace_id: traceIdFromSession(sessionId),
				actor: actorFromResourceAttrs(undefined),
				source: CORE_SOURCE,
				meter: 'claude.tokens',
				quantity: (input ?? 0) + (output ?? 0),
				unit: 'tokens',
				provider: 'anthropic',
				provider_ref: requestId ?? null,
				attrs: clean({
					_cc_source: 'jsonl',
					model: line.model,
					input_tokens: input,
					output_tokens: output,
					cache_read: num(u.cache_read_tokens),
					cache_creation: num(u.cache_creation_tokens),
				}),
			};
			return { events: [], usage: [usage] };
		}
		case 'tool_use': {
			const e = baseEvent({ sessionId, kind: 'tool.call', outcome: 'ok', ctx, ts });
			e.data = clean({ tool_name: line.tool_name, tool_input: line.tool_input, tool_use_id: line.tool_use_id });
			const events = [e];
			const fo = fileOpFor(line.tool_name);
			const path = pathFromToolInput(line.tool_input);
			if (fo && path) {
				const fe = baseEvent({ sessionId, kind: fo.kind, outcome: 'ok', ctx, ts });
				fe.source_file = path;
				fe.data = clean({ op: fo.op, tool_name: line.tool_name });
				events.push(fe);
			}
			return { events, usage: [] };
		}
		case 'tool_result': {
			const outcome: Outcome = line.error ? 'error' : 'ok';
			const e = baseEvent({ sessionId, kind: 'tool.result', outcome, ctx, ts });
			e.data = clean({ tool_use_id: line.tool_use_id, error: line.error ?? undefined });
			return { events: [e], usage: [] };
		}
		case 'session_start': {
			const e = baseEvent({ sessionId, kind: 'cc.transcript.session_start', outcome: 'ok', ctx, ts });
			e.data = clean({ model: line.model, source: line.source });
			return { events: [e], usage: [] };
		}
		default:
			return { events: [], usage: [] }; // unrecognized → drop (schema-drift safe)
	}
}
