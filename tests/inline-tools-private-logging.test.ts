import assert from 'node:assert/strict';
import childProcess from 'node:child_process';
import { syncBuiltinESMExports } from 'node:module';
import { after, it, mock } from 'node:test';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const workspace = mkdtempSync(join(tmpdir(), 'inline-tools-private-logging-'));
const previousEnv = { ...process.env };
const previousPlatform = Object.getOwnPropertyDescriptor(process, 'platform')!;
process.env.SUTANDO_TEST_MODE = '1';
process.env.SUTANDO_WORKSPACE = workspace;
const { clipboardTool, typeTextTool } = await import('../src/inline-tools.js');

after(() => {
	mock.restoreAll();
	syncBuiltinESMExports();
	Object.defineProperty(process, 'platform', previousPlatform);
	process.env = previousEnv;
	rmSync(workspace, { recursive: true, force: true });
});

it('logs clipboard and typed text lengths without logging their contents', async () => {
	const readText = 'clipboard-private-value';
	let clipboard = readText;
	const logs: string[] = [];
	mock.method(childProcess, 'execSync', (...args: unknown[]) => {
		const [command, options] = args as [string, { input?: string }?];
		if (command.includes('-o')) return clipboard;
		clipboard = options?.input ?? '';
		return Buffer.alloc(0);
	});
	mock.method(childProcess, 'execFileSync', (...args: unknown[]) => {
		const [command, , options] = args as [string, unknown, { input?: string }?];
		if (command === 'pbpaste') return clipboard;
		if (command === 'pbcopy' && options?.input !== undefined) clipboard = options.input;
		return Buffer.alloc(0);
	});
	mock.method(console, 'log', (line: string) => logs.push(line));
	syncBuiltinESMExports();

	const read = await clipboardTool.execute({ action: 'read' });
	assert.deepEqual(read, { status: 'read', content: readText });
	const written = 'clipboard-write-private-value';
	const write = await clipboardTool.execute({ action: 'write', text: written });
	assert.deepEqual(write, { status: 'written', text: written });

	Object.defineProperty(process, 'platform', { value: 'darwin' });
	const typed = 'typed-private-value';
	assert.deepEqual(await typeTextTool.execute({ text: typed }), { status: 'typed', text: typed });
	const pasted = 'pasted-private-value\nsecond line';
	assert.deepEqual(await typeTextTool.execute({ text: pasted }), { status: 'typed', text: pasted });

	assert.match(logs[0], new RegExp(`\\[Clipboard\\] read \\(${readText.length} chars\\)`));
	assert.match(logs[1], new RegExp(`\\[Clipboard\\] wrote \\(${written.length} chars\\)`));
	assert.match(logs[2], new RegExp(`\\[TypeText\\] typed \\(mode=at_caret, ${typed.length} chars\\)`));
	assert.match(logs[3], new RegExp(`\\[TypeText\\] pasted \\(multi-line, mode=at_caret, ${pasted.length} chars\\)`));
	for (const secret of [readText, written, typed, pasted]) {
		assert.ok(logs.every(line => !line.includes(secret)));
	}
});
