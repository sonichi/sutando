/**
 * Smoke tests for `scripts/sutando-config.sh` — the bash wrapper around the
 * canonical Python workspace loader. Verifies:
 *   1. `sutando-config.sh workspace` returns the same path as the Python loader
 *      (no shell-vs-py drift — Lucy's PR #1399 review nit re. wrapper coverage)
 *   2. `sutando-config.sh subdirs` returns the documented canonical list
 *   3. `sutando-config.sh bootstrap` creates exactly that subdir set,
 *      idempotently (second run is a no-op)
 *
 * Run: tsx --test tests/sutando-config-shell.test.ts
 */
import { describe, it, beforeEach, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { existsSync, mkdtempSync, readdirSync, rmSync, mkdirSync, statSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { tmpdir } from 'node:os';
import { fileURLToPath } from 'node:url';

const REPO_ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const SCRIPT = join(REPO_ROOT, 'scripts', 'sutando-config.sh');

function runShell(subcmd: string, ws: string): string {
	const proc = spawnSync('bash', [SCRIPT, subcmd], {
		env: { ...process.env, SUTANDO_WORKSPACE: ws, SUTANDO_TEST_MODE: '1' },
		encoding: 'utf-8',
	});
	if (proc.status !== 0) {
		throw new Error(`${subcmd} exit ${proc.status}: stderr=${proc.stderr}`);
	}
	return proc.stdout;
}

function runPyResolve(ws: string): string {
	const proc = spawnSync(
		'python3',
		['-c', 'import sys; sys.path.insert(0, ".."); from importlib import import_module; m = import_module("src.sutando_config"); print(m.resolve_workspace(), end="")'],
		{
			cwd: REPO_ROOT,
			env: { ...process.env, SUTANDO_WORKSPACE: ws, SUTANDO_TEST_MODE: '1' },
			encoding: 'utf-8',
		},
	);
	if (proc.status !== 0) {
		throw new Error(`py resolve_workspace exit ${proc.status}: stderr=${proc.stderr}`);
	}
	return proc.stdout;
}

let scratch: string;

beforeEach(() => {
	scratch = mkdtempSync(join(tmpdir(), 'sutando-config-shell-'));
});

afterEach(() => {
	try { rmSync(scratch, { recursive: true, force: true }); } catch {}
});

describe('sutando-config.sh wrapper', () => {
	it('workspace subcommand matches python loader (no shell-vs-py drift)', () => {
		const ws = join(scratch, 'ws');
		mkdirSync(ws, { recursive: true });
		const shellOut = runShell('workspace', ws);
		// Try the py path directly — but cope with src/ import contexts
		const proc = spawnSync(
			'python3',
			['-c', 'import sys; sys.path.insert(0, "."); from src.sutando_config import resolve_workspace; print(resolve_workspace(), end="")'],
			{ cwd: REPO_ROOT, env: { ...process.env, SUTANDO_WORKSPACE: ws, SUTANDO_TEST_MODE: '1' }, encoding: 'utf-8' },
		);
		assert.equal(proc.status, 0, `py loader failed: ${proc.stderr}`);
		assert.equal(shellOut, proc.stdout, 'shell wrapper diverges from py loader');
	});

	it('subdirs subcommand prints non-empty newline-separated list', () => {
		const out = runShell('subdirs', join(scratch, 'ws')).trim();
		const lines = out.split('\n').filter(Boolean);
		assert.ok(lines.length >= 5, `subdirs returned too few entries: ${lines.length}`);
		// Canonical set we DO expect (anchor — adding new subdirs is fine, removing these is breaking)
		for (const expected of ['state', 'tasks', 'results', 'notes', 'logs']) {
			assert.ok(lines.includes(expected), `subdirs missing canonical entry "${expected}"`);
		}
	});

	it('bootstrap creates every subdir from `subdirs` and is idempotent', () => {
		const ws = join(scratch, 'ws');
		mkdirSync(ws, { recursive: true });
		const subdirsOut = runShell('subdirs', ws).trim().split('\n').filter(Boolean);

		// First run — creates all
		runShell('bootstrap', ws);
		for (const d of subdirsOut) {
			assert.ok(existsSync(join(ws, d)), `bootstrap did not create "${d}"`);
		}

		// Snapshot directory state for strict idempotency comparison.
		// Sorted top-level + recursive-mtimes — if the second run mutates
		// anything (creates extras, touches mtimes, etc.) the snapshot diverges.
		const snapshot = (root: string) => {
			const out: Record<string, number> = {};
			const walk = (p: string) => {
				const stat = statSync(p);
				out[p.slice(root.length)] = stat.mtimeMs;
				if (stat.isDirectory()) {
					for (const child of readdirSync(p).sort()) {
						walk(join(p, child));
					}
				}
			};
			walk(root);
			return out;
		};
		const before = snapshot(ws);

		// Second run — must be a no-op (idempotent)
		const second = spawnSync('bash', [SCRIPT, 'bootstrap'], {
			env: { ...process.env, SUTANDO_WORKSPACE: ws, SUTANDO_TEST_MODE: '1' },
			encoding: 'utf-8',
		});
		assert.equal(second.status, 0, `second bootstrap exit ${second.status}: ${second.stderr}`);
		for (const d of subdirsOut) {
			assert.ok(existsSync(join(ws, d)), `bootstrap idempotency broken: "${d}" missing after second run`);
		}

		// Strict idempotency: the FULL snapshot (paths + mtimes) must be
		// identical across runs. `mkdir -p` is a no-op on existing dirs so
		// mtimes stay frozen — if `bootstrap` touches anything (creates an
		// extra path, chmods, writes a marker), the deep-equal diverges.
		// (Mini + Sutando-Pro PR #1399 round-3 catch: the prior assertion
		// only compared Object.keys, throwing away the mtime values the
		// snapshot collected — claim/code mismatch with the comment above.)
		const after = snapshot(ws);
		assert.deepEqual(
			after,
			before,
			'bootstrap second run mutated the workspace (path set or mtime changed)',
		);
	});
});
