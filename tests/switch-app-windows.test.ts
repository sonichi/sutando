import assert from 'node:assert/strict';
import childProcess, { type ExecFileOptions } from 'node:child_process';
import { mkdtempSync, rmSync } from 'node:fs';
import { syncBuiltinESMExports } from 'node:module';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { after, afterEach, describe, it, mock } from 'node:test';
import type { ToolContext } from 'bodhi-realtime-agent';
import { activateWindowsApp } from '../src/platform.js';

const workspace = mkdtempSync(join(tmpdir(), 'switch-app-'));
const previousEnv = { ...process.env };
process.env.SUTANDO_TEST_MODE = '1';
process.env.SUTANDO_WORKSPACE = workspace;
const { switchAppTool, inlineTools, ownerOnlyTools } = await import('../src/inline-tools.js');
const platform = Object.getOwnPropertyDescriptor(process, 'platform')!;
const context: ToolContext = {
	toolCallId: 'switch-test', agentName: 'test', sessionId: 'test',
	abortSignal: new AbortController().signal,
};

afterEach(() => {
	mock.restoreAll();
	syncBuiltinESMExports();
	Object.defineProperty(process, 'platform', platform);
});
after(() => {
	process.env = previousEnv;
	rmSync(workspace, { recursive: true, force: true });
});

function stubLauncher(stdout: string, error: Error | null = null, stderr = '') {
	const calls: { file: string; args: readonly string[]; options: ExecFileOptions }[] = [];
	mock.method(childProcess, 'execFile', (
		file: string, args: readonly string[], options: ExecFileOptions,
		callback: (error: Error | null, stdout: string, stderr: string) => void,
	) => {
		calls.push({ file, args, options });
		queueMicrotask(() => callback(error, stdout, stderr));
		return new childProcess.ChildProcess();
	});
	syncBuiltinESMExports();
	return calls;
}

describe('Windows launcher protocol', () => {
	it('passes the app as one argument and requires verified foreground success', async () => {
		const app = 'An app with spaces & "quotes"';
		const calls = stubLauncher(JSON.stringify({ status: 'switched', app, foreground_verified: true }));
		assert.deepEqual(await activateWindowsApp(app, 'launcher.ps1', context.abortSignal), { status: 'switched', app });
		assert.deepEqual(calls[0].args.slice(-4), ['-File', 'launcher.ps1', '-App', app]);
		assert.equal(calls[0].file, 'pwsh');
		assert.equal(calls[0].options.signal, context.abortSignal);
		assert.equal(calls[0].options.timeout, 15_000);
		assert.equal(calls[0].options.windowsHide, true);
		assert.ok(calls[0].args.includes('-STA'));
		assert.ok(!calls[0].args.includes('-Command'));
	});

	for (const response of [
		{ status: 'visible', app: 'Wanted' },
		{ status: 'switched', app: 'Wanted', foreground_verified: false },
		{ status: 'switched', app: '', foreground_verified: true },
		{ status: 'ok' }, null, 'success',
	]) {
		it(`rejects unverified response ${JSON.stringify(response)}`, async () => {
			stubLauncher(JSON.stringify(response));
			await assert.rejects(activateWindowsApp('Wanted', 'launcher.ps1'));
		});
	}

	it('surfaces the backend error even when it exits nonzero', async () => {
		stubLauncher('{"status":"error","error":"No interactive desktop"}', new Error('exit 1'));
		await assert.rejects(activateWindowsApp('Wanted', 'launcher.ps1'), /No interactive desktop/);
	});
	it('rejects success-shaped output from a failed process', async () => {
		stubLauncher('{"status":"switched","app":"Wanted","foreground_verified":true}', new Error('timed out'));
		await assert.rejects(activateWindowsApp('Wanted', 'launcher.ps1'), /timed out/);
	});
	it('surfaces missing PowerShell and malformed output', async () => {
		stubLauncher('', new Error('spawn pwsh ENOENT'));
		await assert.rejects(activateWindowsApp('Wanted', 'launcher.ps1'), /ENOENT/);
	});
	it('rejects malformed output on a zero exit', async () => {
		stubLauncher('True\n{"status":"switched"}');
		await assert.rejects(activateWindowsApp('Wanted', 'launcher.ps1'), /invalid JSON/);
	});
});

describe('switch_app platform wiring', () => {
	it('delegates Windows switching to the module-adjacent backend with cancellation', async () => {
		Object.defineProperty(process, 'platform', { value: 'win32', configurable: true });
		const calls = stubLauncher('{"status":"switched","app":"Microsoft Edge","foreground_verified":true}');
		assert.deepEqual(await switchAppTool.execute({ app: 'edge' }, context), { status: 'switched', app: 'Microsoft Edge' });
		assert.equal(calls.length, 1);
		assert.equal(calls[0].args.at(-1), 'Microsoft Edge');
		assert.ok(calls[0].args.some(arg => arg.endsWith('windows-app-launcher.ps1')));
		assert.equal(calls[0].options.signal, context.abortSignal);
	});
	it('returns a tool error rather than claiming success when focus is denied', async () => {
		Object.defineProperty(process, 'platform', { value: 'win32', configurable: true });
		stubLauncher('{"status":"error","error":"Foreground focus denied"}', new Error('exit 1'));
		assert.deepEqual(await switchAppTool.execute({ app: 'Notepad' }, context), {
			error: 'Failed to switch to Notepad: Foreground focus denied',
		});
	});
	it('preserves macOS aliases, escaping, AppleScript calls, and result shape', async () => {
		Object.defineProperty(process, 'platform', { value: 'darwin', configurable: true });
		const execute = mock.method(childProcess, 'execFileSync', () => '');
		syncBuiltinESMExports();
		assert.deepEqual(await switchAppTool.execute({ app: 'vscode' }, context), { status: 'switched', app: 'Visual Studio Code' });
		assert.deepEqual(execute.mock.calls[0].arguments, ['osascript', [
			'-e', 'tell application "Visual Studio Code" to activate',
			'-e', 'tell application "System Events" to set frontmost of process "Code" to true',
		], { timeout: 10_000 }]);
		await switchAppTool.execute({ app: 'An "App"' }, context);
		assert.match(String(execute.mock.calls[1].arguments[1]), /An \\"App\\"/);
	});
	it('keeps unsupported platforms gated without invoking a launcher', async () => {
		Object.defineProperty(process, 'platform', { value: 'linux', configurable: true });
		const calls = stubLauncher('{}');
		assert.match(JSON.stringify(await switchAppTool.execute({ app: 'Calculator' }, context)), /macOS/);
		assert.equal(calls.length, 0);
	});
	it('uses the same tool in both exported tool collections', () => {
		assert.ok(inlineTools.includes(switchAppTool));
		assert.ok(ownerOnlyTools.includes(switchAppTool));
	});
});
