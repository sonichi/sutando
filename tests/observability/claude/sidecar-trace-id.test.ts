import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { ClaudeCodeOtelNormalizer } from '../../../src/observability/claude/otel-normalizer.js';
import { traceIdFromSession } from '../../../src/observability/claude/ids.js';
import type { NormalizeContext } from '../../../src/observability/collector/normalizer.js';

/**
 * THE load-bearing test for the indirect-emit seam.
 *
 * The interactive CLI core is a process we don't control. It does not call
 * `emit()` inline — instead it exports telemetry out-of-band (native OTel to
 * /v1/metrics; .jsonl transcript) and the collector RECONSTRUCTS spine
 * primitives from that external output. The correctness property the whole
 * trace-replay safety net rests on is: the core's `session.id` is ADOPTED as
 * `trace_id` (`cc-sess:<id>`) and SURVIVES that indirect hop onto every emitted
 * primitive — so the three CC sources (OTel, hooks, .jsonl) agree on one trace
 * per session without coordinating (see ids.ts).
 *
 * This asserts it explicitly for the OTel channel that ships in this PR, driven
 * through the REGISTERED normalizer (the real decode∘map seam), not the pure id
 * helper. The obs-event side of the same adoption is also covered by
 * otel-map.test.ts (cc.llm_request carries cc-sess:<id>); the .jsonl-tail twin
 * lands with `jsonl-tail.ts` in its follow-up PR (deferred from this one).
 */

const ctx: NormalizeContext = { node: 'mac-studio', receivedAt: 1_717_900_000 };

/** A realistic OTLP/HTTP-JSON metrics body as Claude Code's OTel exporter posts
 *  to /v1/metrics — carrying the CLI core's session.id on the resource. This is
 *  the "sidecar" input the collector reconstructs from. */
function otlpWithSession(sessionId: string): unknown {
	return {
		resourceMetrics: [
			{
				resource: {
					attributes: [
						{ key: 'session.id', value: { stringValue: sessionId } },
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
										{ timeUnixNano: '1717900001000000000', asInt: '1500', attributes: [{ key: 'type', value: { stringValue: 'input' } }] },
										{ asInt: '340', attributes: [{ key: 'type', value: { stringValue: 'output' } }] },
									],
								},
							},
						],
					},
				],
			},
		],
	};
}

describe('trace_id survives the indirect-emit seam (CLI core → tailed OTel → emitted primitive)', () => {
	it('adopts the core session.id as cc-sess:<id> on EVERY emitted primitive', () => {
		const out = new ClaudeCodeOtelNormalizer().normalize(otlpWithSession('sess-seam'), ctx);
		const want = traceIdFromSession('sess-seam'); // the documented adoption
		assert.equal(want, 'cc-sess:sess-seam');
		const primitives = [...out.events, ...out.usage];
		assert.ok(primitives.length > 0, 'the seam must emit at least one primitive to carry the trace');
		for (const p of primitives) assert.equal(p.trace_id, want, 'trace_id must survive the hop onto every primitive');
	});

	it('is ADOPTED from the core, not minted: a different session.id yields a different (derived) trace_id', () => {
		const norm = new ClaudeCodeOtelNormalizer();
		const a = norm.normalize(otlpWithSession('sess-A'), ctx).usage[0];
		const b = norm.normalize(otlpWithSession('sess-B'), ctx).usage[0];
		assert.equal(a.trace_id, 'cc-sess:sess-A');
		assert.equal(b.trace_id, 'cc-sess:sess-B');
		assert.notEqual(a.trace_id, b.trace_id);
		// a freshly-minted tr_ id here would silently break cross-source correlation
		assert.doesNotMatch(a.trace_id, /^tr_/);
	});

	it('degrades deterministically to cc-sess:unknown when the core omits session.id', () => {
		const noSession = {
			resourceMetrics: [
				{
					resource: { attributes: [] },
					scopeMetrics: [{ metrics: [{ name: 'claude_code.token.usage', sum: { dataPoints: [{ asInt: '10', attributes: [{ key: 'type', value: { stringValue: 'input' } }] }] } }] }],
				},
			],
		};
		const out = new ClaudeCodeOtelNormalizer().normalize(noSession, ctx);
		assert.ok(out.usage.length > 0);
		for (const u of out.usage) assert.equal(u.trace_id, traceIdFromSession(undefined)); // 'cc-sess:unknown', never random
	});
});
