/**
 * voice-config-switch must restart voice-agent through the GUARDED wrapper
 * (impl plan amendment T4 — kill-path inventory), never a direct
 * `launchctl kickstart -k gui/<uid>/com.sutando.voice-agent`.
 *
 * `launchctl kickstart -k` is a kill-and-restart: the pre-kickstart
 * validation (identity of the running job pid) must run as ONE guarded
 * `voice-lock.py takeover` transaction, which is exactly what
 * scripts/restart-voice-agent.sh wraps — including fail-closed behavior
 * when no interpreter is available.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import type { spawn } from 'node:child_process';

import { fireGuardedRestart, GUARDED_RESTART_SCRIPT } from '../src/voice-config-switch.js';

test('fireGuardedRestart spawns the guarded wrapper, detached', () => {
	const calls: Array<{ cmd: string; args: readonly string[]; opts: Record<string, unknown> }> = [];
	let unrefd = false;
	const fakeSpawn = ((cmd: string, args: readonly string[], opts: Record<string, unknown>) => {
		calls.push({ cmd, args, opts });
		return { unref: () => { unrefd = true; } };
	}) as unknown as typeof spawn;

	fireGuardedRestart(fakeSpawn);

	assert.equal(calls.length, 1);
	assert.equal(calls[0].cmd, 'bash');
	assert.deepEqual([...calls[0].args], [GUARDED_RESTART_SCRIPT]);
	// Detached: the wrapper's takeover kills THIS process (the lock holder)
	// mid-script, so it must survive its parent.
	assert.equal(calls[0].opts.detached, true);
	assert.equal(calls[0].opts.stdio, 'ignore');
	assert.ok(unrefd, 'child must be unref()d so the agent can exit');
});

test('the wrapper path is the repo restart-voice-agent.sh and it exists', () => {
	assert.match(GUARDED_RESTART_SCRIPT, /scripts\/restart-voice-agent\.sh$/);
	assert.ok(existsSync(GUARDED_RESTART_SCRIPT), `${GUARDED_RESTART_SCRIPT} missing`);
	// And it really is the guarded wrapper: it runs the voice-lock.py takeover
	// transaction before its kickstart.
	const body = readFileSync(GUARDED_RESTART_SCRIPT, 'utf8');
	assert.match(body, /voice-lock\.py.*takeover|takeover \\/s);
});

test('no direct launchctl kickstart of voice-agent remains in the tool source', () => {
	const src = readFileSync(new URL('../src/voice-config-switch.ts', import.meta.url), 'utf8');
	// Comments may mention kickstart; an argv token list must not.
	assert.doesNotMatch(src, /spawn\(\s*'launchctl'/);
	assert.doesNotMatch(src, /'kickstart'/);
});
