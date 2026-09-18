import assert from 'node:assert/strict';
import childProcess, { execFileSync as realExecFileSync, type ExecFileSyncOptions, type SpawnSyncOptions } from 'node:child_process';
import { syncBuiltinESMExports } from 'node:module';
import { afterEach, it, mock } from 'node:test';
import { existsSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { clipboardRead, clipboardWrite, openWithDefault, resizeImage } from '../src/platform.js';

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

it('Windows resizes through System.Drawing with the paths kept out of the script', () => {
	Object.defineProperty(process, 'platform', { value: 'win32' });
	const dir = mkdtempSync(join(tmpdir(), 'resize-'));
	const src = join(dir, "O’Brien & 王 'shot.png");
	const dest = join(dir, 'shot-sm.jpg');
	const calls: { file: string; args: readonly string[]; options: ExecFileSyncOptions }[] = [];
	mock.method(childProcess, 'execFileSync', (file: string, args: readonly string[], options: ExecFileSyncOptions) => {
		calls.push({ file, args, options });
		writeFileSync(String(options.env?.SUTANDO_RESIZE_OUTPUT), 'jpeg');
		return Buffer.alloc(0);
	});
	syncBuiltinESMExports();
	try {
		assert.equal(resizeImage(src, dest, 800, 2_000), true);
		const call = calls[0];
		assert.equal(call.file, 'powershell.exe');
		assert.equal(call.options.env?.SUTANDO_RESIZE_INPUT, src);
		assert.equal(call.options.env?.SUTANDO_RESIZE_OUTPUT, dest);
		assert.equal(call.options.env?.SUTANDO_RESIZE_MAXDIM, '800');
		assert.ok(!call.args.join(' ').includes(src));
		assert.match(call.args.at(-1)!, /FromFile\(\$env:SUTANDO_RESIZE_INPUT\)/);
		assert.match(call.args.at(-1)!, /Save\(\$env:SUTANDO_RESIZE_OUTPUT, \[System\.Drawing\.Imaging\.ImageFormat\]::Jpeg\)/);
		assert.equal(call.options.shell, undefined);
	} finally {
		rmSync(dir, { recursive: true, force: true });
	}
});

it('a failed resize reports false so callers keep the original frame', () => {
	Object.defineProperty(process, 'platform', { value: 'win32' });
	mock.method(childProcess, 'execFileSync', () => { throw new Error('System.Drawing unavailable'); });
	syncBuiltinESMExports();
	assert.equal(resizeImage('in.png', 'out.jpg', 800), false);
	Object.defineProperty(process, 'platform', { value: 'linux' });
	assert.equal(resizeImage('in.png', 'out.jpg', 800), false);
});

it('macOS resize retains the sips argv contract', () => {
	Object.defineProperty(process, 'platform', { value: 'darwin' });
	const calls: unknown[][] = [];
	mock.method(childProcess, 'execFileSync', (...args: unknown[]) => { calls.push(args); return Buffer.alloc(0); });
	syncBuiltinESMExports();
	resizeImage('/tmp/shot.png', '/tmp/shot-sm.jpg', 800, 2_000);
	assert.deepEqual(calls[0], ['sips', ['-Z', '800', '-s', 'format', 'jpeg', '/tmp/shot.png', '--out', '/tmp/shot-sm.jpg'], { timeout: 2000, stdio: 'ignore' }]);
});

it('native Windows resize shrinks a real frame under a typographic apostrophe', { skip: process.platform !== 'win32' && 'requires System.Drawing' }, () => {
	const dir = mkdtempSync(join(tmpdir(), 'resize-O’Brien-'));
	const src = join(dir, 'frame 王.png');
	const dest = join(dir, 'frame-sm.jpg');
	const paint = '$ErrorActionPreference = "Stop"; Add-Type -AssemblyName System.Drawing; ' +
		'$b = [System.Drawing.Bitmap]::new(1200, 300); $b.Save($env:SUTANDO_TEST_FRAME, [System.Drawing.Imaging.ImageFormat]::Png); $b.Dispose()';
	const measure = '$ErrorActionPreference = "Stop"; Add-Type -AssemblyName System.Drawing; ' +
		'$i = [System.Drawing.Image]::FromFile($env:SUTANDO_TEST_FRAME); Write-Output "$($i.Width)x$($i.Height) $($i.RawFormat.Guid)"; $i.Dispose()';
	try {
		realExecFileSync('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', paint], { env: { ...process.env, SUTANDO_TEST_FRAME: src }, windowsHide: true });
		assert.equal(resizeImage(src, dest, 400), true);
		assert.ok(existsSync(dest));
		const [size, format] = realExecFileSync('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', measure], { env: { ...process.env, SUTANDO_TEST_FRAME: dest }, windowsHide: true, encoding: 'utf8' }).trim().split(' ');
		assert.equal(size, '400x100');
		assert.equal(format, 'b96b3cae-0728-11d3-9d7b-0000f81ef32e'); // ImageFormat.Jpeg
	} finally {
		rmSync(dir, { recursive: true, force: true });
	}
});
