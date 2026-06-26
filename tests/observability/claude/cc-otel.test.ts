import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { decodeOtlpMetrics } from '../../../src/observability/claude/cc-otel.js';
import { ClaudeCodeOtelNormalizer } from '../../../src/observability/claude/otel-normalizer.js';
import type { NormalizeContext } from '../../../src/observability/collector/normalizer.js';

const ctx: NormalizeContext = { node: 'mac-studio', receivedAt: 1_717_900_000 };

// A realistic OTLP/HTTP-JSON metrics export body as Claude Code's OTel exporter
// posts to /v1/metrics: int64s arrive as strings, attrs are tagged-union arrays.
const OTLP = {
	resourceMetrics: [
		{
			resource: {
				attributes: [
					{ key: 'session.id', value: { stringValue: 'sess-9' } },
					{ key: 'user.id', value: { stringValue: 'u-42' } },
					{ key: 'organization.id', value: { stringValue: 'org-7' } },
				],
			},
			scopeMetrics: [
				{
					metrics: [
						{
							name: 'claude_code.token.usage',
							sum: {
								dataPoints: [
									{
										timeUnixNano: '1717900001000000000',
										asInt: '1500',
										attributes: [
											{ key: 'type', value: { stringValue: 'input' } },
											{ key: 'model', value: { stringValue: 'claude-opus-4-8' } },
										],
									},
									{
										asInt: '340',
										attributes: [
											{ key: 'type', value: { stringValue: 'output' } },
											{ key: 'model', value: { stringValue: 'claude-opus-4-8' } },
										],
									},
								],
							},
						},
						{
							name: 'claude_code.cost.usage',
							sum: {
								dataPoints: [{ asDouble: 0.0061, attributes: [{ key: 'model', value: { stringValue: 'claude-opus-4-8' } }] }],
							},
						},
					],
				},
			],
		},
	],
};

describe('decodeOtlpMetrics — OTLP/JSON → OtelRecord[]', () => {
	it('flattens datapoints, coerces string int64s, lifts attrs + resource', () => {
		const recs = decodeOtlpMetrics(OTLP);
		assert.equal(recs.length, 3);
		const first = recs[0];
		assert.equal(first.signal, 'metric');
		if (first.signal !== 'metric') return;
		assert.equal(first.metric.name, 'claude_code.token.usage');
		assert.equal(first.metric.value, 1500); // "1500" → 1500
		assert.equal(first.metric.ts, 1_717_900_001); // nanos → seconds
		assert.equal(first.metric.attrs?.type, 'input');
		assert.equal(first.metric.attrs?.model, 'claude-opus-4-8');
		assert.equal(first.metric.resource?.['session.id'], 'sess-9');
		assert.equal(first.metric.resource?.['organization.id'], 'org-7');
	});

	it('drops non-OTLP bodies', () => {
		assert.deepEqual(decodeOtlpMetrics(null), []);
		assert.deepEqual(decodeOtlpMetrics({}), []);
		assert.deepEqual(decodeOtlpMetrics({ resourceMetrics: 'nope' }), []);
		assert.deepEqual(decodeOtlpMetrics('a string'), []);
	});
});

describe('ClaudeCodeOtelNormalizer — the metering source', () => {
	it('token.usage → claude.tokens; cost.usage → claude.cost (durable usage records)', () => {
		const out = new ClaudeCodeOtelNormalizer().normalize(OTLP, ctx);
		assert.equal(out.events.length, 0); // token/cost metrics are usage, not obs
		assert.equal(out.usage.length, 3);

		const input = out.usage.find((u) => u.meter === 'claude.tokens' && u.attrs.type === 'input');
		assert.ok(input);
		assert.equal(input!.quantity, 1500);
		assert.equal(input!.unit, 'tokens');
		assert.equal(input!.provider, 'anthropic');
		assert.equal(input!.attrs.input_tokens, 1500);
		assert.equal(input!.trace_id, 'cc-sess:sess-9');
		assert.equal(input!.tenant_id, 'org-7');
		assert.equal(input!.actor.user_id, 'u-42');

		const cost = out.usage.find((u) => u.meter === 'claude.cost');
		assert.ok(cost);
		assert.equal(cost!.unit, 'usd');
		assert.equal(cost!.quantity, 0.0061);
		assert.equal(cost!.attrs.cost_usd, 0.0061);
	});

	it('non-metrics body → dropped (decode returns null → EMPTY)', () => {
		const out = new ClaudeCodeOtelNormalizer().normalize({ foo: 1 }, ctx);
		assert.deepEqual(out, { events: [], usage: [] });
	});
});
