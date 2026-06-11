import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { transcriptMap } from '../../../../../src/adapters/executor/claude-code/sources/transcript-map.js';
import type { MapContext } from '../../../../../src/adapters/executor/claude-code/sources/cc-records.js';

const ctx: MapContext = { node: 'mac-studio', receivedAt: 1_717_900_000 };

describe('transcriptMap — enrichment/fallback', () => {
	it('assistant.usage → claude.tokens record stamped _cc_source jsonl (lowest rank)', () => {
		const out = transcriptMap(
			{ type: 'assistant', ts: 1_717_900_002, model: 'claude-opus-4-8', request_id: 'req_01H', session_id: 'sess-9', usage: { input_tokens: 1500, output_tokens: 340, cache_read_tokens: 100, cache_creation_tokens: 0 } },
			ctx,
		);
		assert.equal(out.events.length, 0);
		assert.deepEqual(out.usage[0], {
			schema: 1,
			usage_id: 'cc:tok:req_01H',
			ts: 1_717_900_002,
			tenant_id: null,
			trace_id: 'cc-sess:sess-9',
			actor: { user_id: 'core', channel: 'claude-code', access_tier: 'owner', tenant_id: null },
			source: 'core-cli',
			meter: 'claude.tokens',
			quantity: 1840,
			unit: 'tokens',
			provider: 'anthropic',
			provider_ref: 'req_01H',
			attrs: { _cc_source: 'jsonl', model: 'claude-opus-4-8', input_tokens: 1500, output_tokens: 340, cache_read: 100, cache_creation: 0 },
		});
	});

	it('tool_use Write → tool.call AND paired file.change(written)', () => {
		const out = transcriptMap({ type: 'tool_use', ts: 1_717_900_002, session_id: 'sess-9', tool_name: 'Write', tool_input: { file_path: 'notes/y.md' }, tool_use_id: 'tu5' }, ctx);
		assert.equal(out.usage.length, 0);
		assert.equal(out.events[0].kind, 'tool.call');
		const fe = out.events.find((e) => e.kind === 'file.change');
		assert.ok(fe);
		assert.equal(fe!.source_file, 'notes/y.md');
		assert.equal((fe!.data as Record<string, unknown>).op, 'written');
	});

	it('assistant line without usage → nothing', () => {
		assert.deepEqual(transcriptMap({ type: 'assistant', ts: 1, session_id: 's' }, ctx), { events: [], usage: [] });
	});

	it('unrecognized line type → dropped (drift-safe), never throws', () => {
		assert.deepEqual(transcriptMap({ type: 'totally_unknown_v2', blah: 1 }, ctx), { events: [], usage: [] });
	});
});
