import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { otelMap } from '../../../src/observability/claude/otel-map.js';
import { hookMap } from '../../../src/observability/claude/hook-map.js';
import type { MapContext, MapResult } from '../../../src/observability/claude/cc-records.js';

const ctx: MapContext = { node: 'n', receivedAt: 1 };
const RES = { 'session.id': 's' };

/**
 * POLICY guard: every CC signal lands in meter XOR obs — except `llm_request`,
 * which is deliberately BOTH (durable usage + operational span). Guards the
 * routing table from drift.
 */
const cases: Array<{ name: string; out: MapResult; both?: boolean }> = [
	{ name: 'token.usage', out: otelMap({ signal: 'metric', metric: { name: 'claude_code.token.usage', value: 5, attrs: { type: 'input', request_id: 'r' }, resource: RES } }, ctx) },
	{ name: 'cost.usage', out: otelMap({ signal: 'metric', metric: { name: 'claude_code.cost.usage', value: 0.1, attrs: { request_id: 'r' }, resource: RES } }, ctx) },
	{ name: 'lines_of_code', out: otelMap({ signal: 'metric', metric: { name: 'claude_code.lines_of_code.count', value: 1, resource: RES } }, ctx) },
	{ name: 'user_prompt', out: otelMap({ signal: 'log', log: { event: 'claude_code.user_prompt', attrs: {}, resource: RES } }, ctx) },
	{ name: 'tool_result(log)', out: otelMap({ signal: 'log', log: { event: 'claude_code.tool_result', attrs: { tool_name: 'Bash' }, resource: RES } }, ctx) },
	{ name: 'llm_request', both: true, out: otelMap({ signal: 'span', span: { name: 'claude_code.llm_request', spanId: 'x', attrs: { request_id: 'r', input_tokens: 1, output_tokens: 1 }, resource: RES } }, ctx) },
	{ name: 'tool(span)', out: otelMap({ signal: 'span', span: { name: 'claude_code.tool', spanId: 'x', attrs: { tool_name: 'Read' }, resource: RES } }, ctx) },
	{ name: 'hook PostToolUse', out: hookMap({ hook_event_name: 'PostToolUse', session_id: 's', tool_name: 'Bash' }, ctx) },
];

describe('routing policy — meter xor obs (llm_request = both)', () => {
	for (const c of cases) {
		it(`${c.name}`, () => {
			const hasUsage = c.out.usage.length > 0;
			const hasObs = c.out.events.length > 0;
			if (c.both) {
				assert.ok(hasUsage && hasObs, `${c.name} must produce BOTH`);
			} else {
				assert.ok(hasUsage !== hasObs, `${c.name} must produce meter XOR obs (got usage=${hasUsage} obs=${hasObs})`);
			}
		});
	}
});
