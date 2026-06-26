import { describe, it, beforeEach, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync, readdirSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { JsonlFileSink } from '../../src/observability/sink.js';
import type { ObsEvent } from '../../src/observability/events.js';

function sampleEvent(): ObsEvent {
	return {
		schema: 1,
		ts: Date.now() / 1000,
		trace_id: 'tr_TEST00000000000000000000',
		node: 'mac-studio',
		source: 'filewatcher',
		source_file: 'tasks/task-9.txt',
		actor: { user_id: 'u1', channel: 'discord', access_tier: 'owner' },
		kind: 'file.change',
		outcome: 'ok',
		data: { op: 'created' },
	};
}

let dir: string;
beforeEach(() => {
	dir = mkdtempSync(join(tmpdir(), 'obssink-'));
});
afterEach(() => rmSync(dir, { recursive: true, force: true }));

describe('kernel/obs/JsonlFileSink', () => {
	it('writes exactly one compact JSON line at the daily-dated path', () => {
		const ev = sampleEvent();
		new JsonlFileSink({ dir }).write(ev);

		const files = readdirSync(dir);
		assert.equal(files.length, 1);
		assert.match(files[0], /^events-\d{4}-\d{2}-\d{2}\.jsonl$/);

		const body = readFileSync(join(dir, files[0]), 'utf-8');
		const lines = body.split('\n').filter((l) => l.length > 0);
		assert.equal(lines.length, 1);
		// compact: no spaces, equals JSON.stringify, round-trips to the same object
		assert.equal(lines[0], JSON.stringify(ev));
		assert.deepEqual(JSON.parse(lines[0]), ev);
	});

	it('appends without clobbering across writes', () => {
		const sink = new JsonlFileSink({ dir });
		sink.write(sampleEvent());
		sink.write(sampleEvent());
		const files = readdirSync(dir);
		const body = readFileSync(join(dir, files[0]), 'utf-8');
		assert.equal(body.split('\n').filter((l) => l.length > 0).length, 2);
	});
});
