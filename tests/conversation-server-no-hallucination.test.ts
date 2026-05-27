import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

// Pin the fix for sonichi/sutando#1244 — voice agent fabricated calendar
// events ("design review at 10 AM, marketing campaign planning, virtual
// coffee with a client") when the work tool returned no events. Root cause:
// the result wrapper said "Report this result to the caller now" but
// contained no anti-invention guard, and an empty `result` string gave the
// model an empty pattern to fill.
//
// Fix wraps the injected result with two layers:
//   (1) RESULT_EMPTY sentinel on truly-empty results
//   (2) "only items present verbatim" guardrail on every result

const SRC = readFileSync(
	join(import.meta.dirname ?? '.', '..', 'skills/phone-conversation/scripts/conversation-server.ts'),
	'utf-8',
);

describe('conversation-server — anti-hallucination on work-tool results (sonichi#1244)', () => {
	it('detects truly-empty results via result.length === 0', () => {
		assert.match(
			SRC,
			/const\s+isEmpty\s*=\s*result\.length\s*===\s*0/,
			'must detect empty results with strict-equal length check on the trimmed result string.',
		);
	});

	it('emits a RESULT_EMPTY sentinel for empty results', () => {
		assert.match(
			SRC,
			/RESULT_EMPTY/,
			'the empty-result wrapper must contain the literal RESULT_EMPTY sentinel so the model has an explicit "say nothing" signal.',
		);
	});

	it('explicitly bars invention on empty results', () => {
		assert.match(
			SRC,
			/RESULT_EMPTY[\s\S]{0,300}?Do NOT invent/i,
			'the empty-result wrapper must explicitly bar invention/extrapolation.',
		);
	});

	it('explicitly bars invention on non-empty results too', () => {
		assert.match(
			SRC,
			/items that appear verbatim[\s\S]{0,200}?do NOT invent/i,
			'non-empty result wrapper must require items appear verbatim AND bar invention — silence-filling can also happen with sparse-but-not-empty results.',
		);
	});

	it('still pushes onto the existing resultQueue (no behavioral regression)', () => {
		assert.match(
			SRC,
			/callSession\.resultQueue\.push\(\s*\{\s*text:\s*injectedText\s*\}\s*\)/,
			'result injection must continue to use callSession.resultQueue.push so the existing turn-end drain logic still fires.',
		);
	});

	it('pre-warms the system prompt with TOOL RESULT TRUTHFULNESS clause', () => {
		// Cherry-picked from bassilkhilo-ag2's parallel PR #1249 — session-level
		// backstop so the constraint is in scope BEFORE the per-result wrapper
		// lands. Defense in depth: model gets the rule at boot + at delivery.
		assert.match(
			SRC,
			/TOOL RESULT TRUTHFULNESS/,
			'system prompt must include a TOOL RESULT TRUTHFULNESS clause as session-level anti-hallucination backstop.',
		);
		assert.match(
			SRC,
			/TOOL RESULT TRUTHFULNESS[\s\S]{0,400}?Fabricated events mislead the owner/,
			'TOOL RESULT TRUTHFULNESS clause must include the rationale (fabricated events mislead owner) so the model understands the priority.',
		);
	});
});
