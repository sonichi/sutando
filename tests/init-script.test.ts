import { describe, it, beforeEach, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdirSync, mkdtempSync, writeFileSync, existsSync, readFileSync, rmSync, statSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { tmpdir } from 'node:os';
import { fileURLToPath } from 'node:url';

/**
 * Tests for src/init.sh — the auto-bootstrap + preflight script.
 *
 * Each test runs the actual shell script against a fresh tmpdir treated
 * as a synthetic Sutando repo (pointed at via SUTANDO_REPO). Runtime state
 * (logs, state, tasks, results, placeholder files, …) lives under the
 * workspace, not the repo, since #913 — so each run also gets a dedicated
 * SUTANDO_WORKSPACE dir, and Tier-1 assertions check there.
 */

const REPO_ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const INIT_SH = join(REPO_ROOT, 'src', 'init.sh');

interface RunResult { stdout: string; stderr: string; status: number | null }

function runInit(repoDir: string, mode?: '--auto' | '--preflight'): RunResult {
	const args = ['bash', INIT_SH];
	if (mode) args.push(mode);
	const proc = spawnSync(args[0]!, args.slice(1), {
		env: {
			...process.env,
			SUTANDO_REPO: repoDir,
			SUTANDO_WORKSPACE: join(repoDir, '.workspace'),
			HOME: repoDir + '/.fake-home',
		},
		encoding: 'utf-8',
	});
	return { stdout: proc.stdout, stderr: proc.stderr, status: proc.status };
}

let scratch: string;
let workspace: string;

beforeEach(() => {
	scratch = mkdtempSync(join(tmpdir(), 'sutando-init-'));
	workspace = join(scratch, '.workspace');
});

afterEach(() => {
	try { rmSync(scratch, { recursive: true, force: true }); } catch {}
});

describe('init.sh --auto (Tier 1: directories)', () => {
	it('creates every expected directory in an empty repo', () => {
		const out = runInit(scratch, '--auto');
		assert.equal(out.status, 0, `script exit non-zero: stderr=${out.stderr}`);
		for (const d of ['logs', 'state', 'tasks', 'results', 'results/archive', 'results/calls', 'notes', 'data']) {
			assert.equal(existsSync(join(workspace, d)), true, `expected directory ${d}`);
		}
	});

	it('is idempotent — second invocation does not error or recreate', () => {
		runInit(scratch, '--auto');
		const before = statSync(join(workspace, 'logs')).birthtimeMs;
		const second = runInit(scratch, '--auto');
		assert.equal(second.status, 0);
		const after = statSync(join(workspace, 'logs')).birthtimeMs;
		assert.equal(before, after, 'logs/ should not be recreated on second run');
	});
});

describe('init.sh --auto (Tier 1: placeholder files)', () => {
	it('does NOT create build_log.md at the repo (lives in workspace per contract)', () => {
		runInit(scratch, '--auto');
		// build_log.md is a workspace artifact, owned by workspace_default.py +
		// dashboard/health-check. init.sh seeding at repo would resurrect the
		// pre-2026-05-18 split-brain. Confirm absence at repo root.
		assert.equal(
			existsSync(join(scratch, 'build_log.md')),
			false,
			'build_log.md should not be seeded at the repo root',
		);
	});

	it('creates pending-questions.md with an empty placeholder', () => {
		runInit(scratch, '--auto');
		const body = readFileSync(join(workspace, 'pending-questions.md'), 'utf-8');
		assert.match(body, /^# Pending Questions/);
		assert.match(body, /none open/);
	});

	it('creates contextual-chips.json with a parseable shape', () => {
		runInit(scratch, '--auto');
		const body = readFileSync(join(workspace, 'contextual-chips.json'), 'utf-8');
		const parsed = JSON.parse(body);
		assert.equal(Array.isArray(parsed.chips), true);
		assert.equal(typeof parsed.ts, 'number');
	});

	it('creates core-status.json initialised to idle', () => {
		runInit(scratch, '--auto');
		const body = readFileSync(join(workspace, 'core-status.json'), 'utf-8');
		const parsed = JSON.parse(body);
		assert.equal(parsed.status, 'idle');
	});

	it('creates voice-state.json initialised to disconnected', () => {
		runInit(scratch, '--auto');
		const body = readFileSync(join(workspace, 'voice-state.json'), 'utf-8');
		const parsed = JSON.parse(body);
		assert.equal(parsed.connected, false);
	});

	it('does NOT clobber an existing contextual-chips.json', () => {
		mkdirSync(workspace, { recursive: true });
		writeFileSync(join(workspace, 'contextual-chips.json'), '{"chips":[{"label":"x","desc":"y"}],"ts":1}');
		runInit(scratch, '--auto');
		const body = readFileSync(join(workspace, 'contextual-chips.json'), 'utf-8');
		assert.match(body, /"label":"x"/);
	});
});

describe('init.sh --auto (Tier 1: crons.json copy)', () => {
	it('copies crons.example.json → crons.json when example exists and target is missing', () => {
		const exampleDir = join(scratch, 'skills', 'schedule-crons');
		mkdirSync(exampleDir, { recursive: true });
		writeFileSync(join(exampleDir, 'crons.example.json'), '[{"name":"foo","cron":"* * * * *"}]');
		runInit(scratch, '--auto');
		const body = readFileSync(join(exampleDir, 'crons.json'), 'utf-8');
		assert.match(body, /"foo"/);
	});

	it('does NOT copy when the target already exists', () => {
		const exampleDir = join(scratch, 'skills', 'schedule-crons');
		mkdirSync(exampleDir, { recursive: true });
		writeFileSync(join(exampleDir, 'crons.example.json'), '[{"name":"example"}]');
		writeFileSync(join(exampleDir, 'crons.json'), '[{"name":"my-custom"}]');
		runInit(scratch, '--auto');
		const body = readFileSync(join(exampleDir, 'crons.json'), 'utf-8');
		assert.match(body, /my-custom/);
	});

	it('skips silently when no example file exists (fresh template install case)', () => {
		const out = runInit(scratch, '--auto');
		assert.equal(out.status, 0);
		assert.equal(existsSync(join(scratch, 'skills/schedule-crons/crons.json')), false);
	});
});

describe('init.sh --preflight (Tier 2: missing-env detection)', () => {
	it('reports required=0/1 when .env is missing', () => {
		const out = runInit(scratch, '--preflight');
		assert.equal(out.status, 0);
		assert.match(out.stdout, /\[Preflight\] required=0\/1/);
	});

	it('reports required=1/1 when GEMINI_API_KEY is set', () => {
		writeFileSync(join(scratch, '.env'), 'GEMINI_API_KEY=fake-key-for-test\n');
		const out = runInit(scratch, '--preflight');
		assert.match(out.stdout, /\[Preflight\] required=1\/1/);
	});

	it('counts optional keys when set in .env', () => {
		writeFileSync(join(scratch, '.env'), [
			'GEMINI_API_KEY=k',
			'TWILIO_ACCOUNT_SID=t',
			'NGROK_DOMAIN=n',
		].join('\n') + '\n');
		const out = runInit(scratch, '--preflight');
		assert.match(out.stdout, /optional=2\/8/);
	});

	it('counts external Discord/Telegram envs at $HOME/.claude/channels/...', () => {
		writeFileSync(join(scratch, '.env'), 'GEMINI_API_KEY=k\n');
		const fakeHome = join(scratch, '.fake-home');
		mkdirSync(join(fakeHome, '.claude/channels/discord'), { recursive: true });
		mkdirSync(join(fakeHome, '.claude/channels/telegram'), { recursive: true });
		writeFileSync(join(fakeHome, '.claude/channels/discord/.env'), 'DISCORD_BOT_TOKEN=d\n');
		writeFileSync(join(fakeHome, '.claude/channels/telegram/.env'), 'TELEGRAM_BOT_TOKEN=t\n');
		const out = runInit(scratch, '--preflight');
		assert.match(out.stdout, /optional=2\/8/);
	});

	it('always emits a single [Preflight] summary line on stdout', () => {
		const out = runInit(scratch, '--preflight');
		const summaries = out.stdout.split('\n').filter(l => l.startsWith('[Preflight]'));
		assert.equal(summaries.length, 1);
	});
});

describe('init.sh argument parsing', () => {
	it('rejects unknown flags with a non-zero exit', () => {
		const proc = spawnSync('bash', [INIT_SH, '--bogus'], {
			env: { ...process.env, SUTANDO_REPO: scratch, HOME: scratch + '/.fake-home' },
			encoding: 'utf-8',
		});
		assert.notEqual(proc.status, 0);
		assert.match(proc.stderr + proc.stdout, /Usage:/);
	});

	it('runs both tiers by default (no flag)', () => {
		const out = runInit(scratch);
		assert.equal(out.status, 0);
		assert.equal(existsSync(join(workspace, 'logs')), true, 'Tier 1 should have created logs/');
		assert.match(out.stdout, /\[Preflight\]/, 'Tier 2 should have emitted summary line');
	});
});
