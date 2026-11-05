// platform.notify on macOS asks resolveMacosNotifications() (notifications.macos), off by default.
// Run: npx tsx --test tests/macos-notifications-opt-in.test.ts
import assert from 'node:assert/strict';
import childProcess from 'node:child_process';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { syncBuiltinESMExports } from 'node:module';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterEach, beforeEach, it, mock } from 'node:test';
import { notify } from '../src/platform.js';
import { loadConfig, resetCacheForTests, resolveMacosNotifications } from '../src/sutando_config.js';

const platform = Object.getOwnPropertyDescriptor(process, 'platform')!;
const savedTestMode = process.env.SUTANDO_TEST_MODE;
let spawned: string[] = [];

/** A config root whose local file holds `local`, raw text when it is a string. */
function configRoot(local?: unknown): string {
	const root = mkdtempSync(join(tmpdir(), 'macos-notif-'));
	writeFileSync(join(root, 'sutando.config.json'), JSON.stringify({ workspace: { path: join(root, 'ws') } }));
	if (local !== undefined) {
		writeFileSync(join(root, 'sutando.config.local.json'), typeof local === 'string' ? local : JSON.stringify(local));
	}
	return root;
}

beforeEach(() => {
	spawned = [];
	resetCacheForTests();
	delete process.env.SUTANDO_TEST_MODE;
	Object.defineProperty(process, 'platform', { value: 'darwin' });
	mock.method(childProcess, 'execFileSync', (file: string) => { spawned.push(file); return Buffer.alloc(0); });
	mock.method(console, 'error', () => {});
	syncBuiltinESMExports();
});

afterEach(() => {
	mock.restoreAll();
	syncBuiltinESMExports();
	Object.defineProperty(process, 'platform', platform);
	if (savedTestMode === undefined) delete process.env.SUTANDO_TEST_MODE;
	else process.env.SUTANDO_TEST_MODE = savedTestMode;
	resetCacheForTests();
});

it('no notifications key: off, and notify spawns no osascript', () => {
	loadConfig(configRoot());
	assert.equal(resolveMacosNotifications(), false);
	notify('Voice is offline', 'Sutando — voice offline');
	assert.deepEqual(spawned, []);
});

it('notifications.macos true: on, and notify runs osascript', () => {
	loadConfig(configRoot({ notifications: { macos: true } }));
	assert.equal(resolveMacosNotifications(), true);
	notify('Voice is back online');
	assert.deepEqual(spawned, ['/usr/bin/osascript']);
});

for (const value of [false, 'true', 1, null, { enabled: true }]) {
	it(`notifications.macos ${JSON.stringify(value)}: off`, () => {
		const root = configRoot({ notifications: { macos: value } });
		assert.equal(resolveMacosNotifications(root), false);
	});
}

it('an unreadable config is off and does not throw', () => {
	const root = configRoot('{"notifications": {"macos": true');
	assert.equal(resolveMacosNotifications(root), false);
	notify('x');
	assert.deepEqual(spawned, []);
});
