// platform.notify under SUTANDO_TEST_MODE=1 must not spawn a native notifier: spawned-agent tests
// otherwise pop real banners on the developer's desktop. Run: npx tsx --test tests/platform-notify-test-mode.test.ts
import assert from 'node:assert/strict';
import childProcess from 'node:child_process';
import { syncBuiltinESMExports } from 'node:module';
import { afterEach, beforeEach, it, mock } from 'node:test';
import { notify } from '../src/platform.js';

const platform = Object.getOwnPropertyDescriptor(process, 'platform')!;
const savedTestMode = process.env.SUTANDO_TEST_MODE;
let spawned: string[] = [];
let logged: string[] = [];

beforeEach(() => {
	spawned = [];
	logged = [];
	mock.method(childProcess, 'execFileSync', (file: string) => { spawned.push(file); return Buffer.alloc(0); });
	mock.method(childProcess, 'spawnSync', (file: string) => { spawned.push(file); return { status: 0 }; });
	mock.method(console, 'error', (...args: unknown[]) => { logged.push(args.join(' ')); });
	syncBuiltinESMExports();
});

afterEach(() => {
	mock.restoreAll();
	syncBuiltinESMExports();
	Object.defineProperty(process, 'platform', platform);
	if (savedTestMode === undefined) delete process.env.SUTANDO_TEST_MODE;
	else process.env.SUTANDO_TEST_MODE = savedTestMode;
});

for (const os of ['darwin', 'win32', 'linux'] as const) {
	it(`${os}: SUTANDO_TEST_MODE=1 spawns no notifier and logs the suppressed banner`, () => {
		Object.defineProperty(process, 'platform', { value: os });
		process.env.SUTANDO_TEST_MODE = '1';
		notify('Voice is offline', 'Sutando — voice offline');
		assert.deepEqual(spawned, [], `notify spawned ${spawned.join(', ')} under test mode`);
		assert.equal(logged.length, 1);
		assert.match(logged[0], /SUTANDO_TEST_MODE=1/);
		assert.match(logged[0], /Sutando — voice offline: Voice is offline/);
	});
}

// Control: the fence keys on exactly '1', as every other SUTANDO_TEST_MODE check does.
for (const value of [undefined, '0', 'true']) {
	it(`darwin: SUTANDO_TEST_MODE=${value ?? '(unset)'} still fires osascript`, () => {
		Object.defineProperty(process, 'platform', { value: 'darwin' });
		if (value === undefined) delete process.env.SUTANDO_TEST_MODE;
		else process.env.SUTANDO_TEST_MODE = value;
		notify('Voice is back online');
		assert.deepEqual(spawned, ['/usr/bin/osascript']);
		assert.deepEqual(logged, []);
	});
}
