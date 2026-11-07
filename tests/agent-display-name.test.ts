/**
 * src/agent-display-name.ts: the voice and phone prompts take the agent's name from the Python
 * resolver (src/agent_display_name.py), offline first, then the live answer once it lands.
 * Run: npx tsx --test tests/agent-display-name.test.ts
 */
import { test, beforeEach } from 'node:test';
import assert from 'node:assert';
import { existsSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

const mod = await import('../src/agent-display-name.js');
const { agentDisplayName, agentDisplayNameScriptPath, resolverRunner, resetAgentDisplayNameForTests, nameFromJson, LIVE_REFRESH_MS, FALLBACK_AGENT_NAME } = mod;
const REPO = join(fileURLToPath(new URL('.', import.meta.url)), '..');

function runner(offline: string | Error, live: string | null) {
	const calls: string[][] = [];
	let pending: ((out: string | null) => void)[] = [];
	return {
		calls,
		land() { const p = pending; pending = []; for (const done of p) done(live); },
		sync(args: string[]) { calls.push(args); if (offline instanceof Error) throw offline; return offline; },
		async(args: string[], done: (out: string | null) => void) { calls.push(args); pending.push(done); },
	};
}
const json = (name: string) => JSON.stringify({ name, source: 'x', agent_mxid: '' }) + '\n';

beforeEach(() => resetAgentDisplayNameForTests());

test('the first build uses the offline answer, later builds the live one', () => {
	const r = runner(json('Recorded Name'), json('Display Name X'));
	assert.strictEqual(agentDisplayName(r, 0), 'Recorded Name');
	assert.deepStrictEqual(r.calls, [['--json'], ['--offline', '--json']]);
	r.land();
	assert.strictEqual(agentDisplayName(r, 1), 'Display Name X');
	assert.strictEqual(r.calls.length, 2, 'no second resolve inside the refresh window');
});

test('the live resolve reruns after the refresh window, so a rename is picked up', () => {
	const r = runner(json('Old'), json('Renamed'));
	agentDisplayName(r, 0);
	agentDisplayName(r, LIVE_REFRESH_MS - 1);
	assert.strictEqual(r.calls.filter(c => c.length === 1).length, 1);
	agentDisplayName(r, LIVE_REFRESH_MS);
	assert.strictEqual(r.calls.filter(c => c.length === 1).length, 2);
	r.land();
	assert.strictEqual(agentDisplayName(r, LIVE_REFRESH_MS + 1), 'Renamed');
});

test('a failed resolver falls back to the resolver\'s own last tier; a failed live run keeps the offline name', () => {
	const broken = runner(new Error('python unavailable'), null);
	assert.strictEqual(agentDisplayName(broken, 0), FALLBACK_AGENT_NAME);
	resetAgentDisplayNameForTests();
	const r = runner(json('Recorded Name'), null);
	agentDisplayName(r, 0);
	r.land();
	assert.strictEqual(agentDisplayName(r, 1), 'Recorded Name');
	const throwing = { sync: () => json('Offline'), async: () => { throw new Error('spawn failed'); } };
	resetAgentDisplayNameForTests();
	assert.strictEqual(agentDisplayName(throwing, 0), 'Offline');
});

test('only a JSON name is accepted', () => {
	assert.strictEqual(nameFromJson(json('  A Name ')), 'A Name');
	for (const bad of [null, '', 'not json', '{"name": 3}', '{}']) assert.strictEqual(nameFromJson(bad), '');
});

test('the default runner runs the real resolver', () => {
	const tmp = mkdtempSync(join(tmpdir(), 'adn-ts-'));
	const keys = ['AGENT_MXID', 'SUTANDO_DISPLAY_NAME', 'CLAUDE_CONFIG_DIR'] as const;
	const saved = Object.fromEntries(keys.map(k => [k, process.env[k]]));
	try {
		Object.assign(process.env, { AGENT_MXID: '@not-an-mxid', SUTANDO_DISPLAY_NAME: 'Display Name X',
			CLAUDE_CONFIG_DIR: join(tmp, 'claude') });
		assert.strictEqual(agentDisplayName(), 'Display Name X');
	} finally {
		for (const k of keys) { if (saved[k] === undefined) delete process.env[k]; else process.env[k] = saved[k]; }
		rmSync(tmp, { recursive: true, force: true });
	}
});

test('the bundled layout (module in dist/) finds the resolver in src/', () => {
	const script = agentDisplayNameScriptPath(join(REPO, 'dist'));
	assert.strictEqual(script, join(REPO, 'src', 'agent_display_name.py'));
	assert.ok(existsSync(script));
	assert.strictEqual(agentDisplayNameScriptPath(join(REPO, 'src')), script);
});

test('a missing resolver script is warned about once, by cause, and falls back', () => {
	const warnings: string[] = [];
	const r = resolverRunner(join(REPO, 'dist', 'agent_display_name.py'), (m: string) => warnings.push(m));
	assert.strictEqual(agentDisplayName(r, 0), FALLBACK_AGENT_NAME);
	agentDisplayName(r, LIVE_REFRESH_MS);
	agentDisplayName(r, 2 * LIVE_REFRESH_MS);
	assert.strictEqual(warnings.length, 1, warnings.join('\n'));
	assert.match(warnings[0], /resolver script not found at .*agent_display_name\.py/);
});

test('a failing resolver is warned about once per distinct cause', () => {
	const tmp = mkdtempSync(join(tmpdir(), 'adn-fail-'));
	try {
		const script = join(tmp, 'agent_display_name.py');
		writeFileSync(script, 'import sys\nsys.exit(3)\n');
		const warnings: string[] = [];
		const r = resolverRunner(script, (m: string) => warnings.push(m));
		assert.throws(() => r.sync(['--offline', '--json']));
		assert.throws(() => r.sync(['--offline', '--json']));
		assert.deepStrictEqual(warnings, ['[agent-display-name] resolver exited with status 3; using the last known name or "Sutando"']);
	} finally {
		rmSync(tmp, { recursive: true, force: true });
	}
});
