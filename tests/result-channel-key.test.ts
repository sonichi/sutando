/**
 * Unit tests for src/result-channel-key.ts — the per-channel pull path
 * for task-result files in `results/`.
 *
 * Load-bearing invariant: a `<channel-key>.task-{id}.txt` filename must
 * NOT match any existing consumer's filter (startsWith('task-') / glob
 * 'task-*.txt' / specific-task_id existsSync). We assert that here against
 * the actual patterns those consumers use, so a future change to one of
 * the helper functions can't silently re-open the blast radius.
 *
 * Run: tsx --test tests/result-channel-key.test.ts
 */

import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import {
	sanitizeKey,
	resultFilename,
	parseResultFilename,
	resultBelongsTo,
	phoneCallKey,
} from '../src/result-channel-key.js';

describe('sanitizeKey', () => {
	it('passes through filename-safe input', () => {
		assert.equal(sanitizeKey('1234567890123456789'), '1234567890123456789');
		assert.equal(sanitizeKey('CA1234abcd'), 'CA1234abcd');
		assert.equal(sanitizeKey('local-voice'), 'local-voice');
		assert.equal(sanitizeKey('foo_bar-baz'), 'foo_bar-baz');
	});

	it('collapses unsafe chars to dashes', () => {
		assert.equal(sanitizeKey('a/b'), 'a-b');
		assert.equal(sanitizeKey('a.b'), 'a-b');
		assert.equal(sanitizeKey('a b'), 'a-b');
		assert.equal(sanitizeKey('../etc/passwd'), '---etc-passwd');
	});

	it('returns "unknown" for empty / falsy input', () => {
		assert.equal(sanitizeKey(''), 'unknown');
		assert.equal(sanitizeKey(null), 'unknown');
		assert.equal(sanitizeKey(undefined), 'unknown');
		assert.equal(sanitizeKey('   '), 'unknown');
		// All-unsafe → all dashes (NOT 'unknown' — the input wasn't empty).
		assert.equal(sanitizeKey('...'), '---');
	});
});

describe('resultFilename', () => {
	it('builds <key>.<task-id>.txt', () => {
		assert.equal(
			resultFilename('1234567890123456789', 'task-plugin-voice-1700000000'),
			'1234567890123456789.task-plugin-voice-1700000000.txt',
		);
		assert.equal(
			resultFilename('CA1234abcd', 'task-phone-1700000000'),
			'CA1234abcd.task-phone-1700000000.txt',
		);
	});
});

describe('parseResultFilename', () => {
	it('splits the scoped form', () => {
		assert.deepEqual(
			parseResultFilename('1234567890123456789.task-plugin-voice-1700000000.txt'),
			['1234567890123456789', 'task-plugin-voice-1700000000'],
		);
		assert.deepEqual(
			parseResultFilename('CA1234abcd.task-phone-1700000000'),
			['CA1234abcd', 'task-phone-1700000000'],
		);
	});

	it('returns [null, base] for the legacy flat form', () => {
		assert.deepEqual(parseResultFilename('task-1700000000.txt'), [null, 'task-1700000000']);
		assert.deepEqual(parseResultFilename('task-plugin-voice-1700000000.txt'), [
			null,
			'task-plugin-voice-1700000000',
		]);
	});

	it('returns [null, base] for non-task files (voice-, proactive-)', () => {
		assert.deepEqual(parseResultFilename('voice-1700000000.txt'), [null, 'voice-1700000000']);
		assert.deepEqual(parseResultFilename('proactive-1700000000.txt'), [
			null,
			'proactive-1700000000',
		]);
	});
});

describe('resultBelongsTo', () => {
	it('claims the scoped form for a matching key', () => {
		assert.equal(
			resultBelongsTo('1234567890123456789.task-foo.txt', '1234567890123456789'),
			true,
		);
		assert.equal(resultBelongsTo('CA123.task-phone-1.txt', 'CA123'), true);
	});

	it('rejects a different channel key', () => {
		assert.equal(
			resultBelongsTo('1234567890123456789.task-foo.txt', '9999999999'),
			false,
		);
	});

	it('rejects the legacy flat form (owned by delegating consumer, not by scan)', () => {
		assert.equal(resultBelongsTo('task-1700000000.txt', '1234567890123456789'), false);
		assert.equal(resultBelongsTo('task-plugin-voice-1700000000.txt', 'local-voice'), false);
	});

	it('rejects non-task files', () => {
		assert.equal(resultBelongsTo('voice-1700000000.txt', 'local-voice'), false);
		assert.equal(resultBelongsTo('proactive-1700000000.txt', 'anything'), false);
		assert.equal(resultBelongsTo('1234567890123456789.proactive-foo.txt', '1234567890123456789'), false);
	});

	// Partial-write race: a writer's atomic-write temp file (`<key>.task-X.txt.tmp`,
	// `.sending`, `.partial`, etc.) must NEVER match — picking it up would inject a
	// half-written body and orphan the rename target. The scan loops also gate on
	// `.endsWith('.txt')`, but lock the invariant at the helper too.
	it('rejects atomic-write temp suffixes (partial-write race)', () => {
		const KEY = '1234567890123456789';
		const tempSuffixes = [
			'1234567890123456789.task-plugin-voice-1700000000.txt.tmp',
			'1234567890123456789.task-plugin-voice-1700000000.txt.partial',
			'1234567890123456789.task-plugin-voice-1700000000.txt.sending',
			'1234567890123456789.task-plugin-voice-1700000000.txt.swp',
			'1234567890123456789.task-plugin-voice-1700000000.txt.lock',
			'1234567890123456789.task-plugin-voice-1700000000.txt~',
			'1234567890123456789.task-plugin-voice-1700000000.sending',
			'1234567890123456789.task-plugin-voice-1700000000.tmp',
			'1234567890123456789.task-plugin-voice-1700000000.partial',
			'.1234567890123456789.task-plugin-voice-1700000000.txt', // dotfile prefix (vim swap, atomic-write idioms)
		];
		for (const f of tempSuffixes) {
			assert.equal(
				resultBelongsTo(f, KEY),
				false,
				`resultBelongsTo should reject ${f} (partial-write temp)`,
			);
		}
	});

	// Sanity-check: the canonical `.txt` form for the same key still matches —
	// the temp-suffix rejection didn't accidentally over-reject.
	it('still matches the canonical .txt form', () => {
		assert.equal(
			resultBelongsTo(
				'1234567890123456789.task-plugin-voice-1700000000.txt',
				'1234567890123456789',
			),
			true,
		);
	});
});

// --- The load-bearing invariant: existing consumers don't see new files ---
// These tests replay the EXACT filter patterns the existing consumers use
// against a sample scoped filename. If any of these flips to `true`, the
// new namespace has leaked into a consumer's path and we've broken the
// blast-radius guarantee.
describe('existing consumers do NOT match the scoped namespace', () => {
	const SCOPED = '1234567890123456789.task-plugin-voice-1700000000.txt';
	const SCOPED_BASE = '1234567890123456789.task-plugin-voice-1700000000';

	it('discord-bridge / telegram-bridge / slack-bridge pending_replies lookup', () => {
		// All three bridges do: result_file = RESULTS_DIR / f"{task_id}.txt"
		// where task_id is the id they themselves tracked when writing the
		// task. A scoped filename's `task_id` would be the full prefixed
		// string, which is not a tracked id — so the existsSync miss.
		// Equivalent to: no pending_replies key matches SCOPED.
		const pending: Record<string, boolean> = {
			'task-1700000001': true,
			'task-plugin-voice-1700000000': true, // a hypothetical tracked id
		};
		// The bridge would look up `${task_id}.txt`; SCOPED doesn't equal any
		// `${tracked}.txt`.
		for (const tracked of Object.keys(pending)) {
			assert.notEqual(`${tracked}.txt`, SCOPED, `pending id ${tracked} would match scoped filename`);
		}
	});

	it('agent-api.py task-* glob does NOT match', () => {
		// Python: results_dir.glob("task-*.txt")
		// Equivalent JS: startsWith('task-') && endsWith('.txt')
		assert.equal(SCOPED.startsWith('task-'), false);
	});

	it('task-bridge.ts file.startsWith("voice-") guard does NOT match', () => {
		assert.equal(SCOPED.startsWith('voice-'), false);
	});

	it('task-bridge.ts file.startsWith("task-") guards (dedup-marker + voice-offline forward) do NOT match', () => {
		assert.equal(SCOPED.startsWith('task-'), false);
	});

	it('task-bridge.ts task-chat- guard does NOT match', () => {
		assert.equal(SCOPED_BASE.startsWith('task-chat-'), false);
	});

	// Note: task-bridge.ts has an UNCONDITIONAL fallthrough at line 682
	// (`if (result) { ... onResult(result) ... }`) that fires for any
	// non-empty .txt file when the voice client is connected. The narrow
	// design accepts this: voice-agent and the pull-side plugin / phone are
	// different surfaces, and in practice the active pull-side / phone
	// process will read-and-delete the scoped file before voice-agent's
	// 2s poll claims it. See PR body's verification section for the full
	// trade-off discussion.
});

// Per-consumer prefixes — writer + consumer MUST go through the same typed
// function so the keys agree. Prevents cross-consumer namespace collisions
// when a future consumer ID format overlaps with an existing one.
describe('typed key constructors', () => {
	it('phoneCallKey prefixes with `phone-`', () => {
		assert.equal(phoneCallKey('CA1234abcd'), 'phone-CA1234abcd');
	});

	it('typed keys sanitize input', () => {
		assert.equal(phoneCallKey('../etc'), 'phone----etc');
	});

	it('typed keys fall back on empty / falsy input', () => {
		assert.equal(phoneCallKey(null), 'phone-unknown');
	});

	it('typed keys round-trip through resultFilename + resultBelongsTo', () => {
		const key = phoneCallKey('CA1234abcd');
		const fname = resultFilename(key, 'task-1700000000');
		assert.equal(fname, 'phone-CA1234abcd.task-1700000000.txt');
		assert.equal(resultBelongsTo(fname, key), true);
	});
});
