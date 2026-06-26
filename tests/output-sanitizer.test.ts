import { test } from 'node:test';
import assert from 'node:assert/strict';

// Behavioral spec for the voice-agent output sanitizer (src/voice-agent.ts,
// fix #1 of #1410). The detection logic lives inside main()'s wiring IIFE and is
// not exported, so this test mirrors its exact contract: the regex below MUST stay
// in sync with FABRICATED_OUTPUT_RE, and the reducer mirrors the per-turn buffer.
//
// Covers the two gaps from the 2026-06-20 review:
//   Gap 1 — bare `Silence\.?` dropped (no longer false-positives on natural speech).
//   Gap 2 — per-turn buffer catches a fabricated prefix split across streamed chunks.
const FABRICATED_OUTPUT_RE = /^\s*(\[System:|System:|\[Silence\.?\]|<ctrl\d+>)/i;

// Mirrors the streamed handler: feed delta chunks for one turn, return whether the
// running buffer was flagged as a fabrication (turn suppressed).
function turnFabricated(chunks: string[]): boolean {
	let buffer = '';
	for (const chunk of chunks) {
		buffer += chunk ?? '';
		if (FABRICATED_OUTPUT_RE.test(buffer.trim())) return true;
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

// Gap 3 (Pro re-review 2026-06-26): hold-output model. Mirrors the handler's
// suppress / hold / flush behavior and reports what actually reached the transcript
// ("forwarded") so we can assert a split fabricated prefix leaks NOTHING. A turn
// boundary flushes any still-held CLEAN text.
const FAB_PREFIXES = ['[system:', 'system:', '[silence', '<ctrl'];
function couldStillBeFabrication(raw: string): boolean {
	const s = raw.replace(/^\s+/, '').toLowerCase();
	if (s.length === 0) return true;
	if (s.length > 24) return false;
	return FAB_PREFIXES.some((p) => p.startsWith(s) || s.startsWith(p));
}
function runStream(chunks: string[], turnEnd = true): { forwarded: string; suppressed: boolean } {
	let outputBuffer = '', heldText = '', turnFab = false, turnCleared = false, forwarded = '';
	for (const c of chunks) {
		const chunk = c ?? '';
		if (turnFab) continue;
		if (turnCleared) { forwarded += chunk; continue; }
		outputBuffer += chunk; heldText += chunk;
		if (FABRICATED_OUTPUT_RE.test(outputBuffer.trim())) { turnFab = true; continue; }
		if (!couldStillBeFabrication(outputBuffer)) { turnCleared = true; forwarded += heldText; heldText = ''; }
	}
	if (turnEnd && heldText && !turnFab) { forwarded += heldText; heldText = ''; }
	return { forwarded, suppressed: turnFab };
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
