// The plugin seam: a fake plugin speaks to the client, hears its frames and adds
// voice-only tools and prompt lines, and the core names neither it nor any product.
import { describe, it, test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, mkdirSync, writeFileSync, rmSync, existsSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { createClientFrameHub } from '../src/client-frame-hub.js';
import { collectVoiceSurface, runSkillSetups, type SkillSetupCtx } from '../src/skill-setup-runner.js';

const REPO_ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');

function fakeCtx(hub = createClientFrameHub(() => {})) {
	const sent: Record<string, unknown>[] = [];
	const injected: string[] = [];
	const ctx: SkillSetupCtx = {
		session: {},
		injectText: () => {},
		sendClientFrame: (f) => { sent.push(f); return true; },
		onClientFrame: hub.onClientFrame,
		onClientDisconnected: hub.onClientDisconnected,
		injectContext: (t) => { injected.push(t); },
		setVoiceSessionOrigin: () => {},
		getVoiceSessionOrigin: () => null,
		setVoiceTaskOriginResolver: () => {},
	};
	return { ctx, hub, sent, injected };
}

describe('client frame hub', () => {
	it('a fake plugin answers a client frame, injects context and resets on disconnect', () => {
		const { ctx, hub, sent, injected } = fakeCtx();
		let state = 'idle';
		runSkillSetups([(c) => {
			c.onClientFrame((frame) => {
				if (frame.type !== 'fake.hello') return false;
				state = 'greeted';
				c.sendClientFrame({ type: 'fake.hello.ack', echo: frame.word });
				c.injectContext('the client said hello');
				return true;
			});
			c.onClientDisconnected(() => { state = 'idle'; });
		}], ctx, () => {});
		assert.equal(hub.dispatch({ type: 'other.frame' }), false, 'a frame nobody claims');
		assert.equal(hub.dispatch({ type: 'fake.hello', word: 'hi' }), true);
		assert.deepEqual(sent, [{ type: 'fake.hello.ack', echo: 'hi' }]);
		assert.deepEqual(injected, ['the client said hello']);
		assert.equal(state, 'greeted');
		hub.disconnected();
		assert.equal(state, 'idle');
	});

	it('every handler is offered the frame; a throwing or rejecting one reaches nobody else', async () => {
		const logs: string[] = [];
		const hub = createClientFrameHub((m, d) => logs.push(`${m} ${d ?? ''}`));
		const seen: string[] = [];
		let unhandled: unknown = null;
		const onUnhandled = (r: unknown) => { unhandled = r; };
		process.on('unhandledRejection', onUnhandled);
		try {
			hub.onClientFrame(() => { throw new Error('boom'); });
			hub.onClientFrame(async () => { throw new Error('async boom'); });
			hub.onClientFrame((f) => { seen.push(String(f.type)); });
			hub.onClientDisconnected(() => { throw new Error('bye boom'); });
			hub.onClientDisconnected(() => { seen.push('disconnected'); });
			assert.equal(hub.dispatch({ type: 'x' }), false);
			hub.disconnected();
			await new Promise(r => setTimeout(r, 20));
		} finally {
			process.off('unhandledRejection', onUnhandled);
		}
		assert.deepEqual(seen, ['x', 'disconnected']);
		assert.equal(unhandled, null);
		assert.ok(logs.some(l => l.includes('handler threw')) && logs.some(l => l.includes('async handler rejected')) && logs.some(l => l.includes('disconnect handler threw')), logs.join('|'));
	});

	it('only a JSON object is a frame', () => {
		const hub = createClientFrameHub(() => {});
		let calls = 0;
		hub.onClientFrame(() => { calls++; return true; });
		for (const bad of [null, undefined, 'text', 7, ['a']]) assert.equal(hub.dispatch(bad), false);
		assert.equal(calls, 0);
		hub.onClientFrame(undefined as never);
		hub.onClientDisconnected(null as never);
		assert.equal(hub.dispatch({}), true);
	});
});

describe('collectVoiceSurface', () => {
	it('merges tools, rules and live context lines; a broken hook contributes nothing', () => {
		let where = 'A';
		const logs: string[] = [];
		const tool = { name: 'fake_tool' } as never;
		const merged = collectVoiceSurface([
			() => ({ tools: [tool], promptRules: ['- FAKE: rule one', ''], contextLines: () => [`WHERE: ${where}`] }),
			() => { throw new Error('bad hook'); },
			() => null as never,
			() => ({ contextLines: () => { throw new Error('bad lines'); } }),
			() => ({ promptRules: ['- FAKE: rule two'] }),
		], (m, d) => logs.push(`${m} ${d ?? ''}`));
		assert.deepEqual(merged.tools, [tool]);
		assert.deepEqual(merged.promptRules, ['- FAKE: rule one', '- FAKE: rule two']);
		assert.deepEqual(merged.contextLines(), ['WHERE: A']);
		where = 'B';
		assert.deepEqual(merged.contextLines(), ['WHERE: B'], 're-evaluated per prompt build');
		assert.ok(logs.some(l => l.includes('hook threw')) && logs.some(l => l.includes('contextLines threw')));
	});

	it('no hooks: nothing is added', () => {
		const merged = collectVoiceSurface([]);
		assert.deepEqual([merged.tools, merged.promptRules, merged.contextLines()], [[], [], []]);
	});
});

const TSX_CLI = (() => {
	try { return createRequire(join(REPO_ROOT, 'package.json')).resolve('tsx/cli'); } catch { return null; }
})();

test('a manifest skill\'s voiceSurface() reaches the voice prompt and stays off the shared tool tables', t => {
	if (!TSX_CLI || !existsSync(TSX_CLI)) return t.skip('tsx binary not present (no node_modules)');
	const base = mkdtempSync(join(tmpdir(), 'voice-surface-'));
	try {
		const dir = join(base, 'root', 'skills', 'fake-surface');
		mkdirSync(dir, { recursive: true });
		writeFileSync(join(dir, 'manifest.json'), JSON.stringify({ name: 'fake-surface', enabled: true, tools: './tools.mjs' }));
		writeFileSync(join(dir, 'tools.mjs'),
			`export const tools = [];\n` +
			`export function voiceSurface() { return { tools: [{ name: 'fake_move', description: 'Move the fake surface. Instant.' }], promptRules: ['- FAKE-RULE: call fake_move.'], contextLines: () => ['FAKE-WHERE: the lobby.'] }; }\n`);
		const driver = join(base, 'driver.mjs');
		writeFileSync(driver,
			`const it = await import(process.env.INLINE_TOOLS_URL);\n` +
			`const cfg = await import(process.env.CONFIG_URL);\n` +
			`const base = { resolveCurrentMode: () => ({ mode: 'active' }), isMeetingActive: () => false, googleSearch: false, resetSessionGates() {}, resetNoteViewingDebounce() {}, getRecentConversation: () => '', getSecondsSinceLastTurn: () => null };\n` +
			`const plain = cfg.buildInstructions(base);\n` +
			`const withSurface = cfg.buildInstructions({ ...base, voiceSurface: it.personalVoiceSurface });\n` +
			`console.log('__OUT__' + JSON.stringify({ shared: it.inlineTools.some(t => t.name === 'fake_move'), surfaceTools: it.personalVoiceSurface.tools.map(t => t.name), plainHas: /FAKE|fake_move/.test(plain), lines: withSurface.split('\\n').filter(l => /FAKE|fake_move/.test(l)) }));\n`);
		const out = execFileSync(process.execPath, [TSX_CLI, driver], {
			cwd: REPO_ROOT, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'],
			env: {
				...process.env, SUTANDO_TEST_MODE: '1', SUTANDO_WORKSPACE: join(base, 'ws'),
				// Hermetic: no installed skill sees this machine's own channels or tokens.
				CLAUDE_CONFIG_DIR: join(base, 'claude-home'), REMOTE_TASK_TOKEN: '', AG2_REMOTE_TOKEN: '',
				SUTANDO_EXTERNAL_PLUGIN_DIRS: join(base, 'root'),
				INLINE_TOOLS_URL: pathToFileURL(join(REPO_ROOT, 'src', 'inline-tools.ts')).href,
				CONFIG_URL: pathToFileURL(join(REPO_ROOT, 'src', 'voice-agent-config.ts')).href,
			},
		});
		const line = out.split('\n').find(l => l.startsWith('__OUT__'));
		assert.ok(line, out);
		const got = JSON.parse(line.slice('__OUT__'.length));
		assert.equal(got.shared, false, 'the phone tool table is built from inlineTools');
		assert.deepEqual(got.surfaceTools, ['fake_move']);
		assert.equal(got.plainHas, false, 'a session without the contribution has none of it');
		assert.ok(got.lines.some((l: string) => l === 'FAKE-WHERE: the lobby.'), got.lines.join('|'));
		assert.ok(got.lines.some((l: string) => l === '- FAKE-RULE: call fake_move.'));
		assert.ok(got.lines.some((l: string) => l === '- fake_move: Move the fake surface. Instant.'));
		assert.ok(got.lines.some((l: string) => /fake_move — call these directly/.test(l)));
	} finally {
		rmSync(base, { recursive: true, force: true });
	}
});

test('the core seam names no plugin and no product', () => {
	for (const f of ['src/client-frame-hub.ts', 'src/skill-setup-runner.ts']) {
		assert.doesNotMatch(readFileSync(join(REPO_ROOT, f), 'utf-8'), /ag2 ?space|matrix|navigate_ui|session\.context/i, f);
	}
});
