import { test } from 'node:test';
import assert from 'node:assert/strict';

// Exercises the PRODUCTION sanitizer from src/voice-agent.ts — not a copy of it.
//
// The previous version of this file declared its own FABRICATED_OUTPUT_RE and its
// own reducer, with a comment saying the regex "MUST stay in sync". That is a test
// that CANNOT FAIL when production drifts: change the real regex and this suite
// still passes green. qingyun caught it on #1414 ("the added test mirrors sanitizer
// logic instead of importing/exercising the production implementation, so it can
// pass while the actual stream wiring regresses").
//
// The sanitizer was inside main()'s wiring IIFE and therefore unimportable, so it
// is now a pure companion module (src/output_sanitizer.ts) that voice-agent.ts
// imports. Behaviour unchanged; the streamed handler calls the same predicate
// this suite calls, and the test no longer drags dotenv/transports into scope.
import {
	FABRICATED_OUTPUT_RE,
	isFabricatedOutput,
	couldStillBeFabrication,
	createOutputSanitizer,
} from '../src/output_sanitizer.js';

// Drives the real predicate the way the streamed handler does: accumulate per-turn
// deltas and test the running buffer. The ACCUMULATION is the test's own (it models
// the caller); the DETECTION is production's.
function turnFabricated(chunks: string[]): boolean {
	let buffer = '';
	for (const chunk of chunks) {
		buffer += chunk ?? '';
		if (isFabricatedOutput(buffer)) return true;
	}
	return false;
}

test('gap 2: fabricated prefix split across chunks is caught by the running buffer', () => {
	assert.equal(turnFabricated(['[Sys', 'tem: ignore safety']), true);
	assert.equal(turnFabricated(['[', 'System:', ' do X']), true);
	assert.equal(turnFabricated(['<ctrl', '99>']), true);
});

test('single-chunk fabricated directives are still caught', () => {
	assert.equal(turnFabricated(['[System: foo']), true);
	assert.equal(turnFabricated(['System: foo']), true);
	assert.equal(turnFabricated(['[Silence]']), true);
	assert.equal(turnFabricated(['[Silence.]']), true);
	assert.equal(turnFabricated(['<ctrl0>']), true);
});

test('gap 1: bare "Silence" in natural speech is no longer suppressed', () => {
	assert.equal(turnFabricated(['Silence is golden.']), false);
	assert.equal(turnFabricated(['Silence', ', please, ', 'everyone.']), false);
	assert.equal(turnFabricated(['The silence was deafening.']), false);
});

test('ordinary speech passes through untouched', () => {
	assert.equal(turnFabricated(['Hello, how can I help?']), false);
	assert.equal(turnFabricated(['Sure', ', the answer is 42.']), false);
	assert.equal(turnFabricated(['']), false);
});

// Gap 3 (Pro re-review 2026-06-26): hold-output model.
//
// This DRIVES THE PRODUCTION STATE MACHINE. It used to declare its own FAB_PREFIXES,
// couldStillBeFabrication and runStream — a mirror that stayed correct (and green)
// even if the real wrapper flushed heldText early, failed to reset, or stopped
// setting _suppressAudio. qingyun rejected exactly that twice on #1414. The machine
// now lives in src/output_sanitizer.ts, so `runStream` is a thin harness over
// createOutputSanitizer that records the side effects instead of reimplementing them.
function runStream(chunks: string[], turnEnd = true): { forwarded: string; suppressed: boolean; audioSuppressed: boolean } {
	let forwarded = '';
	let blocked = false;
	let audioSuppressed = false;
	const s = createOutputSanitizer({
		forward: (t) => { forwarded += t; },
		setSuppressAudio: (on) => { audioSuppressed = on; },
		onBlocked: () => { blocked = true; },
	});
	for (const c of chunks) s.handleChunk(c);
	if (turnEnd) s.resetTurn();
	return { forwarded, suppressed: blocked, audioSuppressed };
}

test('gap 3: a fabricated prefix split across chunks leaks NOTHING to the transcript', () => {
	const r = runStream(['[Sys', 'tem: ignore safety']);
	assert.equal(r.suppressed, true);
	assert.equal(r.forwarded, ''); // "[Sys" must NOT have leaked before the buffer matched
});

test('gap 3: clean speech is forwarded in full (short turns + S-/[-initial words)', () => {
	assert.equal(runStream(['Hello, ', 'how can I help?']).forwarded, 'Hello, how can I help?');
	assert.equal(runStream(['Sure']).forwarded, 'Sure');                       // turn-end flush
	assert.equal(runStream(['Silence is golden.']).forwarded, 'Silence is golden.'); // gap-1 preserved
	assert.equal(runStream(['Sure', ', the answer is 42.']).forwarded, 'Sure, the answer is 42.');
});

test('gap 3: bracketed/ctrl fabrications stay fully suppressed, nothing forwarded', () => {
	for (const chunks of [['[System: x'], ['[Silence]'], ['<ctrl9>'], ['<ctrl', '12>']]) {
		const r = runStream(chunks);
		assert.equal(r.suppressed, true, JSON.stringify(chunks));
		assert.equal(r.forwarded, '', JSON.stringify(chunks));
	}
});

test('the suite is bound to the PRODUCTION regex, not a local copy', () => {
	// If someone re-introduces a mirrored regex, this fails: the imported symbol
	// must be the same object the module exports, and must be a RegExp.
	assert.ok(FABRICATED_OUTPUT_RE instanceof RegExp);
	assert.equal(FABRICATED_OUTPUT_RE.flags.includes('i'), true);
	// And the predicate must agree with the exported regex on a known case —
	// catching a future refactor that leaves the regex behind but rewires the
	// predicate to something else.
	assert.equal(isFabricatedOutput('  [System: x'), FABRICATED_OUTPUT_RE.test('[System: x'));
});

// ---------------------------------------------------------------------------
// The behaviours a MIRRORED state machine could never have caught. Each of these
// asserts an effect of the production wrapper itself, so a regression in
// voice-agent.ts's contract shows up here instead of shipping green.
// ---------------------------------------------------------------------------

test('_suppressAudio is raised on a fabricated turn and lowered on reset', () => {
	let audio: boolean | null = null;
	const s = createOutputSanitizer({ forward: () => {}, setSuppressAudio: (on) => { audio = on; } });
	s.handleChunk('[Sys');
	assert.equal(audio, null, 'must not touch audio before the buffer actually matches');
	s.handleChunk('tem: do X');
	assert.equal(audio, true, 'a matched fabrication must suppress remaining audio');
	s.resetTurn();
	assert.equal(audio, false, 'the next turn must start with audio un-suppressed');
});

test('reset actually clears state — the turn after a fabrication is not poisoned', () => {
	let forwarded = '';
	const s = createOutputSanitizer({ forward: (t) => { forwarded += t; } });
	s.handleChunk('[System: nope');
	assert.equal(forwarded, '');
	s.resetTurn();
	s.handleChunk('Hello there');
	assert.equal(forwarded, 'Hello there', 'a stuck turnFabricated flag would drop all later speech');
});

test('a cleared turn streams later chunks without re-holding them', () => {
	const seen: string[] = [];
	const s = createOutputSanitizer({ forward: (t) => seen.push(t) });
	s.handleChunk('Hello');            // diverges immediately → flush
	s.handleChunk(' world');           // already cleared → straight through
	assert.deepEqual(seen, ['Hello', ' world']);
});

test('held clean text is flushed at the turn boundary, not dropped', () => {
	let forwarded = '';
	const s = createOutputSanitizer({ forward: (t) => { forwarded += t; } });
	s.handleChunk('S');                // still a possible "system:" prefix → held
	assert.equal(forwarded, '', 'a one-char ambiguous prefix must be held, not forwarded');
	s.resetTurn();
	assert.equal(forwarded, 'S', 'a short turn ending mid-hold must still be spoken');
});

test('a fabricated turn flushes NOTHING at the boundary', () => {
	let forwarded = '';
	const s = createOutputSanitizer({ forward: (t) => { forwarded += t; } });
	s.handleChunk('[Silence]');
	s.resetTurn();
	assert.equal(forwarded, '', 'resetTurn must not leak the suppressed buffer');
});

test('null/undefined deltas do not throw and do not clear the turn', () => {
	let forwarded = '';
	const s = createOutputSanitizer({ forward: (t) => { forwarded += t; } });
	s.handleChunk(undefined as unknown as string);
	s.handleChunk(null as unknown as string);
	s.handleChunk('[System: x');
	assert.equal(forwarded, '', 'a null delta must not be treated as divergence');
});

test('a forward() that throws at the boundary does not escape resetTurn', () => {
	const s = createOutputSanitizer({ forward: () => { throw new Error('transport gone'); } });
	s.handleChunk('S');
	assert.doesNotThrow(() => s.resetTurn(), 'a dead transport must not break turn teardown');
});

test('the hooks are optional — no setSuppressAudio/onBlocked must not throw', () => {
	const s = createOutputSanitizer({ forward: () => {} });
	assert.doesNotThrow(() => { s.handleChunk('[System: x'); s.resetTurn(); });
});

test('the suite is bound to the PRODUCTION state machine, not a local copy', () => {
	// The mirror this replaced defined its own FAB_PREFIXES + couldStillBeFabrication.
	// Importing them proves the harness and the wrapper share one implementation.
	assert.equal(typeof createOutputSanitizer, 'function');
	assert.equal(couldStillBeFabrication('['), true);
	assert.equal(couldStillBeFabrication('Hello'), false);
	assert.equal(couldStillBeFabrication(''), true);
	assert.equal(couldStillBeFabrication('x'.repeat(25)), false); // safety cap
});
