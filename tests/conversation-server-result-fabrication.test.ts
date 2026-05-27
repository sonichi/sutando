import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

// Pin the fix for sonichi/sutando#1244 — voice agent fabricates calendar
// events when a work task returns empty results. Root cause: the result
// injection text said only "Report this result to the caller now." with no
// guard against empty payloads. Gemini Live filled the silence with
// plausible-sounding fabrications (events, emails) the owner's calendar
// didn't contain.
//
// Fix (two-part):
//   1. Result injection: if result is empty, substitute [RESULT_EMPTY] and
//      tell the model to say "nothing found" — not to invent items.
//   2. System prompt: add TOOL RESULT TRUTHFULNESS clause in ## Known info
//      so the constraint is set at session-open, not only per-result.

const SRC = readFileSync(
	join(import.meta.dirname ?? '.', '..', 'skills/phone-conversation/scripts/conversation-server.ts'),
	'utf-8',
);

describe('conversation-server — result fabrication guard (sonichi#1244)', () => {
	it('substitutes [RESULT_EMPTY] sentinel when result is falsy', () => {
		assert.match(
			SRC,
			/RESULT_EMPTY/,
			'must define a RESULT_EMPTY sentinel for empty task results — model uses it to say "nothing found" instead of fabricating events.',
		);
	});

	it('result injection instructs model to only report items present in the result', () => {
		assert.match(
			SRC,
			/ONLY read back items that appear verbatim in the result/,
			'result injection text must contain an explicit "only verbatim" constraint so the model cannot add items not present in the result.',
		);
	});

	it('result injection instructs model to say "nothing scheduled" on empty result', () => {
		assert.match(
			SRC,
			/nothing scheduled.*nothing found|nothing found.*nothing scheduled/i,
			'result injection must explicitly name the expected fallback phrase for empty results.',
		);
	});

	it('system prompt contains TOOL RESULT TRUTHFULNESS clause', () => {
		assert.match(
			SRC,
			/TOOL RESULT TRUTHFULNESS/,
			'system prompt must include a named TOOL RESULT TRUTHFULNESS rule — a general fabrication warning is insufficient; the model needs an explicit per-result-empty constraint.',
		);
	});

	it('TOOL RESULT TRUTHFULNESS clause forbids inventing events or items not in the result', () => {
		assert.match(
			SRC,
			/never invent.*calendar events|never invent.*emails|never invent.*items/i,
			'TOOL RESULT TRUTHFULNESS clause must explicitly forbid inventing calendar events, emails, or other items.',
		);
	});
});
