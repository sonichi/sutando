import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { after, describe, it } from 'node:test';

// The voice model plans around every tool it is shown, so tools that only
// drive macOS automation must not be advertised on other hosts.

const workspace = mkdtempSync(join(tmpdir(), 'host-surface-'));
const previousEnv = { ...process.env };
process.env.SUTANDO_TEST_MODE = '1';
process.env.SUTANDO_WORKSPACE = workspace;
const { forHostPlatform, inlineTools, ownerOnlyTools, anyCallerTools } = await import('../src/inline-tools.js');
after(() => {
	process.env = previousEnv;
	rmSync(workspace, { recursive: true, force: true });
});

const MACOS_ONLY = ['press_key', 'type_text', 'volume', 'brightness', 'toggle_tasks', 'slide_control', 'fullscreen'];
const sample = [...MACOS_ONLY, 'switch_app', 'clipboard', 'scroll'].map(name => ({ name }));

describe('inline-tools — host-aware tool surface', () => {
	it('keeps every tool on macOS', () => {
		assert.deepEqual(forHostPlatform(sample, 'darwin').map(t => t.name), sample.map(t => t.name));
	});

	it('drops only the macOS automation tools elsewhere; switch_app has a Windows path', () => {
		for (const platform of ['win32', 'linux'] as const) {
			assert.deepEqual(forHostPlatform(sample, platform).map(t => t.name), ['switch_app', 'clipboard', 'scroll']);
		}
	});

	it('both exported tables pass through the host gate', () => {
		const src = readFileSync(join(import.meta.dirname ?? '.', '..', 'src/inline-tools.ts'), 'utf-8');
		assert.match(src, /export const inlineTools = forHostPlatform\(assertUniqueToolNames\(\[/);
		assert.match(src, /export const ownerOnlyTools = forHostPlatform\(\[/);
		const advertised = [...inlineTools, ...ownerOnlyTools, ...anyCallerTools].map(t => t.name);
		const macOnlyShown = advertised.filter(name => MACOS_ONLY.includes(name));
		if (process.platform === 'darwin') assert.ok(new Set(macOnlyShown).size >= 5);
		else assert.deepEqual(macOnlyShown, []);
		assert.ok(advertised.includes('switch_app'));
	});
});
