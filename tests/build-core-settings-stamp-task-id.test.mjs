// The 4th positional arg is the gmail-write-guard slot; stamp-task-id is the
// 5th. Passing '' keeps these cases scoped to PostToolUse.
// Both PostToolUse registrations must survive the merge: a plain object merge
// would drop one array, silently disabling either telemetry or task-ID stamping.
import { test } from 'node:test';
import assert from 'node:assert';
import { execFileSync } from 'node:child_process';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const REPO = join(dirname(fileURLToPath(import.meta.url)), '..');
const BUILDER = join(REPO, 'src', 'agent', 'claude', 'cli', 'build-core-settings.mjs');

const build = (...args) =>
	JSON.parse(execFileSync('node', [BUILDER, ...args], { encoding: 'utf8' }));

const postToolUse = (s) => s.hooks.PostToolUse || [];

test('the stamp-task-id hook is registered when its path is passed', () => {
	const s = build('/g/guard.py', '', '/t/telemetry.py', '', '/s/stamp-task-id.py');
	const cmds = postToolUse(s).flatMap((e) => e.hooks.map((h) => h.command));
	assert.ok(cmds.some((c) => c.includes('stamp-task-id.py')),
		`stamp hook absent from PostToolUse: ${JSON.stringify(postToolUse(s))}`);
});

test('registering it does NOT drop the skill-telemetry entry', () => {
	const s = build('/g/guard.py', '', '/t/telemetry.py', '', '/s/stamp-task-id.py');
	const cmds = postToolUse(s).flatMap((e) => e.hooks.map((h) => h.command));
	assert.ok(cmds.some((c) => c.includes('telemetry.py')), 'telemetry entry was clobbered');
	assert.strictEqual(postToolUse(s).length, 2, 'expected both PostToolUse entries');
});

test('the stamp matcher is unscoped — a result written by bash must stamp too', () => {
	const s = build('/g/guard.py', '', '/t/telemetry.py', '', '/s/stamp-task-id.py');
	const entry = postToolUse(s).find((e) => e.hooks.some((h) => h.command.includes('stamp-task-id')));
	assert.strictEqual(entry.matcher, '',
		`a tool-scoped matcher (${JSON.stringify(entry.matcher)}) would miss bash-written results`);
});

test('omitting the path leaves the settings exactly as before', () => {
	const withStamp = build('/g/guard.py', '', '/t/telemetry.py');
	assert.strictEqual(postToolUse(withStamp).length, 1);
	assert.ok(postToolUse(withStamp)[0].hooks[0].command.includes('telemetry.py'));
});

test('the guard PreToolUse entry is untouched by the new argument', () => {
	const s = build('/g/guard.py', '', '/t/telemetry.py', '', '/s/stamp-task-id.py');
	assert.strictEqual(s.hooks.PreToolUse.length, 1);
	assert.strictEqual(s.hooks.PreToolUse[0].matcher, 'AskUserQuestion');
});
