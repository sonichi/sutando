import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { otelMap } from '../../../src/observability/claude/otel-map.js';
import type { MapContext } from '../../../src/observability/claude/cc-records.js';

const ctx: MapContext = { node: 'mac-studio', receivedAt: 1_717_900_000 };
const RES = { 'session.id': 'sess-9', 'user.id': 'u-42', 'organization.id': 'org-7' };
const ACTOR = { user_id: 'u-42', channel: 'claude-code', access_tier: 'owner', tenant_id: 'org-7' };

describe('otelMap — metrics', () => {
	it('token.usage → one claude.tokens usage record, slotted by type, keyed by request', () => {
		const out = otelMap(
			{ signal: 'metric', metric: { name: 'claude_code.token.usage', value: 1500, ts: 1_717_900_001, attrs: { type: 'input', model: 'claude-opus-4-8', query_source: 'main', request_id: 'req_01H' }, resource: RES } },
			ctx,
		);
		assert.deepEqual(out, {
			events: [],
			usage: [
				{
					schema: 1,
					usage_id: 'cc:tok:req_01H:input',
					ts: 1_717_900_001,
					tenant_id: 'org-7',
					trace_id: 'cc-sess:sess-9',
					actor: ACTOR,
					source: 'core-cli',
					meter: 'claude.tokens',
					quantity: 1500,
					unit: 'tokens',
					provider: 'anthropic',
					provider_ref: 'req_01H',
					attrs: { _cc_source: 'otel-metric', model: 'claude-opus-4-8', query_source: 'main', type: 'input', input_tokens: 1500 },
				},
			],
		});
	});

	it('cost.usage → claude.cost usd record', () => {
		const out = otelMap({ signal: 'metric', metric: { name: 'claude_code.cost.usage', value: 0.0061, ts: 1_717_900_001, attrs: { model: 'claude-opus-4-8', request_id: 'req_01H' }, resource: RES } }, ctx);
		assert.equal(out.usage.length, 1);
		assert.equal(out.usage[0].meter, 'claude.cost');
		assert.equal(out.usage[0].unit, 'usd');
		assert.equal(out.usage[0].quantity, 0.0061);
		assert.equal(out.usage[0].attrs.cost_usd, 0.0061);
		assert.equal(out.events.length, 0);
	});

	it('counters → obs events, never usage', () => {
		const out = otelMap({ signal: 'metric', metric: { name: 'claude_code.lines_of_code.count', value: 42, ts: 1_717_900_001, attrs: { type: 'added' }, resource: RES } }, ctx);
		assert.equal(out.usage.length, 0);
		assert.equal(out.events.length, 1);
		assert.equal(out.events[0].kind, 'cc.code.lines');
		assert.deepEqual(out.events[0].data, { value: 42, type: 'added' });
	});
});

describe('otelMap — logs (obs only)', () => {
	it('user_prompt → cc.prompt', () => {
		const out = otelMap({ signal: 'log', log: { event: 'claude_code.user_prompt', ts: 1_717_900_001, attrs: { 'prompt.id': 'p1', prompt_length: 12 }, resource: RES } }, ctx);
		assert.equal(out.usage.length, 0);
		assert.equal(out.events[0].kind, 'cc.prompt');
		assert.deepEqual(out.events[0].data, { prompt_id: 'p1', prompt_length: 12 });
	});

	it('tool_result with reject → tool.result, outcome denied', () => {
		const out = otelMap({ signal: 'log', log: { event: 'claude_code.tool_result', ts: 1_717_900_001, attrs: { tool_name: 'Bash', tool_decision: 'reject', decision_source: 'user_approval', tool_use_id: 'tu1' }, resource: RES } }, ctx);
		assert.equal(out.events[0].kind, 'tool.result');
		assert.equal(out.events[0].outcome, 'denied');
		assert.equal((out.events[0].data as Record<string, unknown>).tool_name, 'Bash');
	});
});

describe('otelMap — spans', () => {
	it('llm_request → usage record (full breakdown) AND a cc.llm_request obs event', () => {
		const out = otelMap(
			{
				signal: 'span',
				span: {
					name: 'claude_code.llm_request',
					spanId: 'abc',
					parentSpanId: 'par',
					ts: 1_717_900_001.5,
					durationMs: 1200,
					attrs: { request_id: 'req_01H', model: 'claude-opus-4-8', input_tokens: 1500, output_tokens: 340, cache_read_tokens: 100, cache_creation_tokens: 0, ttft_ms: 230, stop_reason: 'end_turn', success: true, query_source: 'main' },
					resource: RES,
				},
			},
			ctx,
		);
		assert.deepEqual(out, {
			events: [
				{
					schema: 1,
					ts: 1_717_900_001.5,
					trace_id: 'cc-sess:sess-9',
					node: 'mac-studio',
					source: 'core-cli',
					actor: ACTOR,
					kind: 'cc.llm_request',
					outcome: 'ok',
					span_id: 'sp_abc',
					parent_span_id: 'sp_par',
					duration_ms: 1200,
					usage: { provider: 'anthropic', model: 'claude-opus-4-8', input_tokens: 1500, output_tokens: 340, cache_read: 100, cache_creation: 0 },
					data: { request_id: 'req_01H', ttft_ms: 230, stop_reason: 'end_turn', query_source: 'main' },
				},
			],
			usage: [
				{
					schema: 1,
					usage_id: 'cc:tok:req_01H',
					ts: 1_717_900_001.5,
					tenant_id: 'org-7',
					trace_id: 'cc-sess:sess-9',
					actor: ACTOR,
					source: 'core-cli',
					meter: 'claude.tokens',
					quantity: 1840,
					unit: 'tokens',
					provider: 'anthropic',
					provider_ref: 'req_01H',
					attrs: { _cc_source: 'otel-span', model: 'claude-opus-4-8', input_tokens: 1500, output_tokens: 340, cache_read: 100, cache_creation: 0 },
				},
			],
		});
	});

	it('tool span → tool.call, lifting file_path into source_file', () => {
		const out = otelMap({ signal: 'span', span: { name: 'claude_code.tool', spanId: 's1', ts: 1_717_900_001, attrs: { tool_name: 'Read', tool_use_id: 'tu9', file_path: 'tasks/task-1.txt' }, resource: RES } }, ctx);
		assert.equal(out.events[0].kind, 'tool.call');
		assert.equal(out.events[0].source_file, 'tasks/task-1.txt');
	});
});
