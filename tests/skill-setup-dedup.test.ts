// The loader runs at module scope, so each case imports inline-tools.ts in a
// CHILD process; a cached same-process import could not vary the scan roots.
import { test } from 'node:test';
import assert from 'node:assert';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, mkdirSync, writeFileSync, rmSync, existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname, delimiter } from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath, pathToFileURL } from 'node:url';

const REPO_ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const TSX_CLI = (() => {
	try {
		return createRequire(join(REPO_ROOT, 'package.json')).resolve('tsx/cli');
	} catch {
		return null;
	}
})();

/** Write one fixture skill that tags `globalThis.__setupCalls` when its setup() runs. */
function writeSkill(root: string, dirName: string, manifestName: string, marker: string): void {
	const dir = join(root, 'skills', dirName);
	mkdirSync(dir, { recursive: true });
	writeFileSync(
		join(dir, 'manifest.json'),
		JSON.stringify({ name: manifestName, enabled: true, tools: './tools.mjs' }),
	);
	writeFileSync(
		join(dir, 'tools.mjs'),
		`export const tools = [];\n` +
			`export function setup() { (globalThis.__setupCalls ??= []).push(${JSON.stringify(marker)}); }\n`,
	);
}

/**
 * Import the real inline-tools module with `roots` injected as external plugin
 * dirs, invoke every registered setup() hook, and return the markers that fired.
 */
function collectSetupEffects(roots: string[]): string[] {
	assert.ok(TSX_CLI);
	const driver = join(roots[0], '..', 'driver.mjs');
	writeFileSync(
		driver,
		`const m = await import(process.env.INLINE_TOOLS_URL);\n` +
			`for (const s of m.personalSkillSetups) s({ session: {}, injectText: () => {} });\n` +
			`console.log('__EFFECTS__' + JSON.stringify(globalThis.__setupCalls ?? []));\n`,
	);
	const out = execFileSync(process.execPath, [TSX_CLI, driver], {
		cwd: REPO_ROOT,
		encoding: 'utf8',
		stdio: ['ignore', 'pipe', 'pipe'],
		env: {
			...process.env,
			SUTANDO_EXTERNAL_PLUGIN_DIRS: roots.join(delimiter),
			INLINE_TOOLS_URL: pathToFileURL(join(REPO_ROOT, 'src', 'inline-tools.ts')).href,
		},
	});
	const line = out.split('\n').find(l => l.startsWith('__EFFECTS__'));
	assert.ok(line, `driver produced no __EFFECTS__ line; output was:\n${out}`);
	return JSON.parse(line.slice('__EFFECTS__'.length)) as string[];
}

test('setup() hooks: same skill in two roots registers once (last-write-wins); a distinct skill survives', t => {
	if (!TSX_CLI || !existsSync(TSX_CLI)) return t.skip('tsx binary not present (no node_modules)');

	const base = mkdtempSync(join(tmpdir(), 'skill-setup-dedup-'));
	const rootA = join(base, 'rootA');
	const rootB = join(base, 'rootB');
	try {
		// The SAME skill (identical manifest name) present in both scan roots...
		writeSkill(rootA, 'dup-skill', 'dup-skill', 'dup:A');
		writeSkill(rootB, 'dup-skill', 'dup-skill', 'dup:B');
		// ...alongside a DIFFERENT skill that also exports setup().
		writeSkill(rootB, 'other-skill', 'other-skill', 'other');

		const effects = collectSetupEffects([rootA, rootB]);
		const dupEffects = effects.filter(e => e.startsWith('dup:'));

		// The regression itself: pre-fix this was ['dup:A', 'dup:B'] — the one
		// skill's handler attached twice, so every session event fired it twice.
		assert.deepStrictEqual(
			dupEffects,
			['dup:B'],
			`the same skill scanned from two roots must register setup() exactly once, ` +
				`and the later root must win; got ${JSON.stringify(effects)}`,
		);
		// Collapsing duplicates must not collapse genuinely distinct skills.
		assert.deepStrictEqual(
			effects.filter(e => e === 'other'),
			['other'],
			`a distinct skill's setup() must survive dedup; got ${JSON.stringify(effects)}`,
		);
	} finally {
		rmSync(base, { recursive: true, force: true });
	}
});

test('setup() hooks: skill identity is the manifest name, not the directory name', t => {
	if (!TSX_CLI || !existsSync(TSX_CLI)) return t.skip('tsx binary not present (no node_modules)');

	const base = mkdtempSync(join(tmpdir(), 'skill-setup-identity-'));
	const rootA = join(base, 'rootA');
	const rootB = join(base, 'rootB');
	try {
		// One skill vendored under two different directory names (a real shape:
		// a sibling checkout renames the folder but keeps the manifest name).
		writeSkill(rootA, 'talk-highlight', 'talk-highlight', 'th:A');
		writeSkill(rootB, 'talk-highlight-dev', 'talk-highlight', 'th:B');

		const effects = collectSetupEffects([rootA, rootB]).filter(e => e.startsWith('th:'));
		assert.deepStrictEqual(
			effects,
			['th:B'],
			`same manifest name under different dir names is ONE skill; got ${JSON.stringify(effects)}`,
		);
	} finally {
		rmSync(base, { recursive: true, force: true });
	}
});
