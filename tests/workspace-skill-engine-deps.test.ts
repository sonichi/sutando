// A workspace skill's manifest tools load like a shipped skill's: a bare import of an engine
// dependency resolves even when the skill's real path has no node_modules above it.
import { test } from 'node:test';
import assert from 'node:assert';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, mkdirSync, writeFileSync, rmSync, existsSync, symlinkSync, realpathSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { allowEngineDependencies } from '../src/skill-dependency-resolve.ts';

const REPO_ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const TSX_CLI = (() => {
	try { return createRequire(join(REPO_ROOT, 'package.json')).resolve('tsx/cli'); } catch { return null; }
})();

function writeSkill(dir: string, tool: string, dep: string, esm: boolean): void {
	mkdirSync(dir, { recursive: true });
	writeFileSync(join(dir, 'manifest.json'), JSON.stringify({ name: tool, enabled: true, tools: './tools.ts' }));
	if (esm) writeFileSync(join(dir, 'package.json'), JSON.stringify({ type: 'module' }));
	writeFileSync(join(dir, 'tools.ts'),
		`import * as dep from '${dep}';\n` +
		`export const tools = [{ name: '${tool}', description: typeof dep, parameters: {}, execution: 'inline', execute: async () => ({}) }];\n`);
}

function loadedToolNames(workspace: string): { names: string[]; log: string } {
	const driver = join(workspace, '..', 'driver.mjs');
	writeFileSync(driver,
		`const m = await import(process.env.INLINE_TOOLS_URL);\n` +
		`console.log('__TOOLS__' + JSON.stringify([...m.envDependentToolNames]));\n`);
	const env: NodeJS.ProcessEnv = {
		...process.env,
		SUTANDO_TEST_MODE: '1',
		SUTANDO_WORKSPACE: workspace,
		INLINE_TOOLS_URL: pathToFileURL(join(REPO_ROOT, 'src', 'inline-tools.ts')).href,
	};
	for (const k of ['SUTANDO_EXTERNAL_PLUGIN_DIRS', 'SUTANDO_MEMORY_DIR', 'SUTANDO_PRIVATE_DIR', 'SUTANDO_WORKSPACE_DIR']) delete env[k];
	const out = execFileSync(process.execPath, [TSX_CLI!, driver], { cwd: REPO_ROOT, encoding: 'utf8', env, stdio: ['ignore', 'pipe', 'pipe'] });
	const line = out.split('\n').find(l => l.startsWith('__TOOLS__'));
	assert.ok(line, `driver printed no tool list:\n${out}`);
	return { names: JSON.parse(line.slice('__TOOLS__'.length)), log: out };
}

test('a workspace skill imports engine dependencies from ESM and CommonJS tool files', t => {
	if (!TSX_CLI || !existsSync(TSX_CLI)) return t.skip('tsx not present (no node_modules)');
	const base = realpathSync(mkdtempSync(join(tmpdir(), 'ws-skill-deps-')));
	try {
		const elsewhere = join(base, 'other-checkout', 'skills');
		writeSkill(join(elsewhere, 'fixture-esm'), 'fixture_esm_tool', 'zod', true);
		writeSkill(join(elsewhere, 'fixture-cjs'), 'fixture_cjs_tool', 'zod', false);
		writeSkill(join(elsewhere, 'fixture-missing'), 'fixture_missing_tool', 'no-such-package-in-the-engine', true);
		const ws = join(base, 'workspace');
		mkdirSync(join(ws, 'skills'), { recursive: true });
		for (const n of ['fixture-esm', 'fixture-cjs', 'fixture-missing']) symlinkSync(join(elsewhere, n), join(ws, 'skills', n));

		const { names, log } = loadedToolNames(ws);
		assert.ok(names.includes('fixture_esm_tool'), `ESM workspace skill tools missing:\n${log}`);
		assert.ok(names.includes('fixture_cjs_tool'), `CommonJS workspace skill tools missing:\n${log}`);
		assert.ok(!names.includes('fixture_missing_tool'), 'a dependency the engine lacks still fails');
	} finally {
		rmSync(base, { recursive: true, force: true });
	}
});

test('without in-thread module hooks the fallback reports false', () => {
	const dir = realpathSync(mkdtempSync(join(tmpdir(), 'ws-skill-nohooks-')));
	try {
		assert.strictEqual(allowEngineDependencies(REPO_ROOT, dir, {}), false);
	} finally {
		rmSync(dir, { recursive: true, force: true });
	}
});

test('a failed hook installation reports false and a later call still installs', () => {
	const dir = realpathSync(mkdtempSync(join(tmpdir(), 'ws-skill-failhooks-')));
	try {
		const failing = { registerHooks: () => { throw new Error('hooks unavailable'); } };
		assert.strictEqual(allowEngineDependencies(REPO_ROOT, dir, failing), false);
		let installs = 0;
		const recording = { registerHooks: () => { installs += 1; } };
		assert.strictEqual(allowEngineDependencies(REPO_ROOT, dir, recording), true);
		assert.strictEqual(installs, 1, 'a failed install must not mark the hook as installed');
	} finally {
		rmSync(dir, { recursive: true, force: true });
	}
});
