import { describe, it, beforeEach, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { emit, registerSink, resetSinks, setSampler } from '../../src/observability/obs.js';
import type { ObsEvent, ObsEventInput } from '../../src/observability/events.js';
import type { Sink } from '../../src/observability/sink.js';

class Capture implements Sink {
	readonly type = 'capture';
	events: ObsEvent[] = [];
	write(ev: ObsEvent): void {
		this.events.push(ev);
	}
}

function baseInput(): ObsEventInput {
	return {
		source: 'voice-agent',
		actor: { user_id: 'u1', channel: 'voice', access_tier: 'owner' },
		kind: 'tool.call',
		outcome: 'ok',
	};
}

let cap: Capture;

beforeEach(() => {
	resetSinks();
	cap = new Capture();
	registerSink(cap);
});
afterEach(() => resetSinks());

describe('kernel/obs/emit', () => {
	it('stamps schema/ts/node/trace_id and keeps caller fields', () => {
		emit({ ...baseInput(), source_file: 'tasks/task-1.txt', data: { tool_name: 'Read' } });
		assert.equal(cap.events.length, 1);
		const ev = cap.events[0];
		assert.equal(ev.schema, 1);
		assert.equal(typeof ev.ts, 'number');
		assert.match(ev.trace_id, /^tr_/);
		assert.ok(ev.node.length > 0);
		assert.equal(ev.source, 'voice-agent');
		assert.equal(ev.source_file, 'tasks/task-1.txt');
		assert.equal(ev.kind, 'tool.call');
		assert.deepEqual(ev.data, { tool_name: 'Read' });
	});

	it('honors a supplied trace_id', () => {
		emit({ ...baseInput(), trace_id: 'tr_FIXED' });
		assert.equal(cap.events[0].trace_id, 'tr_FIXED');
	});

	it('never throws when a sink throws; other sinks still receive', () => {
		const bad: Sink = {
			type: 'bad',
			write() {
				throw new Error('boom');
			},
		};
		registerSink(bad);
		assert.doesNotThrow(() => emit(baseInput()));
		assert.equal(cap.events.length, 1);
	});

	it('samples away ok events when the sampler returns false', () => {
		setSampler(() => false);
		emit(baseInput());
		assert.equal(cap.events.length, 0);
	});

	it('never samples away error / denied / usage.recorded', () => {
		setSampler(() => false);
		emit({ ...baseInput(), outcome: 'error' });
		emit({ ...baseInput(), outcome: 'denied' });
		emit({ ...baseInput(), kind: 'usage.recorded' });
		assert.equal(cap.events.length, 3);
	});

	it('omits optional fields that were not supplied', () => {
		emit(baseInput());
		const ev = cap.events[0];
		assert.equal('span_id' in ev, false);
		assert.equal('duration_ms' in ev, false);
		assert.equal('usage' in ev, false);
		assert.equal('source_file' in ev, false);
	});
});
