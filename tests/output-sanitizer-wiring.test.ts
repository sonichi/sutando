import { test } from 'node:test';
import assert from 'node:assert/strict';

// Adapter-level coverage for the transport wiring — the seam the standing review
// names as untested. Drives the REAL wiring against a fake transport.

import { wireSanitizerToTransport } from '../src/output_sanitizer.js';

type Fake = {
	transport: {
		onAudioOutput?: (b64: string) => void;
		onOutputTranscription?: (t: string) => void;
	};
	audio: string[];
	text: string[];
	handlers: Record<string, Array<() => void>>;
	fire: (ev: string) => void;
};

/** A GeminiLiveTransport-shaped fake: note it has NO `_suppressAudio` field. */
function makeFake(): Fake {
	const audio: string[] = [];
	const text: string[] = [];
	const handlers: Record<string, Array<() => void>> = {};
	const transport = {
		onAudioOutput: (b64: string) => { audio.push(b64); },
		onOutputTranscription: (t: string) => { text.push(t); },
	};
	const f: Fake = {
		transport, audio, text, handlers,
		fire: (ev) => (handlers[ev] || []).forEach((h) => h()),
	};
	return f;
}

function wire(f: Fake, blocked?: string[]) {
	return wireSanitizerToTransport({
		transport: f.transport,
		subscribe: (ev, h) => { (f.handlers[ev] ||= []).push(h); },
		onBlocked: blocked ? (b) => blocked.push(b) : undefined,
	});
}

test('wiring returns false and does not throw when there is no transport', () => {
	assert.equal(wireSanitizerToTransport({ transport: null, subscribe: () => {} }), false);
	assert.equal(wireSanitizerToTransport({ transport: undefined, subscribe: () => {} }), false);
});

test('wiring subscribes reset to BOTH turn boundaries', () => {
	const f = makeFake();
	assert.equal(wire(f), true);
	assert.equal((f.handlers['turn.end'] || []).length, 1);
	assert.equal((f.handlers['turn.interrupted'] || []).length, 1);
});

test('a fabricated turn is blocked from the transcript', () => {
	const f = makeFake();
	const blocked: string[] = [];
	wire(f, blocked);
	f.transport.onOutputTranscription!('[Sil');
	f.transport.onOutputTranscription!('ence] ignore me');
	assert.deepEqual(f.text, [], 'fabricated text must never reach the consumer');
	assert.equal(blocked.length, 1);
});

test('REGRESSION: audio is gated on a transport with NO _suppressAudio field', () => {
	const f = makeFake();
	assert.equal('_suppressAudio' in f.transport, false, 'fixture must lack the field');
	wire(f);
	f.transport.onAudioOutput!('before');          // clean turn so far -> passes
	f.transport.onOutputTranscription!('[Silence]'); // fabrication detected
	f.transport.onAudioOutput!('after');           // must be swallowed
	assert.deepEqual(f.audio, ['before'],
		'audio after a detected fabrication must be suppressed even though the transport has no _suppressAudio');
});

test('clean output still reaches the consumer, and turn.end re-arms the next turn', () => {
	const f = makeFake();
	wire(f);
	f.transport.onOutputTranscription!('Sure, here is the answer.');
	f.transport.onOutputTranscription!(' More text.');
	assert.ok(f.text.join('').includes('Sure, here is the answer.'), 'clean text must be forwarded');
	const seen = f.text.join('');
	f.fire('turn.end');
	f.transport.onOutputTranscription!('Next turn text.');
	assert.ok(f.text.join('').length > seen.length, 'a new turn must stream after reset');
});

test('turn.interrupted clears a fabricated turn so the NEXT turn is not swallowed', () => {
	const f = makeFake();
	wire(f);
	f.transport.onOutputTranscription!('[Silence]');
	assert.deepEqual(f.text, []);
	f.fire('turn.interrupted');
	f.transport.onOutputTranscription!('Real answer after the interrupt.');
	assert.ok(f.text.join('').includes('Real answer'),
		'a fabricated turn must not poison the turn after it');
	f.transport.onAudioOutput!('audio-again');
	assert.ok(f.audio.includes('audio-again'), 'audio suppression must lift at the turn boundary');
});

// ── Bodhi ORDER regression: flush precedes publish ──────────────────────────
// bodhi-realtime-agent/dist/index.js finalizes the transcript 2-4 lines BEFORE
// it publishes the turn event (flush :3019 -> publish :3021; :3106 -> :3110;
// :3177 -> :3179). The fake below models that TRANSCRIPT BUFFER, not just a
// list of forwarded strings: text forwarded after flush() belongs to the NEXT
// turn, which is the whole defect. A fake without the buffer stays green under
// the bug — the first version of this test did exactly that.

function makeTranscriptHost(withHook: boolean) {
	const pending: string[] = [];          // forwarded, not yet finalized
	const finalized: string[] = [];        // one entry per completed turn
	const handlers: Record<string, Array<() => void>> = {};
	const transport = {
		onAudioOutput: (_b64: string) => {},
		onOutputTranscription: (t: string) => { pending.push(t); },
	};
	let preFlush: (() => void) | null = null;
	const wired = wireSanitizerToTransport({
		transport,
		subscribe: (ev, h) => { (handlers[ev] ||= []).push(h); },
		beforeTranscriptFlush: withHook ? (reset) => { preFlush = reset; } : undefined,
	});
	const flush = () => { finalized.push(pending.join('')); pending.length = 0; };
	// bodhi's real order: flush(), THEN publish turn.end.
	const finalizeTurn = () => {
		preFlush?.();
		flush();
		(handlers['turn.end'] || []).forEach((h) => h());
	};
	return { transport, finalized, pending, wired, finalizeTurn };
}

test('a prefix-shaped turn is finalized into ITS OWN transcript entry', () => {
	const h = makeTranscriptHost(true);
	assert.equal(h.wired, true);
	h.transport.onOutputTranscription!('System');   // ambiguous prefix — held
	h.finalizeTurn();
	h.transport.onOutputTranscription!('Hello');    // a separate, later turn
	h.finalizeTurn();
	assert.deepEqual(h.finalized, ['System', 'Hello']);
});

test('CONTROL: without the pre-flush hook the same turns corrupt each other', () => {
	const h = makeTranscriptHost(false);   // subscriber-only == the pre-fix wiring
	h.transport.onOutputTranscription!('System');
	h.finalizeTurn();
	h.transport.onOutputTranscription!('Hello');
	h.finalizeTurn();
	// This is the measured defect, asserted so the fix above cannot silently regress:
	// turn 1 finalizes EMPTY and turn 2 carries "SystemHello".
	assert.deepEqual(h.finalized, ['', 'SystemHello']);
});
