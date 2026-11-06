/**
 * Structural regression guard: task-bridge.ts must include confineUserContent()
 * and apply it to all three user-controlled task-body insertion points.
 *
 * confineUserContent() is a module-private function so we can't import it
 * directly. Instead we grep the source to verify:
 *   1. The function is defined with the right shape.
 *   2. Every user-supplied text variable is wrapped before landing in the
 *      task: field or the voice-transcript context block.
 *
 * This mirrors the approach used in tests/agent-api-task-field-injection.test.py
 * for the Python paths.
 *
 * Run: node --import tsx/esm tests/task-bridge-confine.test.ts
 */

import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

const SRC_PATH = resolve('src/task-bridge.ts');
const SRC = readFileSync(SRC_PATH, 'utf8');
const GEN = readFileSync(resolve('src/header_keys.ts'), 'utf8');

describe('task-bridge confineUserContent guard', () => {

	it('confineUserContent function is defined', () => {
		assert.ok(
			SRC.includes('function confineUserContent'),
			'confineUserContent must be defined in task-bridge.ts',
		);
	});

	it('confineUserContent uses ZWSP (U+200B)', () => {
		assert.ok(
			SRC.includes("'\\u200B'") || SRC.includes('"\\u200B"') || SRC.includes('​'),
			'confineUserContent must use U+200B (ZWSP) as the defang prefix',
		);
	});

	it('confineUserContent handles CR/CRLF normalization', () => {
		assert.ok(
			SRC.includes('\\r\\n') && SRC.includes('\\r'),
			'confineUserContent must normalize \\r\\n and \\r to \\n',
		);
	});

	it('voice task body is confined: confineUserContent(task)', () => {
		assert.ok(
			SRC.includes('confineUserContent(task)'),
			'voice task: field must wrap `task` in confineUserContent()',
		);
	});

	it('recent transcript is confined: confineUserContent(recent)', () => {
		assert.ok(
			SRC.includes('confineUserContent(recent)'),
			'voice context block must wrap `recent` transcript in confineUserContent()',
		);
	});

	it('context-drop content is confined: confineUserContent(content)', () => {
		assert.ok(
			SRC.includes('confineUserContent(content)'),
			'context-drop task body must wrap `content` in confineUserContent()',
		);
	});

	it('chat task description is confined: confineUserContent(taskDescription)', () => {
		assert.ok(
			SRC.includes('confineUserContent(taskDescription)'),
			'writeChatTask must wrap taskDescription in confineUserContent()',
		);
	});

	it('confineUserContent applied at all four sites', () => {
		const occurrences = (SRC.match(/confineUserContent\(/g) || []).length;
		// 1 definition + 4 call sites
		assert.ok(
			occurrences >= 5,
			`Expected ≥5 occurrences of confineUserContent( (1 def + 4 calls), got ${occurrences}`,
		);
	});

	it('HEADER_KEYS covers access_tier and source', () => {
		// The keys moved to the generated src/header_keys.ts, which task-bridge
		// imports; asserting the property where it lives, not where it was copied.
		assert.ok(
			GEN.includes("'access_tier'"),
			'confineUserContent must guard access_tier header key',
		);
		assert.ok(
			GEN.includes("'source'"),
			'confineUserContent must guard source header key',
		);
	});

	it('task-bridge derives the key set rather than restating it', () => {
		// Without this, deleting the import and pasting a literal back passes
		// every other assertion here — the generated file would still be right.
		assert.ok(
			SRC.includes("from './header_keys.js'"),
			'task-bridge.ts must import the generated HEADER_KEYS',
		);
		assert.ok(
			!/const _HEADER_KEYS = \[/.test(SRC),
			'task-bridge.ts must not carry a literal header-key array',
		);
	});

	it('FENCE_RE covers ===fence=== pattern', () => {
		assert.ok(
			SRC.includes('^={3,}'),
			'confineUserContent must guard ===fence=== pattern with ^={3,}',
		);
	});
});
