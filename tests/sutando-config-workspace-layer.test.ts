/**
 * TS twin of tests/sutando-config-workspace-layer.test.py: the
 * `<workspace>/sutando.config.local.json` layer must resolve identically here.
 *
 * Run: tsx --test tests/sutando-config-workspace-layer.test.ts
 */
import { describe, it, beforeEach, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { chmodSync, mkdirSync, mkdtempSync, realpathSync, rmSync, symlinkSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { loadConfig, resetCacheForTests, resolveVault, resolveWorkspace } from '../src/sutando_config.js';

const ENV_KEYS = ['SUTANDO_WORKSPACE', 'SUTANDO_TEST_MODE', 'SUTANDO_DEFAULT_WORKSPACE'] as const;

function write(path: string, body: unknown): void {
	writeFileSync(path, typeof body === 'string' ? body : JSON.stringify(body), 'utf8');
}

describe('workspace config layer (TS twin)', () => {
	let base: string;
	let repo: string;
	let ws: string;
	const saved: Record<string, string | undefined> = {};

	beforeEach(() => {
		for (const k of ENV_KEYS) {
			saved[k] = process.env[k];
			delete process.env[k];
		}
		resetCacheForTests();
		base = realpathSync(mkdtempSync(join(tmpdir(), 'sutando-ws-layer-')));
		repo = join(base, 'engine');
		ws = join(base, 'durable-workspace');
		mkdirSync(repo);
		mkdirSync(ws);
		symlinkSync(ws, join(repo, 'workspace'));
		write(join(repo, 'sutando.config.json'), {
			workspace: { path: '${REPO_DIR}/workspace' },
			core: { runtime: 'claude', effort: 'low' },
			vault: { remote_url: '', sync: { include: ['notes/'], exclude: ['tasks/'] } },
		});
	});

	afterEach(() => {
		resetCacheForTests();
		for (const k of ENV_KEYS) {
			if (saved[k] === undefined) delete process.env[k];
			else process.env[k] = saved[k];
		}
		rmSync(base, { recursive: true, force: true });
	});

	it('absent layer changes nothing', () => {
		assert.deepEqual(loadConfig(repo).core, { runtime: 'claude', effort: 'low' });
	});

	it('precedence: workspace > repo-local > tracked', () => {
		write(join(repo, 'sutando.config.local.json'), { core: { runtime: 'codex', effort: 'high' } });
		write(join(ws, 'sutando.config.local.json'), { core: { effort: 'max' } });
		assert.deepEqual(loadConfig(repo).core, { runtime: 'codex', effort: 'max' });
	});

	it('exclude_extra from the layer stays additive', () => {
		write(join(ws, 'sutando.config.local.json'), { vault: { sync: { exclude_extra: ['notes/private/'] } } });
		assert.deepEqual(resolveVault(repo).sync.exclude, ['tasks/', 'notes/private/']);
	});

	it('drops a workspace key and keeps resolving the same workspace', () => {
		write(join(ws, 'sutando.config.local.json'), { workspace: { path: '/somewhere/else' }, core: { effort: 'max' } });
		const cfg = loadConfig(repo);
		assert.equal((cfg.workspace as { path: string }).path, `${repo}/workspace`);
		assert.equal((cfg.core as { effort: string }).effort, 'max');
		resetCacheForTests();
		assert.equal(resolveWorkspace(repo), join(repo, 'workspace'));
	});

	it('malformed layer throws naming the file', () => {
		write(join(ws, 'sutando.config.local.json'), '{"vault": ');
		assert.throws(() => loadConfig(repo), /durable-workspace\/sutando\.config\.local\.json|workspace\/sutando\.config\.local\.json/);
	});

	it('unreadable layer throws naming the file', { skip: process.getuid?.() === 0 }, () => {
		const layer = join(ws, 'sutando.config.local.json');
		write(layer, { core: { effort: 'max' } });
		chmodSync(layer, 0);
		try {
			assert.throws(() => loadConfig(repo), (e: Error) => e.message.startsWith(`sutando config: cannot read ${join(repo, 'workspace', 'sutando.config.local.json')}:`));
		} finally {
			chmodSync(layer, 0o600);
		}
	});

	it('an unknown key is reported against the workspace file', () => {
		write(join(ws, 'sutando.config.local.json'), { vualt: {} });
		const orig = process.stderr.write.bind(process.stderr);
		let err = '';
		process.stderr.write = ((chunk: string | Uint8Array) => {
			err += String(chunk);
			return true;
		}) as typeof process.stderr.write;
		try {
			loadConfig(repo);
		} finally {
			process.stderr.write = orig;
		}
		assert.ok(err.includes(`${join(repo, 'workspace', 'sutando.config.local.json')} has top-level keys`), err);
		assert.ok(!err.includes(`${join(repo, 'sutando.config.json')} has top-level keys`), err);
	});

	it('${REPO_DIR} in the layer is the repo root', () => {
		write(join(ws, 'sutando.config.local.json'), { vault: { remote_url: '${REPO_DIR}/x' } });
		assert.equal((loadConfig(repo).vault as { remote_url: string }).remote_url, `${repo}/x`);
	});

	it('scalar block in the layer is rejected like repo-local', () => {
		write(join(ws, 'sutando.config.local.json'), '{"vault": "nope"}');
		assert.throws(() => loadConfig(repo), /key 'vault' must be a JSON object/);
	});

	it('workspace at the repo root reads the local file once', () => {
		write(join(repo, 'sutando.config.local.json'), { workspace: { path: '${REPO_DIR}' }, core: { effort: 'high' } });
		assert.equal((loadConfig(repo).workspace as { path: string }).path, repo);
	});

	it('layer follows SUTANDO_DEFAULT_WORKSPACE when config names none', () => {
		write(join(repo, 'sutando.config.json'), {});
		const other = join(base, 'embedder-ws');
		mkdirSync(other);
		write(join(other, 'sutando.config.local.json'), { core: { effort: 'max' } });
		process.env.SUTANDO_DEFAULT_WORKSPACE = other;
		assert.equal((loadConfig(repo).core as { effort: string }).effort, 'max');
	});
});
