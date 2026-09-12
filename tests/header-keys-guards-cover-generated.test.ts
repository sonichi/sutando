/**
 * Asserts BEHAVIOUR on the constructed regex, not that the guard imports the
 * key set: a 45-key literal dropping one key leaves an import-only test green.
 */

import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { HEADER_KEYS } from '../src/header_keys.js';
import { _HEADER_RE } from '../src/task-bridge.js';

describe('task-bridge: the constructed guard covers every generated key', () => {
	it('matches a header line for EVERY generated key', () => {
		assert.ok(HEADER_KEYS.length >= 40, `only ${HEADER_KEYS.length} keys — generator suspect`);
		const missed = HEADER_KEYS.filter((k) => !_HEADER_RE.test(`${k}: x`));
		assert.deepEqual(missed, [], `guard does not cover: ${missed.join(', ')}`);
	});

	it('does NOT match a non-key, so the test cannot pass by over-matching', () => {
		// Without this, a guard of /^.*:/ would satisfy the assertion above.
		assert.equal(_HEADER_RE.test('not_a_header_key: x'), false);
		assert.equal(_HEADER_RE.test('just prose'), false);
	});

	it('covers requested_worker specifically — the key the mutation dropped', () => {
		assert.ok(HEADER_KEYS.includes('requested_worker'));
		assert.ok(_HEADER_RE.test('requested_worker: worker-1'));
	});
});

describe('conversation-server: the construction site names the generated symbol', () => {
	// STRUCTURAL, and deliberately labelled so: the module creates an HTTP
	// server at import, so its regex cannot be evaluated from a test.
	const SRC = readFileSync(
		resolve('skills/phone-conversation/scripts/conversation-server.ts'), 'utf8');

	it('builds _CONF_HEADER_RE from HEADER_KEY_ALTERNATION, not a literal', () => {
		const line = SRC.split('\n').find((l) => l.includes('_CONF_HEADER_RE') && l.includes('new RegExp'));
		assert.ok(line, '_CONF_HEADER_RE construction not found');
		assert.ok(line!.includes('${HEADER_KEY_ALTERNATION}'),
			`construction does not interpolate the generated symbol: ${line}`);
	});

	it('carries no literal header-key alternation anywhere', () => {
		// The mutation's shape: a long `a|b|c|...` literal beside the import.
		const literal = SRC.match(/['"`][a-z_]+(\|[a-z_]+){10,}['"`]/);
		assert.equal(literal, null, `literal alternation present: ${literal?.[0]?.slice(0, 60)}`);
	});
});
