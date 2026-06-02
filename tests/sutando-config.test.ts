/**
 * Tests for src/sutando_config.ts — TypeScript twin of the canonical
 * workspace + vault loader. Mirrors tests/sutando-config.test.py — both
 * languages must agree byte-for-byte on the resolved config so that Python
 * services (bridges, health-check) and TS services (voice-agent, task-bridge)
 * land in the same workspace.
 *
 * Cross-language consistency is the explicit Mini cold-review item #8: same
 * config → same workspace.path from py / ts. Add the same fixtures to both
 * sides; if either drifts, this test (along with its Python twin) flags it.
 *
 * Run: tsx --test tests/sutando-config.test.ts
 */
import { describe, it, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

import {
	detectEnvWorkspaceInDotenv,
	loadConfig,
	resetCacheForTests,
	resolveVault,
	resolveWorkspace,
} from '../src/sutando_config.js';

interface ConfigBody {
	[k: string]: unknown;
}

function makeRepo(): string {
	return mkdtempSync(join(tmpdir(), 'sutando-config-test-'));
}

function writeConfig(repo: string, name: string, body: ConfigBody | string): string {
	const path = join(repo, name);
	const content = typeof body === 'string' ? body : JSON.stringify(body, null, 2);
	writeFileSync(path, content, 'utf8');
	return path;
}

describe('sutando_config loader', () => {
	let savedEnv: string | undefined;
	let repo: string;

	beforeEach(() => {
		// Snapshot + clear env; reset per-process cache.
		savedEnv = process.env.SUTANDO_WORKSPACE;
		delete process.env.SUTANDO_WORKSPACE;
		resetCacheForTests();
		repo = makeRepo();
	});

	const restoreEnvAndRepo = () => {
		resetCacheForTests();
		delete process.env.SUTANDO_WORKSPACE;
		if (savedEnv !== undefined) process.env.SUTANDO_WORKSPACE = savedEnv;
		if (repo) rmSync(repo, { recursive: true, force: true });
	};

	// ------------------------------------------------------------------ //
	//  1. Env var precedence over .local.json                            //
	// ------------------------------------------------------------------ //

	it('env var takes precedence over .local.json', () => {
		writeConfig(repo, 'sutando.config.json', { workspace: { path: '${REPO_DIR}/workspace' } });
		writeConfig(repo, 'sutando.config.local.json', { workspace: { path: '/from/local' } });
		process.env.SUTANDO_WORKSPACE = '/from/env';
		try {
			const resolved = resolveWorkspace(repo);
			assert.equal(resolved, resolve('/from/env'));
		} finally {
			restoreEnvAndRepo();
		}
	});

	// ------------------------------------------------------------------ //
	//  2. Deep-merge: dicts merge, arrays REPLACE                        //
	// ------------------------------------------------------------------ //

	it('.local.json deep-merges dict subtrees', () => {
		writeConfig(repo, 'sutando.config.json', {
			workspace: { path: '${REPO_DIR}/workspace' },
			vault: { enabled: false, remote_url: '', interval_seconds: 1800 },
		});
		writeConfig(repo, 'sutando.config.local.json', {
			vault: { enabled: true, remote_url: 'https://vault.example/repo.git' },
		});
		try {
			const cfg = loadConfig(repo);
			const vault = cfg.vault as ConfigBody;
			assert.equal(vault.enabled, true);
			assert.equal(vault.remote_url, 'https://vault.example/repo.git');
			assert.equal(vault.interval_seconds, 1800); // unchanged-key survives
		} finally {
			restoreEnvAndRepo();
		}
	});

	it('.local.json replaces arrays wholesale (no union)', () => {
		writeConfig(repo, 'sutando.config.json', {
			vault: {
				sync: {
					include: ['notes/', 'memory/', 'skills/'],
					exclude: ['tasks/', 'logs/'],
				},
			},
		});
		writeConfig(repo, 'sutando.config.local.json', {
			vault: { sync: { include: ['notes/'] } },
		});
		try {
			const cfg = loadConfig(repo);
			const sync = (cfg.vault as ConfigBody).sync as ConfigBody;
			assert.deepEqual(sync.include, ['notes/']);
			assert.deepEqual(sync.exclude, ['tasks/', 'logs/']); // unchanged
		} finally {
			restoreEnvAndRepo();
		}
	});

	// ------------------------------------------------------------------ //
	//  3. ${REPO_DIR} expansion in values, NOT keys                      //
	// ------------------------------------------------------------------ //

	it('${REPO_DIR} expands in string values', () => {
		writeConfig(repo, 'sutando.config.json', { workspace: { path: '${REPO_DIR}/workspace' } });
		try {
			const cfg = loadConfig(repo);
			assert.equal((cfg.workspace as ConfigBody).path, `${repo}/workspace`);
		} finally {
			restoreEnvAndRepo();
		}
	});

	it('${REPO_DIR} as a key name is NOT expanded', () => {
		writeConfig(repo, 'sutando.config.json', {
			workspace: { path: '${REPO_DIR}/ws' },
			['${REPO_DIR}']: 'this key should not expand',
		});
		try {
			const cfg = loadConfig(repo);
			assert.ok('${REPO_DIR}' in cfg);
			assert.equal(cfg['${REPO_DIR}'], 'this key should not expand');
			assert.equal((cfg.workspace as ConfigBody).path, `${repo}/ws`);
		} finally {
			restoreEnvAndRepo();
		}
	});

	// ------------------------------------------------------------------ //
	//  4. _-prefixed comment keys stripped                                //
	// ------------------------------------------------------------------ //

	it('_-prefixed keys stripped at every depth', () => {
		writeConfig(repo, 'sutando.config.json', {
			_comment: 'this is documentation, not config',
			_another: { nested: 'also dropped' },
			workspace: { _comment: 'nested annotation', path: '/ws' },
		});
		try {
			const cfg = loadConfig(repo);
			assert.ok(!('_comment' in cfg));
			assert.ok(!('_another' in cfg));
			assert.ok(!('_comment' in (cfg.workspace as ConfigBody)));
			assert.equal((cfg.workspace as ConfigBody).path, '/ws');
		} finally {
			restoreEnvAndRepo();
		}
	});

	// ------------------------------------------------------------------ //
	//  5. Malformed JSON → Error naming the file                          //
	// ------------------------------------------------------------------ //

	it('malformed JSON throws with file path in message', () => {
		writeConfig(repo, 'sutando.config.json', '{ this is not JSON }');
		try {
			assert.throws(
				() => loadConfig(repo),
				(err: unknown) => {
					const msg = err instanceof Error ? err.message : String(err);
					return msg.includes('sutando.config.json') && /failed to parse/i.test(msg);
				},
			);
		} finally {
			restoreEnvAndRepo();
		}
	});

	it('non-object top-level throws with explanatory message', () => {
		writeConfig(repo, 'sutando.config.json', '[1, 2, 3]');
		try {
			assert.throws(() => loadConfig(repo), /must be a JSON object/);
		} finally {
			restoreEnvAndRepo();
		}
	});

	// ------------------------------------------------------------------ //
	//  6. Empty / missing .local.json treated as {}                       //
	// ------------------------------------------------------------------ //

	it('empty .local.json is treated as {}', () => {
		writeConfig(repo, 'sutando.config.json', { workspace: { path: '${REPO_DIR}/workspace' } });
		writeFileSync(join(repo, 'sutando.config.local.json'), '', 'utf8'); // zero bytes
		try {
			const cfg = loadConfig(repo);
			assert.equal((cfg.workspace as ConfigBody).path, `${repo}/workspace`);
		} finally {
			restoreEnvAndRepo();
		}
	});

	it('whitespace-only .local.json is treated as {}', () => {
		writeConfig(repo, 'sutando.config.json', { workspace: { path: '${REPO_DIR}/workspace' } });
		writeFileSync(join(repo, 'sutando.config.local.json'), '   \n\n  \n', 'utf8');
		try {
			const cfg = loadConfig(repo);
			assert.equal((cfg.workspace as ConfigBody).path, `${repo}/workspace`);
		} finally {
			restoreEnvAndRepo();
		}
	});

	it('missing .local.json is treated as {}', () => {
		writeConfig(repo, 'sutando.config.json', { workspace: { path: '/from/defaults' } });
		try {
			const cfg = loadConfig(repo);
			assert.equal((cfg.workspace as ConfigBody).path, '/from/defaults');
		} finally {
			restoreEnvAndRepo();
		}
	});

	// ------------------------------------------------------------------ //
	//  7. Cache reset across repo_root changes                            //
	// ------------------------------------------------------------------ //

	it('cache returns a fresh config when repo_root changes', () => {
		const repoA = join(repo, 'a');
		const repoB = join(repo, 'b');
		mkdirSync(repoA, { recursive: true });
		mkdirSync(repoB, { recursive: true });
		writeConfig(repoA, 'sutando.config.json', { workspace: { path: '/from/a' } });
		writeConfig(repoB, 'sutando.config.json', { workspace: { path: '/from/b' } });
		try {
			const cfgA = loadConfig(repoA);
			const cfgB = loadConfig(repoB);
			assert.equal((cfgA.workspace as ConfigBody).path, '/from/a');
			assert.equal((cfgB.workspace as ConfigBody).path, '/from/b');
		} finally {
			restoreEnvAndRepo();
		}
	});

	it('cache hit when repo_root unchanged; invalidated by reset', () => {
		writeConfig(repo, 'sutando.config.json', { workspace: { path: '/x' } });
		try {
			const first = loadConfig(repo);
			writeConfig(repo, 'sutando.config.json', { workspace: { path: '/y' } });
			const second = loadConfig(repo);
			assert.strictEqual(first, second); // cache hit → same object
			resetCacheForTests();
			const third = loadConfig(repo);
			assert.equal((third.workspace as ConfigBody).path, '/y');
		} finally {
			restoreEnvAndRepo();
		}
	});

	// ------------------------------------------------------------------ //
	//  8. resolveVault() safe defaults                                    //
	// ------------------------------------------------------------------ //

	it('resolveVault returns safe defaults when vault subtree absent', () => {
		writeConfig(repo, 'sutando.config.json', {});
		try {
			const vault = resolveVault(repo);
			assert.equal(vault.enabled, false);
			assert.equal(vault.remote_url, '');
			assert.deepEqual(vault.sync.include, []);
			assert.deepEqual(vault.sync.exclude, []);
			assert.equal(vault.interval_seconds, 1800);
		} finally {
			restoreEnvAndRepo();
		}
	});

	it('resolveVault propagates configured overrides', () => {
		writeConfig(repo, 'sutando.config.json', {
			vault: {
				enabled: true,
				remote_url: 'https://vault.example/x.git',
				sync: { include: ['notes/'], exclude: ['tasks/'] },
				interval_seconds: 600,
			},
		});
		try {
			const vault = resolveVault(repo);
			assert.equal(vault.enabled, true);
			assert.equal(vault.remote_url, 'https://vault.example/x.git');
			assert.deepEqual(vault.sync.include, ['notes/']);
			assert.deepEqual(vault.sync.exclude, ['tasks/']);
			assert.equal(vault.interval_seconds, 600);
		} finally {
			restoreEnvAndRepo();
		}
	});

	// ------------------------------------------------------------------ //
	//  Bonus: detectEnvWorkspaceInDotenv                                  //
	// ------------------------------------------------------------------ //

	it('detectEnvWorkspaceInDotenv finds the line', () => {
		writeConfig(repo, 'sutando.config.json', {});
		writeFileSync(join(repo, '.env'), 'SOMETHING_ELSE=foo\nSUTANDO_WORKSPACE=/from/dotenv\n', 'utf8');
		try {
			assert.equal(detectEnvWorkspaceInDotenv(repo), '/from/dotenv');
		} finally {
			restoreEnvAndRepo();
		}
	});

	it('detectEnvWorkspaceInDotenv handles quoted values', () => {
		writeConfig(repo, 'sutando.config.json', {});
		writeFileSync(join(repo, '.env'), 'SUTANDO_WORKSPACE="/quoted/path"\n', 'utf8');
		try {
			assert.equal(detectEnvWorkspaceInDotenv(repo), '/quoted/path');
		} finally {
			restoreEnvAndRepo();
		}
	});

	it('detectEnvWorkspaceInDotenv returns undefined when absent', () => {
		writeConfig(repo, 'sutando.config.json', {});
		writeFileSync(join(repo, '.env'), 'OTHER_VAR=foo\n', 'utf8');
		try {
			assert.equal(detectEnvWorkspaceInDotenv(repo), undefined);
		} finally {
			restoreEnvAndRepo();
		}
	});
});
