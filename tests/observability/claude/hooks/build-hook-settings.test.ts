import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

// Builder is plain .mjs (run by start-cli.sh via `node <builder> <hook-path>`),
// so we exercise it exactly as the shell does: exec it and parse stdout.
const BUILDER = fileURLToPath(
	new URL('../../../../src/observability/claude/hooks/build-hook-settings.mjs', import.meta.url),
);

function build(hookPath: string): any {
	const out = execFileSync('node', [BUILDER, hookPath], { encoding: 'utf8' });
	return JSON.parse(out); // throws if the builder emitted invalid JSON
}

/** Evaluate the `command` string's shell quoting and return the path the shell
 *  would actually pass to `bash` — i.e. strip the leading `bash ` and let a real
 *  shell re-parse the quoted remainder. */
function shellParsedPath(command: string): string {
	const arg = command.replace(/^bash /, '');
	return execFileSync('/bin/bash', ['-c', `printf %s ${arg}`], { encoding: 'utf8' });
}

const ADVERSARIAL = [
	'/Users/john/ag2/sutando/src/observability/claude/hooks/obs-hook.sh',
	'/Users/o brien/my repo/hooks/obs-hook.sh', // spaces
	'/Users/o"brien/hooks/obs-hook.sh', // double quote
	"/Users/o'brien/hooks/obs-hook.sh", // single quote (POSIX '\'' escape)
	'/path/with$dollar/and`tick`/obs-hook.sh', // $ + backtick must not expand
];

const EXPECTED_EVENTS = [
	'UserPromptSubmit',
	'UserPromptExpansion',
	'MessageDisplay',
	'PreToolUse',
	'PostToolUse',
	'Stop',
	'SessionStart',
	'SessionEnd',
	'PreCompact',
	'Notification',
];

describe('build-hook-settings.mjs', () => {
	it('emits exactly the documented event keys', () => {
		const o = build('/x/obs-hook.sh');
		assert.deepEqual(Object.keys(o.hooks).sort(), [...EXPECTED_EVENTS].sort());
	});

	it('only PreToolUse/PostToolUse carry a matcher; lifecycle events do not', () => {
		const o = build('/x/obs-hook.sh');
		const withMatcher = Object.entries(o.hooks)
			.filter(([, v]: any) => v[0].matcher !== undefined)
			.map(([k]) => k);
		assert.deepEqual(withMatcher.sort(), ['PostToolUse', 'PreToolUse']);
		assert.equal((o.hooks.PreToolUse as any)[0].matcher, '*');
	});

	for (const p of ADVERSARIAL) {
		it(`round-trips a path through valid JSON + shell quoting: ${p}`, () => {
			const o = build(p);
			const cmd = o.hooks.PreToolUse[0].hooks[0].command as string;
			assert.match(cmd, /^bash '/); // POSIX single-quoted
			assert.equal(shellParsedPath(cmd), p); // shell sees the EXACT original path
		});
	}

	it('exits non-zero when no hook path is given', () => {
		assert.throws(() => execFileSync('node', [BUILDER], { encoding: 'utf8', stdio: 'pipe' }));
	});
});
