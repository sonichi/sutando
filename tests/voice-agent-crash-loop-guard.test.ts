import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

// Guards against the 2026-07-29 orphan incident: a voice-agent whose spawning
// parent died lost its stdout/stderr; every console.* then raised, and the
// "log and stay alive" uncaughtException handler became an infinite
// throw→log→throw loop at 100% CPU while its pidfile blocked replacements.
// voice-agent.ts has import-time side effects (credential resolution,
// process.exit on missing keys), so like voice-agent-key-format.test.ts this
// asserts on source shape rather than importing the module.
const SRC = readFileSync(
	join(import.meta.dirname ?? '.', '..', 'src/voice-agent.ts'),
	'utf8',
);

describe('voice-agent crash-loop guards', () => {
	it('absorbs stdout/stderr stream errors instead of letting them go uncaught', () => {
		assert.match(SRC, /process\.stdout\.on\('error',\s*\(\)\s*=>\s*\{\}\)/);
		assert.match(SRC, /process\.stderr\.on\('error',\s*\(\)\s*=>\s*\{\}\)/);
	});

	it('rate-limits crash-handler invocations with a loop breaker', () => {
		assert.match(SRC, /function crashLoopBreaker\(/);
		// Breaker must fire before the handler logs, so a throwing log path
		// still trips it on the next iteration.
		const uncaught = SRC.match(/process\.on\('uncaughtException',[\s\S]*?\n\t\}\);/)?.[0];
		assert.ok(uncaught, 'uncaughtException handler present');
		const rejection = SRC.match(/process\.on\('unhandledRejection',[\s\S]*?\n\t\}\);/)?.[0];
		assert.ok(rejection, 'unhandledRejection handler present');
		for (const [name, body] of [['uncaughtException', uncaught], ['unhandledRejection', rejection]] as const) {
			assert.match(body, /crashLoopBreaker\(/, `${name} handler must call crashLoopBreaker`);
			assert.ok(
				body.indexOf('crashLoopBreaker(') < body.search(/safeError\(/),
				`${name} handler must call crashLoopBreaker before logging`,
			);
		}
	});

	it('crash handlers and shutdown log via throw-safe helpers, never bare console', () => {
		assert.match(SRC, /function safeLog\(/);
		assert.match(SRC, /function safeError\(/);
		const uncaught = SRC.match(/process\.on\('uncaughtException',[\s\S]*?\n\t\}\);/)?.[0] ?? '';
		const rejection = SRC.match(/process\.on\('unhandledRejection',[\s\S]*?\n\t\}\);/)?.[0] ?? '';
		const shutdown = SRC.match(/const shutdown = async \(\) => \{[\s\S]*?\n\t\};/)?.[0] ?? '';
		assert.ok(shutdown, 'shutdown present');
		for (const [name, body] of [['uncaughtException', uncaught], ['unhandledRejection', rejection], ['shutdown', shutdown]] as const) {
			assert.doesNotMatch(
				body,
				/(?<!safeError\(.*)\bconsole\.(log|error)\(/,
				`${name} must not call bare console.* — it throws when the parent's pipe is gone`,
			);
		}
	});

	it('shuts down when orphaned (reparented away from its spawning parent)', () => {
		assert.match(SRC, /const initialPpid = process\.ppid/);
		assert.match(SRC, /process\.ppid === initialPpid/);
		// launchd-managed deployments legitimately start with ppid 1 — the
		// watchdog must not fire for them.
		assert.match(SRC, /initialPpid !== 1/);
	});
});
