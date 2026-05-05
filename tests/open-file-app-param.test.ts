import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { openFileTool } from '../src/inline-tools.js';

// Issue #560: open_file gains an optional `app` parameter so callers can
// direct macOS to use a specific app (e.g. "Sublime Text", "TablePlus")
// instead of the default file-type handler. We verify the schema and
// description rather than actually opening files (would mutate the test
// machine's app state).

describe('open_file — issue #560 app parameter', () => {
	it('declares an optional `app` string parameter in the zod schema', () => {
		// eslint-disable-next-line @typescript-eslint/no-explicit-any
		const shape = (openFileTool.parameters as any).shape;
		assert.ok('app' in shape, 'schema must include `app` field');
		// optional: parsing without `app` must succeed
		// eslint-disable-next-line @typescript-eslint/no-explicit-any
		const parsed = (openFileTool.parameters as any).safeParse({ path: '/tmp/nonexistent.txt' });
		assert.equal(parsed.success, true, 'omitting `app` must remain valid');
		// providing `app` as string must also succeed
		// eslint-disable-next-line @typescript-eslint/no-explicit-any
		const parsedWithApp = (openFileTool.parameters as any).safeParse({ path: '/tmp/x.py', app: 'Sublime Text' });
		assert.equal(parsedWithApp.success, true, 'passing `app` as string must be valid');
	});

	it('description explains when to set `app`', () => {
		const desc = openFileTool.description ?? '';
		assert.match(desc, /\bapp\b/i, 'description must mention `app` parameter');
		// per issue: explicit-app phrasing AND contextual-inference phrasing
		assert.match(desc, /open with|open .* in /i, 'description should describe explicit-app trigger phrases');
	});

	it('still surfaces a File-not-found error when path does not exist (no app)', async () => {
		// eslint-disable-next-line @typescript-eslint/no-explicit-any
		const result = await (openFileTool.execute as any)({ path: '/tmp/__nonexistent_for_test_560__' }, null) as { error?: string };
		assert.ok(result.error, 'should return error for missing path');
		assert.match(result.error!, /File not found/, 'error mentions File not found');
	});

	it('still surfaces a File-not-found error when path does not exist (with app)', async () => {
		// eslint-disable-next-line @typescript-eslint/no-explicit-any
		const result = await (openFileTool.execute as any)({ path: '/tmp/__nonexistent_for_test_560__', app: 'Sublime Text' }, null) as { error?: string };
		assert.ok(result.error, 'should return error for missing path');
		assert.match(result.error!, /File not found/, 'error mentions File not found even with app');
	});
});
