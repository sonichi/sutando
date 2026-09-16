import assert from 'node:assert/strict';
import childProcess, { type ExecFileSyncOptions, type SpawnSyncOptions } from 'node:child_process';
import { syncBuiltinESMExports } from 'node:module';
import { afterEach, it, mock } from 'node:test';
import { clipboardRead, clipboardWrite, openWithDefault } from '../src/platform.js';

const platform = Object.getOwnPropertyDescriptor(process, 'platform')!;
afterEach(() => {
	mock.restoreAll();
	syncBuiltinESMExports();
	Object.defineProperty(process, 'platform', platform);
});

it('Windows opens literal file paths and URLs without cmd interpretation', () => {
	Object.defineProperty(process, 'platform', { value: 'win32' });
	const calls: { file: string; args: readonly string[]; options: ExecFileSyncOptions }[] = [];
	mock.method(childProcess, 'execFileSync', (file: string, args: readonly string[], options: ExecFileSyncOptions) => {
		calls.push({ file, args, options });
		return Buffer.alloc(0);
	});
	mock.method(childProcess, 'spawnSync', () => { throw new Error('must not invoke cmd/start'); });
	syncBuiltinESMExports();
	for (const target of [String.raw`C:\Downloads\a&calc.pdf`, String.raw`C:\José 王\100%PATH%^'x.pdf`, 'https://example.com/?a=1&b=%25']) {
		openWithDefault(target);
		const call = calls.at(-1)!;
		assert.equal(call.file, 'powershell.exe');
		assert.equal(call.options.env?.SUTANDO_OPEN_TARGET, target);
		assert.ok(!call.args.join(' ').includes(target));
		assert.match(call.args.at(-1)!, /FileName = \$env:SUTANDO_OPEN_TARGET/);
		assert.match(call.args.at(-1)!, /UseShellExecute = \$true/);
		assert.equal(call.options.shell, undefined);
		assert.equal(call.options.timeout, 5000);
	}
});

it('Windows default-open errors reach the caller', () => {
	Object.defineProperty(process, 'platform', { value: 'win32' });
	mock.method(childProcess, 'execFileSync', () => { throw new Error('no file association'); });
	mock.method(childProcess, 'spawnSync', () => ({ status: 1, stderr: 'no file association' }));
	syncBuiltinESMExports();
	assert.throws(() => openWithDefault('unregistered.file'), /no file association/);
});

it('Windows clipboard preserves Unicode, newlines and literal shell syntax', () => {
	Object.defineProperty(process, 'platform', { value: 'win32' });
	const text = "José 王 😀\r\nline 2\n'$(Write-Error nope)'";
	const calls: { args: readonly string[]; options: SpawnSyncOptions }[] = [];
	mock.method(childProcess, 'spawnSync', (_file: string, args: readonly string[], options: SpawnSyncOptions) => {
		calls.push({ args, options });
		return { status: 0, stdout: text };
	});
	syncBuiltinESMExports();
	clipboardWrite(text);
	assert.equal(calls[0].options.input, text);
	assert.ok(!calls[0].args.join(' ').includes(text));
	assert.match(calls[0].args.at(-1)!, /InputEncoding.*UTF8Encoding/);
	assert.match(calls[0].args.at(-1)!, /In.ReadToEnd\(\)/);
	assert.equal(clipboardRead(), text);
	assert.match(calls[1].args.at(-1)!, /OutputEncoding.*UTF8Encoding/);
	assert.match(calls[1].args.at(-1)!, /Get-Clipboard -Raw/);
});

it('macOS default-open retains its direct argv contract', () => {
	Object.defineProperty(process, 'platform', { value: 'darwin' });
	const calls: unknown[][] = [];
	mock.method(childProcess, 'execFileSync', (...args: unknown[]) => { calls.push(args); return Buffer.alloc(0); });
	syncBuiltinESMExports();
	openWithDefault('/tmp/a & b.pdf');
	assert.deepEqual(calls[0], ['open', ['/tmp/a & b.pdf'], { timeout: 5000 }]);
});
