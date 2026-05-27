/**
 * Regression guard: discord-voice operational log must use a single WriteStream
 * opened at module init, not appendFileSync+mkdirSync on every console.log call.
 *
 * Background (sonichi/sutando#1053): the previous appendOperationalLog() ran
 * mkdirSync + appendFileSync on EVERY log line — both block the event loop and
 * the syscall cost is non-trivial on a turn-latency-sensitive voice session.
 * The fix opens one WriteStream at init; appendOperationalLog writes to it.
 *
 * These are structural source tests — they verify the fix shape without needing
 * to import the full discord-voice-server (which has complex module-level side
 * effects and requires Discord.js / Gemini / prism-media runtime deps).
 *
 * Guards:
 *  1. `createWriteStream` is imported from node:fs
 *  2. `_opLogStream` WriteStream is declared at module scope
 *  3. `mkdirSync(dirname(DISCORD_VOICE_LOG))` is hoisted to the init block (not in appendOperationalLog)
 *  4. `appendOperationalLog` uses `_opLogStream?.write` (non-blocking)
 *  5. `appendOperationalLog` does NOT contain `appendFileSync` (blocking path removed)
 *  6. `appendOperationalLog` does NOT contain `mkdirSync` (per-call syscall removed)
 */

import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const SRC = readFileSync(
	join(__dirname, '../skills/discord-voice/scripts/discord-voice-server.ts'),
	'utf8',
);

// Extract the appendOperationalLog function body for targeted assertions.
const fnMatch = SRC.match(
	/function appendOperationalLog\([\s\S]*?\n\}/,
);
const FN_BODY = fnMatch ? fnMatch[0] : '';

describe('discord-voice operational log stream (#1053)', () => {
	it('imports createWriteStream from node:fs', () => {
		assert.match(
			SRC,
			/import\s*\{[^}]*createWriteStream[^}]*\}\s*from\s*['"]node:fs['"]/,
			'createWriteStream must be imported from node:fs',
		);
	});

	it('declares _opLogStream WriteStream at module scope', () => {
		assert.ok(
			SRC.includes('_opLogStream: WriteStream | null'),
			'_opLogStream must be declared as WriteStream | null at module scope',
		);
	});

	it('hoists mkdirSync(dirname(DISCORD_VOICE_LOG)) to the init block', () => {
		// The call must appear OUTSIDE appendOperationalLog — i.e. before the
		// function declaration in the source.
		const fnStart = SRC.indexOf('function appendOperationalLog(');
		const mkdirPos = SRC.indexOf('mkdirSync(dirname(DISCORD_VOICE_LOG)');
		assert.ok(mkdirPos >= 0, 'mkdirSync(dirname(DISCORD_VOICE_LOG)) must be present in source');
		assert.ok(
			mkdirPos < fnStart,
			'mkdirSync(dirname(DISCORD_VOICE_LOG)) must be hoisted BEFORE appendOperationalLog — not inside it',
		);
	});

	it('appendOperationalLog uses _opLogStream?.write (non-blocking)', () => {
		assert.ok(FN_BODY, 'appendOperationalLog function not found in source');
		assert.ok(
			FN_BODY.includes('_opLogStream?.write('),
			'appendOperationalLog must write via _opLogStream?.write() for non-blocking I/O',
		);
	});

	it('appendOperationalLog does not call appendFileSync (blocking path removed)', () => {
		assert.ok(FN_BODY, 'appendOperationalLog function not found in source');
		assert.ok(
			!FN_BODY.includes('appendFileSync'),
			'appendOperationalLog must NOT call appendFileSync — blocking sync I/O on hot log path',
		);
	});

	it('appendOperationalLog does not call mkdirSync (per-call syscall removed)', () => {
		assert.ok(FN_BODY, 'appendOperationalLog function not found in source');
		assert.ok(
			!FN_BODY.includes('mkdirSync'),
			'appendOperationalLog must NOT call mkdirSync — dir creation is hoisted to module init',
		);
	});
});
