import { test } from 'node:test';
import assert from 'node:assert';
import { spawnSync } from 'node:child_process';
import { accessSync, constants, existsSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
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

test('sibling skills require explicit plugin configuration', t => {
	if (!TSX_CLI || !existsSync(TSX_CLI)) return t.skip('tsx binary not present (no node_modules)');

	const siblingsRoot = dirname(REPO_ROOT);
	try {
		accessSync(siblingsRoot, constants.W_OK);
	} catch {
		return t.skip(`cannot create sibling fixture: ${siblingsRoot} is not writable`);
	}

	const fixture = join(siblingsRoot, `sutando-sibling-fixture-${process.pid}`);
	const scratch = mkdtempSync(join(tmpdir(), 'inline-tools-sibling-'));
	const marker = join(scratch, 'loaded');
	const skill = join(fixture, 'skills', 'sibling-probe');
	const driver = join(scratch, 'driver.mjs');
	try {
		mkdirSync(join(skill), { recursive: true });
		writeFileSync(join(skill, 'manifest.json'), JSON.stringify({
			name: 'sibling-probe', enabled: true, tools: './tools.mjs',
		}));
		writeFileSync(join(skill, 'tools.mjs'),
			`import { writeFileSync } from 'node:fs';\n` +
			`writeFileSync(process.env.SIBLING_MARKER, 'loaded');\n` +
			`export const tools = [{ name: 'sibling_probe', description: 'fixture' }];\n`);
		writeFileSync(driver, `await import(process.env.INLINE_TOOLS_URL);\n`);

		const run = (external: boolean): string => {
			const env: NodeJS.ProcessEnv = {
				...process.env,
				SUTANDO_TEST_MODE: '1',
				SUTANDO_WORKSPACE: join(scratch, 'workspace'),
				SUTANDO_MEMORY_DIR: join(scratch, 'memory'),
				CLAUDE_CONFIG_DIR: join(scratch, 'claude-home'),
				REMOTE_TASK_TOKEN: '',
				AG2_REMOTE_TOKEN: '',
				SIBLING_MARKER: marker,
				INLINE_TOOLS_URL: pathToFileURL(join(REPO_ROOT, 'src', 'inline-tools.ts')).href,
			};
			delete env.SUTANDO_EXTERNAL_PLUGIN_DIRS;
			if (external) env.SUTANDO_EXTERNAL_PLUGIN_DIRS = fixture;
			const result = spawnSync(process.execPath, [TSX_CLI!, driver], {
				cwd: REPO_ROOT,
				encoding: 'utf8',
				stdio: ['ignore', 'pipe', 'pipe'],
				env,
			});
			assert.equal(result.status, 0, `${result.stdout}${result.stderr}`);
			return `${result.stdout}${result.stderr}`;
		};

		const implicitOutput = run(false);
		assert.equal(existsSync(marker), false, 'an unlisted sibling must not import executable tools');
		assert.match(implicitOutput, /not loading .*sibling-probe|not loading .*sutando-sibling-fixture/);

		rmSync(marker, { force: true });
		run(true);
		assert.equal(existsSync(marker), true, 'an explicitly listed sibling remains loadable');
	} finally {
		rmSync(fixture, { recursive: true, force: true });
		rmSync(scratch, { recursive: true, force: true });
	}
});
