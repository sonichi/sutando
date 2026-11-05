import { describe, it, after } from 'node:test';
import assert from 'node:assert/strict';
import { mkdirSync, writeFileSync, utimesSync } from 'node:fs';
import { join } from 'node:path';
import { setupTempWorkspace } from './_helpers/temp-workspace.js';

const { workspace, cleanup } = setupTempWorkspace('readnote');
const { sharedPersonalPath } = await import('../src/util_paths.js');
const { readNoteTool } = await import('../src/inline-tools.js');
const notes = sharedPersonalPath('notes', workspace);
after(cleanup);

function note(name: string, body: string, minutesAgo: number) {
	mkdirSync(notes, { recursive: true });
	const p = join(notes, `${name}.md`);
	writeFileSync(p, `---\ntitle: ${name}\n---\n${body}\n`);
	const t = new Date(Date.now() - minutesAgo * 60_000);
	utimesSync(p, t, t);
}

describe('read_note', () => {
	it('returns the newest matching note, not the first by name, and lists the others', async () => {
		note('meeting-mode-design', 'design doc', 30 * 24 * 60);
		note('meeting-2026-10-09-2023', 'yesterday', 24 * 60);
		note('meeting-2026-10-10-1119', 'weather in Cupertino', 5);
		const r = await readNoteTool.execute({ name: 'meeting' }) as { title: string; content: string; otherMatches?: string[] };
		assert.equal(r.title, 'meeting-2026-10-10-1119');
		assert.match(r.content, /weather in Cupertino/);
		assert.deepEqual(r.otherMatches, ['meeting-2026-10-09-2023', 'meeting-mode-design'], 'so the model can ask which one');
	});

	it('a single match has no otherMatches', async () => {
		note('groceries', 'milk', 1);
		const r = await readNoteTool.execute({ name: 'groceries' }) as { title: string; otherMatches?: string[] };
		assert.equal(r.title, 'groceries');
		assert.equal(r.otherMatches, undefined);
	});
});
