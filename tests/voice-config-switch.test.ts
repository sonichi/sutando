// Tests for src/voice-config-switch.ts — runtime voice-model preset switching.
//
// The tool writes a config file under $SUTANDO_WORKSPACE/config/voice-agent.json
// and schedules a launchctl kickstart (detached, 1.5s delay). Tests set
// SUTANDO_WORKSPACE to a tmp dir so no real config is touched. The launchctl
// call fires after each test completes but is harmless in test contexts:
// spawn is detached + stdio:ignore, the child is unref()'d, and launchctl
// will find no matching agent to restart.

import { describe, it, after } from 'node:test';
import assert from 'node:assert/strict';
import { mkdirSync, existsSync, readFileSync, rmSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';

const TMP = join(tmpdir(), `sutando-voice-switch-test-${Date.now()}`);
mkdirSync(TMP, { recursive: true });
process.env.SUTANDO_WORKSPACE = TMP;

const { switchVoiceConfigTool } = await import('../src/voice-config-switch.js');
const { VOICE_CONFIG_DEFAULTS } = await import('../src/voice-config.js');

after(() => {
	try { rmSync(TMP, { recursive: true, force: true }); } catch { /* best-effort */ }
});

const CONFIG_PATH = join(TMP, 'config', 'voice-agent.json');

function readConfig(): Record<string, unknown> {
	return JSON.parse(readFileSync(CONFIG_PATH, 'utf-8'));
}

// ── tool metadata ─────────────────────────────────────────────────────────────

describe('switchVoiceConfigTool metadata', () => {
	it('has name switch_voice_config', () => {
		assert.equal(switchVoiceConfigTool.name, 'switch_voice_config');
	});

	it('has execution inline', () => {
		assert.equal(switchVoiceConfigTool.execution, 'inline');
	});

	it('has a description string', () => {
		assert.ok(typeof switchVoiceConfigTool.description === 'string');
		assert.ok(switchVoiceConfigTool.description.length > 0);
	});
});

// ── search preset ─────────────────────────────────────────────────────────────

describe('execute — search preset', { concurrency: 1 }, () => {
	it('returns ok:true for search preset', async () => {
		const result = await switchVoiceConfigTool.execute({ preset: 'search' }, null) as Record<string, unknown>;
		assert.equal(result.ok, true);
	});

	it('returns preset:search in result', async () => {
		const result = await switchVoiceConfigTool.execute({ preset: 'search' }, null) as Record<string, unknown>;
		assert.equal(result.preset, 'search');
	});

	it('returns googleSearch:true for search preset', async () => {
		const result = await switchVoiceConfigTool.execute({ preset: 'search' }, null) as Record<string, unknown>;
		assert.equal(result.googleSearch, true);
	});

	it('returns a model string for search preset', async () => {
		const result = await switchVoiceConfigTool.execute({ preset: 'search' }, null) as Record<string, unknown>;
		assert.ok(typeof result.model === 'string' && (result.model as string).length > 0);
	});

	it('returns a summary string for search preset', async () => {
		const result = await switchVoiceConfigTool.execute({ preset: 'search' }, null) as Record<string, unknown>;
		assert.ok(typeof result.summary === 'string' && (result.summary as string).length > 0);
	});

	it('writes config file at $SUTANDO_WORKSPACE/config/voice-agent.json', async () => {
		await switchVoiceConfigTool.execute({ preset: 'search' }, null);
		assert.equal(existsSync(CONFIG_PATH), true);
	});

	it('written config has googleSearch:true for search preset', async () => {
		await switchVoiceConfigTool.execute({ preset: 'search' }, null);
		assert.equal(readConfig().googleSearch, true);
	});

	it('written config has the search model', async () => {
		const result = await switchVoiceConfigTool.execute({ preset: 'search' }, null) as Record<string, unknown>;
		assert.equal(readConfig().model, result.model);
	});
});

// ── no-search preset ──────────────────────────────────────────────────────────

describe('execute — no-search preset', { concurrency: 1 }, () => {
	it('returns ok:true for no-search preset', async () => {
		const result = await switchVoiceConfigTool.execute({ preset: 'no-search' }, null) as Record<string, unknown>;
		assert.equal(result.ok, true);
	});

	it('returns googleSearch:false for no-search preset', async () => {
		const result = await switchVoiceConfigTool.execute({ preset: 'no-search' }, null) as Record<string, unknown>;
		assert.equal(result.googleSearch, false);
	});

	it('written config has googleSearch:false for no-search preset', async () => {
		await switchVoiceConfigTool.execute({ preset: 'no-search' }, null);
		assert.equal(readConfig().googleSearch, false);
	});

	it('search and no-search use different models', async () => {
		const s = await switchVoiceConfigTool.execute({ preset: 'search' }, null) as Record<string, unknown>;
		const n = await switchVoiceConfigTool.execute({ preset: 'no-search' }, null) as Record<string, unknown>;
		assert.notEqual(s.model, n.model);
	});
});

// ── config file merges defaults ───────────────────────────────────────────────

describe('written config — merges VOICE_CONFIG_DEFAULTS', { concurrency: 1 }, () => {
	it('written config includes owner_mode from defaults', async () => {
		await switchVoiceConfigTool.execute({ preset: 'search' }, null);
		const cfg = readConfig();
		assert.ok('owner_mode' in cfg, 'written config must include owner_mode from VOICE_CONFIG_DEFAULTS');
	});

	it('written config includes channels from defaults', async () => {
		await switchVoiceConfigTool.execute({ preset: 'search' }, null);
		const cfg = readConfig();
		assert.ok('channels' in cfg, 'written config must include channels from VOICE_CONFIG_DEFAULTS');
	});

	it('owner_mode defaults to false (fail-closed)', async () => {
		await switchVoiceConfigTool.execute({ preset: 'search' }, null);
		assert.equal(readConfig().owner_mode, VOICE_CONFIG_DEFAULTS.owner_mode);
	});

	it('config is valid JSON (readable back)', async () => {
		await switchVoiceConfigTool.execute({ preset: 'no-search' }, null);
		assert.doesNotThrow(() => JSON.parse(readFileSync(CONFIG_PATH, 'utf-8')));
	});
});

// ── invalid preset ────────────────────────────────────────────────────────────

describe('execute — invalid preset', { concurrency: 1 }, () => {
	it('returns error for an unknown preset', async () => {
		const result = await switchVoiceConfigTool.execute({ preset: 'turbo' as 'search' }, null) as Record<string, unknown>;
		assert.ok('error' in result, 'execute with unknown preset must return { error: ... }');
	});

	it('does not return ok:true for unknown preset', async () => {
		const result = await switchVoiceConfigTool.execute({ preset: 'turbo' as 'search' }, null) as Record<string, unknown>;
		assert.notEqual(result.ok, true);
	});
});

// ── config dir creation ───────────────────────────────────────────────────────

describe('execute — config directory creation', { concurrency: 1 }, () => {
	it('creates the config/ directory if absent', async () => {
		const subTmp = join(TMP, `sub-${Date.now()}`);
		mkdirSync(subTmp, { recursive: true });
		process.env.SUTANDO_WORKSPACE = subTmp;
		try {
			await switchVoiceConfigTool.execute({ preset: 'search' }, null);
			assert.equal(existsSync(join(subTmp, 'config')), true);
		} finally {
			process.env.SUTANDO_WORKSPACE = TMP;
		}
	});
});
