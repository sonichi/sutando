import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { runInNewContext } from 'node:vm';
import ts from 'typescript';

test('call cleanup clears the shared playback selection with a per-user temp directory', () => {
	const source = readFileSync(new URL('../skills/phone-conversation/scripts/conversation-server.ts', import.meta.url), 'utf8');
	const ast = ts.createSourceFile('conversation-server.ts', source, ts.ScriptTarget.Latest, true);
	const cleanup = ast.statements.find((node): node is ts.FunctionDeclaration =>
		ts.isFunctionDeclaration(node) && node.name?.text === 'cleanupCall');
	assert.ok(cleanup?.body);
	const imports = ast.statements.filter(ts.isImportDeclaration);
	const bindings = imports.find(node => ts.isStringLiteral(node.moduleSpecifier)
		&& node.moduleSpecifier.text === '../../../src/tmp-paths.js')?.importClause?.namedBindings;
	const context: Record<string, unknown> = {};
	const playbackPath = '/fixture/per-user-temp/sutando-playback-path';
	if (bindings && ts.isNamedImports(bindings)) {
		for (const binding of bindings.elements) {
			if ((binding.propertyName ?? binding.name).text === 'PLAYBACK_PATH') {
				context[binding.name.text] = playbackPath;
			}
		}
	}
	const files = new Map([[playbackPath, '/fixture/previous-call.mp4']]);
	context.unlinkSync = (path: string) => {
		if (!files.delete(path)) throw new Error('ENOENT');
	};
	// Execute production unlink blocks without starting the phone service or contacting Twilio.
	const blocks = cleanup.body.statements.filter(node => ts.isTryStatement(node)
		&& node.getText(ast).includes('unlinkSync('));
	assert.ok(blocks.length > 0);
	for (const block of blocks) runInNewContext(block.getText(ast), context);
	assert.equal(files.has(playbackPath), false, 'previous call playback must not survive cleanup');
	for (const block of blocks) runInNewContext(block.getText(ast), context);
});
